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
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from export_lead_prioritizer_to_lovable_json import (
    LovableExportError,
    export_workbook_to_lovable_json,
)

# Central country list — add future export countries here.
COUNTRY_PLACEHOLDER = "Select country..."
EXPORT_COUNTRIES = ["Italy", "Brazil", "Uruguay"]


def parse_cold_callers(text: str) -> list[str]:
    """One non-empty caller name per line."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


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

    output_dir = st.text_input("Output directory", value="lovable_export")

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
        if not (output_dir or "").strip():
            st.error("Enter an output directory.")
            return

        try:
            # Keep the original workbook name so it shows up in the manifest.
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / (uploaded.name or "workbook.xlsx")
                tmp_path.write_bytes(uploaded.getvalue())
                with st.spinner("Exporting workbook to Lovable JSON..."):
                    manifest = export_workbook_to_lovable_json(
                        input_xlsx=tmp_path,
                        output_dir=output_dir.strip(),
                        export_country=country,
                        cold_callers=cold_callers,
                        include_skipped=include_skipped,
                        foreign_hq_only=foreign_hq_only,
                        bucket_size=int(bucket_size),
                    )
        except LovableExportError as exc:
            st.error(f"Export failed: {exc}")
            return

        st.success(
            f"Export complete: {manifest['rows_exported']} companies written "
            f"to {manifest['bucket_count']} detail bucket(s)."
        )

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
            file_name="lovable_export.zip",
            mime="application/zip",
        )


if __name__ == "__main__":
    main()
