"""
Streamlit UI for HQ Score Recalculation.

Recalculates commercial fit scores only for rows where HQ Recovery
changed sig_foreign_hq_score.

Run with:
    streamlit run hq_score_recalc_app.py
"""

import io
import time
from datetime import datetime

import streamlit as st

from recalculate_hq_changed_scores import (
    DEFAULT_SHEET,
    SCORING_PROFILE,
    recalculate_hq_changed_scores_workbook,
)

st.set_page_config(
    page_title="HQ Score Recalculation",
    page_icon="🔁",
    layout="wide",
)

st.title("🔁 HQ Score Recalculation")
st.caption(
    "Recalculates commercial fit scores only for rows where HQ Recovery "
    "changed `sig_foreign_hq_score`. Uses scoring profile: "
    f"`{SCORING_PROFILE}`."
)
st.info(
    "**Large workbooks can take several minutes.** "
    "The app reads both files, recalculates changed HQ rows, and writes a new "
    "Excel workbook before the download appears.",
    icon="⏱",
)

# ── Inputs ────────────────────────────────────────────────────────────────────

col_a, col_b = st.columns(2)
with col_a:
    f_enriched = st.file_uploader(
        "1. Original enriched workbook (.xlsx)",
        type=["xlsx"],
        key="enr_upload",
    )
with col_b:
    f_hqr = st.file_uploader(
        "2. HQ Recovery output workbook (.xlsx)",
        type=["xlsx"],
        key="hqr_upload",
    )

sheet_name = st.text_input(
    "Sheet name",
    value=DEFAULT_SHEET,
    help="Sheet to read from both workbooks. Falls back to first sheet if not found.",
)

fast_output = st.checkbox(
    "Fast output mode (skip column-width formatting)",
    value=True,
    help="Keeps freeze panes and autofilter but skips column-width calculation. "
         "Uncheck only if you want auto-fitted column widths in the downloaded file.",
)

run_btn = st.button(
    "Recalculate HQ-changed scores",
    type="primary",
    disabled=(f_enriched is None or f_hqr is None),
    key="recalc_run_button",
)

# ── Run ───────────────────────────────────────────────────────────────────────

if run_btn:
    # Clear any previous result so the UI is fresh
    for _k in ("_recalc_excel_bytes", "_recalc_summary", "_recalc_filename"):
        st.session_state.pop(_k, None)

    _status = st.status("Running recalculation…", expanded=True)
    try:
        _t0 = time.monotonic()
        _status.write(f"Files loaded ✓  ({datetime.now().strftime('%H:%M:%S')})")
        _status.write("Running recalculation…")
        excel_bytes, summary = recalculate_hq_changed_scores_workbook(
            io.BytesIO(f_enriched.getvalue()),
            io.BytesIO(f_hqr.getvalue()),
            sheet_name=sheet_name,
            fast_output=fast_output,
        )
        _t1 = time.monotonic()
        _status.write(f"Recalculation done — {_t1 - _t0:.1f}s elapsed")
        _status.write(f"Output workbook ready — {_t1 - _t0:.1f}s total")
        _status.write("Done ✓")
        _status.update(label=f"Complete ({_t1 - _t0:.1f}s)", state="complete", expanded=False)
    except Exception as exc:
        _status.update(label="Error", state="error", expanded=True)
        st.error(f"Unexpected error: {exc}")
        st.stop()

    if summary.get("error"):
        st.error(summary["error"])
        st.stop()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    enr_stem = f_enriched.name.replace(".xlsx", "")
    st.session_state["_recalc_excel_bytes"] = excel_bytes
    st.session_state["_recalc_summary"]     = summary
    st.session_state["_recalc_filename"]    = f"{enr_stem}_hq_recalculated_{ts}.xlsx"

# ── Results (persisted in session_state across reruns) ───────────────────────

if "_recalc_excel_bytes" in st.session_state:
    excel_bytes = st.session_state["_recalc_excel_bytes"]
    summary     = st.session_state["_recalc_summary"]
    out_name    = st.session_state["_recalc_filename"]

    # ── Metrics ───────────────────────────────────────────────────────────────
    st.subheader("Summary")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Enriched rows",         summary["n_enr"])
    m2.metric("Matched rows",          summary["n_matched"])
    m3.metric("Eligible (HQ changed)", summary["n_eligible"])
    m4.metric("Recalculated",          summary["n_recalculated"])

    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Upgrades 0→3",       summary["n_upgrades"])
    m6.metric("Downgrades 3→0",     summary["n_downgrades"])
    m7.metric("Other changes",      summary["n_other"])
    m8.metric("Unchanged rows",     summary["n_enr"] - summary["n_recalculated"])

    st.caption(f"Matching strategy: **{summary['strategy']}**")

    # ── Delta tables ──────────────────────────────────────────────────────────
    deltas = summary["deltas"]
    if deltas:
        all_d = [x[4] for x in deltas]
        st.subheader("Score delta statistics")
        d1, d2, d3 = st.columns(3)
        d1.metric("Max increase", f"+{max(all_d):.4f}")
        d2.metric("Max decrease", f"{min(all_d):.4f}")
        d3.metric("Mean delta",   f"{sum(all_d)/len(all_d):.4f}")

        import pandas as pd

        _cols = ["company", "domain", "cfs_before", "cfs_after", "delta"]

        top_pos = sorted(deltas, key=lambda x: -x[4])[:20]
        top_neg = sorted(deltas, key=lambda x:  x[4])[:20]

        ta, tb = st.columns(2)
        with ta:
            st.markdown("**Top 20 biggest increases**")
            st.dataframe(
                pd.DataFrame(top_pos, columns=_cols).style.format(
                    {"cfs_before": "{:.4f}", "cfs_after": "{:.4f}", "delta": "{:+.4f}"}
                ),
                use_container_width=True,
                hide_index=True,
            )
        with tb:
            st.markdown("**Top 20 biggest decreases**")
            st.dataframe(
                pd.DataFrame(top_neg, columns=_cols).style.format(
                    {"cfs_before": "{:.4f}", "cfs_after": "{:.4f}", "delta": "{:+.4f}"}
                ),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("No rows with HQ score changes found — nothing was recalculated.")

    # ── Download ──────────────────────────────────────────────────────────────
    st.subheader("Download")
    st.download_button(
        label=f"⬇ Download {out_name}",
        data=excel_bytes,
        file_name=out_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="recalc_download_button",
    )
