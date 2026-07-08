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

No new dependency: uploads/downloads reuse the exact same ``gcloud storage
cp`` / ``gsutil cp`` subprocess pattern as ``lovable_gcs_upload.py``
(``resolve_gcs_upload_tool`` / ``upload_file``) — no Python GCS SDK.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
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


# ---------------------------------------------------------------------------
# GCS download / upload — ONE subprocess call each, at the start/end of a run.
# ---------------------------------------------------------------------------

def _cache_index_filename(country_slug: str) -> str:
    return f"{country_slug}_cache_index.json"


def _cache_index_gcs_path(bucket: str, country_slug: str) -> str:
    return f"gs://{bucket}/{_CACHE_INDEX_GCS_PREFIX}/{_cache_index_filename(country_slug)}"


def load_cache_index(bucket: str, country_slug: str) -> dict:
    """Download the shared per-country cache index into memory.

    Returns ``{}`` (never raises) when: the bucket/country slug is blank, no
    ``gcloud``/``gsutil`` is on ``PATH``, the download fails (including the
    normal "doesn't exist yet" case on the very first run for a country), or
    the downloaded file is not valid JSON. Every failure path prints a short,
    secret-free warning so a silent cache-miss run is still visible in logs.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {}

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


def save_cache_index(bucket: str, country_slug: str, index: dict) -> dict:
    """Write the index locally and upload it back to GCS via the same
    ``upload_file()`` pattern as ``lovable_gcs_upload.py``.

    Returns a result dict in the exact same shape as ``upload_file()``
    (``{"success": bool, ...}``) — never raises. A failed upload only means
    this run's cache updates are lost, never that the run itself failed.
    """
    bucket = str(bucket or "").strip()
    country_slug = str(country_slug or "").strip()
    if not bucket or not country_slug:
        return {"success": False, "error": "bucket and country_slug are required."}
    if not isinstance(index, dict):
        return {"success": False, "error": "index must be a dict."}

    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return {
            "success": False,
            "error": "Neither gcloud nor gsutil was found on PATH. Install "
                     "or authenticate the Google Cloud SDK to persist the "
                     "shared cache.",
        }

    remote_path = _cache_index_gcs_path(bucket, country_slug)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / _cache_index_filename(country_slug)
            local_path.write_text(
                json.dumps(index, ensure_ascii=False), encoding="utf-8")
            return upload_file(tool_cmd, str(local_path), remote_path)
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
