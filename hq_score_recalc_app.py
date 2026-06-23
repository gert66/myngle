"""
Streamlit UI for Score Recalculation.

Supports three modes:
- HQ changes only
- Competitor signal removal only
- Both HQ changes and competitor signal removal

Scoring note: competitor_signal_strength_score and language_competitor_strength_score
are NOT in LEAN_COEFFICIENTS, so competitor removal does not change
final_commercial_fit_score directly.

Run with:
    streamlit run hq_score_recalc_app.py
"""

import io
import time
from datetime import datetime

import streamlit as st

from recalculate_hq_changed_scores import (
    DEFAULT_SHEET,
    SCOPE_BOTH,
    SCOPE_COMPETITOR,
    SCOPE_HQ,
    SCORING_PROFILE,
    recalculate_hq_changed_scores_workbook,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Score Recalculation",
    page_icon="🔁",
    layout="wide",
)

st.title("🔁 Score Recalculation")
st.caption(
    f"Recalculates commercial fit scores. Scoring profile: `{SCORING_PROFILE}`."
)
st.info(
    "**Large workbooks can take several minutes.** "
    "The app reads both files, recalculates eligible rows, and writes a new "
    "Excel workbook before the download appears.",
    icon="⏱",
)

# ---------------------------------------------------------------------------
# File uploaders
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

sheet_name = st.text_input(
    "Sheet name",
    value=DEFAULT_SHEET,
    help="Sheet to read from both workbooks. Falls back to first sheet if not found.",
)

_SCOPE_LABELS = {
    "HQ changes only":                              SCOPE_HQ,
    "Competitor signal removal only":               SCOPE_COMPETITOR,
    "Both HQ changes and competitor signal removal": SCOPE_BOTH,
}
_SCOPE_HELP = {
    "HQ changes only": (
        "Recalculates rows where HQ Recovery changed `sig_foreign_hq_score`."
    ),
    "Competitor signal removal only": (
        "Recalculates rows with competitor signal, setting "
        "`competitor_signal_strength_score` and `language_competitor_strength_score` "
        "to 0 in the scoring copy.\n\n"
        "⚠️ Note: these fields are NOT in LEAN_COEFFICIENTS, so "
        "`final_commercial_fit_score` will not change unless the model is updated."
    ),
    "Both HQ changes and competitor signal removal": (
        "Applies reviewed HQ score where changed AND neutralizes competitor signal. "
        "`score_company` is called only once per row."
    ),
}

scope_label = st.radio(
    "Recalculation scope",
    options=list(_SCOPE_LABELS.keys()),
    index=0,
    help="Select which rows and signals to recalculate.",
)
scope = _SCOPE_LABELS[scope_label]
st.caption(_SCOPE_HELP[scope_label])

fast_output = st.checkbox(
    "Fast output mode (skip column-width formatting)",
    value=True,
    help="Keeps freeze panes and autofilter but skips column-width calculation. "
         "Uncheck only if you want auto-fitted column widths.",
)

refresh_app_text = st.checkbox(
    "Refresh Lovable app text fields",
    value=True,
    key="refresh_app_text_cb",
    help=(
        "Regenerates what_is_hot_app, what_is_not_app, evidence_summary_app, "
        "key_source_links_app, caution_app, advanced_notes_app and related fields "
        "for recalculated rows."
    ),
)

_test_mode = st.checkbox(
    "Test mode (limit recalculated rows)",
    value=False,
    help="Process only the first N eligible rows to verify output on a small subset.",
)
max_recalculated_rows = 0
if _test_mode:
    max_recalculated_rows = int(st.number_input(
        "Max recalculated rows",
        min_value=1, max_value=9999, value=10, step=1,
    ))

# ---------------------------------------------------------------------------
# Run button
# ---------------------------------------------------------------------------

run_btn = st.button(
    "▶ Recalculate scores",
    type="primary",
    disabled=(f_enriched is None or f_hqr is None),
    key="recalc_run_button",
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if run_btn:
    for _k in ("_recalc_excel_bytes", "_recalc_summary", "_recalc_filename"):
        st.session_state.pop(_k, None)

    _status = st.status("Running recalculation…", expanded=True)

    # ── Live progress widgets (inside the status container) ───────────────────
    with _status:
        _prog_bar   = st.progress(0.0)
        _prog_text  = st.empty()   # "Processing row X / Y"
        _eta_text   = st.empty()   # "Elapsed | Remaining | Finish"
        _met_cols   = st.columns(4)
        _met_matched    = _met_cols[0].empty()
        _met_eligible   = _met_cols[1].empty()
        _met_recalc     = _met_cols[2].empty()
        _met_skipped    = _met_cols[3].empty()

    _ui_start = time.monotonic()

    def _fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, r = divmod(seconds, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _progress_cb(payload: dict) -> None:
        total   = payload["total_rows"]
        current = payload["row_index"]
        frac    = current / total if total > 0 else 0.0
        elapsed = payload["elapsed_seconds"]
        recalc  = payload["recalculated_rows"]
        limit   = payload["limit"]

        _prog_bar.progress(min(frac, 1.0))

        if payload["done"]:
            _prog_text.markdown(
                f"**Completed** — {total:,} rows scanned, "
                f"{recalc:,} recalculated  ✓"
            )
        else:
            limit_note = f"  *(test limit: {limit})*" if limit > 0 else ""
            _prog_text.markdown(
                f"Processing row **{current:,}** / {total:,}{limit_note}"
            )

        # ETA (only meaningful after a few rows)
        if elapsed > 0 and current > 0 and not payload["done"]:
            rps       = current / elapsed
            remaining = (total - current) / rps if rps > 0 else 0
            finish_dt = datetime.now() + __import__("datetime").timedelta(seconds=remaining)
            _eta_text.caption(
                f"Elapsed: {_fmt_duration(elapsed)}  |  "
                f"Est. remaining: {_fmt_duration(remaining)}  |  "
                f"Est. finish: {finish_dt.strftime('%H:%M')}"
            )
        elif payload["done"]:
            _eta_text.caption(
                f"Total elapsed: {_fmt_duration(elapsed)}  |  "
                f"Finished: {datetime.now().strftime('%H:%M:%S')}"
            )

        _met_matched.metric("Matched",     payload["matched_rows"])
        _met_eligible.metric("Eligible",   payload["eligible_rows_seen"])
        _met_recalc.metric("Recalculated", recalc)
        _met_skipped.metric("Skipped",     payload["skipped_by_limit"])

    try:
        _t0 = time.monotonic()
        _status.write(f"Files loaded ✓  ({datetime.now().strftime('%H:%M:%S')})")
        _status.write(f"Scope: **{scope_label}**  |  Profile: `{SCORING_PROFILE}`")

        excel_bytes, summary = recalculate_hq_changed_scores_workbook(
            io.BytesIO(f_enriched.getvalue()),
            io.BytesIO(f_hqr.getvalue()),
            sheet_name=sheet_name,
            fast_output=fast_output,
            max_recalculated_rows=max_recalculated_rows,
            scope=scope,
            refresh_app_text=refresh_app_text,
            progress_callback=_progress_cb,
        )
        _t1 = time.monotonic()
        _status.write(f"Output workbook ready — {_t1 - _t0:.1f}s total")
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
    st.session_state["_recalc_filename"]    = f"{enr_stem}_recalculated_{ts}.xlsx"

# ---------------------------------------------------------------------------
# Results (persisted in session_state across reruns)
# ---------------------------------------------------------------------------

if "_recalc_excel_bytes" not in st.session_state:
    st.stop()

excel_bytes = st.session_state["_recalc_excel_bytes"]
summary     = st.session_state["_recalc_summary"]
out_name    = st.session_state["_recalc_filename"]
_scope      = summary.get("scope", SCOPE_HQ)

st.success("Recalculation complete.")

# ── General metrics ───────────────────────────────────────────────────────────
st.subheader("Summary")
g1, g2, g3, g4 = st.columns(4)
g1.metric("Enriched rows",     summary["n_enr"])
g2.metric("Matched rows",      summary["n_matched"])
g3.metric("Recalculated",      summary["n_recalculated"])
g4.metric("Skipped (limit)",   summary["skipped_by_recalc_limit"])
st.caption(
    f"Scope: **{scope_label}**  |  "
    f"Strategy: **{summary['strategy']}**  |  "
    f"Test mode: {'Yes — limit ' + str(summary['max_recalculated_rows']) if summary['test_mode_active'] else 'No'}"
)

# ── HQ metrics ────────────────────────────────────────────────────────────────
if _scope in (SCOPE_HQ, SCOPE_BOTH):
    st.subheader("HQ changes")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("HQ eligible rows",       summary["n_hq_eligible"])
    h2.metric("HQ rows recalculated",   summary.get("n_hq_recalculated", 0))
    h3.metric("HQ skipped (limit)",     summary.get("n_hq_skipped_limit", 0))
    h4.metric("Upgrades 0→3",           summary["n_upgrades"])
    hh1, hh2 = st.columns(2)
    hh1.metric("Downgrades 3→0",        summary["n_downgrades"])
    hh2.metric("Other HQ changes",      summary["n_other"])

# ── Competitor metrics ────────────────────────────────────────────────────────
if _scope in (SCOPE_COMPETITOR, SCOPE_BOTH):
    st.subheader("Competitor signal removal")
    c1, c2, c3 = st.columns(3)
    c1.metric("Competitor rows detected",    summary["n_competitor_detected"])
    c2.metric("Competitor rows recalculated", summary["n_competitor_recalculated"])
    c3.metric("Avg signal before (non-zero)", f"{summary['avg_competitor_before']:.4f}")
    st.caption(
        "⚠️ `competitor_signal_strength_score` and `language_competitor_strength_score` "
        "are **not** in LEAN_COEFFICIENTS — setting them to 0 does not change "
        "`final_commercial_fit_score` unless the model is updated."
    )

# ── App text metrics ─────────────────────────────────────────────────────────
if summary.get("n_app_text_refreshed", 0) > 0:
    st.subheader("Lovable app text refresh")
    at1, at2, at3, at4 = st.columns(4)
    at1.metric("App text refreshed",     summary.get("n_app_text_refreshed", 0))
    at2.metric("HQ notes added",         summary.get("n_hq_notes", 0))
    at3.metric("Competitor notes added", summary.get("n_comp_notes", 0))
    at4.metric("Conflicting text rmvd",  summary.get("n_conflict_removed", 0))

# ── Lovable App Export sheet metrics ─────────────────────────────────────────
_lov = summary.get("lovable_export", {})
if _lov:
    st.subheader("Lovable App Export sheet")
    lv1, lv2, lv3 = st.columns(3)
    lv1.metric("Columns",              _lov.get("n_cols", 0))
    lv2.metric("Rows",                 _lov.get("n_rows", 0))
    lv3.metric("HQ-colored rows",      _lov.get("n_hq", 0))
    lv4, lv5, _ = st.columns(3)
    lv4.metric("Competitor-colored",   _lov.get("n_comp", 0))
    lv5.metric("Both-colored",         _lov.get("n_both", 0))

# ── Score delta tables ────────────────────────────────────────────────────────
deltas = summary["deltas"]
if deltas:
    all_d = [x[4] for x in deltas]
    pos_d = [d for d in all_d if d > 0]
    neg_d = [d for d in all_d if d < 0]

    st.subheader("Score delta statistics")
    sd1, sd2, sd3 = st.columns(3)
    sd1.metric("Score increases", len(pos_d))
    sd2.metric("Score decreases", len(neg_d))
    sd3.metric("Mean delta",      f"{sum(all_d)/len(all_d):+.4f}")

    import pandas as pd

    _cols = ["company", "domain", "score_before", "score_after", "score_delta"]
    _fmt  = {"score_before": "{:.4f}", "score_after": "{:.4f}", "score_delta": "{:+.4f}"}

    top_pos = sorted([d for d in deltas if d[4] > 0], key=lambda x: -x[4])[:20]
    top_neg = sorted([d for d in deltas if d[4] < 0], key=lambda x:  x[4])[:20]

    ta, tb = st.columns(2)
    with ta:
        st.markdown(f"**Top 20 biggest increases** ({len(top_pos)} rows)")
        if top_pos:
            st.dataframe(
                pd.DataFrame(top_pos, columns=_cols).style.format(_fmt),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No score increases found.")
    with tb:
        st.markdown(f"**Top 20 biggest decreases** ({len(top_neg)} rows)")
        if top_neg:
            st.dataframe(
                pd.DataFrame(top_neg, columns=_cols).style.format(_fmt),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No score decreases found.")

    st.caption(
        "Score = Commercial Fit Score. "
        f"Recalculated with scoring profile `{SCORING_PROFILE}`."
    )
else:
    st.info("No rows were recalculated — no score deltas to show.")

# ── Download ──────────────────────────────────────────────────────────────────
st.subheader("Download")
st.download_button(
    label=f"⬇ Download {out_name}",
    data=excel_bytes,
    file_name=out_name,
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    key="recalc_download_button",
)
