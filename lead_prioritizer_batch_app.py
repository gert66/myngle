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
import re
from pathlib import Path
from typing import Optional

from lead_prioritizer_batch_core import (
    BatchRunConfig,
    build_excel_workbook_bytes,
    run_batch_dataframe,
    run_batch_foreign_hq_only,
    FOREIGN_HQ_ONLY_MODE,
    apply_c5_adjudication,
    add_c5_summary_fields,
    row_selected_for_c5,
    C5_SCORING_BEHAVIORS,
    C5_SCOPES,
)

from lead_hq_sonnet_adjudicator import (
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    C5_MODEL_TIER_CHOICES,
)
from run_hq_sonnet_adjudication_probe import (
    resolve_c5_model,
    check_opus_guardrail,
    _OPUS_WARNING,
)

CONFIRM_THRESHOLD = 50
_C5_OPUS_ROW_CAP = 10

# Run-mode radio labels → core modes (order defines UI order; first is default).
MODE_LABELS: list[str] = [
    "Full v2 enrichment",
    "HQ only",
    "Evidence only",
    "Signals, no score",
    "Full, no score",
    "Full enrichment, confirmed foreign-HQ only",
]
_LABEL_TO_MODE: dict[str, str] = {
    "Full v2 enrichment": "full",
    "HQ only": "hq_only",
    "Evidence only": "evidence_only",
    "Signals, no score": "signals_no_score",
    "Full, no score": "full_no_score",
    "Full enrichment, confirmed foreign-HQ only": FOREIGN_HQ_ONLY_MODE,
}

FOREIGN_HQ_ONLY_HELP_TEXT = (
    "This first runs HQ detection and optional C5 adjudication, then performs "
    "full enrichment only for leads with confirmed foreign-HQ score 3. "
    "Non-confirmed rows are kept in the output and marked as skipped."
)

# Likely column names for preselection.
COMPANY_CANDIDATES = ["company_name", "Company Name", "name", "legal_name"]
DOMAIN_CANDIDATES = [
    "domain", "validated_domain", "input_domain", "website_domain",
    "Website", "website",
]
COUNTRY_CANDIDATES = ["input_country", "country", "Country"]

# Central list of default-input-country choices. Add new countries here only —
# no other code path hardcodes a country name.
SUPPORTED_DEFAULT_INPUT_COUNTRIES = ["Italy", "Brazil", "Uruguay"]
DEFAULT_COUNTRY_PLACEHOLDER = "Select country..."
DEFAULT_COUNTRY_REQUIRED_MESSAGE = "Please select a default input country before running."

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


def resolve_default_input_country(selected: str) -> tuple[Optional[str], Optional[str]]:
    """Validate the selected default input country.

    Returns ``(country, error)`` — exactly one of the two is not ``None``.
    ``country`` is the selected value verbatim (e.g. "Italy", "Brazil",
    "Uruguay"); ``error`` is the user-facing message when the placeholder is
    still selected (or an unknown value is passed in).
    """
    if selected in SUPPORTED_DEFAULT_INPUT_COUNTRIES:
        return selected, None
    return None, DEFAULT_COUNTRY_REQUIRED_MESSAGE


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


DEFAULT_AUTOSAVE_DIR = "batch_outputs"
AUTOSAVE_HELP_TEXT = (
    "When enabled, the completed Excel output will be saved automatically on "
    "this machine in the selected folder. The download button will still be shown."
)


def sanitize_run_mode_for_filename(run_mode: str) -> str:
    """Reduce a run mode to a filesystem-safe token (alnum, ``_``, ``-``)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(run_mode or "").strip()).strip("_")
    return safe or "run"


def autosave_output_workbook(output_bytes: bytes, output_dir: str, run_mode: str,
                             now=None) -> Path:
    """Write the completed workbook bytes to ``output_dir`` and return the path.

    - Creates the directory (and parents) when missing.
    - Relative paths resolve against the current working directory; ``~`` is
      expanded. Windows-safe via ``pathlib``.
    - Filename: ``lead_prioritizer_v2_{run_mode}_{YYYYMMDD_HHMMSS}.xlsx`` with
      the run mode sanitized. Never overwrites: an existing name gets a ``_2``,
      ``_3``, … suffix.
    - Writes only the already-built workbook bytes — no keys, no secrets.
    - Raises on failure (unwritable path etc.); the caller decides how to
      surface the error.
    """
    from datetime import datetime as _dt

    stamp = (now or _dt.now()).strftime("%Y%m%d_%H%M%S")
    base = f"lead_prioritizer_v2_{sanitize_run_mode_for_filename(run_mode)}_{stamp}"

    directory = Path(output_dir or DEFAULT_AUTOSAVE_DIR).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{base}.xlsx"
    counter = 2
    while target.exists():
        target = directory / f"{base}_{counter}.xlsx"
        counter += 1

    target.write_bytes(output_bytes)
    return target.resolve()


def format_duration(seconds) -> str:
    """Format a duration in seconds as HH:MM:SS.  Negative/None → 00:00:00."""
    try:
        total = int(max(0, round(float(seconds))))
    except (TypeError, ValueError):
        return "00:00:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress_status_text(payload: dict, started_at: float, now: Optional[float] = None) -> str:
    """Build a calm one-line status string with elapsed time and ETA.

    ``started_at`` / ``now`` are wall-clock epoch seconds (``time.time()``).
    ETA is based on average time per processed row, so it is unknown until at
    least one row has been processed and grows more reliable with more rows.
    Contains no secrets.
    """
    import time as _time
    from datetime import datetime as _dt

    if now is None:
        now = _time.time()

    processed = int(payload.get("processed_rows", 0) or 0)
    selected = int(payload.get("selected_rows", 0) or 0)
    success = int(payload.get("success_count", 0) or 0)
    errors = int(payload.get("error_count", 0) or 0)
    current = str(payload.get("current_company_name") or "?")

    elapsed = max(0.0, now - started_at)
    parts = [
        f"Processed {processed}/{selected}",
        f"Success {success}",
        f"Errors {errors}",
        f"Current: {current}",
        f"Elapsed {format_duration(elapsed)}",
    ]

    if processed > 0 and selected > 0:
        avg = elapsed / processed
        remaining = avg * max(0, selected - processed)
        finish = _dt.fromtimestamp(now + remaining)
        parts.append(f"ETA {format_duration(remaining)}")
        parts.append(f"Finish around {finish.strftime('%H:%M')}")
    else:
        parts.append("ETA unknown")

    return " | ".join(parts)


def build_phase_progress_status_text(
    payload: dict, started_at: float, now: Optional[float] = None,
) -> str:
    """Status line for phased runs (the foreign-HQ-only mode).

    Renders the ``phase`` / ``phase_label`` / ``phase_processed`` /
    ``phase_total`` keys emitted by ``run_batch_foreign_hq_only``, with
    success/error counts when the phase provides them.  Payloads without phase
    info fall back to ``build_progress_status_text``, so this is safe as a
    single renderer for any progress payload.  Contains no secrets.
    """
    if "phase" not in payload:
        return build_progress_status_text(payload, started_at, now)

    import time as _time

    if now is None:
        now = _time.time()

    phase = int(payload.get("phase", 0) or 0)
    phase_count = int(payload.get("phase_count", 3) or 3)
    label = str(payload.get("phase_label") or "")
    done = int(payload.get("phase_processed", 0) or 0)
    total = int(payload.get("phase_total", 0) or 0)
    current = str(payload.get("current_company_name") or "?")
    elapsed = max(0.0, now - started_at)

    parts = [
        f"Phase {phase}/{phase_count}: {label}",
        f"Processed {done}/{total}",
    ]
    if "success_count" in payload or "error_count" in payload:
        parts.append(f"Success {int(payload.get('success_count', 0) or 0)}")
        parts.append(f"Errors {int(payload.get('error_count', 0) or 0)}")
    parts.append(f"Current: {current}")
    parts.append(f"Elapsed {format_duration(elapsed)}")
    return " | ".join(parts)


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

    default_country_choice = st.selectbox(
        "Default input country",
        [DEFAULT_COUNTRY_PLACEHOLDER] + SUPPORTED_DEFAULT_INPUT_COUNTRIES,
        index=0,
        help="Used as the fallback input_country when the row's own "
             "input_country column is blank. Must be chosen explicitly.")
    default_country, default_country_error = resolve_default_input_country(default_country_choice)
    if default_country_error:
        st.error(default_country_error)

    # ── Run mode ──────────────────────────────────────────────────────────────
    st.subheader("Run mode")
    mode_label = st.radio("Mode", MODE_LABELS, index=0)
    run_mode = mode_label_to_core_mode(mode_label)
    if run_mode == FOREIGN_HQ_ONLY_MODE:
        st.caption(FOREIGN_HQ_ONLY_HELP_TEXT)

    # ── Row controls ──────────────────────────────────────────────────────────
    st.subheader("Rows")
    rc1, rc2 = st.columns(2)
    start_row = rc1.number_input("Start row", min_value=0, value=0, step=1)
    row_limit = rc2.number_input("Row limit (0 = all remaining)", min_value=0, value=10, step=1)
    stop_on_error = st.checkbox("Stop on first row error", value=False)
    include_raw_ai_json = st.checkbox("Include raw AI JSON", value=False)

    # ── Autosave (output workbook to disk when the run completes) ─────────────
    autosave_enabled = st.checkbox(
        "Autosave output workbook when run completes", value=False)
    autosave_dir = DEFAULT_AUTOSAVE_DIR
    if autosave_enabled:
        autosave_dir = st.text_input("Autosave directory", value=DEFAULT_AUTOSAVE_DIR)
        st.caption(AUTOSAVE_HELP_TEXT)

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

    # ── C5 Sonnet HQ adjudication (optional, country-agnostic) ────────────────
    st.subheader("C5 Sonnet HQ adjudication")
    c5_enabled = st.checkbox("Use C5 Sonnet adjudication", value=False)
    c5_scoring_behavior = "append_only"
    c5_scope = "score_3_or_manual_review"
    c5_model_tier = "sonnet"
    c5_model_override = ""
    c5_model_used = ""
    c5_model_error = None
    c5_opus_confirm = False
    c5_block_reason = ""
    if c5_enabled:
        c5_scoring_behavior = st.selectbox(
            "C5 scoring behavior", list(C5_SCORING_BEHAVIORS), index=0,
            help="append_only: add C5 fields only. conservative_adjustment: may "
                 "confirm/downgrade existing score-3 positives; never auto-upgrades "
                 "score-0 rows.")
        c5_scope = st.selectbox(
            "Rows to send to C5", list(C5_SCOPES),
            index=list(C5_SCOPES).index("score_3_or_manual_review"))
        c5_model_tier = st.selectbox("C5 model tier", list(C5_MODEL_TIER_CHOICES), index=0)
        if c5_model_tier == "sonnet":
            st.caption(f"Sonnet default model: **{DEFAULT_SONNET_ADJUDICATION_MODEL}**")
        c5_model_override = st.text_input(
            "C5 explicit model override (optional)", value="",
            help="Overrides the tier. Required for the opus tier.")
        c5_model_used, c5_model_error = resolve_c5_model(c5_model_tier, c5_model_override)
        if c5_model_tier == "opus":
            st.warning(_OPUS_WARNING)
            rl = int(row_limit)
            if rl == 0 or rl > _C5_OPUS_ROW_CAP:
                c5_opus_confirm = st.checkbox(
                    "I understand Opus is expensive and want to continue", value=False)
        if c5_model_error:
            c5_block_reason = c5_model_error
        elif c5_model_tier == "opus":
            _guard = check_opus_guardrail(c5_model_tier, int(row_limit), c5_opus_confirm)
            if _guard:
                c5_block_reason = _guard
        if c5_block_reason:
            st.error(c5_block_reason)

    run_disabled = (not keys_ok) or (not big_run_ok) or selected_count == 0 \
        or bool(c5_block_reason) or bool(default_country_error)

    # ── Run ─────────────────────────────────────────────────────────────────
    if st.button("Run batch enrichment", type="primary", disabled=run_disabled):
        if default_country_error:
            st.error(default_country_error)
            return
        config = BatchRunConfig(
            company_name_column=company_col,
            domain_column=domain_col,
            input_country_column=input_country_column,
            default_input_country=default_country,
            run_mode=run_mode,
            start_row=int(start_row),
            row_limit=int(row_limit),
            continue_on_error=not stop_on_error,
            include_raw_ai_json=include_raw_ai_json,
        )
        import time as _time

        progress_bar = st.progress(0.0)
        status = st.empty()
        started_at = _time.time()

        def _on_progress(payload: dict) -> None:
            selected = int(payload.get("selected_rows", 0) or 0)
            processed = int(payload.get("processed_rows", 0) or 0)
            frac = (processed / selected) if selected else 0.0
            progress_bar.progress(min(1.0, max(0.0, frac)))
            status.info(build_progress_status_text(payload, started_at))

        if run_mode == FOREIGN_HQ_ONLY_MODE:
            # HQ+C4+optional-C5 screening and the confirmed-only full-enrichment
            # pass both happen inside run_batch_foreign_hq_only; C5 must not be
            # re-applied afterward here (it already ran as part of the decision).
            def _on_phase_progress(payload: dict) -> None:
                phase = int(payload.get("phase", 1) or 1)
                phase_count = int(payload.get("phase_count", 3) or 3)
                total = int(payload.get("phase_total", 0) or 0)
                done = int(payload.get("phase_processed", 0) or 0)
                within = min(1.0, done / total) if total else 0.0
                overall = ((phase - 1) + within) / phase_count
                progress_bar.progress(min(1.0, max(0.0, overall)))
                status.info(build_phase_progress_status_text(payload, started_at))

            with st.spinner(
                "Running HQ screening, optional C5 adjudication, and "
                "confirmed-only full enrichment..."
            ):
                tables = run_batch_foreign_hq_only(
                    df, config, serper, anthropic,
                    c5_enabled=c5_enabled,
                    c5_scoring_behavior=c5_scoring_behavior,
                    c5_scope=c5_scope,
                    c5_model_used=c5_model_used,
                    c5_model_tier=c5_model_tier,
                    progress_callback=_on_phase_progress,
                )
            progress_bar.progress(1.0)
        else:
            with st.spinner("Running batch enrichment..."):
                tables = run_batch_dataframe(
                    df, config, serper, anthropic, progress_callback=_on_progress)

            progress_bar.progress(1.0)

            # ── Optional C5 adjudication (after normal batch processing) ──────────
            c5_counts = {}
            if c5_enabled:
                c5_bar = st.progress(0.0)
                c5_status = st.empty()

                def _on_c5_progress(payload: dict) -> None:
                    sel = int(payload.get("c5_selected", 0) or 0)
                    dn = int(payload.get("c5_processed", 0) or 0)
                    c5_bar.progress(min(1.0, dn / sel) if sel else 1.0)
                    c5_status.info(
                        f"C5 {dn}/{sel}: {payload.get('current_company_name', '')}")

                with st.spinner("Running C5 Sonnet adjudication..."):
                    c5_rows, c5_counts = apply_c5_adjudication(
                        tables["enriched_leads"],
                        anthropic_api_key=anthropic,
                        model_used=c5_model_used,
                        model_tier=c5_model_tier,
                        scoring_behavior=c5_scoring_behavior,
                        scope=c5_scope,
                        include_raw=include_raw_ai_json,
                        progress_callback=_on_c5_progress,
                    )
                c5_bar.progress(1.0)
                tables["enriched_leads"] = pd.DataFrame(c5_rows)

            # Extend Run Summary with C5 settings/counts (always records enabled flag).
            tables["run_summary"] = add_c5_summary_fields(
                tables["run_summary"],
                c5_enabled=c5_enabled,
                c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
                c5_scope=c5_scope if c5_enabled else "",
                c5_model_tier=c5_model_tier if c5_enabled else "",
                c5_model_used=c5_model_used if c5_enabled else "",
                counts=c5_counts,
            )

        data = build_excel_workbook_bytes(tables)

        _total_elapsed = format_duration(_time.time() - started_at)
        _summary = tables["run_summary"].iloc[0].to_dict() if len(tables["run_summary"]) else {}
        status.success(
            f"Completed {_summary.get('processed_rows', 0)} rows in {_total_elapsed} "
            f"(success {_summary.get('success_count', 0)}, errors {_summary.get('error_count', 0)})."
        )
        st.session_state["v2_batch_output_bytes"] = data
        st.session_state["v2_batch_tables"] = tables
        st.session_state["v2_batch_mode"] = run_mode

        # ── Optional autosave (same bytes as the download button) ─────────────
        if autosave_enabled:
            try:
                saved_path = autosave_output_workbook(data, autosave_dir, run_mode)
                st.success(f"Autosaved output workbook to: {saved_path}")
            except Exception as exc:
                st.error(
                    f"Autosave failed: {exc}. "
                    "You can still use the download button below."
                )

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
