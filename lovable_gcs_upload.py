"""Local Google Cloud Storage upload helper for Lovable JSON exports.

Uploads the JSON files produced by ``export_lead_prioritizer_to_lovable_json``
to GCS using the local ``gcloud``/``gsutil`` CLI — no Python
``google-cloud-storage`` dependency required. This module never touches
bucket IAM/CORS (the bucket is assumed already public/CORS-configured), never
prints secrets, and never runs a shell (every command is an explicit argv
list, no ``shell=True``).

Upload is always user-triggered from the Streamlit app; nothing in this
module runs automatically.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_GCS_BUCKET = "myngle-company-data-104527058436"

# Curated slugs for the countries the Lead Prioritizer batch app already
# supports (see SUPPORTED_DEFAULT_INPUT_COUNTRIES in lead_prioritizer_batch_app.py).
_COUNTRY_FOLDER_SLUGS = {
    "brazil": "brazil",
    "italy": "italy",
    "australia": "australia",
    "uruguay": "uruguay",
    "new zealand": "newzealand",
    "netherlands": "netherlands",
    "japan": "japan",
    "south korea": "south-korea",
    "switzerland": "switzerland",
    "germany": "germany",
    "spain": "spain",
    "test": "test",
}


def country_folder_slug(country: str) -> str:
    """Map an export country name to its GCS country-folder slug.

    Known countries (Brazil, Italy, Australia, Uruguay, New Zealand) use the
    curated mapping above to match the existing bucket layout; anything else
    falls back to a lower-cased, hyphen-separated slug so the function never
    raises for an unrecognised country.
    """
    norm = re.sub(r"\s+", " ", str(country or "").strip().lower())
    if norm in _COUNTRY_FOLDER_SLUGS:
        return _COUNTRY_FOLDER_SLUGS[norm]
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug or "unknown"


def default_gcs_run_folder(run_mode: str, now: Optional[datetime] = None) -> str:
    """Default GCS run folder: ``YYYY-MM-DD_<run_mode_slug>``."""
    now = now or datetime.now()
    mode_slug = re.sub(r"[^a-z0-9]+", "_", str(run_mode or "").strip().lower()).strip("_")
    return f"{now.strftime('%Y-%m-%d')}_{mode_slug or 'run'}"


# Filename patterns export_lead_prioritizer_to_lovable_json produces. Only
# these are ever candidates for upload — no old/unrelated file in the export
# folder is ever selected.
_ALLOWED_GLOB_PATTERNS = ("companies.list.json", "company-details-*.json",
                          "export_manifest.json")

_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def select_lovable_export_files(export_dir) -> list[str]:
    """Return only the Lovable-export filenames present in ``export_dir``.

    Matches exactly ``companies.list.json``, ``company-details-*.json``, and
    ``export_manifest.json`` — nothing else in the folder is ever selected,
    so a stale file from a previous run (or anything unrelated) is never
    uploaded. Returns a sorted list of filenames (not full paths); an
    absent/empty directory yields an empty list rather than raising.
    """
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        return []
    found: set[str] = set()
    for pattern in _ALLOWED_GLOB_PATTERNS:
        for path in export_dir.glob(pattern):
            if path.is_file():
                found.add(path.name)
    return sorted(found)


def validate_gcs_bucket(bucket: str) -> Optional[str]:
    """Return a user-facing error for an invalid/blank bucket name, else
    ``None``. Conservative check against GCS bucket naming rules (lowercase
    letters, digits, dots, hyphens, underscores; 3-63 characters) — not
    exhaustive, just enough to catch an empty or obviously wrong value
    before shelling out."""
    text = str(bucket or "").strip()
    if not text:
        return "GCS bucket name is required."
    if not _BUCKET_NAME_RE.match(text):
        return (
            f"{text!r} does not look like a valid GCS bucket name "
            "(lowercase letters, digits, dots, hyphens, underscores; "
            "3-63 characters, must start/end with a letter or digit)."
        )
    return None


def gcs_flat_path(bucket: str, prefix: str, filename: str) -> str:
    """``gs://`` destination for a single flat prefix (no current/archive
    split) — used by the standalone Lovable JSON export app, where the
    prefix itself already encodes the desired subfolder (e.g. "brazil/current")."""
    prefix_norm = normalize_gcs_prefix(prefix)
    path = f"{prefix_norm}/{filename}" if prefix_norm else filename
    return f"gs://{bucket}/{path}"


def public_url_flat(bucket: str, prefix: str, filename: str) -> str:
    """Public HTTPS URL matching ``gcs_flat_path``'s destination."""
    prefix_norm = normalize_gcs_prefix(prefix)
    path = f"{prefix_norm}/{filename}" if prefix_norm else filename
    return f"https://storage.googleapis.com/{bucket}/{path}"


def build_flat_upload_plan(
    export_dir, filenames: list[str], bucket: str, prefix: str,
) -> list[dict]:
    """Build upload jobs for a single flat ``gs://bucket/prefix/`` destination.

    Each job is ``{"local_path", "destination", "target"}`` (``target`` is
    always ``"flat"``, kept only for shape-compatibility with
    ``run_upload_plan``/the batch app's current/archive jobs).
    """
    export_dir = Path(export_dir)
    return [
        {
            "local_path": str(export_dir / filename),
            "destination": gcs_flat_path(bucket, prefix, filename),
            "target": "flat",
        }
        for filename in filenames
    ]


def gcs_current_path(bucket: str, country_folder: str, filename: str) -> str:
    """``gs://`` destination for the "always current" copy of one file."""
    return f"gs://{bucket}/{country_folder}/current/{filename}"


def gcs_archive_path(bucket: str, country_folder: str, run_folder: str, filename: str) -> str:
    """``gs://`` destination for the per-run archive copy of one file."""
    return f"gs://{bucket}/{country_folder}/runs/{run_folder}/{filename}"


def public_url(bucket: str, country_folder: str, filename: str) -> str:
    """Public HTTPS URL for a file in the "current" folder."""
    return f"https://storage.googleapis.com/{bucket}/{country_folder}/current/{filename}"


COUNTRIES_INDEX_FILENAME = "countries.index.json"


def gcs_manifest_path(bucket: str, filename: str = COUNTRIES_INDEX_FILENAME) -> str:
    """``gs://`` destination for the root-level countries manifest."""
    return f"gs://{bucket}/{filename}"


def public_manifest_url(bucket: str, filename: str = COUNTRIES_INDEX_FILENAME) -> str:
    """Public HTTPS URL for the root-level countries manifest."""
    return f"https://storage.googleapis.com/{bucket}/{filename}"


def resolve_gcs_upload_tool() -> Optional[list[str]]:
    """Base argv prefix for uploads: prefers ``gcloud storage cp``, falls
    back to ``gsutil cp``. Returns ``None`` if neither is on ``PATH``.

    Uses the exact path from ``shutil.which`` as ``command[0]`` — on Windows
    the bare name ``"gcloud"``/``"gsutil"`` is a ``.cmd`` shim that
    ``subprocess.run`` (no ``shell=True``) cannot execute directly, which
    raises ``FileNotFoundError: [WinError 2]`` even though ``shutil.which``
    resolves it fine.
    """
    gcloud_path = shutil.which("gcloud")
    if gcloud_path:
        return [gcloud_path, "storage", "cp"]
    gsutil_path = shutil.which("gsutil")
    if gsutil_path:
        return [gsutil_path, "cp"]
    return None


def resolve_gcs_cat_tool() -> Optional[list[str]]:
    """Base argv prefix for reading a small GCS object's contents: prefers
    ``gcloud storage cat``, falls back to ``gsutil cat``. Returns ``None`` if
    neither is on ``PATH`` (same resolution as ``resolve_gcs_upload_tool``,
    kept separate since the subcommand differs)."""
    gcloud_path = shutil.which("gcloud")
    if gcloud_path:
        return [gcloud_path, "storage", "cat"]
    gsutil_path = shutil.which("gsutil")
    if gsutil_path:
        return [gsutil_path, "cat"]
    return None


_GCS_NOT_FOUND_RE = re.compile(
    r"No URLs matched|No such object|not found|NotFoundException|404",
    re.IGNORECASE,
)


def fetch_gcs_text(destination: str) -> dict:
    """Read one small GCS object's text contents (no ``shell=True``), e.g. to
    check whether a previous export is already published at a prefix before
    deciding to overwrite or merge.

    Returns ``{"success", "exists", "text", "error"}``. A simply-absent
    object is ``{"success": False, "exists": False, "error": None}`` — the
    normal "nothing published here yet" case, not a failure callers need to
    report. Any other failure (missing CLI, auth, network) comes back as
    ``exists=None`` with an ``error`` message, so callers can tell
    "definitely absent" apart from "could not check" and avoid silently
    treating the latter as an empty bucket.
    """
    tool_cmd = resolve_gcs_cat_tool()
    if tool_cmd is None:
        return {
            "success": False, "exists": None, "text": None,
            "error": "Neither gcloud nor gsutil was found on PATH. Install "
                     "or authenticate the Google Cloud SDK and try again.",
        }
    cmd = [*tool_cmd, destination]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {
            "success": False, "exists": None, "text": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if proc.returncode == 0:
        return {"success": True, "exists": True, "text": proc.stdout, "error": None}
    stderr = (proc.stderr or "").strip()
    if _GCS_NOT_FOUND_RE.search(stderr):
        return {"success": False, "exists": False, "text": None, "error": None}
    return {
        "success": False, "exists": None, "text": None,
        "error": stderr[-2000:] or f"cat exited with code {proc.returncode}",
    }


def normalize_gcs_prefix(value: str) -> str:
    """Normalize a user-entered GCS path segment: no leading/trailing slash,
    no doubled slashes. Never raises on blank/odd input."""
    text = re.sub(r"/+", "/", str(value or "").strip())
    return text.strip("/")


def check_gcloud_available() -> dict:
    """Informational upload-tool check (never gates the upload itself —
    ``run_upload_plan`` already handles a missing tool with a clear error).

    Returns ``{"available", "tool", "version"}``; ``version`` is only the
    first output line, truncated, so nothing verbose ever reaches the UI.
    """
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return {"available": False, "tool": None, "version": ""}
    tool_name = tool_cmd[0]
    version = ""
    try:
        proc = subprocess.run(
            [tool_name, "--version"], capture_output=True, text=True, timeout=10)
        first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        version = first_line[0][:200] if first_line else ""
    except Exception:
        pass
    return {"available": True, "tool": tool_name, "version": version}


def describe_gcloud_environment() -> dict:
    """Informational active gcloud account/project — never raises.

    Only the active account email and project id are read (both already
    visible locally via ``gcloud config list``); no tokens or other secrets
    are read, printed, or returned.
    """
    account = ""
    project = ""
    if shutil.which("gcloud"):
        try:
            proc = subprocess.run(
                ["gcloud", "auth", "list", "--filter=status:ACTIVE",
                 "--format=value(account)"],
                capture_output=True, text=True, timeout=10)
            lines = (proc.stdout or "").strip().splitlines()
            account = lines[0] if lines else ""
        except Exception:
            pass
        try:
            proc = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True, text=True, timeout=10)
            project = (proc.stdout or "").strip()
        except Exception:
            pass
    return {"account": account, "project": project}


#: current/ objects are re-read live by the Lovable app on every page load;
#: without an explicit Cache-Control, GCS's default (public, max-age=3600)
#: lets an already-open client keep serving a pre-promote/pre-export copy for
#: up to an hour. Archive (runs/<run_folder>/) objects are immutable
#: snapshots and deliberately keep GCS's default caching.
CURRENT_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


def build_upload_command(
    tool_cmd: list[str], local_path: str, destination: str,
    cache_control: Optional[str] = None,
) -> list[str]:
    """Build the argv list for one file upload. Never uses ``shell=True``.

    ``cache_control`` (default ``None``, meaning "leave GCS's default
    caching alone") sets the uploaded object's ``Cache-Control`` header.
    ``gcloud storage cp`` accepts ``--cache-control`` on the ``cp``
    subcommand directly; ``gsutil`` has no per-subcommand header flag and
    instead needs a top-level ``-h "Cache-Control:..."`` flag BEFORE its
    subcommand -- so the two tools' argv shapes differ here even though
    every other call in this module treats them interchangeably.
    """
    if not cache_control:
        return [*tool_cmd, local_path, destination]
    if Path(tool_cmd[0]).stem.lower() == "gsutil":
        return [tool_cmd[0], "-h", f"Cache-Control:{cache_control}",
                *tool_cmd[1:], local_path, destination]
    return [*tool_cmd, f"--cache-control={cache_control}", local_path, destination]


def build_upload_plan(
    output_dir,
    filenames: list[str],
    bucket: str,
    country_folder: str,
    run_folder: str,
    *,
    upload_current: bool = True,
    upload_archive: bool = True,
) -> list[dict]:
    """Build the list of upload jobs for every generated JSON file.

    Each job is ``{"local_path", "destination", "target"}`` where ``target``
    is ``"current"`` or ``"archive"``. Honors the current/archive toggles;
    with both off, returns an empty list.
    """
    output_dir = Path(output_dir)
    jobs: list[dict] = []
    for filename in filenames:
        local_path = str(output_dir / filename)
        if upload_current:
            jobs.append({
                "local_path": local_path,
                "destination": gcs_current_path(bucket, country_folder, filename),
                "target": "current",
            })
        if upload_archive:
            jobs.append({
                "local_path": local_path,
                "destination": gcs_archive_path(bucket, country_folder, run_folder, filename),
                "target": "archive",
            })
    return jobs


def upload_file(
    tool_cmd: list[str], local_path: str, destination: str,
    cache_control: Optional[str] = None,
) -> dict:
    """Upload one local file via subprocess (no ``shell=True``).

    Verifies the local file exists before shelling out. Never raises — any
    failure (missing file, missing CLI, non-zero exit, timeout) comes back
    as ``{"success": False, ...}`` with a concise, secret-free error/stderr.
    ``cache_control`` (default ``None``) is passed straight through to
    ``build_upload_command`` — see its docstring.
    """
    if not Path(local_path).exists():
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"Local file not found: {local_path}",
        }
    cmd = build_upload_command(tool_cmd, local_path, destination, cache_control=cache_control)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "success": proc.returncode == 0,
        "local_path": local_path,
        "destination": destination,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


def download_file(tool_cmd: list[str], source: str, local_path: str) -> dict:
    """Download one GCS object via subprocess (mirrors ``upload_file``'s
    shape, source/destination reversed — ``gcloud storage cp``/``gsutil cp``
    take the same argv shape either direction).

    Never raises. A missing/non-existent remote object — the normal "nothing
    uploaded here yet" case for a first-ever merge — comes back as
    ``{"success": False, ...}`` exactly like any other failure, so a merge
    caller can treat it as "start from empty" without special-casing.
    """
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [*tool_cmd, source, local_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return {
            "success": False, "source": source, "local_path": local_path,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "success": proc.returncode == 0 and Path(local_path).exists(),
        "source": source,
        "local_path": local_path,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


# ---------------------------------------------------------------------------
# Merging a fresh export into the existing current/ company set
# ---------------------------------------------------------------------------
#
# current/ is meant to be the CUMULATIVE latest-known state across runs, not
# just this run's rows -- re-running the same (or an updated) input file
# should be able to ADD new companies and refresh existing ones without
# losing companies only a PREVIOUS run covered. company_id is domain-based
# (see make_company_id in export_lead_prioritizer_to_lovable_json.py) and
# therefore stable across separate export runs, which is what makes merging
# by company_id meaningful instead of just concatenating and de-duplicating
# blindly.

def merge_company_records(
    existing_list_items: list[dict],
    existing_details: dict[str, dict],
    new_list_items: list[dict],
    new_details: dict[str, dict],
) -> tuple[list[dict], dict[str, dict]]:
    """Merge a freshly-exported company set into an existing one, keyed by
    ``company_id``.

    Conflict rule (same company_id on both sides): the NEW record wins
    UNLESS it is ``enrichment_skipped`` (thin -- e.g. skipped by the
    foreign-HQ cost gate, or from a cheaper run mode) while the EXISTING
    record is not (fully enriched) -- in that case the richer existing
    record is kept, so a smaller/cheaper/gated run can never silently
    downgrade a company a previous run fully enriched. A company present on
    only one side is always kept.

    Ordering: existing companies keep their original list position (updated
    in place when the new record wins); genuinely new companies are
    appended in their new-run order. This keeps ``detail_bucket`` assignment
    for every PRE-EXISTING company stable across merges (see
    ``rebucket_company_details``) -- only the newly appended tail changes
    bucket boundaries. ``assigned_cold_caller``/``assigned_cold_caller_rank``
    are deliberately left exactly as they are on whichever record wins —
    merging never reassigns a company already on a caller's list to someone
    else.

    Never mutates its arguments. Returns ``(merged_list_items,
    merged_details)`` — NOT yet re-bucketed; call ``rebucket_company_details``
    on the result before writing/uploading.
    """
    new_by_id = {item["company_id"]: item for item in new_list_items}
    merged_details = dict(existing_details)
    merged_list_items: list[dict] = []
    seen_ids: set[str] = set()

    for old_item in existing_list_items:
        cid = old_item["company_id"]
        seen_ids.add(cid)
        new_item = new_by_id.get(cid)
        if new_item is None:
            merged_list_items.append(old_item)
            continue
        new_skipped = bool(new_item.get("enrichment_skipped"))
        old_skipped = bool(old_item.get("enrichment_skipped"))
        new_wins = (not new_skipped) or old_skipped
        if new_wins:
            merged_list_items.append(new_item)
            if cid in new_details:
                merged_details[cid] = new_details[cid]
        else:
            merged_list_items.append(old_item)

    for new_item in new_list_items:
        cid = new_item["company_id"]
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        merged_list_items.append(new_item)
        if cid in new_details:
            merged_details[cid] = new_details[cid]

    return merged_list_items, merged_details


def rebucket_company_details(
    list_items: list[dict], details_by_id: dict[str, dict], bucket_size: int,
) -> tuple[list[dict], dict[str, dict]]:
    """Re-assign ``detail_bucket`` across a merged company set, in
    ``list_items`` order — see ``merge_company_records``'s ordering
    guarantee: pre-existing companies keep their position, so as long as
    ``bucket_size`` is unchanged between runs, every PRE-EXISTING company
    keeps the exact same bucket file across merges; only the newly appended
    tail creates/extends buckets.

    Never mutates its arguments. Returns ``(list_items, buckets)`` — new
    list_items with ``detail_bucket`` set, and
    ``{bucket_filename: {company_id: detail}}`` with ``detail_bucket`` set
    on each detail record too (matching ``export_workbook_to_lovable_json``'s
    own bucket-writing shape).
    """
    if bucket_size < 1:
        raise ValueError("bucket_size must be >= 1.")
    updated_items: list[dict] = []
    buckets: dict[str, dict] = {}
    for i, item in enumerate(list_items):
        cid = item["company_id"]
        bucket_file = f"company-details-{i // bucket_size:03d}.json"
        updated_item = dict(item)
        updated_item["detail_bucket"] = bucket_file
        updated_items.append(updated_item)
        detail = details_by_id.get(cid)
        if detail is not None:
            detail = dict(detail)
            detail["detail_bucket"] = bucket_file
            buckets.setdefault(bucket_file, {})[cid] = detail
    return updated_items, buckets


def run_upload_plan(jobs: list[dict]) -> list[dict]:
    """Execute every job from ``build_upload_plan`` and return per-file results.

    If neither ``gcloud`` nor ``gsutil`` is available, every job fails with a
    clear "authenticate/install the Google Cloud SDK" message instead of
    raising.
    """
    tool_cmd = resolve_gcs_upload_tool()
    if tool_cmd is None:
        return [
            {
                **job, "success": False,
                "error": "Neither gcloud nor gsutil was found on PATH. Install "
                         "or authenticate the Google Cloud SDK and try again.",
            }
            for job in jobs
        ]
    results = []
    for job in jobs:
        cache_control = CURRENT_CACHE_CONTROL if job.get("target") == "current" else None
        result = upload_file(
            tool_cmd, job["local_path"], job["destination"], cache_control=cache_control)
        result["target"] = job.get("target")
        results.append(result)
    return results
