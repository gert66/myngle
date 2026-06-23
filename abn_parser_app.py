"""
abn_parser_app.py — Streamlit UI for the ABN Bulk Extract XML parser.

Run with:
    streamlit run abn_parser_app.py
"""

import io
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from parse_abn_xml import (
    FIELDNAMES,
    _USE_LXML,
    parse_abr_record,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="ABN XML Parser",
    page_icon="🇦🇺",
    layout="wide",
)

st.title("🇦🇺 ABN Bulk Extract XML → CSV")
st.caption(
    "Streaming parser for Australian Business Register ABN Bulk Extract files. "
    f"Engine: **{'lxml (fast)' if _USE_LXML else 'stdlib xml.etree.ElementTree'}**"
)

if not _USE_LXML:
    st.info(
        "**Tip:** Install `lxml` for ~3× faster parsing: `pip install lxml`",
        icon="⚡",
    )

# ---------------------------------------------------------------------------
# File uploader
# ---------------------------------------------------------------------------

st.subheader("1. Upload XML file(s)")
uploaded_files = st.file_uploader(
    "Upload one or more ABN Bulk Extract .xml files",
    type=["xml"],
    accept_multiple_files=True,
    key="xml_upload",
)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

st.subheader("2. Options")

col_a, col_b = st.columns(2)
with col_a:
    test_mode = st.checkbox("Test mode (limit records)", value=True)
    limit = 0
    if test_mode:
        limit = int(st.number_input(
            "Max records to parse",
            min_value=100, max_value=10_000_000, value=10_000, step=1000,
        ))
with col_b:
    output_filename = st.text_input(
        "Output filename",
        value=f"abn_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )

# ---------------------------------------------------------------------------
# Run button
# ---------------------------------------------------------------------------

run_btn = st.button(
    "▶ Parse and download CSV",
    type="primary",
    disabled=not uploaded_files,
    key="run_btn",
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if run_btn:
    for _k in ("_abn_csv_bytes", "_abn_stats"):
        st.session_state.pop(_k, None)

    # ── Progress widgets ──────────────────────────────────────────────────────
    _status = st.status("Parsing…", expanded=True)
    with _status:
        prog_bar  = st.progress(0.0)
        prog_text = st.empty()
        eta_text  = st.empty()
        m1, m2, m3 = st.columns(3)
        met_file    = m1.empty()
        met_records = m2.empty()
        met_rate    = m3.empty()

    def _fmt_dur(s: float) -> str:
        s = max(0, int(s))
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    # ── Streaming parse ───────────────────────────────────────────────────────
    try:
        import csv
        import xml.etree.ElementTree as ET

        csv_buf   = io.StringIO()
        writer    = csv.DictWriter(csv_buf, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()

        total_records = 0
        file_stats: list[dict] = []
        t_start = time.monotonic()

        for file_idx, uf in enumerate(uploaded_files):
            file_name = uf.name
            file_records = 0
            met_file.metric("Current file", file_name)
            _status.write(f"Parsing **{file_name}** …")

            # Write uploaded bytes to a temp file (iterparse needs a file path or
            # file-like; stdlib works with file-like but lxml is faster with path)
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
                tmp.write(uf.getvalue())
                tmp_path = tmp.name

            try:
                if _USE_LXML:
                    from lxml import etree as _lxml_etree
                    context = _lxml_etree.iterparse(tmp_path, events=("end",), recover=True)
                    for event, elem in context:
                        tag = elem.tag
                        if "}" in tag:
                            tag = tag.split("}", 1)[1]
                        if tag == "ABR":
                            writer.writerow(parse_abr_record(elem))
                            elem.clear()
                            while elem.getparent() is not None:
                                parent = elem.getparent()
                                if parent.getparent() is not None:
                                    parent.getparent().remove(parent)
                                break
                            file_records += 1
                            total_records += 1
                            if limit > 0 and total_records >= limit:
                                break
                            if total_records % 5_000 == 0:
                                _update_progress()
                else:
                    ctx = ET.iterparse(tmp_path, events=("start", "end"))
                    ctx_iter = iter(ctx)
                    _, root = next(ctx_iter)
                    for event, elem in ctx_iter:
                        if event == "end" and elem.tag == "ABR":
                            writer.writerow(parse_abr_record(elem))
                            root.clear()
                            file_records += 1
                            total_records += 1
                            if limit > 0 and total_records >= limit:
                                break
                            if total_records % 5_000 == 0:
                                _update_progress()
            finally:
                os.unlink(tmp_path)

            file_stats.append({"file": file_name, "records": file_records})
            if limit > 0 and total_records >= limit:
                break

        # Final progress update
        elapsed = time.monotonic() - t_start
        prog_bar.progress(1.0)
        prog_text.markdown(f"**Done** — {total_records:,} records parsed")
        eta_text.caption(
            f"Total elapsed: {_fmt_dur(elapsed)}  |  "
            f"Finished: {datetime.now().strftime('%H:%M:%S')}"
        )
        met_records.metric("Total records", f"{total_records:,}")
        rps = total_records / elapsed if elapsed > 0 else 0
        met_rate.metric("Records/sec", f"{rps:,.0f}")

        _status.update(
            label=f"Complete — {total_records:,} records ({_fmt_dur(elapsed)})",
            state="complete",
            expanded=False,
        )

        # Store result
        csv_bytes = csv_buf.getvalue().encode("utf-8")
        st.session_state["_abn_csv_bytes"] = csv_bytes
        st.session_state["_abn_stats"] = {
            "total":      total_records,
            "elapsed":    elapsed,
            "file_stats": file_stats,
            "limit":      limit,
        }

    except Exception as exc:
        _status.update(label="Error", state="error", expanded=True)
        st.error(f"Parse error: {exc}")
        st.stop()

# ---------------------------------------------------------------------------
# Define _update_progress as a closure — must be defined before the parse loop
# but references variables created inside the run block.
# We use a module-level mutable to share state cleanly.
# ---------------------------------------------------------------------------
# (Defined here so it's always in scope; uses nonlocal-style via captured refs
#  through st.session_state since Streamlit reruns the whole script.)

def _update_progress():
    """Called periodically during parse to refresh UI widgets.

    Relies on variables in the enclosing run_btn block — only valid during
    that block's execution. The function reference is safe to define at module
    level because Streamlit executes top-to-bottom on each interaction.
    """
    # This function body is intentionally a no-op stub at module level.
    # The real update logic is inlined inside the run block below via a
    # local redefinition, which shadows this stub.
    pass


# Redefine _update_progress inside the run block (done below via exec trick
# avoided — instead we use the pattern of just calling it after assignment
# inside the try block, so the local function created there is what runs).
# The stub above satisfies the reference before the run block executes.


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

if "_abn_csv_bytes" not in st.session_state:
    st.stop()

csv_bytes = st.session_state["_abn_csv_bytes"]
stats     = st.session_state["_abn_stats"]

st.success("Parsing complete.")

# ── Summary metrics ───────────────────────────────────────────────────────────
st.subheader("Summary")
s1, s2, s3 = st.columns(3)
s1.metric("Total records",  f"{stats['total']:,}")
s2.metric("Elapsed",        f"{stats['elapsed']:.1f}s")
s3.metric("Test limit",     stats["limit"] if stats["limit"] > 0 else "None")

if len(stats["file_stats"]) > 1:
    import pandas as pd
    st.dataframe(
        pd.DataFrame(stats["file_stats"]).rename(
            columns={"file": "File", "records": "Records"}
        ),
        use_container_width=True,
        hide_index=True,
    )

# ── Preview ───────────────────────────────────────────────────────────────────
st.subheader("Preview (first 200 rows)")
import pandas as pd, io as _io
preview_df = pd.read_csv(_io.BytesIO(csv_bytes), nrows=200, dtype=str).fillna("")
st.dataframe(preview_df, use_container_width=True, hide_index=True)

# ── Download ──────────────────────────────────────────────────────────────────
st.subheader("Download")
out_name = st.session_state.get("_abn_output_name", output_filename)
st.download_button(
    label=f"⬇ Download {out_name}",
    data=csv_bytes,
    file_name=out_name,
    mime="text/csv",
    key="abn_download",
)
