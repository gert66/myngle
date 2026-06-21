"""
hq_lookup_probe_app.py

Streamlit UI for hq_lookup_probe.py.

Run with:
    streamlit run hq_lookup_probe_app.py

This app is experimental and does not modify any production enrichment output.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

# Import the probe library (same directory)
try:
    from hq_lookup_probe import (
        DEFAULT_COMPANY_COL,
        DEFAULT_DOMAIN_COL,
        DEFAULT_COUNTRY_COL,
        DEFAULT_INPUT_COUNTRY,
        OLD_ENRICHMENT_COLS,
        PROBE_COLS,
        _COUNTRY_ALIASES,
        build_excel_bytes,
        get_excel_sheet_names,
        read_input_from_fileobj,
        run_probe_on_rows,
    )
except ImportError as _e:
    st.error(f"Could not import hq_lookup_probe: {_e}")
    st.stop()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="mYngle HQ Lookup Probe",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 mYngle HQ Lookup Probe")
st.caption(
    "This tool tests a simple headquarters lookup approach. "
    "It is **experimental** and does not change production enrichment output."
)

# ---------------------------------------------------------------------------
# Helper: column guesser
# ---------------------------------------------------------------------------

_COMPANY_GUESSES = ["company_name", "company", "naam", "azienda", "name"]
_DOMAIN_GUESSES  = ["domain", "website", "url", "website_url", "domein"]
_COUNTRY_GUESSES = ["inferred_input_country", "input_country", "country", "paese", "land"]


def _guess_col(columns: list[str], guesses: list[str]) -> str:
    cols_lower = {c.lower(): c for c in columns}
    for g in guesses:
        if g.lower() in cols_lower:
            return cols_lower[g.lower()]
    return columns[0] if columns else ""


# ---------------------------------------------------------------------------
# Session-state keys
# ---------------------------------------------------------------------------

_KEY_RESULTS    = "hq_probe_results"
_KEY_INPUT_ROWS = "hq_probe_input_rows"
_KEY_OLD_COLS   = "hq_probe_old_cols"
_KEY_META       = "hq_probe_meta"
_KEY_COLS_CFG   = "hq_probe_cols_cfg"

# ---------------------------------------------------------------------------
# Sidebar: file input
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Input")

    upload_tab, path_tab = st.tabs(["Upload file", "Local path"])

    uploaded_file = None
    local_path_str = ""

    with upload_tab:
        uploaded_file = st.file_uploader(
            "Excel (.xlsx) or CSV",
            type=["xlsx", "csv"],
            help="Upload a file with company names and domains.",
        )

    with path_tab:
        local_path_str = st.text_input(
            "Path on disk",
            placeholder=r"C:\Users\...\input.xlsx",
            help="Use this for large files already on your machine.",
        )

    # Resolve file source
    file_source = None
    file_suffix = ""
    file_label  = ""
    sheet_names: list[str] = []
    selected_sheet: str | None = None

    if uploaded_file is not None:
        file_suffix = Path(uploaded_file.name).suffix
        file_label  = uploaded_file.name
        if file_suffix.lower() in (".xlsx", ".xls"):
            try:
                sheet_names = get_excel_sheet_names(uploaded_file)
                uploaded_file.seek(0)
            except Exception:
                sheet_names = []
        file_source = "upload"

    elif local_path_str.strip():
        p = Path(local_path_str.strip())
        if p.exists():
            file_suffix = p.suffix
            file_label  = p.name
            if file_suffix.lower() in (".xlsx", ".xls"):
                try:
                    sheet_names = get_excel_sheet_names(p)
                except Exception:
                    sheet_names = []
            file_source = "path"
        else:
            st.warning(f"Path not found: {p}")

    if sheet_names and len(sheet_names) > 1:
        selected_sheet = st.selectbox("Sheet", sheet_names)
    elif sheet_names:
        selected_sheet = sheet_names[0]

    st.header("Columns")

    # We need to peek at headers to populate column dropdowns.
    # Cache the peeked headers so we don't re-read on every widget interaction.
    peeked_headers: list[str] = []
    peek_error = ""

    if file_source == "upload" and uploaded_file is not None:
        try:
            uploaded_file.seek(0)
            preview_rows = read_input_from_fileobj(
                uploaded_file, file_suffix, limit=1, sheet_name=selected_sheet
            )
            peeked_headers = list(preview_rows[0].keys()) if preview_rows else []
            uploaded_file.seek(0)
        except Exception as exc:
            peek_error = str(exc)

    elif file_source == "path":
        try:
            with open(local_path_str.strip(), "rb") as f:
                preview_rows = read_input_from_fileobj(
                    f, file_suffix, limit=1, sheet_name=selected_sheet
                )
            peeked_headers = list(preview_rows[0].keys()) if preview_rows else []
        except Exception as exc:
            peek_error = str(exc)

    if peek_error:
        st.warning(f"Could not read headers: {peek_error}")

    if peeked_headers:
        company_col = st.selectbox(
            "Company column",
            peeked_headers,
            index=peeked_headers.index(_guess_col(peeked_headers, _COMPANY_GUESSES)),
        )
        domain_col = st.selectbox(
            "Domain column",
            peeked_headers,
            index=peeked_headers.index(_guess_col(peeked_headers, _DOMAIN_GUESSES)),
        )
        # Country column: add "(not in file)" option
        country_opts = ["(use default)"] + peeked_headers
        country_guess = _guess_col(peeked_headers, _COUNTRY_GUESSES)
        country_guess_idx = (
            country_opts.index(country_guess) if country_guess in country_opts else 0
        )
        country_col_sel = st.selectbox(
            "Input country column",
            country_opts,
            index=country_guess_idx,
        )
        country_col = country_col_sel if country_col_sel != "(use default)" else DEFAULT_COUNTRY_COL
    else:
        company_col = st.text_input("Company column", DEFAULT_COMPANY_COL)
        domain_col  = st.text_input("Domain column",  DEFAULT_DOMAIN_COL)
        country_col = st.text_input("Input country column", DEFAULT_COUNTRY_COL)

    default_country = st.text_input(
        "Default country (fallback)",
        DEFAULT_INPUT_COUNTRY,
        help="Used when the country column is absent or blank.",
    )

    st.header("Options")

    limit = st.number_input("Row limit", min_value=1, max_value=200, value=50, step=10)

    only_fhq_signal = st.checkbox(
        "Only rows with old foreign HQ signal",
        value=False,
        help="Filter input to rows where sig_foreign_hq_score > 0 or foreign_hq_sanitized = True/Yes (if those columns exist).",
    )

    st.header("API keys")

    serper_key = st.text_input(
        "Serper API key",
        value=os.environ.get("SERPER_API_KEY", "") or os.environ.get("SERPER_KEY", ""),
        type="password",
        help="Required for live searches. Get one at serper.dev.",
    )

    use_model = st.checkbox(
        "Use model fallback for unresolved cases",
        value=False,
        help="Calls Claude Haiku for companies where pattern matching finds nothing. Uses Anthropic API credits.",
    )

    anthropic_key = ""
    if use_model:
        anthropic_key = st.text_input(
            "Anthropic API key",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            type="password",
        )

# ---------------------------------------------------------------------------
# Main area: run button + results
# ---------------------------------------------------------------------------

if not file_source:
    st.info("Upload a file or enter a local path in the sidebar to get started.")
    st.stop()

st.markdown(
    "⚠️ **Each row may use up to 8 Serper search calls.** "
    f"With limit={int(limit)}, that is up to **{int(limit) * 8:,} calls**."
)

run_btn = st.button("▶ Run HQ Probe", type="primary", disabled=(not serper_key))
if not serper_key:
    st.warning("Enter a Serper API key in the sidebar to enable the run button.")

if run_btn:
    # ── Load input rows ──────────────────────────────────────────────────────
    with st.spinner("Reading input file…"):
        try:
            if file_source == "upload":
                uploaded_file.seek(0)
                input_rows = read_input_from_fileobj(
                    uploaded_file, file_suffix,
                    limit=int(limit), sheet_name=selected_sheet,
                )
            else:
                with open(local_path_str.strip(), "rb") as f:
                    input_rows = read_input_from_fileobj(
                        f, file_suffix,
                        limit=int(limit), sheet_name=selected_sheet,
                    )
        except Exception as exc:
            st.error(f"Failed to read input: {exc}")
            st.stop()

    if not input_rows:
        st.warning("No rows found in the input file.")
        st.stop()

    # ── Apply old-FHQ-signal filter ──────────────────────────────────────────
    if only_fhq_signal:
        def _has_fhq_signal(row: dict) -> bool:
            score = row.get("sig_foreign_hq_score")
            sanitized = str(row.get("foreign_hq_sanitized") or "").strip().lower()
            try:
                score_val = float(score)
            except (TypeError, ValueError):
                score_val = 0.0
            return score_val > 0 or sanitized in {"true", "yes", "1"}

        filtered = [r for r in input_rows if _has_fhq_signal(r)]
        if filtered:
            st.info(f"Old FHQ filter: {len(filtered)} / {len(input_rows)} rows have a signal.")
            input_rows = filtered
        else:
            st.warning("No rows matched the old FHQ signal filter. Running on all rows.")

    # ── Detect old enrichment cols ───────────────────────────────────────────
    sample_keys = set(input_rows[0].keys()) if input_rows else set()
    present_old_cols = [c for c in OLD_ENRICHMENT_COLS if c in sample_keys]

    # ── Run probe ────────────────────────────────────────────────────────────
    progress_bar = st.progress(0.0, text="Starting…")
    status_area  = st.empty()
    probe_results: list[dict] = []
    total = len(input_rows)

    def _progress(current: int, total: int) -> None:
        pct = current / total
        row = input_rows[current - 1]
        company = str(row.get(company_col) or "").strip()[:45]
        probe   = probe_results[-1] if probe_results else {}
        country_hit = probe.get("hq_detected_country") or "…"
        progress_bar.progress(pct, text=f"[{current}/{total}] {company} → {country_hit}")

    cache: dict = {}
    error_rows: list[str] = []

    for i, row in enumerate(input_rows):
        from hq_lookup_probe import probe_company
        company = str(row.get(company_col) or "").strip()
        domain  = str(row.get(domain_col)  or "").strip()
        country = str(row.get(country_col) or default_country).strip() or default_country

        probe = probe_company(
            company_name=company,
            domain=domain,
            input_country=country,
            serper_key=serper_key,
            use_model=use_model,
            anthropic_key=anthropic_key,
            cache=cache,
        )
        probe_results.append(probe)
        if probe.get("probe_error"):
            error_rows.append(f"Row {i+1} ({company}): {probe['probe_error']}")
        _progress(i + 1, total)

    progress_bar.empty()
    status_area.empty()

    # ── Store in session state ───────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.session_state[_KEY_RESULTS]    = probe_results
    st.session_state[_KEY_INPUT_ROWS] = input_rows
    st.session_state[_KEY_OLD_COLS]   = present_old_cols
    st.session_state[_KEY_COLS_CFG]   = (company_col, domain_col, country_col)
    st.session_state[_KEY_META] = {
        "timestamp":        ts,
        "input_file":       file_label,
        "use_model":        use_model,
        "model":            "claude-haiku-4-5-20251001" if use_model else "",
        "serper_available": bool(serper_key),
        "limit":            int(limit),
    }

    if error_rows:
        with st.expander(f"⚠️ {len(error_rows)} row error(s)", expanded=False):
            for e in error_rows:
                st.text(e)

# ---------------------------------------------------------------------------
# Show results (persists across reruns via session state)
# ---------------------------------------------------------------------------

if _KEY_RESULTS not in st.session_state:
    st.stop()

probe_results: list[dict]  = st.session_state[_KEY_RESULTS]
input_rows: list[dict]     = st.session_state[_KEY_INPUT_ROWS]
present_old_cols: list[str] = st.session_state[_KEY_OLD_COLS]
company_col_r, domain_col_r, country_col_r = st.session_state[_KEY_COLS_CFG]
qa_meta: dict              = st.session_state[_KEY_META]

# ── Summary metrics ──────────────────────────────────────────────────────────
def _is_italy(country: str) -> bool:
    return _COUNTRY_ALIASES.get(country.lower(), country) == "Italy"

detected_italy   = sum(1 for p in probe_results if _is_italy(p.get("hq_detected_country") or ""))
detected_foreign = sum(1 for p in probe_results if p.get("foreign_hq_simple") == "True")
detected_unknown = sum(1 for p in probe_results if not p.get("hq_detected_country"))
needs_review_cnt = sum(1 for p in probe_results if p.get("needs_manual_review") == "Yes")

st.markdown("---")
st.subheader("Results")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Rows processed",    len(probe_results))
c2.metric("Italy HQ detected", detected_italy)
c3.metric("Foreign HQ",        detected_foreign)
c4.metric("Unknown",           detected_unknown)
c5.metric("Needs review",      needs_review_cnt)

# ── Build display DataFrame ───────────────────────────────────────────────────
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# Key columns for the results table
_KEY_VIEW_COLS = [
    company_col_r, domain_col_r, country_col_r,
    "sig_foreign_hq_score", "sig_foreign_hq_evidence",
    "foreign_hq_sanitized", "foreign_hq_sanitizer_reason",
    "hq_detected_city", "hq_detected_region", "hq_detected_country",
    "hq_confidence", "foreign_hq_simple", "needs_manual_review",
    "hq_reason", "hq_evidence_url", "hq_evidence_quote", "hq_query_used",
]

def _build_display_rows(
    input_rows: list[dict],
    probe_results: list[dict],
    cols: list[str],
) -> list[dict]:
    rows = []
    for i, (in_row, probe) in enumerate(zip(input_rows, probe_results), start=1):
        r: dict[str, Any] = {"#": i}
        for c in cols:
            if c in probe:
                r[c] = probe[c]
            else:
                r[c] = in_row.get(c, "")
        rows.append(r)
    return rows


# ── Filters ──────────────────────────────────────────────────────────────────
st.markdown("**Filters**")
f1, f2, f3, f4 = st.columns(4)
f_review  = f1.checkbox("Needs manual review only")
f_foreign = f2.checkbox("Foreign HQ only (new)")
f_unknown = f3.checkbox("Unknown only")
f_disagree = f4.checkbox("Old score > 0 but new = not foreign")

all_display = _build_display_rows(input_rows, probe_results, _KEY_VIEW_COLS)

def _apply_filters(rows: list[dict]) -> list[dict]:
    out = rows
    if f_review:
        out = [r for r in out if r.get("needs_manual_review") == "Yes"]
    if f_foreign:
        out = [r for r in out if r.get("foreign_hq_simple") == "True"]
    if f_unknown:
        out = [r for r in out if not r.get("hq_detected_country")]
    if f_disagree:
        def _disagree(r: dict) -> bool:
            try:
                old_score = float(r.get("sig_foreign_hq_score") or 0)
            except (ValueError, TypeError):
                old_score = 0.0
            return old_score > 0 and r.get("foreign_hq_simple") == "False"
        out = [r for r in out if _disagree(r)]
    return out

filtered_display = _apply_filters(all_display)
st.caption(f"Showing {len(filtered_display)} of {len(all_display)} rows")

# Only include columns that actually have data
present_view_cols = ["#"] + [
    c for c in _KEY_VIEW_COLS
    if any(str(r.get(c, "")).strip() for r in filtered_display)
]

if HAS_PANDAS:
    import pandas as pd
    df = pd.DataFrame(filtered_display)[present_view_cols]
    st.dataframe(df, use_container_width=True, height=420)
else:
    # Fallback: render as a plain table
    st.table(filtered_display)

# ── Downloads ─────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Download results")

dl1, dl2 = st.columns(2)

with dl1:
    @st.cache_data(show_spinner=False)
    def _make_excel(
        _input_rows_key: int,
        _probe_key: int,
    ) -> bytes:
        return build_excel_bytes(
            input_rows=input_rows,
            probe_results=probe_results,
            present_old_cols=present_old_cols,
            company_col=company_col_r,
            domain_col=domain_col_r,
            country_col=country_col_r,
            qa_meta=qa_meta,
        )

    excel_bytes = _make_excel(id(input_rows), id(probe_results))
    ts = qa_meta.get("timestamp", "")
    dl1.download_button(
        label="⬇️ Download Excel",
        data=excel_bytes,
        file_name=f"hq_probe_results_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with dl2:
    def _make_csv() -> bytes:
        buf = io.StringIO()
        if not all_display:
            return b""
        fieldnames = list(all_display[0].keys())
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_display)
        return buf.getvalue().encode("utf-8-sig")

    dl2.download_button(
        label="⬇️ Download CSV",
        data=_make_csv(),
        file_name=f"hq_probe_results_{ts}.csv",
        mime="text/csv",
    )
