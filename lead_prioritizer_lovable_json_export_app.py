"""Streamlit app: export Lead Prioritizer v2 Excel output to Lovable JSON.

Thin UI over ``export_lead_prioritizer_to_lovable_json``. Upload a Lead
Prioritizer workbook, pick the authoritative export country and cold callers,
and generate the static JSON files (companies.list.json, detail buckets,
export_manifest.json) the Lovable Company Hub frontend expects.

Export/packaging only: no API calls, no Serper, no Anthropic, no Lusha, and no
changes to scoring, C4, C5, or HQ detection logic. Standalone and removable.

Run:
    streamlit run lead_prioritizer_lovable_json_export_app.py
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st

from export_lead_prioritizer_to_lovable_json import (
    LovableExportError,
    export_workbook_to_lovable_json,
    SUPPORTED_CONTENT_LANGUAGES,
    DEFAULT_CONTENT_LANGUAGE,
)
from lovable_gcs_merge import fetch_existing_flat_export, prepare_merge
from lovable_gcs_upload import (
    DEFAULT_GCS_BUCKET,
    build_flat_upload_plan,
    check_gcloud_available,
    country_folder_slug,
    describe_gcloud_environment,
    public_url_flat,
    run_upload_plan,
    select_lovable_export_files,
    validate_gcs_bucket,
)

# Central country list — add future export countries here. Kept alphabetical.
COUNTRY_PLACEHOLDER = "Select country..."
EXPORT_COUNTRIES = ["Australia", "Brazil", "Italy", "Netherlands", "New Zealand", "Uruguay"]

DEFAULT_LOVABLE_OUTPUT_DIR = "lovable_export"
SOURCE_FOLDER_HELP_TEXT = (
    "Paste the folder where this country workbook lives. JSON export will "
    "default to a subfolder here."
)

# Stable local folder for uploaded workbooks. Deliberately not a
# tempfile.TemporaryDirectory: on Windows, pandas/openpyxl can still hold a
# file handle open on the uploaded .xlsx after export returns, and
# TemporaryDirectory.__exit__ calling shutil.rmtree while that handle is open
# raises PermissionError ([WinError 32]) and crashes the app.
UPLOAD_TEMP_DIR = Path("batch_temp_uploads")


def parse_cold_callers(text: str) -> list[str]:
    """One non-empty caller name per line."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def make_upload_path(original_name: str, upload_dir: Path = UPLOAD_TEMP_DIR) -> Path:
    """Build a unique, safe path for an uploaded workbook in a stable folder."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name or "workbook.xlsx").suffix or ".xlsx"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"uploaded_{timestamp}_{uuid.uuid4().hex}{suffix}"
    return upload_dir / filename


def save_uploaded_workbook(uploaded_file, upload_dir: Path = UPLOAD_TEMP_DIR) -> Path:
    """Write an uploaded workbook's bytes to a stable temp folder.

    The file handle is closed (via the ``with`` block) before this returns, so
    the caller can safely hand the path to pandas/openpyxl right after.
    """
    upload_path = make_upload_path(uploaded_file.name, upload_dir)
    with open(upload_path, "wb") as f:
        f.write(uploaded_file.getvalue())
    return upload_path


WORKBOOK_LOCKED_MESSAGE = (
    "The workbook file is still open or locked. "
    "Please close Excel/preview and try again."
)


class WorkbookSourceLockedError(RuntimeError):
    """Raised when the export source workbook cannot be written because
    another process (Excel, a preview pane, antivirus) holds a lock."""


def _is_lock_error(exc: OSError) -> bool:
    """True for Windows sharing-violation style locks (WinError 32) and
    PermissionError on any platform."""
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 32


def make_export_source_path(
    country: str, timestamp, upload_dir: Path = UPLOAD_TEMP_DIR,
) -> Path:
    """Unique, export-specific source workbook path, e.g.
    ``lovable_json_source_Brazil_20260703_141530_ab12cd34.xlsx``."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    country_part = sanitize_filename_part(country, fallback="Country")
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return upload_dir / (
        f"lovable_json_source_{country_part}_{stamp}_{uuid.uuid4().hex[:8]}.xlsx"
    )


def write_export_source_workbook(
    data: bytes,
    country: str,
    timestamp,
    upload_dir: Path = UPLOAD_TEMP_DIR,
    attempts: int = 3,
    retry_delay_seconds: float = 0.5,
) -> Path:
    """Copy in-memory workbook bytes to a fresh export-specific file.

    The JSON export never reads a possibly-locked upload/download path
    directly: it always gets its own copy, written from bytes with the file
    handle closed before this returns. Retry-safe: each attempt uses a fresh
    unique filename, so a file locked by Excel/preview/antivirus never blocks
    the next attempt. Raises ``WorkbookSourceLockedError`` when every attempt
    hits a lock; other I/O errors propagate unchanged.
    """
    last_exc: OSError | None = None
    for attempt in range(attempts):
        path = make_export_source_path(country, timestamp, upload_dir)
        try:
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError as exc:
            if not _is_lock_error(exc):
                raise
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(retry_delay_seconds)
    raise WorkbookSourceLockedError(str(last_exc))


def cleanup_uploaded_workbook(upload_path: Path) -> str | None:
    """Best-effort delete of an uploaded workbook; never raises.

    Returns a warning message if cleanup failed (e.g. Windows still holding a
    file handle open), or None on success/no-op.
    """
    try:
        Path(upload_path).unlink(missing_ok=True)
        return None
    except PermissionError as exc:
        return (
            f"Could not remove temporary upload file {upload_path}: {exc}. "
            "This is harmless and the file can be deleted manually later."
        )


def sanitize_filename_part(value, fallback: str = "value") -> str:
    """Reduce a string to a Windows-safe filename/folder component.

    Whitespace becomes ``_``; anything outside ``[A-Za-z0-9_-]`` is stripped.
    Returns ``fallback`` when the result would otherwise be empty (blank,
    None, or entirely unsafe characters).
    """
    safe = re.sub(r"\s+", "_", str(value or "").strip())
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", safe).strip("_")
    return safe or fallback


def clean_user_path(value) -> Optional[Path]:
    """Turn user-entered path text into a ``Path``, or ``None`` when blank.

    Strips surrounding whitespace and a single pair of matching quotes (users
    often paste Windows paths wrapped in quotes), then expands ``~``. Never
    raises — an unparseable value is treated as blank.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except Exception:
        return None


def resolve_lovable_output_dir(source_folder) -> Path:
    """Resolve the base output directory for a Lovable JSON export.

    If ``source_folder`` is a usable path, exports default to
    ``<source_folder>/lovable_export`` — organized next to the country
    workbook. Otherwise falls back to the safe relative ``lovable_export``
    folder. Never defaults to Downloads.
    """
    base = clean_user_path(source_folder)
    if base is not None:
        return base / "lovable_export"
    return Path(DEFAULT_LOVABLE_OUTPUT_DIR)


def make_lovable_export_folder_name(country: str, timestamp) -> str:
    """Run-specific export folder name.

    ``<Country>_lovable_json_enriched_<YYYYMMDD_HHMMSS>``, e.g.
    ``Brazil_lovable_json_enriched_20260702_231500``.
    """
    country_part = sanitize_filename_part(country, fallback="Country")
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    return f"{country_part}_lovable_json_enriched_{stamp}"


def make_lovable_zip_filename(country: str, timestamp) -> str:
    """Zip download filename — same pattern as the export folder, ``.zip``."""
    return f"{make_lovable_export_folder_name(country, timestamp)}.zip"


def build_zip_bytes(file_paths: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in file_paths:
            p = Path(path)
            if p.exists():
                zf.write(p, arcname=p.name)
    buf.seek(0)
    return buf.getvalue()


def main() -> None:
    st.set_page_config(page_title="Lovable JSON Export", page_icon="📦",
                       layout="wide")
    st.title("📦 Lead Prioritizer → Lovable JSON export")
    st.caption(
        "Converts a Lead Prioritizer v2 Excel workbook into Lovable Company "
        "Hub JSON (companies.list.json + detail buckets). Export only — no "
        "API calls, no scoring changes."
    )

    uploaded = st.file_uploader(
        "Lead Prioritizer Excel workbook", type=["xlsx"])

    country = st.selectbox(
        "Country for this JSON export",
        [COUNTRY_PLACEHOLDER] + EXPORT_COUNTRIES,
        help="Authoritative country written to every exported record.",
    )

    callers_text = st.text_area(
        "Cold callers",
        help="Enter one cold caller name per line.",
    )

    include_skipped = st.checkbox("Include skipped rows", value=False)
    foreign_hq_only = st.checkbox(
        "Export only companies with detected foreign headquarters", value=True)

    bucket_size = st.number_input(
        "Detail bucket size", min_value=50, max_value=5000, value=500, step=50)

    content_language = st.selectbox(
        "Lovable content language", list(SUPPORTED_CONTENT_LANGUAGES),
        index=list(SUPPORTED_CONTENT_LANGUAGES).index(DEFAULT_CONTENT_LANGUAGE),
        help="Demo option. Only caller-facing JSON text values are "
             "localized. Scores, IDs, URLs and audit fields stay unchanged.")

    # ── Output location ──────────────────────────────────────────────────────
    source_folder_text = st.text_input(
        "Input/source folder for JSON export", value="",
        help=SOURCE_FOLDER_HELP_TEXT)
    base_output_dir = resolve_lovable_output_dir(source_folder_text)
    if not source_folder_text.strip():
        st.caption(
            "Streamlit cannot infer the original upload folder automatically; "
            "paste the source folder if you want the export saved next to "
            "your input workbook."
        )
    _country_for_preview = country if country != COUNTRY_PLACEHOLDER else "<Country>"
    st.caption(
        f"Resolved output directory: `{base_output_dir}` — each export "
        f"creates its own subfolder: "
        f"`{_country_for_preview}_lovable_json_enriched_<timestamp>/`"
    )

    if st.button("Create Lovable JSON export", type="primary"):
        cold_callers = parse_cold_callers(callers_text)

        if uploaded is None:
            st.error("Upload a Lead Prioritizer Excel workbook first.")
            return
        if country == COUNTRY_PLACEHOLDER:
            st.error("Select a country for this JSON export.")
            return
        if not cold_callers:
            st.error("Enter at least one cold caller (one name per line).")
            return

        export_timestamp = datetime.now()
        folder_name = make_lovable_export_folder_name(country, export_timestamp)
        resolved_output_dir = base_output_dir / folder_name

        # The export never reads a possibly-locked upload/download path
        # directly: the in-memory upload bytes are copied to a fresh,
        # export-specific file in a stable folder (not TemporaryDirectory),
        # with the handle closed before pandas/openpyxl sees the path.
        try:
            upload_path = write_export_source_workbook(
                uploaded.getvalue(), country, export_timestamp)
        except WorkbookSourceLockedError:
            st.error(WORKBOOK_LOCKED_MESSAGE)
            return

        try:
            with st.spinner("Exporting workbook to Lovable JSON..."):
                manifest = export_workbook_to_lovable_json(
                    input_xlsx=upload_path,
                    output_dir=resolved_output_dir,
                    export_country=country,
                    cold_callers=cold_callers,
                    include_skipped=include_skipped,
                    foreign_hq_only=foreign_hq_only,
                    bucket_size=int(bucket_size),
                    content_language=content_language,
                )
        except LovableExportError as exc:
            st.error(f"Export failed: {exc}")
            return
        except OSError as exc:
            if _is_lock_error(exc):
                st.error(WORKBOOK_LOCKED_MESSAGE)
            else:
                st.error(f"Export failed: {exc}")
            return
        except Exception as exc:
            st.error(f"Export failed: {exc}")
            return
        finally:
            cleanup_warning = cleanup_uploaded_workbook(upload_path)
            if cleanup_warning:
                st.warning(cleanup_warning)

        # Record where this run actually landed. Adds two keys on top of the
        # core export manifest and rewrites export_manifest.json to match —
        # no change to export_lead_prioritizer_to_lovable_json.py itself.
        manifest["output_folder_name"] = folder_name
        manifest["resolved_output_dir"] = str(resolved_output_dir.resolve())
        try:
            manifest_path = resolved_output_dir / "export_manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except Exception as exc:
            st.warning(
                f"Export succeeded, but could not rewrite export_manifest.json "
                f"with folder metadata: {exc}"
            )

        # Stored so the result view (and the GCS upload button below) survives
        # the rerun triggered by later widget interactions — Streamlit reruns
        # the whole script on every click, so this state would otherwise be
        # lost the moment the user touches the GCS section.
        st.session_state["lovable_export_manifest"] = manifest
        st.session_state["lovable_export_output_dir"] = str(resolved_output_dir)
        st.session_state["lovable_export_country"] = country
        st.session_state["lovable_export_timestamp"] = export_timestamp
        st.session_state["lovable_export_upload_path"] = str(upload_path)

    # ── Export result (persists across reruns via session_state) ──────────────
    manifest = st.session_state.get("lovable_export_manifest")
    manifest_output_dir = st.session_state.get("lovable_export_output_dir")
    if manifest is not None and manifest_output_dir:
        result_country = st.session_state.get("lovable_export_country", "")
        result_timestamp = st.session_state.get("lovable_export_timestamp") or datetime.now()

        st.success(
            f"Export complete: {manifest['rows_exported']} companies written "
            f"to {manifest['bucket_count']} detail bucket(s)."
        )
        st.caption(f"Saved to: {manifest['resolved_output_dir']}")

        col1, col2, col3 = st.columns(3)
        col1.metric("Total rows read", manifest["total_rows_read"])
        col1.metric("Rows exported", manifest["rows_exported"])
        col2.metric("Skipped rows excluded", manifest["skipped_rows_excluded"])
        col2.metric("Foreign-HQ rows exported",
                    manifest["foreign_hq_rows_exported"])
        col3.metric("Non-foreign-HQ excluded",
                    manifest["non_foreign_hq_rows_excluded"])
        col3.metric("Bucket count", manifest["bucket_count"])

        st.subheader("Caller distribution")
        st.table(
            [{"cold_caller": caller, "companies": count}
             for caller, count in manifest["caller_distribution"].items()]
        )

        st.subheader("Output files")
        for path in manifest["output_files"]:
            st.code(path, language=None)

        if manifest["warnings"]:
            st.subheader("Warnings")
            for warning in manifest["warnings"]:
                st.warning(warning)

        with st.expander("Full export manifest"):
            st.json(manifest)

        st.download_button(
            "Download JSON files as zip",
            data=build_zip_bytes(manifest["output_files"]),
            file_name=make_lovable_zip_filename(result_country, result_timestamp),
            mime="application/zip",
            key="lovable_zip_download",
        )

        upload_path = st.session_state.get("lovable_export_upload_path")
        if upload_path:
            with st.expander("Debug: upload temp file"):
                st.code(upload_path, language=None)

        # ── Optional Google Cloud Storage upload ───────────────────────────
        st.markdown("---")
        st.markdown("**Optional Google Cloud Storage upload**")
        st.caption(
            "Experimental / manual only: uses your local gcloud CLI. "
            "Nothing uploads until you check the box and click the button "
            "below. Local download above always works regardless."
        )
        gcs_enabled = st.checkbox(
            "Upload generated Lovable JSON to Google Cloud Storage",
            value=False, key="lovable_gcs_enabled")
        if gcs_enabled:
            gcloud_info = check_gcloud_available()
            if not gcloud_info["available"]:
                st.warning(
                    "Neither gcloud nor gsutil was found on PATH. Install or "
                    "authenticate the Google Cloud SDK before uploading."
                )
            else:
                env_info = describe_gcloud_environment()
                st.caption(
                    f"Detected CLI: `{gcloud_info['tool']}`"
                    + (f" ({gcloud_info['version']})" if gcloud_info["version"] else "")
                    + (f" — account: {env_info['account']}" if env_info["account"] else "")
                    + (f" — project: {env_info['project']}" if env_info["project"] else "")
                )

            gc1, gc2 = st.columns(2)
            gcs_bucket = gc1.text_input(
                "GCS bucket", value=DEFAULT_GCS_BUCKET, key="lovable_gcs_bucket")
            gcs_prefix = gc2.text_input(
                "GCS prefix/path",
                value=f"{country_folder_slug(result_country)}/current",
                key="lovable_gcs_prefix")

            export_files = select_lovable_export_files(manifest_output_dir)
            bucket_error = validate_gcs_bucket(gcs_bucket)
            if bucket_error:
                st.error(bucket_error)
            if not export_files:
                st.error(
                    "No exportable JSON files found in the export folder "
                    f"({manifest_output_dir})."
                )

            # ── Check whether something is already published at this
            # prefix, so a second batch (e.g. the rest of a large list
            # after a 1,000-row test) doesn't silently wipe out the first
            # one. Only checked once bucket/prefix are valid, to avoid a
            # pointless network call on every keystroke.
            existing_check = None
            if not bucket_error and gcloud_info["available"]:
                with st.spinner("Checking for an existing export at this location..."):
                    existing_check = fetch_existing_flat_export(
                        gcs_bucket.strip(), gcs_prefix)
                if existing_check["exists"] is None:
                    st.warning(
                        "Could not check for an existing export at this "
                        f"location: {existing_check['error']}. Proceeding "
                        "will overwrite whatever is there."
                    )
                elif existing_check["exists"]:
                    st.info(
                        f"An export with "
                        f"{len(existing_check['list_items'] or [])} companies "
                        "is already published at this location."
                    )

            existing_found = bool(existing_check and existing_check["exists"])
            upload_mode = "overwrite"
            if existing_found:
                upload_mode = st.radio(
                    "This location already has published data. What do you want to do?",
                    options=["merge", "overwrite"],
                    format_func=lambda v: (
                        "Merge — add/update companies from this batch, keep "
                        "everything already published"
                        if v == "merge" else
                        "Overwrite — replace the published export with this batch only"
                    ),
                    index=0,
                    key="lovable_gcs_upload_mode",
                )

            upload_disabled = bool(bucket_error) or not export_files
            button_label = (
                "Merge into GCS" if upload_mode == "merge" else "Upload JSON to GCS"
            )
            if st.button(button_label, key="lovable_gcs_upload_button",
                        disabled=upload_disabled):
                if upload_mode == "merge":
                    with st.spinner("Merging with the existing export..."):
                        merge_result = prepare_merge(
                            manifest_output_dir, gcs_bucket.strip(), gcs_prefix)
                    if merge_result["fetch_error"]:
                        st.error(
                            "Could not fetch the existing export to merge with: "
                            f"{merge_result['fetch_error']}"
                        )
                        jobs = None
                    else:
                        jobs = merge_result["jobs"]
                        stats = merge_result["stats"]
                        st.caption(
                            f"Merge: {stats['added']} new, {stats['updated']} "
                            f"updated, {stats['kept_from_existing']} kept "
                            "unchanged from the existing export."
                        )
                else:
                    jobs = build_flat_upload_plan(
                        manifest_output_dir, export_files, gcs_bucket.strip(), gcs_prefix)

                if jobs:
                    with st.spinner("Uploading to Google Cloud Storage..."):
                        results = run_upload_plan(jobs)
                    failures = [r for r in results if not r["success"]]
                    if failures:
                        st.error(f"{len(failures)} of {len(results)} uploads failed.")
                        for r in failures:
                            st.code(f"{r['destination']}: "
                                   f"{r.get('error') or r.get('stderr') or ''}")
                    else:
                        st.success(f"Uploaded {len(results)} file(s) to Google Cloud Storage.")
                        st.markdown("**Public URLs**")
                        for filename in export_files:
                            st.write(public_url_flat(gcs_bucket.strip(), gcs_prefix, filename))
        else:
            st.caption(
                "GCS upload is off by default. Check the box above to "
                "upload this export's JSON files."
            )


if __name__ == "__main__":
    main()
