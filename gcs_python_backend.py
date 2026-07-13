"""
gcs_python_backend.py — GCS access via the google-cloud-storage library
=========================================================================
Fallback backend for hosts where the ``gcloud storage``/``gsutil`` CLI is
not installed — most importantly Streamlit Community Cloud, which runs the
deployed apps (rescore / caller-reallocation) in a container without the
Google Cloud SDK. ``rescore_from_gcs`` prefers the CLI when it is on PATH
(local behavior is unchanged) and only routes through this module when it
is not.

Credentials are resolved in this order:

1. ``st.secrets["gcp_service_account"]`` — the standard Streamlit Cloud
   pattern: paste the service-account key JSON as a ``[gcp_service_account]``
   TOML table (or a JSON string) in the app's Secrets settings. See
   ``.streamlit/secrets.toml.example``.
2. The ``GCP_SERVICE_ACCOUNT_JSON`` environment variable containing the raw
   service-account key JSON — for non-Streamlit hosts (CI, servers).
3. Application Default Credentials — ``GOOGLE_APPLICATION_CREDENTIALS`` or
   ``gcloud auth application-default login``.

Every public function mirrors the result shape of its CLI counterpart in
``rescore_from_gcs`` (``{"success": bool, ...}`` dicts, empty lists on
listing failure) so callers can switch backends without translating
results. No secrets are ever included in returned errors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

#: Streamlit secrets key holding the service-account key (TOML table or JSON string).
SERVICE_ACCOUNT_SECRET_KEY = "gcp_service_account"
#: Environment variable holding the raw service-account key JSON.
SERVICE_ACCOUNT_ENV_VAR = "GCP_SERVICE_ACCOUNT_JSON"

NO_CLIENT_ERROR = (
    "No google-cloud-storage client available. Install google-cloud-storage "
    f"and provide credentials via st.secrets['{SERVICE_ACCOUNT_SECRET_KEY}'], "
    f"the {SERVICE_ACCOUNT_ENV_VAR} environment variable, or Application "
    "Default Credentials."
)

_client = None


def _service_account_info() -> "Optional[dict]":
    """Service-account key dict from Streamlit secrets or the environment,
    or ``None`` to fall through to Application Default Credentials. Never
    raises — a missing/broken secrets file just means "not configured here"."""
    try:
        import streamlit as st

        if SERVICE_ACCOUNT_SECRET_KEY in st.secrets:
            value = st.secrets[SERVICE_ACCOUNT_SECRET_KEY]
            return json.loads(value) if isinstance(value, str) else dict(value)
    except Exception:
        pass
    raw = os.environ.get(SERVICE_ACCOUNT_ENV_VAR, "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def build_client():
    """A ``google.cloud.storage.Client`` from the resolved credentials.
    Raises when the library is missing or no credentials can be found —
    ``get_client`` is the never-raising wrapper."""
    from google.cloud import storage

    info = _service_account_info()
    if info:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(info)
        return storage.Client(project=info.get("project_id"), credentials=credentials)
    return storage.Client()


def get_client():
    """Cached client, or ``None`` when unavailable. A failure is NOT cached:
    the next call retries, so adding secrets to a running Streamlit app
    starts working on the next rerun without a process restart."""
    global _client
    if _client is None:
        try:
            _client = build_client()
        except Exception:
            return None
    return _client


def available() -> bool:
    """True when a client can be built — i.e. this backend can serve as the
    fallback for a missing gcloud/gsutil CLI."""
    return get_client() is not None


# =============================================================================
# Listing — mirrors rescore_from_gcs.list_country_folders / list_current_files
# =============================================================================


def list_country_folders(bucket: str) -> list[str]:
    """Top-level "folder" prefixes in the bucket. Returns an empty list
    (never raises) when no client is available or the listing fails."""
    client = get_client()
    if client is None:
        return []
    try:
        blobs = client.list_blobs(bucket, delimiter="/")
        list(blobs)  # consume the iterator so .prefixes is populated
        return sorted(p.rstrip("/") for p in blobs.prefixes)
    except Exception:
        return []


def list_files(bucket: str, prefix: str) -> list[str]:
    """Basenames of the objects directly under ``prefix/`` (no deeper
    nesting). Returns an empty list (never raises) on any failure."""
    client = get_client()
    if client is None:
        return []
    full_prefix = prefix.rstrip("/") + "/"
    try:
        names = []
        for blob in client.list_blobs(bucket, prefix=full_prefix, delimiter="/"):
            name = blob.name[len(full_prefix):]
            if name:
                names.append(name)
        return sorted(names)
    except Exception:
        return []


# =============================================================================
# Download / upload — mirror the CLI functions' result dict shapes
# =============================================================================


def download_file(bucket: str, blob_name: str, local_path: str) -> dict:
    """Download one object. Never raises — any failure comes back as
    ``{"success": False, ...}``, same shape as the CLI ``download_file``."""
    source = f"gs://{bucket}/{blob_name}"
    client = get_client()
    if client is None:
        return {
            "success": False, "source": source, "local_path": local_path,
            "error": NO_CLIENT_ERROR,
        }
    try:
        client.bucket(bucket).blob(blob_name).download_to_filename(local_path)
    except Exception as exc:
        return {
            "success": False, "source": source, "local_path": local_path,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"success": True, "source": source, "local_path": local_path}


def download_files_batch(bucket: str, blob_names: list[str], dest_dir) -> dict:
    """Download many objects into ``dest_dir`` (each under its basename).
    Same result shape as the CLI ``download_files_batch``; stops at the
    first failure. No-op success when ``blob_names`` is empty."""
    if not blob_names:
        return {"success": True, "stdout": "", "stderr": ""}
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for blob_name in blob_names:
        filename = blob_name.rsplit("/", 1)[-1]
        result = download_file(bucket, blob_name, str(dest / filename))
        if not result["success"]:
            return {"success": False, "error": f"{result['source']}: {result['error']}"}
    return {"success": True, "stdout": "", "stderr": ""}


def upload_file(bucket: str, local_path: str, blob_name: str,
                cache_control: "str | None" = None) -> dict:
    """Upload one local file. Never raises; verifies the local file exists
    first — same contract and result shape as the CLI ``upload_file``.
    ``cache_control`` sets the uploaded object's Cache-Control header (the
    ``current/`` no-cache requirement — see
    ``rescore_from_gcs.CURRENT_CACHE_CONTROL``)."""
    destination = f"gs://{bucket}/{blob_name}"
    if not Path(local_path).exists():
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"Local file not found: {local_path}",
        }
    client = get_client()
    if client is None:
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": NO_CLIENT_ERROR,
        }
    try:
        blob = client.bucket(bucket).blob(blob_name)
        if cache_control:
            blob.cache_control = cache_control
        content_type = "application/json" if local_path.endswith(".json") else None
        blob.upload_from_filename(local_path, content_type=content_type)
    except Exception as exc:
        return {
            "success": False, "local_path": local_path, "destination": destination,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"success": True, "local_path": local_path, "destination": destination}
