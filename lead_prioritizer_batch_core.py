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

from dataclasses import dataclass
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
