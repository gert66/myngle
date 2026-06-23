"""
abn_xml_to_csv_app.py

Streamlit app to convert Australian ABN Bulk Extract XML files to CSV.

Files are read directly from disk — no browser upload — so there is no
Streamlit 200 MB upload limit.

Run with:
    streamlit run abn_xml_to_csv_app.py
"""

from __future__ import annotations

import csv
import fnmatch
import io
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="ABN XML → CSV",
    page_icon="🇦🇺",
    layout="wide",
)

st.title("🇦🇺 ABN Bulk Extract — XML to CSV")
st.caption(
    "Reads ABN XML files directly from a local folder (no browser upload limit). "
    "Uses an iterative streaming parser — the full file is never loaded into memory."
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum CSV size (bytes) to offer a browser download button
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

# CSV columns we extract from each <ABR> record
_CSV_COLUMNS = [
    "abn",
    "abn_status",
    "abn_status_from_date",
    "record_last_updated_date",
    "entity_type_code",
    "entity_type_description",
    "main_name",
    "trading_name",
    "legal_name_given_name",
    "legal_name_other_given_name",
    "legal_name_family_name",
    "state",
    "postcode",
    "gst_status",
    "gst_status_from_date",
    "dgr_endorsed",
    "acnc_registration",
]

# ---------------------------------------------------------------------------
# Streaming XML parser
# ---------------------------------------------------------------------------

def _iter_abr_records(xml_path: Path) -> Iterator[dict[str, str]]:
    """
    Iterate over <ABR> elements in the XML file one at a time using
    iterparse so the full file is never held in memory.

    Yields one dict per ABR element.
    """
    context = ET.iterparse(str(xml_path), events=("end",))
    for event, elem in context:
        if elem.tag != "ABR":
            continue

        def _t(tag: str, subtag: str = "", default: str = "") -> str:
            """Get text from elem/tag or elem/tag/subtag."""
            node = elem.find(tag)
            if node is None:
                return default
            if subtag:
                sub = node.find(subtag)
                return (sub.text or default) if sub is not None else default
            return node.text or default

        def _attr(tag: str, attribute: str, default: str = "") -> str:
            node = elem.find(tag)
            if node is None:
                return default
            return node.get(attribute, default)

        # ABN
        abn_node = elem.find("ABN")
        abn = (abn_node.text or "").strip() if abn_node is not None else ""
        abn_status = _attr("ABN", "status")
        abn_status_from = _attr("ABN", "ABNStatusFromDate")

        # Record metadata
        record_updated = elem.get("recordLastUpdatedDate", "")

        # Entity type
        et_node = elem.find("EntityType")
        entity_type_code = ""
        entity_type_desc = ""
        if et_node is not None:
            ec = et_node.find("EntityTypeCode")
            ed = et_node.find("EntityTypeIndicator")
            entity_type_code = (ec.text or "").strip() if ec is not None else ""
            entity_type_desc = (ed.text or "").strip() if ed is not None else ""

        # Main name (for non-individual entities)
        main_name_node = elem.find("MainEntity/NonIndividualName/NonIndividualNameText")
        main_name = (main_name_node.text or "").strip() if main_name_node is not None else ""

        # Trading name
        trading_node = elem.find("OtherEntity/OtherEntityName/NonIndividualNameText")
        trading_name = (trading_node.text or "").strip() if trading_node is not None else ""

        # Legal name (individual)
        ln = elem.find("LegalEntity/IndividualName")
        given = family = other_given = ""
        if ln is not None:
            gn = ln.find("GivenName")
            ogn = ln.find("OtherGivenName")
            fn = ln.find("FamilyName")
            given       = (gn.text  or "").strip() if gn  is not None else ""
            other_given = (ogn.text or "").strip() if ogn is not None else ""
            family      = (fn.text  or "").strip() if fn  is not None else ""

        # Address (state + postcode)
        addr = elem.find("MainEntity/BusinessAddress/AddressDetails")
        if addr is None:
            addr = elem.find("LegalEntity/BusinessAddress/AddressDetails")
        state    = ""
        postcode = ""
        if addr is not None:
            s = addr.find("State")
            p = addr.find("Postcode")
            state    = (s.text or "").strip() if s is not None else ""
            postcode = (p.text or "").strip() if p is not None else ""

        # GST
        gst_node = elem.find("GST")
        gst_status = _attr("GST", "status")
        gst_from   = _attr("GST", "GSTStatusFromDate")

        # DGR
        dgr_node = elem.find("DGR")
        dgr_endorsed = "Yes" if dgr_node is not None else "No"

        # ACNC
        acnc_node = elem.find("ACNC")
        acnc_reg = "Yes" if acnc_node is not None else "No"

        yield {
            "abn":                       abn,
            "abn_status":                abn_status,
            "abn_status_from_date":      abn_status_from,
            "record_last_updated_date":  record_updated,
            "entity_type_code":          entity_type_code,
            "entity_type_description":   entity_type_desc,
            "main_name":                 main_name,
            "trading_name":              trading_name,
            "legal_name_given_name":     given,
            "legal_name_other_given_name": other_given,
            "legal_name_family_name":    family,
            "state":                     state,
            "postcode":                  postcode,
            "gst_status":                gst_status,
            "gst_status_from_date":      gst_from,
            "dgr_endorsed":              dgr_endorsed,
            "acnc_registration":         acnc_reg,
        }

        # Free memory for this element before moving to the next
        elem.clear()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _find_xml_files(folder: Path, pattern: str) -> list[Path]:
    """Return XML files in folder that match the glob/filename pattern."""
    pattern = pattern.strip() or "*.xml"
    matches = sorted(
        p for p in folder.iterdir()
        if p.is_file() and fnmatch.fnmatch(p.name, pattern)
    )
    return matches


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def _parse_to_csv(
    xml_files: list[Path],
    output_path: Path,
    test_mode: bool,
    max_records: int,
    status_placeholder,
    progress_placeholder,
) -> tuple[int, str]:
    """
    Stream all xml_files into output_path CSV.

    Returns (total_records_written, error_message_or_empty).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        out_fh = open(output_path, "w", newline="", encoding="utf-8-sig")
    except OSError as exc:
        return 0, f"Cannot create output file: {exc}"

    writer = csv.DictWriter(out_fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()

    total = 0
    limit = max_records if test_mode else None

    try:
        for file_idx, xml_path in enumerate(xml_files, start=1):
            status_placeholder.info(
                f"📄 Processing file {file_idx}/{len(xml_files)}: **{xml_path.name}**"
            )
            file_count = 0
            try:
                for record in _iter_abr_records(xml_path):
                    writer.writerow(record)
                    total += 1
                    file_count += 1
                    if file_count % 5_000 == 0 or file_count == 1:
                        progress_placeholder.markdown(
                            f"  &nbsp;&nbsp;↳ records parsed: **{total:,}**"
                        )
                    if limit is not None and total >= limit:
                        status_placeholder.info(
                            f"🛑 Test-mode limit reached ({limit:,} records)."
                        )
                        return total, ""
            except ET.ParseError as exc:
                out_fh.close()
                return total, f"XML parse error in {xml_path.name}: {exc}"

        progress_placeholder.markdown(f"  &nbsp;&nbsp;↳ records parsed: **{total:,}**")
    finally:
        out_fh.close()

    return total, ""


# ---------------------------------------------------------------------------
# Sidebar — configuration
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ Settings")

    input_folder_str = st.text_input(
        "Input folder",
        value="",
        placeholder=r"C:\Users\gertm\Downloads",
        help="Local folder containing the ABN XML file(s).",
    )

    file_pattern = st.text_input(
        "File pattern",
        value="*.xml",
        placeholder="20260617_Public01.xml  or  *.xml",
        help=(
            "Exact filename or glob pattern (e.g. *.xml). "
            "Only files in the input folder are scanned — no subdirectories."
        ),
    )

    output_folder_str = st.text_input(
        "Output folder",
        value="",
        placeholder=r"C:\Users\gertm\Downloads\abn_output",
        help="Folder where the CSV will be written. Created if it does not exist.",
    )

    output_filename = st.text_input(
        "Output filename",
        value=f"abn_export_{datetime.now().strftime('%Y%m%d')}.csv",
        help="Name of the output CSV file.",
    )

    st.divider()

    test_mode = st.checkbox("🧪 Test mode", value=False)
    max_records = st.number_input(
        "Max records (test mode)",
        min_value=1,
        max_value=10_000_000,
        value=10_000,
        step=1_000,
        disabled=not test_mode,
    )

    run_button = st.button("▶ Parse XML to CSV", type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
# Main area — validation and run
# ---------------------------------------------------------------------------

status_box    = st.empty()
progress_box  = st.empty()
result_area   = st.container()

if run_button:
    # ── Validate input folder ──────────────────────────────────────────────
    if not input_folder_str.strip():
        st.error("Please enter an input folder path.")
        st.stop()

    input_folder = Path(input_folder_str.strip())
    if not input_folder.exists():
        st.error(f"❌ Input folder not found: `{input_folder}`")
        st.stop()
    if not input_folder.is_dir():
        st.error(f"❌ Path is not a folder: `{input_folder}`")
        st.stop()

    # ── Validate output folder ─────────────────────────────────────────────
    if not output_folder_str.strip():
        st.error("Please enter an output folder path.")
        st.stop()

    output_folder = Path(output_folder_str.strip())
    output_path   = output_folder / output_filename.strip()

    # ── Find matching XML files ────────────────────────────────────────────
    pattern = file_pattern.strip() or "*.xml"
    xml_files = _find_xml_files(input_folder, pattern)

    if not xml_files:
        st.error(
            f"❌ No files matching **`{pattern}`** found in `{input_folder}`."
        )
        st.stop()

    with result_area:
        st.markdown("**Files found:**")
        for f in xml_files:
            size_mb = f.stat().st_size / 1_048_576
            st.markdown(f"  - `{f.name}` &nbsp; ({size_mb:,.1f} MB)")

    if test_mode:
        st.info(f"🧪 Test mode — will stop after **{max_records:,}** records.")

    # ── Run ────────────────────────────────────────────────────────────────
    status_box.info(f"⏳ Starting… output → `{output_path}`")

    total_written, error_msg = _parse_to_csv(
        xml_files=xml_files,
        output_path=output_path,
        test_mode=test_mode,
        max_records=int(max_records),
        status_placeholder=status_box,
        progress_placeholder=progress_box,
    )

    if error_msg:
        st.error(f"❌ {error_msg}")
        if total_written:
            st.warning(f"{total_written:,} records were written before the error.")
        st.stop()

    # ── Success ────────────────────────────────────────────────────────────
    status_box.success(
        f"✅ Done — **{total_written:,}** records written to:\n\n`{output_path.resolve()}`"
    )
    progress_box.empty()

    # Download button (only if CSV is small enough)
    try:
        csv_size = output_path.stat().st_size
    except OSError:
        csv_size = 0

    with result_area:
        st.markdown(f"**Output path:** `{output_path.resolve()}`")
        st.markdown(f"**Records written:** {total_written:,}")
        if csv_size:
            st.markdown(f"**CSV size:** {csv_size / 1_048_576:,.2f} MB")

        if 0 < csv_size <= _MAX_DOWNLOAD_BYTES:
            try:
                csv_bytes = output_path.read_bytes()
                st.download_button(
                    label="⬇️ Download CSV",
                    data=csv_bytes,
                    file_name=output_filename.strip(),
                    mime="text/csv",
                )
            except OSError as exc:
                st.warning(f"Could not read CSV for download: {exc}")
        elif csv_size > _MAX_DOWNLOAD_BYTES:
            st.info(
                f"CSV is {csv_size / 1_048_576:,.0f} MB — too large for browser download. "
                "Open it directly from the output folder."
            )
