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


def build_upload_command(tool_cmd: list[str], local_path: str, destination: str) -> list[str]:
    """Build the argv list for one file upload. Never uses ``shell=True``."""
    return [*tool_cmd, local_path, destination]


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


def upload_file(tool_cmd: list[str], local_path: str, destination: str) -> dict:
    """Upload one local file via subprocess (no ``shell=True``).

    Verifies the local file exists before shelling out. Never raises — any
    failure (missing file, missing CLI, non-zero exit, timeout) comes back
    as ``{"success": False, ...}`` with a concise, secret-free error/stderr.
    """
    if not Path(local_path).exists():
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"Local file not found: {local_path}",
        }
    cmd = build_upload_command(tool_cmd, local_path, destination)
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
        result = upload_file(tool_cmd, job["local_path"], job["destination"])
        result["target"] = job.get("target")
        results.append(result)
    return results
