"""Shared batch core for Lead Prioritizer v2.

Reusable, UI-agnostic batch engine that runs ``prioritize_single_lead`` over a
pandas DataFrame and returns DataFrames ready for an output workbook.  It is
imported by both the (future) Streamlit upload/download app and the (future)
CLI runner — so this module deliberately has:

- no Streamlit imports,
- no command-line parsing,
- no new enrichment logic (it only orchestrates ``prioritize_single_lead``).

Secret hygiene: API keys are passed straight through to the pipeline and are
never printed, never stored on the result, and never written to any returned
DataFrame.  Raw Serper payloads are never surfaced; raw AI JSON is excluded
unless ``include_raw_ai_json=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional

import io

import pandas as pd

from lead_output_schema import LeadInput
from lead_prioritizer_core import prioritize_single_lead


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPPORTED_RUN_MODES = (
    "full",
    "hq_only",
    "evidence_only",
    "signals_no_score",
    "full_no_score",
)

# "Full enrichment, confirmed foreign-HQ only" — a separate, opt-in batch mode
# handled by ``run_batch_foreign_hq_only`` (not ``run_batch_dataframe`` /
# ``resolve_pipeline_flags``), since it needs a two-phase per-row decision
# (HQ+C4+optional-C5 screening, then a conditional full-enrichment pass).
# Deliberately excluded from SUPPORTED_RUN_MODES / the CLI so existing run
# modes stay untouched; only the Streamlit app offers it today.
FOREIGN_HQ_ONLY_MODE = "full_foreign_hq_only"


@dataclass
class BatchRunConfig:
    company_name_column: str
    domain_column: str
    input_country_column: Optional[str] = None
    default_input_country: str = "Italy"
    run_mode: str = "full"
    start_row: int = 0
    row_limit: int = 10
    continue_on_error: bool = True
    max_evidence_urls: int = 6
    include_raw_ai_json: bool = False


# ---------------------------------------------------------------------------
# Pipeline flag resolution
# ---------------------------------------------------------------------------

_ALL_FLAGS = (
    "collect_non_hq_evidence",
    "extract_non_hq_signals_flag",
    "build_app_summary_fields_flag",
    "calculate_commercial_score_flag",
    "build_caller_app_fields_flag",
    "run_full_v2_pipeline",
)


def resolve_pipeline_flags(run_mode: str) -> dict:
    """Map a run mode to the ``prioritize_single_lead`` flag kwargs."""
    flags = {k: False for k in _ALL_FLAGS}

    if run_mode == "full":
        flags["run_full_v2_pipeline"] = True
    elif run_mode == "hq_only":
        pass  # every optional flag stays False
    elif run_mode == "evidence_only":
        flags["collect_non_hq_evidence"] = True
    elif run_mode == "signals_no_score":
        flags["collect_non_hq_evidence"] = True
        flags["extract_non_hq_signals_flag"] = True
        flags["build_app_summary_fields_flag"] = True
    elif run_mode == "full_no_score":
        flags["collect_non_hq_evidence"] = True
        flags["extract_non_hq_signals_flag"] = True
        flags["build_app_summary_fields_flag"] = True
        flags["build_caller_app_fields_flag"] = True
        # commercial score intentionally False
    else:
        raise ValueError(f"Unknown run_mode: {run_mode!r}")

    return flags


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------

def select_batch_rows(df: pd.DataFrame, config: BatchRunConfig) -> pd.DataFrame:
    """Apply start_row and row_limit, preserving the original DataFrame index.

    ``row_limit == 0`` means all remaining rows from ``start_row``.
    """
    start = max(0, int(config.start_row))
    sub = df.iloc[start:]
    if config.row_limit and int(config.row_limit) > 0:
        sub = sub.iloc[: int(config.row_limit)]
    return sub


# ---------------------------------------------------------------------------
# Flatten helpers
# ---------------------------------------------------------------------------

# Result fields flattened onto the Enriched Leads sheet (curated, ordered).
# NOTE: excludes list fields (evidence_items / signals — own sheets),
# ai_hq_raw_json (gated), and any competitor field (never displayed).
_RESULT_FLAT_FIELDS = [
    "company_name", "domain", "input_country", "v2_pipeline_mode",
    # HQ
    "hq_detected_country", "hq_detected_city", "hq_confidence",
    "foreign_hq_simple", "needs_manual_review", "hq_reason",
    "hq_evidence_url", "hq_evidence_quote", "hq_structure_type",
    "sig_foreign_hq_score_for_next_scoring",
    "domain_root", "query_used", "parser_source",
    # C4 positive-score safety audit
    "hq_query_risk_flag", "hq_evidence_domain_match",
    "hq_evidence_domain_mismatch_warning",
    "hq_positive_score_suppressed_for_review", "hq_review_reason",
    "ai_hq_model", "ai_hq_classification", "ai_hq_confidence",
    "ai_parent_company", "ai_parent_hq_country", "ai_parent_hq_city",
    "ai_call_attempted", "ai_call_success", "ai_hq_error",
    # non-HQ signal scores / reasons / evidence
    "sig_international_profile_score", "sig_onboarding_training_need_score",
    "sig_company_size_complexity_score", "sig_icp_keyword_match_score",
    "international_profile_reason", "onboarding_training_need_reason",
    "company_size_complexity_reason", "icp_keyword_match_reason",
    "international_profile_evidence_url", "onboarding_training_need_evidence_url",
    "company_size_complexity_evidence_url", "icp_keyword_match_evidence_url",
    "international_profile_evidence_quote", "onboarding_training_need_evidence_quote",
    "company_size_complexity_evidence_quote", "icp_keyword_match_evidence_quote",
    # score / tier
    "final_commercial_fit_score", "commercial_tier", "icp_similarity_score",
    "lean_model_prob", "lr_z_score", "scoring_profile", "scoring_notes",
    "missing_scoring_fields", "top_score_drivers", "weak_score_drivers",
    "v2_score_input_mapping_note",
    "score_input_foreign_hq", "score_input_intl_footprint",
    "score_input_explicit_lnd", "score_input_lnd_onboarding",
    "score_input_rapid_growth",
    # app / evidence summary
    "evidence_summary_app", "key_source_links_app", "advanced_notes_app",
    # caller / app
    "commercial_fit_score_app", "commercial_tier_app",
    "what_is_hot_app", "what_is_not_app", "why_relevant_app",
    "caller_angle_app", "call_starter_app", "caution_app",
    "foreign_hq_signal_used_in_app", "foreign_hq_country_app", "foreign_hq_city_app",
]


def flatten_result_for_excel(
    result,
    original_row: dict,
    source_index,
    run_success: bool,
    run_error: str,
    include_raw_ai_json: bool = False,
) -> dict:
    """Flatten one result into a single Enriched Leads row.

    Preserves the original input columns, then adds run metadata and the curated
    result fields.  On error (``result is None``) only input columns + run
    metadata are present.
    """
    out: dict = dict(original_row)  # original input columns first
    out["source_index"] = source_index
    out["run_success"] = run_success
    out["run_error"] = run_error or ""

    if result is None:
        out["evidence_count"] = 0
        out["signal_count"] = 0
        return out

    for field in _RESULT_FLAT_FIELDS:
        out[field] = getattr(result, field, None)

    out["evidence_count"] = len(result.evidence_items or [])
    out["signal_count"] = len(result.signals or [])

    if include_raw_ai_json:
        out["ai_hq_raw_json"] = result.ai_hq_raw_json

    return out


def flatten_evidence_for_excel(result, source_index) -> list[dict]:
    """One row per LeadEvidence item on the result."""
    rows: list[dict] = []
    for ev in (getattr(result, "evidence_items", None) or []):
        rows.append({
            "source_index": source_index,
            "evidence_id": ev.evidence_id,
            "signal_name": ev.signal_name,
            "query_used": ev.query_used,
            "source_url": ev.source_url,
            "source_title": ev.source_title,
            "source_snippet": ev.source_snippet,
            "source_type": ev.source_type,
            "parser_source": ev.parser_source,
            "retrieved_at": ev.retrieved_at,
            "confidence": ev.confidence,
            "notes": ev.notes,
        })
    return rows


def flatten_signals_for_excel(result, source_index) -> list[dict]:
    """One row per LeadSignal on the result."""
    rows: list[dict] = []
    for sig in (getattr(result, "signals", None) or []):
        rows.append({
            "source_index": source_index,
            "signal_name": sig.signal_name,
            "signal_value": sig.signal_value,
            "signal_score": sig.signal_score,
            "signal_confidence": sig.signal_confidence,
            "signal_reason": sig.signal_reason,
            "evidence_url": sig.evidence_url,
            "evidence_quote": sig.evidence_quote,
            "evidence_title": sig.evidence_title,
            "query_used": sig.query_used,
            "parser_source": sig.parser_source,
            "needs_manual_review": sig.needs_manual_review,
        })
    return rows


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

def build_run_summary_dataframe(
    config: BatchRunConfig,
    total_input_rows: int,
    selected_rows: int,
    processed_rows: int,
    success_count: int,
    error_count: int,
) -> pd.DataFrame:
    """Single-row summary DataFrame.  Contains no API keys."""
    return pd.DataFrame([{
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_mode": config.run_mode,
        "default_input_country": config.default_input_country,
        "total_input_rows": total_input_rows,
        "selected_rows": selected_rows,
        "processed_rows": processed_rows,
        "success_count": success_count,
        "error_count": error_count,
        "start_row": config.start_row,
        "row_limit": config.row_limit,
        "company_name_column": config.company_name_column,
        "domain_column": config.domain_column,
        "input_country_column": config.input_country_column,
    }])


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch_dataframe(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run Lead Prioritizer v2 over selected rows.

    Returns a dict of DataFrames: ``enriched_leads``, ``evidence``, ``signals``,
    ``run_summary``.  API keys are passed through only; they are never printed or
    written into any output.

    ``progress_callback`` (optional) is invoked once after each processed row —
    including rows that error — with a secret-free payload dict.  It defaults to
    ``None`` for backward compatibility (the CLI and existing callers are
    unaffected).  If the callback raises, the exception is swallowed so it can
    never break enrichment.
    """
    flags = resolve_pipeline_flags(config.run_mode)
    selected = select_batch_rows(df, config)
    selected_rows = len(selected)

    enriched_rows: list[dict] = []
    evidence_rows: list[dict] = []
    signal_rows: list[dict] = []
    processed = success = error = 0

    for idx, row in selected.iterrows():
        original = row.to_dict()
        company = str(original.get(config.company_name_column, "") or "").strip()
        domain = str(original.get(config.domain_column, "") or "").strip() or None
        country = None
        if config.input_country_column:
            country = str(original.get(config.input_country_column, "") or "").strip() or None

        processed += 1
        run_success = True
        run_error = ""
        result = None
        try:
            result = prioritize_single_lead(
                LeadInput(company_name=company, domain=domain, input_country=country),
                serper_api_key=serper_api_key,
                anthropic_api_key=anthropic_api_key,
                default_input_country=config.default_input_country,
                **flags,
            )
            success += 1
        except Exception as exc:  # per-row isolation
            run_success = False
            run_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            error += 1

        enriched_rows.append(flatten_result_for_excel(
            result, original, idx, run_success, run_error, config.include_raw_ai_json,
        ))
        if result is not None:
            evidence_rows.extend(flatten_evidence_for_excel(result, idx))
            signal_rows.extend(flatten_signals_for_excel(result, idx))

        # Secret-free progress notification (never breaks the batch).
        if progress_callback is not None:
            try:
                progress_callback({
                    "processed_rows": processed,
                    "selected_rows": selected_rows,
                    "success_count": success,
                    "error_count": error,
                    "current_source_index": idx,
                    "current_company_name": company,
                    "current_domain": domain,
                    "run_success": run_success,
                    "run_error": run_error,
                })
            except Exception:
                pass  # a broken callback must never break enrichment

        if not run_success and not config.continue_on_error:
            break

    return {
        "enriched_leads": pd.DataFrame(enriched_rows),
        "evidence": pd.DataFrame(evidence_rows),
        "signals": pd.DataFrame(signal_rows),
        "run_summary": build_run_summary_dataframe(
            config,
            total_input_rows=len(df),
            selected_rows=len(selected),
            processed_rows=processed,
            success_count=success,
            error_count=error,
        ),
    }


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------

_SHEET_NAMES = {
    "enriched_leads": "Enriched Leads",
    "evidence": "Evidence",
    "signals": "Signals",
    "run_summary": "Run Summary",
}


def build_excel_workbook_bytes(output_tables: dict) -> bytes:
    """Write the batch output tables to an xlsx workbook and return the bytes."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for key, sheet_name in _SHEET_NAMES.items():
            frame = output_tables.get(key)
            if frame is None:
                frame = pd.DataFrame()
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Optional C5 Sonnet HQ adjudication layer (country-agnostic)
# ---------------------------------------------------------------------------
#
# C5 runs AFTER normal batch processing, over the Enriched Leads rows. It is a
# separate, opt-in step: the core imports the C5 layer lazily inside
# ``apply_c5_adjudication`` so the base batch flow stays independent and C5
# remains removable. It adds no Serper calls and is country-agnostic — the
# per-row ``input_country`` is passed straight through to C5.

C5_SCORING_BEHAVIORS = ("append_only", "conservative_adjustment")
C5_SCOPES = (
    "all_rows",
    "score_3_only",
    "score_3_or_manual_review",
    "manual_review_or_suppressed",
)

# Full set of C5 columns with safe defaults for rows NOT sent to C5.
_C5_BLANK_DEFAULTS = {
    "c5_adjudication": "",
    "c5_confidence": "",
    "c5_target_company_match": "",
    "c5_parent_company": "",
    "c5_parent_hq_country": "",
    "c5_parent_hq_city": "",
    "c5_reason": "",
    "c5_sonnet_model": "",
    "c5_model_used": "",
    "c5_model_tier": "",
    "c5_call_attempted": False,
    "c5_call_success": False,
    "c5_error": "",
    "c5_recommended_hq_score": None,
    "c5_recommended_manual_review": False,
    "c5_recommendation_reason": "",
    "c5_possible_foreign_parent_for_review": False,
}


def _c5_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1")


def _c5_score(v):
    """Parse a score to float, or None when absent/blank/unparseable."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def row_selected_for_c5(row: dict, scope: str) -> bool:
    """Country-agnostic C5 row selection based on the existing HQ output."""
    score = _c5_score(row.get("sig_foreign_hq_score_for_next_scoring"))
    is3 = (score == 3.0)
    review = _c5_truthy(row.get("needs_manual_review"))
    suppressed = _c5_truthy(row.get("hq_positive_score_suppressed_for_review"))
    if scope == "all_rows":
        return True
    if scope == "score_3_only":
        return is3
    if scope == "score_3_or_manual_review":
        return is3 or review
    if scope == "manual_review_or_suppressed":
        return review or suppressed
    return False


def _c5_blank_row(original: dict, include_raw: bool) -> dict:
    out = dict(original)
    out.update(_C5_BLANK_DEFAULTS)
    if include_raw:
        out["c5_raw_json"] = None
    return out


def _append_hq_reason(row: dict, note: str) -> None:
    prev = str(row.get("hq_reason") or "").strip()
    row["hq_reason"] = f"{prev} | {note}" if prev else note


def _apply_conservative_adjustment(enriched: dict, result, counts: dict) -> None:
    """Conservative C5 scoring: confirm/downgrade score-3 positives; never
    auto-upgrade score-0 rows. Mutates ``enriched`` in place."""
    old = _c5_score(enriched.get("sig_foreign_hq_score_for_next_scoring"))
    confirmed = (
        bool(result.call_success)
        and result.adjudication == "foreign_parent_confirmed"
        and result.confidence in ("High", "Medium")
        and result.target_company_match == "yes"
    )

    if old == 3.0:
        if confirmed:
            return  # keep score 3
        enriched["sig_foreign_hq_score_for_next_scoring"] = 0.0
        enriched["needs_manual_review"] = True
        _append_hq_reason(
            enriched,
            "C5 downgraded previous HQ score-3 because foreign parent was not confirmed.")
        counts["c5_downgraded_score_3_count"] += 1
        return

    if old == 0.0:
        enriched["sig_foreign_hq_score_for_next_scoring"] = 0.0  # never auto-upgrade
        if confirmed:
            enriched["c5_possible_foreign_parent_for_review"] = True
            enriched["needs_manual_review"] = True
            _append_hq_reason(
                enriched,
                "C5 possible foreign parent, not auto-upgraded under conservative mode.")
            counts["c5_possible_foreign_parent_for_review_count"] += 1
        elif not result.call_success:
            # Row was selected for C5 but the call/parse failed → stay safe.
            enriched["needs_manual_review"] = True
        return
    # Other/absent old scores: leave untouched under conservative mode.


def apply_c5_adjudication(
    enriched_rows,
    *,
    anthropic_api_key: str,
    model_used: str,
    model_tier: str,
    scoring_behavior: str = "append_only",
    scope: str = "score_3_or_manual_review",
    include_raw: bool = False,
    progress_callback=None,
) -> tuple:
    """Apply optional C5 Sonnet adjudication over Enriched Leads rows.

    Reuses the single-source ``adjudicate_row`` from the C5 probe (lazy import),
    so no C5 prompt/parser logic is duplicated. Country-agnostic: each row's
    ``input_country`` is passed straight to C5.

    Returns ``(out_rows, counts)``. ``append_only`` never changes
    ``sig_foreign_hq_score_for_next_scoring`` / ``needs_manual_review``;
    ``conservative_adjustment`` may confirm/downgrade score-3 positives but never
    auto-upgrades score-0 rows.
    """
    from run_hq_sonnet_adjudication_probe import adjudicate_row  # lazy; keeps C5 removable

    if isinstance(enriched_rows, pd.DataFrame):
        rows = enriched_rows.to_dict("records")
    else:
        rows = [dict(r) for r in enriched_rows]

    counts = {
        "c5_rows_attempted": 0,
        "c5_success_count": 0,
        "c5_error_count": 0,
        "c5_foreign_parent_confirmed_count": 0,
        "c5_domestic_confirmed_count": 0,
        "c5_unclear_count": 0,
        "c5_recommended_score_3_count": 0,
        "c5_possible_foreign_parent_for_review_count": 0,
        "c5_downgraded_score_3_count": 0,
    }

    flags = [row_selected_for_c5(r, scope) for r in rows]
    total = sum(flags)
    done = 0
    out_rows: list[dict] = []

    for r, selected in zip(rows, flags):
        if not selected:
            out_rows.append(_c5_blank_row(r, include_raw))
            continue

        enriched, result, rec = adjudicate_row(
            r, anthropic_api_key, model_used, model_tier,
            source_index=r.get("source_index"), include_raw=include_raw,
        )
        enriched.setdefault("c5_possible_foreign_parent_for_review", False)

        counts["c5_rows_attempted"] += 1
        if result.call_success:
            counts["c5_success_count"] += 1
        else:
            counts["c5_error_count"] += 1
        if result.adjudication == "foreign_parent_confirmed":
            counts["c5_foreign_parent_confirmed_count"] += 1
        elif result.adjudication == "domestic_confirmed":
            counts["c5_domestic_confirmed_count"] += 1
        else:
            counts["c5_unclear_count"] += 1
        if rec["c5_recommended_hq_score"] == 3.0:
            counts["c5_recommended_score_3_count"] += 1

        if scoring_behavior == "conservative_adjustment":
            _apply_conservative_adjustment(enriched, result, counts)
        # append_only: never touch score / needs_manual_review.

        out_rows.append(enriched)
        done += 1
        if progress_callback is not None:
            try:
                progress_callback({
                    "c5_processed": done,
                    "c5_selected": total,
                    "current_company_name": str(r.get("company_name") or ""),
                })
            except Exception:
                pass

    return out_rows, counts


def add_c5_summary_fields(
    run_summary: pd.DataFrame,
    *,
    c5_enabled: bool,
    c5_scoring_behavior: str,
    c5_scope: str,
    c5_model_tier: str,
    c5_model_used: str,
    counts: dict,
) -> pd.DataFrame:
    """Return a copy of the run-summary DataFrame with C5 settings/counts added."""
    df = run_summary.copy() if run_summary is not None else pd.DataFrame([{}])
    if len(df) == 0:
        df = pd.DataFrame([{}])
    df["c5_enabled"] = c5_enabled
    df["c5_scoring_behavior"] = c5_scoring_behavior
    df["c5_scope"] = c5_scope
    df["c5_model_tier"] = c5_model_tier
    df["c5_model_used"] = c5_model_used
    for key in (
        "c5_rows_attempted", "c5_success_count", "c5_error_count",
        "c5_foreign_parent_confirmed_count", "c5_domestic_confirmed_count",
        "c5_unclear_count", "c5_recommended_score_3_count",
        "c5_possible_foreign_parent_for_review_count", "c5_downgraded_score_3_count",
    ):
        df[key] = counts.get(key, 0)
    return df


# ---------------------------------------------------------------------------
# "Full enrichment, confirmed foreign-HQ only" batch mode (country-agnostic)
# ---------------------------------------------------------------------------
#
# Reduces cost/noise for Brazil-and-similar runs: HQ detection (+ C4, and
# optionally C5) runs for every row first; full v2 enrichment (evidence,
# signals, scoring, caller fields) then runs ONLY for rows confirmed
# foreign-HQ. Everything is reused — no duplicated HQ, C4, or C5 logic:
#   Phase 1 delegates to run_batch_dataframe(run_mode="hq_only").
#   Phase 2 (optional) delegates to apply_c5_adjudication.
#   Phase 3 delegates to prioritize_single_lead(run_full_v2_pipeline=True) and
#   flatten_result_for_excel, but only for confirmed rows.

def run_batch_foreign_hq_only(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the "Full enrichment, confirmed foreign-HQ only" batch mode.

    "Confirmed foreign-HQ" is the FINAL post-C4/post-C5 value of
    ``sig_foreign_hq_score_for_next_scoring == 3.0``. If C5 is disabled the
    decision is based on the post-C4 HQ score; if C5 is enabled with
    ``conservative_adjustment`` the decision uses the C5-adjusted score. C5's
    ``c5_possible_foreign_parent_for_review`` flag is never treated as
    confirmation, and a previous score of 0 is never auto-upgraded to 3 (this
    already holds by construction — ``apply_c5_adjudication`` never sets a
    score-0 row to 3.0).

    Rows that are not confirmed are kept in the output, unenriched, with
    ``enrichment_skipped=True`` / ``enrichment_skip_reason`` set; confirmed
    rows get ``enrichment_skipped=False`` / ``""`` and the full v2 fields.

    Returns the same dict shape as ``run_batch_dataframe``: ``enriched_leads``,
    ``evidence``, ``signals``, ``run_summary`` (extended with the mode's own
    counts and, always, the C5 settings/counts columns).
    """
    hq_config = replace(config, run_mode="hq_only")
    hq_tables = run_batch_dataframe(
        df, hq_config, serper_api_key, anthropic_api_key,
        progress_callback=progress_callback,
    )
    rows = hq_tables["enriched_leads"].to_dict("records")

    c5_counts: dict = {}
    if c5_enabled:
        rows, c5_counts = apply_c5_adjudication(
            rows,
            anthropic_api_key=anthropic_api_key,
            model_used=c5_model_used,
            model_tier=c5_model_tier,
            scoring_behavior=c5_scoring_behavior,
            scope=c5_scope,
            include_raw=config.include_raw_ai_json,
            progress_callback=progress_callback,
        )

    out_rows: list[dict] = []
    evidence_rows: list[dict] = []
    signal_rows: list[dict] = []
    attempted = skipped = confirmed = 0

    for row in rows:
        score = _c5_score(row.get("sig_foreign_hq_score_for_next_scoring"))
        is_confirmed = (score == 3.0)

        if not is_confirmed:
            out_row = dict(row)
            out_row["enrichment_skipped"] = True
            out_row["enrichment_skip_reason"] = "Not confirmed foreign HQ"
            out_rows.append(out_row)
            skipped += 1
            continue

        confirmed += 1
        attempted += 1
        company = str(row.get(config.company_name_column, "") or "").strip()
        domain = str(row.get(config.domain_column, "") or "").strip() or None
        country = None
        if config.input_country_column:
            country = str(row.get(config.input_country_column, "") or "").strip() or None
        source_index = row.get("source_index")

        try:
            result = prioritize_single_lead(
                LeadInput(company_name=company, domain=domain, input_country=country),
                serper_api_key=serper_api_key,
                anthropic_api_key=anthropic_api_key,
                default_input_country=config.default_input_country,
                run_full_v2_pipeline=True,
            )
            out_row = flatten_result_for_excel(
                result, row, source_index, True, "", config.include_raw_ai_json,
            )
            evidence_rows.extend(flatten_evidence_for_excel(result, source_index))
            signal_rows.extend(flatten_signals_for_excel(result, source_index))
        except Exception as exc:  # per-row isolation, matching run_batch_dataframe
            out_row = dict(row)
            out_row["run_success"] = False
            out_row["run_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

        out_row["enrichment_skipped"] = False
        out_row["enrichment_skip_reason"] = ""
        out_rows.append(out_row)

        if progress_callback is not None:
            try:
                progress_callback({
                    "foreign_hq_full_processed": attempted,
                    "foreign_hq_full_selected": confirmed,
                    "current_company_name": company,
                })
            except Exception:
                pass

    success_count = sum(1 for r in out_rows if r.get("run_success", True))
    error_count = len(out_rows) - success_count

    run_summary = build_run_summary_dataframe(
        config, total_input_rows=len(df), selected_rows=len(rows),
        processed_rows=len(rows), success_count=success_count, error_count=error_count,
    )
    run_summary["total_processed_rows"] = len(rows)
    run_summary["full_enrichment_attempted_count"] = attempted
    run_summary["full_enrichment_skipped_count"] = skipped
    run_summary["confirmed_foreign_hq_count"] = confirmed
    run_summary = add_c5_summary_fields(
        run_summary,
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts=c5_counts,
    )

    return {
        "enriched_leads": pd.DataFrame(out_rows),
        "evidence": pd.DataFrame(evidence_rows),
        "signals": pd.DataFrame(signal_rows),
        "run_summary": run_summary,
    }
