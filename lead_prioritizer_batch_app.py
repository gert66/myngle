"""Local Streamlit batch app for Lead Prioritizer v2.

Upload an Excel file, map columns, pick a run mode, run the shared batch core,
and download an enriched workbook.  Intended for synchronous local runs (small /
medium batches, HQ-only checks, full v2 for manageable sizes) — NOT the future
async Anthropic Message Batch workflow, which will be designed separately.

This app adds no enrichment logic and does not duplicate batch logic: it uses
``BatchRunConfig`` / ``run_batch_dataframe`` / ``build_excel_workbook_bytes``
from ``lead_prioritizer_batch_core.py``.  It does not import or modify any legacy
app, ``enrich_clients_claude.py``, or ``commercial_fit_scoring.py``.

The ``import streamlit`` is deliberately lazy (inside ``main``) so the pure
helper functions below can be imported and unit-tested without Streamlit
installed.
"""

from __future__ import annotations

import os
from typing import Optional

from lead_prioritizer_batch_core import (
    BatchRunConfig,
    build_excel_workbook_bytes,
    run_batch_dataframe,
)

CONFIRM_THRESHOLD = 50

# Run-mode radio labels → core modes (order defines UI order; first is default).
MODE_LABELS: list[str] = [
    "Full v2 enrichment",
    "HQ only",
    "Evidence only",
    "Signals, no score",
    "Full, no score",
]
_LABEL_TO_MODE: dict[str, str] = {
    "Full v2 enrichment": "full",
    "HQ only": "hq_only",
    "Evidence only": "evidence_only",
    "Signals, no score": "signals_no_score",
    "Full, no score": "full_no_score",
}

# Likely column names for preselection.
COMPANY_CANDIDATES = ["company_name", "Company Name", "name", "legal_name"]
DOMAIN_CANDIDATES = [
    "domain", "validated_domain", "input_domain", "website_domain",
    "Website", "website",
]
COUNTRY_CANDIDATES = ["input_country", "country", "Country"]

_SERPER_KEY_NAME = "SERPER_API_KEY"
_ANTHROPIC_KEY_NAME = "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Pure helpers (no Streamlit import required)
# ---------------------------------------------------------------------------

def _load_streamlit_secrets():
    """Return ``st.secrets`` if available, else None.  Never raises."""
    try:
        import streamlit as st  # lazy
        return st.secrets
    except Exception:
        return None


def get_secret_or_env(
    key: str,
    secrets=None,
    env: Optional[dict] = None,
) -> str:
    """Resolve a key from Streamlit secrets first, then environment variables.

    Returns "" when absent.  Never raises and never surfaces the value beyond
    returning it to the caller.
    """
    env = os.environ if env is None else env
    if secrets is None:
        secrets = _load_streamlit_secrets()
    try:
        if secrets is not None and key in secrets:
            val = secrets[key]
            if val:
                return str(val).strip()
    except Exception:
        pass
    return (env.get(key) or "").strip()


def resolve_default_column(columns, candidates) -> Optional[str]:
    """Pick the first candidate present in ``columns`` (exact, then case-insensitive)."""
    cols = list(columns)
    for cand in candidates:
        if cand in cols:
            return cand
    lower_map = {str(c).lower(): c for c in cols}
    for cand in candidates:
        hit = lower_map.get(str(cand).lower())
        if hit is not None:
            return hit
    return None


def count_selected_rows(total_rows: int, start_row: int, row_limit: int) -> int:
    """Mirror ``select_batch_rows``: start offset + row_limit (0 = all remaining)."""
    remaining = max(0, int(total_rows) - max(0, int(start_row)))
    if row_limit and int(row_limit) > 0:
        return min(remaining, int(row_limit))
    return remaining


def mode_label_to_core_mode(label: str) -> str:
    """Map a UI radio label to a core run mode."""
    try:
        return _LABEL_TO_MODE[label]
    except KeyError:
        raise ValueError(f"Unknown run-mode label: {label!r}")


def build_download_filename(mode: str) -> str:
    return f"lead_prioritizer_v2_{mode}_enriched.xlsx"


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import io

    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Lead Prioritizer v2 Batch", layout="wide")
    st.title("Lead Prioritizer v2 Batch Excel App")
    st.markdown(
        "- Upload an Excel file and choose **HQ only** or **full** enrichment.\n"
        "- Download an enriched workbook (Enriched Leads, Evidence, Signals, "
        "Run Summary).\n"
        "- Intended for **synchronous local runs**.\n"
        "- Large async Anthropic Message Batch processing will be handled "
        "separately later."
    )

    # ── API keys ──────────────────────────────────────────────────────────────
    serper = get_secret_or_env(_SERPER_KEY_NAME)
    anthropic = get_secret_or_env(_ANTHROPIC_KEY_NAME)
    with st.sidebar:
        st.header("API keys (secrets or environment)")
        st.write(f"{_SERPER_KEY_NAME}:", "✅ set" if serper else "❌ missing")
        st.write(f"{_ANTHROPIC_KEY_NAME}:", "✅ set" if anthropic else "❌ missing")
        st.caption(
            "Local secrets in `.streamlit/secrets.toml`, or environment "
            "variables. Key values are never shown or written to output."
        )
    keys_ok = bool(serper and anthropic)
    if not keys_ok:
        st.error(
            "Missing API key(s). Set SERPER_API_KEY and ANTHROPIC_API_KEY in "
            ".streamlit/secrets.toml or the environment. The run button is "
            "disabled until both are present."
        )

    # ── Upload ────────────────────────────────────────────────────────────────
    uploaded = st.file_uploader("Upload an .xlsx file", type=["xlsx"])
    if not uploaded:
        st.info("Upload an Excel workbook to begin.")
        return

    try:
        xls = pd.ExcelFile(uploaded)
    except Exception as exc:
        st.error(f"Could not read workbook: {exc}")
        return

    sheet_names = list(xls.sheet_names)
    sheet = sheet_names[0] if len(sheet_names) == 1 else st.selectbox(
        "Sheet", sheet_names)

    try:
        df = xls.parse(sheet)
    except Exception as exc:
        st.error(f"Could not read sheet {sheet!r}: {exc}")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Sheet", sheet)
    c2.metric("Rows", len(df))
    c3.metric("Columns", len(df.columns))
    st.dataframe(df.head(5), use_container_width=True)

    # ── Column mapping ────────────────────────────────────────────────────────
    st.subheader("Column mapping")
    cols = list(df.columns)

    def _index_of(default):
        return cols.index(default) if default in cols else 0

    company_col = st.selectbox(
        "Company name column", cols,
        index=_index_of(resolve_default_column(cols, COMPANY_CANDIDATES)))
    domain_col = st.selectbox(
        "Domain column", cols,
        index=_index_of(resolve_default_column(cols, DOMAIN_CANDIDATES)))

    country_default = resolve_default_column(cols, COUNTRY_CANDIDATES)
    country_options = ["(None)"] + cols
    country_choice = st.selectbox(
        "Input country column (optional)", country_options,
        index=(country_options.index(country_default) if country_default in country_options else 0))
    input_country_column = None if country_choice == "(None)" else country_choice

    default_country = st.text_input("Default input country", "Italy")

    # ── Run mode ──────────────────────────────────────────────────────────────
    st.subheader("Run mode")
    mode_label = st.radio("Mode", MODE_LABELS, index=0)
    run_mode = mode_label_to_core_mode(mode_label)

    # ── Row controls ──────────────────────────────────────────────────────────
    st.subheader("Rows")
    rc1, rc2 = st.columns(2)
    start_row = rc1.number_input("Start row", min_value=0, value=0, step=1)
    row_limit = rc2.number_input("Row limit (0 = all remaining)", min_value=0, value=10, step=1)
    stop_on_error = st.checkbox("Stop on first row error", value=False)
    include_raw_ai_json = st.checkbox("Include raw AI JSON", value=False)

    selected_count = count_selected_rows(len(df), int(start_row), int(row_limit))
    st.caption(f"Selected rows: **{selected_count}**")

    big_run_ok = True
    if selected_count > CONFIRM_THRESHOLD:
        st.warning(
            f"{selected_count} selected rows exceeds {CONFIRM_THRESHOLD}. Full "
            "mode makes multiple Serper + Anthropic calls per row (cost and time)."
        )
        big_run_ok = st.checkbox(
            "I understand this may use many API calls and want to run this batch",
            value=False)

    run_disabled = (not keys_ok) or (not big_run_ok) or selected_count == 0

    # ── Run ─────────────────────────────────────────────────────────────────
    if st.button("Run batch enrichment", type="primary", disabled=run_disabled):
        config = BatchRunConfig(
            company_name_column=company_col,
            domain_column=domain_col,
            input_country_column=input_country_column,
            default_input_country=default_country.strip() or "Italy",
            run_mode=run_mode,
            start_row=int(start_row),
            row_limit=int(row_limit),
            continue_on_error=not stop_on_error,
            include_raw_ai_json=include_raw_ai_json,
        )
        with st.spinner("Running batch enrichment..."):
            tables = run_batch_dataframe(df, config, serper, anthropic)
            data = build_excel_workbook_bytes(tables)
        st.session_state["v2_batch_output_bytes"] = data
        st.session_state["v2_batch_tables"] = tables
        st.session_state["v2_batch_mode"] = run_mode

    # ── Output ────────────────────────────────────────────────────────────────
    tables = st.session_state.get("v2_batch_tables")
    data = st.session_state.get("v2_batch_output_bytes")
    if tables is not None and data is not None:
        st.subheader("Results")
        summary = tables["run_summary"].iloc[0].to_dict() if len(tables["run_summary"]) else {}
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Processed rows", summary.get("processed_rows", 0))
        m2.metric("Success", summary.get("success_count", 0))
        m3.metric("Errors", summary.get("error_count", 0))
        m4.metric("Run mode", summary.get("run_mode", st.session_state.get("v2_batch_mode", "")))

        enriched = tables["enriched_leads"]
        st.markdown("**Enriched Leads (preview)**")
        st.dataframe(enriched.head(20), use_container_width=True)

        if "run_success" in enriched.columns:
            errors = enriched[enriched["run_success"] == False]  # noqa: E712
            if len(errors):
                st.markdown("**Rows with errors**")
                _wanted = ["source_index", "company_name", "domain", "run_error"]
                _err_cols = [c for c in _wanted if c in errors.columns]
                st.dataframe(errors[_err_cols] if _err_cols else errors,
                             use_container_width=True)

        st.download_button(
            "⬇️ Download enriched workbook",
            data=data,
            file_name=build_download_filename(st.session_state.get("v2_batch_mode", run_mode)),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()
