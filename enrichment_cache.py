"""Shared, GCS-backed enrichment cache for Serper + Firecrawl results.

Motivation
----------
The user runs Lead Prioritizer batches from more than one machine (a home
laptop and a work desktop). A local on-disk cache (like
``linkedin_signal_demo.py``'s ``linkedin_signal_cache/`` pattern) does not
travel between machines, so the same company or page gets looked up/scraped
again on every machine. This module stores one shared, per-country cache
index in the same GCS bucket already used for Lovable exports
(``myngle-company-data-104527058436``), so every machine sees the same cache.

Performance design — ONE download/upload per run, not per lead
----------------------------------------------------------------
A ``gcloud``/``gsutil`` subprocess call per company would be far too slow for
a batch of hundreds/thousands of leads. Instead:
  1. At the START of a batch run (for the country being processed), download
     the country's cache index ONCE (see ``load_cache_index``) into an
     in-memory ``dict``.
  2. During the run, every lookup/write touches ONLY that in-memory dict
     (``get_cached`` / ``put_cached``) — zero network calls per lead/page.
  3. At the END of the run (and/or periodically, e.g. every ~50 leads, as a
     safety net against a crashed run), upload the updated index back to GCS
     once (see ``save_cache_index``).

Cache is always an optimization, never a hard dependency: every function
here degrades to "no cache" (an empty dict, or a no-op) rather than raising,
so a missing ``gcloud``/``gsutil``, no network, or a first-ever run for a
country never blocks a batch from running live.

One index file per country, one shared key namespace
-------------------------------------------------------
``_enrichment_cache/{country_slug}_cache_index.json`` holds BOTH Serper and
Firecrawl entries side by side, distinguished by a ``"<source>|..."`` key
prefix (see ``_cache_key``) — e.g.::

    {
      "serper|acme.it|hq": {"fetched_at": "...", "response": {...}},
      "firecrawl|https://acme.it/about": {"fetched_at": "...", "response": {...}}
    }

Only the RAW response is ever stored (the full Serper JSON payload, or the
Firecrawl scrape result dict) — never a derived score or signal — so cached
evidence stays traceable exactly like a live call's evidence.

Two transports, Python client first, CLI subprocess as fallback
------------------------------------------------------------------
A Cloud Run Job container (see ``Dockerfile``) only ever has the
``google-cloud-storage`` Python package installed — no ``gcloud``/``gsutil``
CLI — but DOES have Application Default Credentials automatically (the
job's attached service account), so the ``google.cloud.storage`` client
"just works" there with zero setup. A local dev machine is the opposite:
it typically has ``gcloud auth login`` (CLI credentials) but not
``gcloud auth application-default login`` (ADC), so the Python client
often can't authenticate there, while the same ``gcloud storage cp`` /
``gsutil cp`` subprocess pattern ``lovable_gcs_upload.py`` already uses
does work. Every download/upload therefore tries the Python client first
and falls back to the CLI subprocess on any failure (missing package,
missing ADC, ...) — this is what actually makes the shared cache work
inside a deployed Cloud Run Job, not just on a local machine.

Concurrent writers — merge on save, never blind-overwrite
------------------------------------------------------------
A Cloud Run Jobs run executes 10–50 tasks in PARALLEL, each running this
module's load→use→save cycle against the SAME per-country index. A blind
"upload my in-memory dict" save would be last-writer-wins: every task
started from the same initial index, so the final upload would erase all
other tasks' new entries. ``save_cache_index`` therefore never overwrites
blindly: it re-downloads the current index, merges it with the local one
(per key, newest ``fetched_at`` wins — see ``merge_cache_indexes``), and
uploads the merged result with a GCS generation precondition
(``if_generation_match``), retrying a few times when another task got in
between. The CLI fallback path merges too but has no precondition — that's
acceptable because the CLI path only runs on local machines, where runs are
sequential; parallel Cloud Run tasks always have the Python client.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from lovable_gcs_upload import resolve_gcs_upload_tool, upload_file

# ---------------------------------------------------------------------------
# TTL configuration — the ONE place to update per-source-type freshness.
# ---------------------------------------------------------------------------
# All Serper cache entries share one 120-day TTL, matching Firecrawl below —
# no per-signal differentiation.
SERPER_TTL_DAYS: dict[str, int] = {
    "hq": 120,
    "_default": 120,
}

# Firecrawl scrapes of a company's own domain change rarely — a longer TTL
# than the Serper default is deliberate.
FIRECRAWL_TTL_DAYS: int = 120

# Safety-net cadence suggestion for batch orchestration: save the index back
# to GCS at least this often during a long run, not only at the very end.
INTERMEDIATE_SAVE_INTERVAL: int = 50

_CACHE_INDEX_GCS_PREFIX = "_enrichment_cache"


def serper_ttl_days(signal_type: str) -> int:
    """TTL (in days) for a Serper cache entry of this ``signal_type``
    (``"hq"`` or one of the five non-HQ signal names) — falls back to
    ``SERPER_TTL_DAYS["_default"]`` for anything not explicitly listed."""
    key = str(signal_type or "").strip().lower()
    return SERPER_TTL_DAYS.get(key, SERPER_TTL_DAYS["_default"])


# ---------------------------------------------------------------------------
# Cache key normalization
# ---------------------------------------------------------------------------

def _normalize_domain_for_cache_key(value: str) -> str:
    """Lowercase, no scheme, no ``www.`` — otherwise left intact (the TLD is
    kept, unlike ``hq_simple_detector.derive_domain_root``, so
    ``acme.com``/``acme.nl`` never collide on the same cache key)."""
    s = str(value or "").strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = re.sub(r"^www\.", "", s)
    return s.strip(".")


def _normalize_url_for_cache_key(value: str) -> str:
    """Lowercase, no trailing slash — the FULL url is otherwise kept as-is
    (scheme included), per the task spec: a Firecrawl key is the complete URL."""
    s = str(value or "").strip().lower()
    if len(s) > 1:
        s = s.rstrip("/")
    return s


def _cache_key(source: str, *parts: str) -> str:
    """Build a normalized cache key.

    ``_cache_key("serper", domain, signal_type)`` ->
        ``"serper|<normalized-domain>|<signal_type>"``
    ``_cache_key("firecrawl", url)`` -> ``"firecrawl|<normalized-url>"``

    Any other ``source`` falls back to a generic ``source|part1|part2|...``
    shape (lowercased parts) so the function never raises for forward
    compatibility / tests, even though only "serper" and "firecrawl" are
    used by the pipeline today.
    """
    src = str(source or "").strip().lower()
    if src == "serper":
        domain = _normalize_domain_for_cache_key(parts[0]) if len(parts) > 0 else ""
        signal_type = str(parts[1]).strip().lower() if len(parts) > 1 else ""
        return f"serper|{domain}|{signal_type}"
    if src == "firecrawl":
        url = _normalize_url_for_cache_key(parts[0]) if parts else ""
        return f"firecrawl|{url}"
    rest = "|".join(str(p or "").strip().lower() for p in parts)
    return f"{src}|{rest}" if rest else src


# ---------------------------------------------------------------------------
# In-memory index operations (no network) — safe to call on every lead/page.
# ---------------------------------------------------------------------------

def get_cached(
    index: dict,
    source: str,
    *parts: str,
    ttl_days: int,
    force_refresh: bool = False,
) -> Optional[dict]:
    """Return the cached raw response, or ``None`` when there is no usable
    entry: absent, malformed, older than ``ttl_days``, or ``force_refresh``
    is True. Never raises — a corrupt entry is treated as a miss."""
    if force_refresh or not isinstance(index, dict):
        return None
    entry = index.get(_cache_key(source, *parts))
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return None
    try:
        fetched_dt = datetime.fromisoformat(str(fetched_at))
        if fetched_dt.tzinfo is None:
            fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    age = datetime.now(timezone.utc) - fetched_dt
    if age > timedelta(days=max(0, int(ttl_days))):
        return None
    return entry.get("response")


def put_cached(index: dict, source: str, *parts: str, response: dict) -> None:
    """Write/overwrite one entry in the in-memory index (no upload yet —
    see ``save_cache_index``). Silently no-ops on a non-dict index rather
    than raising, since caching must never be able to break a live run."""
    if not isinstance(index, dict):
        return
    index[_cache_key(source, *parts)] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "response": response,
    }


def _entry_fetched_dt(entry) -> Optional[datetime]:
    """Parsed, tz-aware ``fetched_at`` of a cache entry, or ``None`` for a
    malformed entry / missing / unparseable timestamp."""
    if not isinstance(entry, dict):
        return None
    try:
        dt = datetime.fromisoformat(str(entry.get("fetched_at") or ""))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def merge_cache_indexes(base: dict, updates: dict) -> dict:
    """Union of two cache indexes; on a key collision the entry with the
    newest ``fetched_at`` wins (an entry without a parseable ``fetched_at``
    always loses to one with a valid timestamp; ``updates`` wins ties and
    both-invalid collisions). Never mutates its arguments, never raises —
    non-dict input is treated as empty."""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(updates, dict):
        return dict(base)
    merged = dict(base)
    for key, entry in updates.items():
        current = merged.get(key)
        if current is None:
            merged[key] = entry
            continue
        current_dt = _entry_fetched_dt(current)
        update_dt = _entry_fetched_dt(entry)
        if current_dt is None or (update_dt is not None and update_dt >= current_dt):
            merged[key] = entry
    return merged


def max_ttl_days() -> int:
    """The longest configured TTL across all source types — anything older
    can never be a cache hit again for any source."""
    return max(*SERPER_TTL_DAYS.values(), FIRECRAWL_TTL_DAYS)


def _prune_expired_entries(index: dict) -> dict:
    """Drop entries whose ``fetched_at`` parses AND is older than the longest
    configured TTL. Since ``save_cache_index`` merges instead of overwriting,
    the index would otherwise grow forever — expired entries would never
    leave. Entries with an unparseable timestamp are deliberately kept
    (conservative: they're dead weight for ``get_cached``, but pruning must
    never eat an entry a future format change could still read)."""
    horizon = timedelta(days=max_ttl_days())
    now = datetime.now(timezone.utc)
    pruned = {}
    for key, entry in index.items():
        fetched_dt = _entry_fetched_dt(entry)
        if fetched_dt is None or now - fetched_dt <= horizon:
            pruned[key] = entry
    return pruned


# ---------------------------------------------------------------------------
# GCS download / upload — ONE call each, at the start/end of a run. Python
# client tried first (works in Cloud Run via ADC), CLI subprocess as fallback
# (works on a local machine with ``gcloud auth login`` but no ADC).
# ---------------------------------------------------------------------------

def _cache_index_filename(country_slug: str) -> str:
    return f"{country_slug}_cache_index.json"


def _cache_index_blob_name(country_slug: str) -> str:
    return f"{_CACHE_INDEX_GCS_PREFIX}/{_cache_index_filename(country_slug)}"


def _cache_index_gcs_path(bucket: str, country_slug: str) -> str:
    return f"gs://{bucket}/{_cache_index_blob_name(country_slug)}"


def _storage_client():
    """Lazily construct a ``google.cloud.storage.Client``, or ``None`` when
    the package is missing or Application Default Credentials aren't
    available here. Never raises — every caller treats ``None`` as "fall
    back to the gcloud/gsutil CLI subprocess" rather than a hard failure."""
    try:
        from google.cloud import storage
        return storage.Client()
    except Exception:
        return None


def _load_cache_index_via_client(bucket: str, country_slug: str) -> Optional[dict]:
    """Try the Python ``google-cloud-storage`` client. Returns ``None`` (not
    ``{}``) to mean "client unusable, try the CLI fallback instead" — as
    opposed to ``{}``, which means "client worked, index just doesn't exist
    yet" (the normal first-run-for-this-country case)."""
    client = _storage_client()
    if client is None:
        return None
    try:
        blob = client.bucket(bucket).blob(_cache_index_blob_name(country_slug))
        if not blob.exists():
            print(f"[enrichment_cache] No existing cache index for "
                  f"{country_slug!r} yet -- starting with an empty cache.")
            return {}
        try:
            data = json.loads(blob.download_as_text())
        except Exception as exc:
            print(f"[enrichment_cache] Could not parse cache index for "
                  f"{country_slug!r} ({type(exc).__name__}) -- starting empty.")
            return {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[enrichment_cache] google-cloud-storage download failed for "
              f"{country_slug!r} ({type(exc).__name__}) -- falling back to "
              f"the gcloud/gsutil CLI.")
        return None


def _load_cache_index_via_cli(bucket: str, country_slug: str) -> dict:
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        print(f"[enrichment_cache] No gcloud/gsutil on PATH -- running "
              f"without a shared cache for {country_slug!r}.")
        return {}

    remote_path = _cache_index_gcs_path(bucket, country_slug)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / _cache_index_filename(country_slug)
            proc = subprocess.run(
                [*tool_cmd, remote_path, str(local_path)],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0 or not local_path.exists():
                # Normal on the first-ever run for a country -- the index
                # simply doesn't exist in GCS yet.
                print(f"[enrichment_cache] No existing cache index for "
                      f"{country_slug!r} yet (or download failed) -- "
                      f"starting with an empty cache.")
                return {}
            try:
                data = json.loads(local_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[enrichment_cache] Could not parse cache index for "
                      f"{country_slug!r} ({type(exc).__name__}) -- "
                      f"starting empty.")
                return {}
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[enrichment_cache] Download failed for {country_slug!r} "
              f"({type(exc).__name__}) -- running without a shared cache.")
        return {}


def load_cache_index(bucket: str, country_slug: str) -> dict:
    """Download the shared per-country cache index into memory.

    Tries the Python ``google-cloud-storage`` client first (this is what
    makes the cache actually work inside a Cloud Run Job, which has ADC via
    its service account but no ``gcloud``/``gsutil`` CLI installed), and
    falls back to the ``gcloud storage cp`` / ``gsutil cp`` CLI subprocess
    (for a local machine with CLI credentials but no ADC configured).

    Returns ``{}`` (never raises) when: the bucket/country slug is blank,
    neither transport is usable, the download fails (including the normal
    "doesn't exist yet" case on the very first run for a country), or the
    downloaded file is not valid JSON. Every failure path prints a short,
    secret-free warning so a silent cache-miss run is still visible in logs.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {}

    via_client = _load_cache_index_via_client(bucket, country_slug)
    if via_client is not None:
        return via_client
    return _load_cache_index_via_cli(bucket, country_slug)


# How often a compare-and-swap save re-reads and retries after another
# writer got in between. With up to ~50 tasks whose saves are spread across
# a long run this is plenty; a save that still loses only costs that one
# task's new entries (cache stays an optimization, never a dependency).
_SAVE_CAS_ATTEMPTS = 4


def _save_cache_index_via_client(bucket: str, country_slug: str, index: dict) -> Optional[dict]:
    """Try the Python client. Returns ``None`` (try the CLI fallback) when
    the client itself is unusable; returns a result dict on any outcome
    once the client was actually usable (including a genuine upload failure
    — that's a real result, not "try something else").

    Read-merge-write with a GCS generation precondition: parallel Cloud Run
    tasks all save the same country index, so a plain upload would be
    last-writer-wins and erase every other task's entries. Each attempt
    re-downloads the current index, merges the local one in, and uploads
    with ``if_generation_match`` — when another task wrote in between, the
    precondition fails and we re-read and retry."""
    client = _storage_client()
    if client is None:
        return None
    try:
        from google.api_core import exceptions as gcs_exceptions

        bucket_obj = client.bucket(bucket)
        blob_name = _cache_index_blob_name(country_slug)
        for attempt in range(_SAVE_CAS_ATTEMPTS):
            try:
                current_blob = bucket_obj.get_blob(blob_name)
                if current_blob is None:
                    # if_generation_match=0 means "only if it doesn't exist yet".
                    generation = 0
                    current: dict = {}
                else:
                    generation = current_blob.generation
                    text = current_blob.download_as_text(if_generation_match=generation)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {}  # corrupt remote index: replace with merged
                    current = data if isinstance(data, dict) else {}
                merged = _prune_expired_entries(merge_cache_indexes(current, index))
                bucket_obj.blob(blob_name).upload_from_string(
                    json.dumps(merged, ensure_ascii=False),
                    content_type="application/json",
                    if_generation_match=generation,
                )
                return {"success": True,
                        "destination": _cache_index_gcs_path(bucket, country_slug)}
            except (gcs_exceptions.PreconditionFailed, gcs_exceptions.NotFound):
                # Another task wrote (or deleted) the index between our read
                # and write — back off briefly and re-read.
                time.sleep(random.uniform(0.1, 0.4) * (attempt + 1))
        return {
            "success": False,
            "error": f"Gave up after {_SAVE_CAS_ATTEMPTS} attempts: concurrent "
                     "writers kept updating the cache index. This run's new "
                     "cache entries are lost; the run itself is unaffected.",
        }
    except Exception as exc:
        print(f"[enrichment_cache] google-cloud-storage upload failed for "
              f"{country_slug!r} ({type(exc).__name__}) -- falling back to "
              f"the gcloud/gsutil CLI.")
        return None


def _save_cache_index_via_cli(bucket: str, country_slug: str, index: dict) -> dict:
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return {
            "success": False,
            "error": "Neither the google-cloud-storage client nor gcloud/"
                     "gsutil could persist the shared cache.",
        }

    # Best-effort merge with the current remote index before uploading. The
    # CLI has no generation preconditions (no compare-and-swap), so a truly
    # simultaneous writer can still win — acceptable, because the CLI path
    # only runs on local machines where runs are sequential; parallel Cloud
    # Run tasks always use the Python-client path above.
    current = _load_cache_index_via_cli(bucket, country_slug)
    merged = _prune_expired_entries(merge_cache_indexes(current, index))

    remote_path = _cache_index_gcs_path(bucket, country_slug)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / _cache_index_filename(country_slug)
            local_path.write_text(
                json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            return upload_file(tool_cmd, str(local_path), remote_path)
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def save_cache_index(bucket: str, country_slug: str, index: dict) -> dict:
    """Merge the updated index into the shared one on GCS (never a blind
    overwrite — see "Concurrent writers" in the module docstring): the
    current remote index is re-read and merged in (newest ``fetched_at``
    wins per key, entries older than the longest TTL are pruned), so
    parallel Cloud Run tasks can't erase each other's entries.

    Tries the Python ``google-cloud-storage`` client first (works inside a
    Cloud Run Job via ADC; uses an ``if_generation_match`` compare-and-swap
    with retries), falls back to the ``gcloud``/``gsutil`` CLI subprocess
    otherwise (merge without precondition — local runs are sequential).

    Returns a result dict (``{"success": bool, ...}``) — never raises. A
    failed upload only means this run's cache updates are lost, never that
    the run itself failed.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {"success": False, "error": "bucket and country_slug are required."}
    if not isinstance(index, dict):
        return {"success": False, "error": "index must be a dict."}

    via_client = _save_cache_index_via_client(bucket, country_slug, index)
    if via_client is not None:
        return via_client
    return _save_cache_index_via_cli(bucket, country_slug, index)
