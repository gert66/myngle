"""Shared, GCS-backed ledger of settled "definitely not foreign-HQ" domains.

Motivation
----------
``enrichment_cache.py`` caches RAW Serper/Firecrawl responses — never a
derived verdict, by explicit design (see its docstring: "never a derived
score or signal"). That is not enough to avoid re-paying for a domain's HQ
screening on every rerun when "Foreign-HQ-only export" is on: a domestic
(not confirmed foreign) company never lands in ``current/`` (see
``export_lead_prioritizer_to_lovable_json.detect_foreign_hq_for_export``'s
``foreign_hq_only`` filter), so ``cloud_run_streamlit_app.py``'s
``current/``-based skip-filter (``known_enriched_company_ids``) has no way
to know it was already screened — it gets re-screened (a fresh Serper call
AND a fresh Anthropic HQ-interpretation call, which ``enrichment_cache.py``
never covers at all) on every single rerun, forever.

This module stores the DERIVED verdict instead — deliberately a SEPARATE
file/module from ``enrichment_cache.py``, to keep that module's
raw-response/audit-trail contract intact. One JSON ledger per country, in
its own GCS prefix (``_screened_domains/``), independent of what
``current/`` ends up containing after export filtering.

Deliberately conservative: only a CLEARLY SETTLED "not foreign" verdict
(``is_clearly_domestic``) is ever recorded — an ambiguous, manual-review,
or C5-unclear/error outcome is never treated as permanently skippable,
since a future run (different evidence, C5 turned on, ...) might resolve
it differently. No TTL/expiry: unlike a scraped page or a search result, a
company's HQ location essentially never "goes stale" within any practical
operating horizon.

Same GCS transport shape as ``enrichment_cache.py``: the Python
``google-cloud-storage`` client first (works inside a Cloud Run Job via
ADC), falling back to a ``gcloud``/``gsutil`` CLI subprocess (works on a
local machine), read-merge-write with a GCS generation precondition
(compare-and-swap) so parallel Cloud Run tasks — or a task and a Streamlit
session — writing concurrently can't lose each other's entries. Always an
optimization, never a hard dependency: every function degrades to "no
ledger" (an empty dict, or a no-op) rather than raising.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lovable_gcs_upload import resolve_gcs_upload_tool, upload_file

_LEDGER_GCS_PREFIX = "_screened_domains"

# Compare-and-swap retry budget for save() -- see enrichment_cache.py's
# identical rationale: a save that still loses only costs this run's new
# ledger entries, never breaks the run itself.
_SAVE_CAS_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# Domain normalization -- deliberately its own small copy rather than an
# import of enrichment_cache._normalize_domain_for_cache_key: that's a
# private helper of a module with a different contract (raw responses only),
# and this normalization is only 4 lines, not worth the cross-module coupling.
# ---------------------------------------------------------------------------

def normalize_domain(value: str) -> str:
    """Lowercase, no scheme, no ``www.`` — TLD kept (so ``acme.com``/
    ``acme.nl`` never collide)."""
    s = str(value or "").strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = re.sub(r"^www\.", "", s)
    return s.strip(".")


# ---------------------------------------------------------------------------
# Building ledger updates from a batch's Enriched Leads rows
# ---------------------------------------------------------------------------

def _row_score(row: dict) -> Optional[float]:
    v = row.get("sig_foreign_hq_score_for_next_scoring")
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "y")


def is_clearly_domestic(row: dict) -> bool:
    """True only when a row's HQ screening produced an unambiguous, SETTLED
    "not foreign" verdict — safe to permanently skip re-screening in a
    future run.

    Deliberately conservative: a score of 0 with ``needs_manual_review``
    or ``hq_positive_score_suppressed_for_review`` set, or any C5 outcome
    other than an explicit ``domestic_confirmed`` (e.g. "unclear", a failed
    call, no C5 attempted at all is fine — that's the common case when C5
    is off), is NOT treated as settled.
    """
    score = _row_score(row)
    if score != 0.0:
        return False
    if _row_truthy(row.get("needs_manual_review")):
        return False
    if _row_truthy(row.get("hq_positive_score_suppressed_for_review")):
        return False
    c5_adjudication = str(row.get("c5_adjudication") or "").strip().lower()
    if c5_adjudication and c5_adjudication != "domestic_confirmed":
        return False
    return True


def build_ledger_updates(rows: list[dict]) -> dict:
    """From Enriched-Leads-shaped rows (every row a batch actually ran HQ
    screening for — gated or not, thin or fully enriched), build
    ``{normalized_domain: {"confirmed_foreign_hq": False, "screened_at": iso}}``
    for rows that are clearly, settledly domestic (see
    ``is_clearly_domestic``).

    Rows that are ambiguous, confirmed foreign, or lack a usable domain are
    simply not included — absence from the ledger just means "screen it
    again if seen," never an active "unknown" marker.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    updates: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not is_clearly_domestic(row):
            continue
        domain = normalize_domain(row.get("domain") or row.get("normalized_domain"))
        if not domain:
            continue
        updates[domain] = {"confirmed_foreign_hq": False, "screened_at": now_iso}
    return updates


def known_domestic_domains(ledger: dict) -> set[str]:
    """Normalized domains in ``ledger`` recorded as settled non-foreign —
    the set a skip-filter should treat as safe to skip, ONLY when this run
    also has "Foreign-HQ-only export" on (a domestic company still belongs
    in the output otherwise)."""
    if not isinstance(ledger, dict):
        return set()
    return {
        domain for domain, entry in ledger.items()
        if isinstance(entry, dict) and entry.get("confirmed_foreign_hq") is False
    }


# ---------------------------------------------------------------------------
# GCS transport — Python client first (Cloud Run ADC), CLI fallback (local).
# Read-merge-write with a generation precondition, same pattern as
# enrichment_cache.py's save_cache_index.
# ---------------------------------------------------------------------------

def _ledger_filename(country_slug: str) -> str:
    return f"{country_slug}_screened_domains.json"


def _ledger_blob_name(country_slug: str) -> str:
    return f"{_LEDGER_GCS_PREFIX}/{_ledger_filename(country_slug)}"


def _ledger_gcs_path(bucket: str, country_slug: str) -> str:
    return f"gs://{bucket}/{_ledger_blob_name(country_slug)}"


def _storage_client():
    try:
        from google.cloud import storage
        return storage.Client()
    except Exception:
        return None


def _load_ledger_via_client(bucket: str, country_slug: str) -> Optional[dict]:
    """Returns ``None`` (try the CLI fallback) when the client itself is
    unusable; ``{}`` means the client worked but no ledger exists yet."""
    client = _storage_client()
    if client is None:
        return None
    try:
        blob = client.bucket(bucket).blob(_ledger_blob_name(country_slug))
        if not blob.exists():
            return {}
        try:
            data = json.loads(blob.download_as_text())
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[screened_domains_ledger] google-cloud-storage download failed "
              f"for {country_slug!r} ({type(exc).__name__}) -- falling back "
              f"to the gcloud/gsutil CLI.")
        return None


def _load_ledger_via_cli(bucket: str, country_slug: str) -> dict:
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return {}
    remote_path = _ledger_gcs_path(bucket, country_slug)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / _ledger_filename(country_slug)
            proc = subprocess.run(
                [*tool_cmd, remote_path, str(local_path)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0 or not local_path.exists():
                return {}
            try:
                data = json.loads(local_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_ledger(bucket: str, country_slug: str) -> dict:
    """Download the shared per-country domestic-domain ledger.

    Returns ``{}`` (never raises) when: the bucket/country slug is blank,
    neither transport is usable, the download fails (including the normal
    "doesn't exist yet" first-ever-run case), or the file isn't valid JSON.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {}
    via_client = _load_ledger_via_client(bucket, country_slug)
    if via_client is not None:
        return via_client
    return _load_ledger_via_cli(bucket, country_slug)


def merge_ledgers(base: dict, updates: dict) -> dict:
    """Union of two ledgers; on a domain collision the entry with the
    newest ``screened_at`` wins. Never mutates its arguments, never raises."""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(updates, dict):
        return dict(base)
    merged = dict(base)
    for domain, entry in updates.items():
        current = merged.get(domain)
        if current is None or not isinstance(current, dict):
            merged[domain] = entry
            continue
        try:
            current_dt = datetime.fromisoformat(str(current.get("screened_at") or ""))
        except (TypeError, ValueError):
            current_dt = None
        try:
            update_dt = datetime.fromisoformat(str(entry.get("screened_at") or ""))
        except (TypeError, ValueError):
            update_dt = None
        if current_dt is None or (update_dt is not None and update_dt >= current_dt):
            merged[domain] = entry
    return merged


def _save_ledger_via_client(bucket: str, country_slug: str, updates: dict) -> Optional[dict]:
    client = _storage_client()
    if client is None:
        return None
    try:
        from google.api_core import exceptions as gcs_exceptions

        bucket_obj = client.bucket(bucket)
        blob_name = _ledger_blob_name(country_slug)
        for attempt in range(_SAVE_CAS_ATTEMPTS):
            try:
                current_blob = bucket_obj.get_blob(blob_name)
                if current_blob is None:
                    generation = 0
                    current: dict = {}
                else:
                    generation = current_blob.generation
                    text = current_blob.download_as_text(if_generation_match=generation)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {}
                    current = data if isinstance(data, dict) else {}
                merged = merge_ledgers(current, updates)
                bucket_obj.blob(blob_name).upload_from_string(
                    json.dumps(merged, ensure_ascii=False),
                    content_type="application/json",
                    if_generation_match=generation,
                )
                return {"success": True, "destination": _ledger_gcs_path(bucket, country_slug)}
            except (gcs_exceptions.PreconditionFailed, gcs_exceptions.NotFound):
                time.sleep(random.uniform(0.1, 0.4) * (attempt + 1))
        return {
            "success": False,
            "error": f"Gave up after {_SAVE_CAS_ATTEMPTS} attempts: concurrent "
                     "writers kept updating the ledger. This run's new entries "
                     "are lost; the run itself is unaffected.",
        }
    except Exception as exc:
        print(f"[screened_domains_ledger] google-cloud-storage upload failed "
              f"for {country_slug!r} ({type(exc).__name__}) -- falling back "
              f"to the gcloud/gsutil CLI.")
        return None


def _save_ledger_via_cli(bucket: str, country_slug: str, updates: dict) -> dict:
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return {
            "success": False,
            "error": "Neither the google-cloud-storage client nor gcloud/"
                     "gsutil could persist the ledger.",
        }
    current = _load_ledger_via_cli(bucket, country_slug)
    merged = merge_ledgers(current, updates)
    remote_path = _ledger_gcs_path(bucket, country_slug)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / _ledger_filename(country_slug)
            local_path.write_text(
                json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            return upload_file(tool_cmd, str(local_path), remote_path)
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def save_ledger(bucket: str, country_slug: str, updates: dict) -> dict:
    """Merge ``updates`` into the shared per-country ledger on GCS (never a
    blind overwrite — re-reads and merges, same "Concurrent writers"
    guarantee as ``enrichment_cache.save_cache_index``).

    Returns a result dict (``{"success": bool, ...}``) — never raises. A
    failed save only means this run's new domestic-domain entries aren't
    persisted, never that the run itself failed.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {"success": False, "error": "bucket and country_slug are required."}
    if not isinstance(updates, dict):
        return {"success": False, "error": "updates must be a dict."}
    if not updates:
        return {"success": True, "destination": _ledger_gcs_path(bucket, country_slug),
                 "skipped": "no updates"}
    via_client = _save_ledger_via_client(bucket, country_slug, updates)
    if via_client is not None:
        return via_client
    return _save_ledger_via_cli(bucket, country_slug, updates)
