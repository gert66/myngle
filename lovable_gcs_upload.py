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
    "new zealand": "new-zealand",
    "netherlands": "netherlands",
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


def gcs_current_path(bucket: str, country_folder: str, filename: str) -> str:
    """``gs://`` destination for the "always current" copy of one file."""
    return f"gs://{bucket}/{country_folder}/current/{filename}"


def gcs_archive_path(bucket: str, country_folder: str, run_folder: str, filename: str) -> str:
    """``gs://`` destination for the per-run archive copy of one file."""
    return f"gs://{bucket}/{country_folder}/runs/{run_folder}/{filename}"


def public_url(bucket: str, country_folder: str, filename: str) -> str:
    """Public HTTPS URL for a file in the "current" folder."""
    return f"https://storage.googleapis.com/{bucket}/{country_folder}/current/{filename}"


def resolve_gcs_upload_tool() -> Optional[list[str]]:
    """Base argv prefix for uploads: prefers ``gcloud storage cp``, falls
    back to ``gsutil cp``. Returns ``None`` if neither is on ``PATH``."""
    if shutil.which("gcloud"):
        return ["gcloud", "storage", "cp"]
    if shutil.which("gsutil"):
        return ["gsutil", "cp"]
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
