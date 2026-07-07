"""Offline refresh of caller-facing app text fields in an enriched workbook.

Reads an existing Lead Prioritizer v2 "Enriched Leads" workbook, reconstructs
just enough of ``LeadPrioritizationResult`` from the already-present columns
to call the deterministic ``build_caller_app_fields`` builder, and overwrites
only the caller/app-facing text columns. No enrichment, HQ detection, C4, C5,
or commercial-fit scoring is re-run, and no external APIs are called.
"""

from __future__ import annotations

import argparse
import math
import sys

import pandas as pd

from lead_caller_app_fields_builder import build_caller_app_fields
from lead_output_schema import LeadEvidence, LeadPrioritizationResult, LeadSignal

DEFAULT_SHEET = "Enriched Leads"

# Fields explicitly named in requirement 7 — always overwritten.
TARGET_FIELDS = [
    "cold_caller_summary_app",
    "parent_hq_summary_app",
    "why_relevant_app",
    "what_is_hot_app",
    "what_is_not_app",
    "caller_angle_app",
    "call_starter_app",
    "caution_app",
]

# Fields explicitly named in requirement 8 — allowed to pass through
# unchanged, but the underlying scoring values are never recalculated.
PASSTHROUGH_FIELDS = [
    "commercial_fit_score_app",
    "commercial_tier_app",
    "foreign_hq_signal_used_in_app",
    "foreign_hq_country_app",
    "foreign_hq_city_app",
]

BAD_PHRASES = [
    "International profile evidence found",
    "Onboarding/training need evidence found",
    "Company complexity evidence found",
    "ICP keyword evidence found",
    "Relevant because the lead shows",
    "foreign HQ signal and international profile evidence",
]


def _clean(value):
    """Normalize NaN/empty-string cells from pandas to None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _clean_str(value):
    value = _clean(value)
    return None if value is None else str(value)


def _clean_float(value):
    value = _clean(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_bool(value):
    value = _clean(value)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("true", "yes", "1")


def _clean_int(value):
    value = _clean(value)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def row_to_result(row: dict) -> LeadPrioritizationResult:
    """Reconstruct the subset of LeadPrioritizationResult needed by
    build_caller_app_fields from an existing Enriched Leads row.

    evidence_items/signals are only used for truthiness downstream, so a
    placeholder non-empty list is used when the corresponding count column is
    greater than zero; detailed evidence text is not reconstructed.
    """
    evidence_count = _clean_int(row.get("evidence_count"))
    signal_count = _clean_int(row.get("signal_count"))

    evidence_items = [LeadEvidence()] * evidence_count
    signals = [LeadSignal(signal_name="placeholder")] * signal_count

    return LeadPrioritizationResult(
        company_name=_clean_str(row.get("company_name")) or "",
        domain=_clean_str(row.get("domain")),
        input_country=_clean_str(row.get("input_country")),
        hq_detected_country=_clean_str(row.get("hq_detected_country")),
        hq_detected_city=_clean_str(row.get("hq_detected_city")),
        hq_confidence=_clean_str(row.get("hq_confidence")),
        needs_manual_review=_clean_bool(row.get("needs_manual_review")),
        sig_foreign_hq_score_for_next_scoring=_clean_float(
            row.get("sig_foreign_hq_score_for_next_scoring")
        ),
        hq_evidence_domain_mismatch_warning=_clean_str(
            row.get("hq_evidence_domain_mismatch_warning")
        ),
        hq_positive_score_suppressed_for_review=_clean_str(
            row.get("hq_positive_score_suppressed_for_review")
        ),
        ai_parent_company=_clean_str(row.get("ai_parent_company")),
        ai_parent_hq_country=_clean_str(row.get("ai_parent_hq_country")),
        ai_parent_hq_city=_clean_str(row.get("ai_parent_hq_city")),
        ai_hq_error=_clean_str(row.get("ai_hq_error")),
        sig_international_profile_score=_clean_float(
            row.get("sig_international_profile_score")
        ),
        sig_onboarding_training_need_score=_clean_float(
            row.get("sig_onboarding_training_need_score")
        ),
        sig_company_size_complexity_score=_clean_float(
            row.get("sig_company_size_complexity_score")
        ),
        sig_icp_keyword_match_score=_clean_float(row.get("sig_icp_keyword_match_score")),
        evidence_items=evidence_items,
        signals=signals,
        final_commercial_fit_score=_clean_float(row.get("final_commercial_fit_score")),
        commercial_tier=_clean_str(row.get("commercial_tier")),
        missing_scoring_fields=_clean_str(row.get("missing_scoring_fields")),
    )


def count_bad_phrases(df: pd.DataFrame) -> int:
    """Count occurrences of the known bad phrases across the target columns."""
    count = 0
    for field in TARGET_FIELDS:
        if field not in df.columns:
            continue
        for value in df[field]:
            if not isinstance(value, str):
                continue
            for phrase in BAD_PHRASES:
                count += value.count(phrase)
    return count


def refresh_enriched_leads(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Return a copy of df with target app fields refreshed, plus rows_updated."""
    df = df.copy()
    for field in TARGET_FIELDS:
        if field not in df.columns:
            df[field] = None

    rows_updated = 0
    for idx, row in df.iterrows():
        result = row_to_result(row.to_dict())
        app_fields = build_caller_app_fields(result)

        changed = False
        for field in TARGET_FIELDS:
            old_value = _clean(df.at[idx, field])
            new_value = app_fields.get(field)
            if old_value != new_value:
                changed = True
            df.at[idx, field] = new_value

        if changed:
            rows_updated += 1

    return df, rows_updated


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh only the caller-facing app text fields in an existing "
            "Lead Prioritizer Enriched Leads workbook, offline."
        )
    )
    parser.add_argument("--input-xlsx", required=True, help="Path to the input workbook.")
    parser.add_argument("--output-xlsx", required=True, help="Path to write the new workbook.")
    parser.add_argument(
        "--sheet",
        default=DEFAULT_SHEET,
        help=f"Enriched Leads sheet name (default: {DEFAULT_SHEET!r}).",
    )
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    xls = pd.ExcelFile(args.input_xlsx)
    if args.sheet not in xls.sheet_names:
        print(f"Sheet {args.sheet!r} not found in {args.input_xlsx}", file=sys.stderr)
        return 1

    sheets = {name: xls.parse(name) for name in xls.sheet_names}

    enriched = sheets[args.sheet]
    rows_read = len(enriched)
    bad_phrases_before = count_bad_phrases(enriched)

    refreshed, rows_updated = refresh_enriched_leads(enriched)
    sheets[args.sheet] = refreshed

    bad_phrases_after = count_bad_phrases(refreshed)

    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        for name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, sheet_name=name, index=False)

    print(f"rows_read: {rows_read}")
    print(f"rows_updated: {rows_updated}")
    print(f"fields_refreshed: {', '.join(TARGET_FIELDS)}")
    print(f"output_xlsx: {args.output_xlsx}")
    print(f"bad_phrases_before: {bad_phrases_before}")
    print(f"bad_phrases_after: {bad_phrases_after}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
