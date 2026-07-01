"""Standalone Streamlit app for the C5 HQ Sonnet adjudication probe.

Upload an HQ-only output workbook, run the C5 Sonnet second-opinion adjudication
over selected rows, and download an adjudicated workbook. This app is a thin UI
over the existing C5 logic — it reuses:

  - ``lead_hq_sonnet_adjudicator``: adjudication, recommendation, model config
  - ``run_hq_sonnet_adjudication_probe``: row filtering, model resolution, the
    Opus cost guardrail, and the per-row / summary builders

It adds no enrichment, no Serper/Lusha/LinkedIn calls, and does not touch the HQ
flow, C4, scoring, taxonomy, or the other apps. Standalone and removable:
deleting this file removes the app with no effect on C5 CLI/tests.

Run:
    streamlit run lead_hq_sonnet_adjudication_app.py
"""

from __future__ import annotations

import io
import os

import pandas as pd
import streamlit as st

from lead_hq_sonnet_adjudicator import (
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    C5_MODEL_TIER_CHOICES,
)
from run_hq_sonnet_adjudication_probe import (
    adjudicate_row,
    build_c5_summary_dict,
    filter_probe_rows,
    resolve_c5_model,
    check_opus_guardrail,
    _OPUS_WARNING,
    _OPUS_ROW_LIMIT_SOFT_CAP,
    _DEFAULT_SHEET,
)

_ANTHROPIC_KEY_NAME = "ANTHROPIC_API_KEY"

_PREVIEW_COLUMNS = [
    "company_name", "domain", "ai_hq_classification", "ai_parent_hq_country",
    "sig_foreign_hq_score_for_next_scoring", "needs_manual_review",
    "hq_positive_score_suppressed_for_review",
    "c5_adjudication", "c5_confidence", "c5_target_company_match",
    "c5_parent_company", "c5_parent_hq_country",
    "c5_recommended_hq_score", "c5_recommended_manual_review",
    "c5_recommendation_reason", "c5_model_used",
]


def _get_anthropic_key(pasted: str = "") -> str:
    """Resolve the Anthropic key: pasted (session-only) → st.secrets → env.

    Never displayed or written to disk.
    """
    if pasted and pasted.strip():
        return pasted.strip()
    try:
        if _ANTHROPIC_KEY_NAME in st.secrets:
            v = st.secrets[_ANTHROPIC_KEY_NAME]
            if v:
                return str(v).strip()
    except Exception:
        pass
    return (os.environ.get(_ANTHROPIC_KEY_NAME) or "").strip()


def _build_workbook_bytes(out_df: pd.DataFrame, summary_df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        out_df.to_excel(writer, sheet_name="C5 Adjudication", index=False)
        summary_df.to_excel(writer, sheet_name="C5 Summary", index=False)
    buf.seek(0)
    return buf.getvalue()


st.set_page_config(page_title="C5 HQ Sonnet Adjudication Probe", layout="wide")
st.title("C5 HQ Sonnet Adjudication Probe")
st.markdown(
    "Second-opinion HQ adjudication for risky / manual-review rows from an "
    "HQ-only output workbook. Reuses the existing C5 logic; no Serper/Lusha "
    "calls, no changes to production scoring."
)

# ── API key ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Anthropic API key")
    pasted_key = st.text_input(
        "Paste key (session only, optional)", type="password",
        help="Used only for this session; never written to disk. Leave blank to "
             "use st.secrets or the ANTHROPIC_API_KEY environment variable.")
    anthropic_key = _get_anthropic_key(pasted_key)
    st.write(f"{_ANTHROPIC_KEY_NAME}:", "✅ set" if anthropic_key else "❌ missing")
    if not anthropic_key:
        st.error(
            "No Anthropic API key found. Paste one above, or set "
            "`ANTHROPIC_API_KEY` in `.streamlit/secrets.toml` or the environment."
        )

# ── Upload ──────────────────────────────────────────────────────────────────
uploaded = st.file_uploader("Upload an HQ-only .xlsx workbook", type=["xlsx"])
if not uploaded:
    st.info("Upload a workbook (default sheet: 'Enriched Leads') to begin.")
    st.stop()

try:
    xls = pd.ExcelFile(uploaded)
except Exception as exc:
    st.error(f"Could not read workbook: {exc}")
    st.stop()

sheet_names = list(xls.sheet_names)
_default_sheet_idx = sheet_names.index(_DEFAULT_SHEET) if _DEFAULT_SHEET in sheet_names else 0
sheet = st.selectbox("Sheet", sheet_names, index=_default_sheet_idx)

try:
    df = xls.parse(sheet)
except Exception as exc:
    st.error(f"Could not read sheet {sheet!r}: {exc}")
    st.stop()

st.metric("Detected rows", len(df))

# ── Row controls ──────────────────────────────────────────────────────────
st.subheader("Rows")
rc1, rc2 = st.columns(2)
start_row = rc1.number_input("Start row", min_value=0, value=0, step=1)
row_limit = rc2.number_input("Row limit (0 = all remaining)", min_value=0, value=30, step=1)
only_manual_review = st.checkbox("Only manual-review rows", value=False)
only_suppressed = st.checkbox("Only C4-suppressed rows", value=False)
include_raw = st.checkbox("Include raw JSON / raw model response", value=False)

# ── Model controls ────────────────────────────────────────────────────────
st.subheader("Model")
model_tier = st.selectbox("Model tier", list(C5_MODEL_TIER_CHOICES), index=0)
if model_tier == "sonnet":
    st.caption(f"Sonnet default model: **{DEFAULT_SONNET_ADJUDICATION_MODEL}**")
model_override = st.text_input(
    "Explicit model override (optional)", value="",
    help="Overrides the tier. Required for the opus tier (no default ID is baked in).")

model_used, model_error = resolve_c5_model(model_tier, model_override)

opus_confirm = False
if model_tier == "opus":
    st.warning(_OPUS_WARNING)
    rl = int(row_limit)
    if rl == 0 or rl > _OPUS_ROW_LIMIT_SOFT_CAP:
        opus_confirm = st.checkbox(
            "I understand Opus is expensive and want to continue", value=False)

# Preview the selection count with the current filters.
selected_preview = filter_probe_rows(
    df, only_manual_review, only_suppressed, int(start_row), int(row_limit))
st.caption(f"Selected rows with current filters: **{len(selected_preview)}**")

# ── Run gating ────────────────────────────────────────────────────────────
run_blocked_reason = ""
if not anthropic_key:
    run_blocked_reason = "Anthropic API key is missing."
elif model_error:
    run_blocked_reason = model_error
elif model_tier == "opus":
    guard = check_opus_guardrail(model_tier, int(row_limit), opus_confirm)
    if guard:
        run_blocked_reason = guard
elif len(selected_preview) == 0:
    run_blocked_reason = "No rows selected with the current filters."

if run_blocked_reason:
    st.error(run_blocked_reason)

run = st.button("Run C5 adjudication", type="primary", disabled=bool(run_blocked_reason))

# ── Processing ──────────────────────────────────────────────────────────────
if run and not run_blocked_reason:
    selected = filter_probe_rows(
        df, only_manual_review, only_suppressed, int(start_row), int(row_limit))
    total = len(selected)
    progress = st.progress(0.0)
    status = st.empty()

    out_rows = []
    n_foreign = n_domestic = n_unclear = n_review = 0
    n_success = n_failed = 0
    n_score3 = 0

    for i, (idx, row) in enumerate(selected.iterrows(), start=1):
        original = row.to_dict()
        company = str(original.get("company_name") or "").strip() or "(unknown)"
        status.info(f"Adjudicating {i}/{total}: {company}")
        enriched, result, rec = adjudicate_row(
            original, anthropic_key, model_used, model_tier,
            source_index=idx, include_raw=include_raw,
        )
        out_rows.append(enriched)

        if result.call_success:
            n_success += 1
        else:
            n_failed += 1
        if result.adjudication == "foreign_parent_confirmed":
            n_foreign += 1
        elif result.adjudication == "domestic_confirmed":
            n_domestic += 1
        else:
            n_unclear += 1
        if rec["c5_recommended_manual_review"]:
            n_review += 1
        if rec["c5_recommended_hq_score"] == 3.0:
            n_score3 += 1

        progress.progress(min(1.0, i / total) if total else 1.0)

    progress.progress(1.0)
    status.success(f"Completed {len(out_rows)} rows with model {model_used}.")

    out_df = pd.DataFrame(out_rows)
    summary_df = pd.DataFrame([build_c5_summary_dict(
        input_label=getattr(uploaded, "name", "uploaded.xlsx"),
        sheet=sheet,
        model_used=model_used,
        model_tier=model_tier,
        confirm_expensive_opus=opus_confirm,
        total_rows=len(df),
        selected_rows=total,
        adjudicated_rows=len(out_rows),
        n_foreign=n_foreign,
        n_domestic=n_domestic,
        n_unclear=n_unclear,
        n_review=n_review,
        only_manual_review=only_manual_review,
        only_suppressed=only_suppressed,
    )])

    # ── Summary metrics ──────────────────────────────────────────────────────
    st.subheader("Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Selected rows", total)
    m2.metric("Successful calls", n_success)
    m3.metric("Failed calls", n_failed)
    m4.metric("Recommend score 3", n_score3)
    m5, m6, m7, m8 = st.columns(4)
    m5.metric("Foreign parent", n_foreign)
    m6.metric("Domestic", n_domestic)
    m7.metric("Unclear", n_unclear)
    m8.metric("Recommend manual review", n_review)
    st.caption(f"Model used: **{model_used}**  ·  Model tier: **{model_tier}**")

    # ── Preview ──────────────────────────────────────────────────────────────
    st.subheader("Adjudication preview")
    _cols = [c for c in _PREVIEW_COLUMNS if c in out_df.columns]
    st.dataframe(out_df[_cols] if _cols else out_df, use_container_width=True)

    # ── Download ─────────────────────────────────────────────────────────────
    st.download_button(
        "Download C5 adjudicated workbook",
        data=_build_workbook_bytes(out_df, summary_df),
        file_name="lead_prioritizer_v2_c5_adjudicated.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
