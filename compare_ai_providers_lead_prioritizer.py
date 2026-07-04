"""Compare Anthropic vs OpenAI HQ interpretation for the Lead Prioritizer.

Runs the same input rows twice through ``prioritize_single_lead`` — once with
the Anthropic provider (default claude-haiku-4-5-20251001) and once with the
experimental OpenAI provider (default gpt-5.4-nano) — and writes a comparison
Excel with classifications, scores, deltas, and usage/cost fields.

Comparison-only tooling: it changes no scoring, C4, C5, HQ, Serper, Excel, or
Lovable export behavior, and it never runs unless invoked explicitly. Each row
costs one Serper call plus one AI call per provider (HQ + commercial score
only — no non-HQ enrichment), so keep --row-limit small.

Usage:
    python compare_ai_providers_lead_prioritizer.py \
        --input leads.xlsx --output-xlsx provider_comparison.xlsx \
        --default-input-country Brazil --row-limit 5

    # Dry run: show which rows/models would run, no API calls
    python compare_ai_providers_lead_prioritizer.py \
        --input leads.xlsx --output-xlsx out.xlsx --dry-run

Environment: SERPER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from lead_hq_ai_interpreter import (
    _DEFAULT_AI_MODEL as DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    SUPPORTED_OPENAI_MODELS,
)
from lead_output_schema import LeadInput
from lead_prioritizer_core import prioritize_single_lead

DEFAULT_ROW_LIMIT = 5

# Fixed OpenAI models for the nano-vs-mini triple comparison (Anthropic vs
# OpenAI nano vs OpenAI mini). Comparison-only — does not change which model
# "OpenAI only" / two-way "compare" use.
DEFAULT_OPENAI_NANO_MODEL = "gpt-5.4-nano"
DEFAULT_OPENAI_MINI_MODEL = "gpt-5.4-mini"

COMPARISON_COLUMNS = [
    "company_name", "domain", "input_country",
    "anthropic_hq_classification", "openai_hq_classification",
    "anthropic_parent_hq_country", "openai_parent_hq_country",
    "anthropic_foreign_hq_score", "openai_foreign_hq_score",
    "anthropic_final_score", "openai_final_score",
    "score_delta", "tier_delta", "classification_match",
    "anthropic_tier", "openai_tier",
    "anthropic_model", "openai_model",
    "anthropic_input_tokens", "anthropic_output_tokens",
    "anthropic_total_tokens", "anthropic_estimated_cost_usd",
    "openai_input_tokens", "openai_output_tokens",
    "openai_total_tokens", "openai_estimated_cost_usd",
    "anthropic_ai_error", "openai_ai_error",
]


def read_input_rows(input_path: Path) -> pd.DataFrame:
    """Read an .xlsx or .csv input file into a DataFrame."""
    suffix = input_path.suffix.lower()
    if suffix == ".xlsx":
        return pd.read_excel(input_path)
    if suffix == ".csv":
        return pd.read_csv(input_path)
    raise ValueError(f"Unsupported input file type: {input_path.name!r} "
                     "(expected .xlsx or .csv)")


def select_rows(df: pd.DataFrame, start_row: int, row_limit: int) -> pd.DataFrame:
    """Same selection semantics as the batch core: offset + limit (0 = all)."""
    sub = df.iloc[max(0, int(start_row)):]
    if row_limit and int(row_limit) > 0:
        sub = sub.iloc[:int(row_limit)]
    return sub


def _cell(row, column) -> str:
    if not column:
        return ""
    value = row.get(column)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def build_comparison_row(row_input: dict, anthropic_result, openai_result) -> dict:
    """Flatten two per-provider results into one comparison record."""
    a, o = anthropic_result, openai_result

    a_score = a.final_commercial_fit_score
    o_score = o.final_commercial_fit_score
    score_delta = (
        round(float(o_score) - float(a_score), 4)
        if a_score is not None and o_score is not None else None
    )

    a_tier = a.commercial_tier or ""
    o_tier = o.commercial_tier or ""
    tier_delta = "same" if a_tier == o_tier else f"{a_tier or '?'} -> {o_tier or '?'}"

    a_clf = a.ai_hq_classification or ""
    o_clf = o.ai_hq_classification or ""

    return {
        "company_name": row_input.get("company_name", ""),
        "domain": row_input.get("domain", ""),
        "input_country": row_input.get("input_country", ""),
        "anthropic_hq_classification": a_clf,
        "openai_hq_classification": o_clf,
        "anthropic_parent_hq_country": a.ai_parent_hq_country or "",
        "openai_parent_hq_country": o.ai_parent_hq_country or "",
        "anthropic_foreign_hq_score": a.sig_foreign_hq_score_for_next_scoring,
        "openai_foreign_hq_score": o.sig_foreign_hq_score_for_next_scoring,
        "anthropic_final_score": a_score,
        "openai_final_score": o_score,
        "score_delta": score_delta,
        "tier_delta": tier_delta,
        "classification_match": bool(a_clf) and a_clf == o_clf,
        "anthropic_tier": a_tier,
        "openai_tier": o_tier,
        "anthropic_model": a.ai_hq_model or "",
        "openai_model": o.ai_hq_model or "",
        "anthropic_input_tokens": a.ai_hq_input_tokens,
        "anthropic_output_tokens": a.ai_hq_output_tokens,
        "anthropic_total_tokens": a.ai_hq_total_tokens,
        "anthropic_estimated_cost_usd": a.ai_hq_estimated_cost_usd,
        "openai_input_tokens": o.ai_hq_input_tokens,
        "openai_output_tokens": o.ai_hq_output_tokens,
        "openai_total_tokens": o.ai_hq_total_tokens,
        "openai_estimated_cost_usd": o.ai_hq_estimated_cost_usd,
        "anthropic_ai_error": a.ai_hq_error or "",
        "openai_ai_error": o.ai_hq_error or "",
    }


def run_comparison(
    df: pd.DataFrame,
    *,
    company_column: str,
    domain_column: str,
    country_column: str = "",
    default_input_country: str = "Italy",
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL,
    openai_model: str = DEFAULT_OPENAI_MODEL,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    openai_api_key: str = "",
    prioritize_fn=prioritize_single_lead,
) -> pd.DataFrame:
    """Run each row once per provider and return the comparison DataFrame.

    ``prioritize_fn`` is injectable for tests; live runs use
    ``prioritize_single_lead`` with ``calculate_commercial_score_flag=True``
    (HQ + commercial score only — no non-HQ enrichment, no C5).
    """
    records = []
    for _, raw in df.iterrows():
        row = raw.to_dict()
        company = _cell(row, company_column)
        domain = _cell(row, domain_column) or None
        country = _cell(row, country_column) or None
        lead = LeadInput(company_name=company, domain=domain, input_country=country)

        common = dict(
            serper_api_key=serper_api_key,
            anthropic_api_key=anthropic_api_key,
            openai_api_key=openai_api_key,
            default_input_country=default_input_country,
            calculate_commercial_score_flag=True,
        )
        anthropic_result = prioritize_fn(
            lead, ai_provider="anthropic", ai_model=anthropic_model, **common)
        openai_result = prioritize_fn(
            lead, ai_provider="openai", ai_model=openai_model, **common)

        records.append(build_comparison_row(
            {"company_name": company, "domain": domain or "",
             "input_country": country or default_input_country},
            anthropic_result, openai_result,
        ))

    return pd.DataFrame(records, columns=COMPARISON_COLUMNS)


# ---------------------------------------------------------------------------
# Triple comparison: Anthropic vs OpenAI nano vs OpenAI mini
# ---------------------------------------------------------------------------
# Same comparison-only contract as the two-way compare above: no scoring, C4,
# C5, HQ, Serper, Excel, or Lovable export behavior changes; never runs unless
# invoked explicitly. Each row costs one Serper call plus three AI calls
# (Anthropic + OpenAI nano + OpenAI mini), so keep --row-limit small.

TRIPLE_COMPARISON_COLUMNS = [
    "company_name", "domain", "input_country",
    "anthropic_hq_classification", "openai_nano_hq_classification",
    "openai_mini_hq_classification",
    "anthropic_parent_hq_country", "openai_nano_parent_hq_country",
    "openai_mini_parent_hq_country",
    "anthropic_foreign_hq_score", "openai_nano_foreign_hq_score",
    "openai_mini_foreign_hq_score",
    "anthropic_final_score", "openai_nano_final_score", "openai_mini_final_score",
    "anthropic_tier", "openai_nano_tier", "openai_mini_tier",
    "anthropic_model", "openai_nano_model", "openai_mini_model",
    "anthropic_input_tokens", "anthropic_output_tokens",
    "anthropic_total_tokens", "anthropic_estimated_cost_usd",
    "openai_nano_input_tokens", "openai_nano_output_tokens",
    "openai_nano_total_tokens", "openai_nano_estimated_cost_usd",
    "openai_mini_input_tokens", "openai_mini_output_tokens",
    "openai_mini_total_tokens", "openai_mini_estimated_cost_usd",
    "anthropic_ai_error", "openai_nano_ai_error", "openai_mini_ai_error",
]


def build_triple_comparison_row(
    row_input: dict, anthropic_result, nano_result, mini_result,
) -> dict:
    """Flatten three per-provider results into one triple-comparison record."""
    a, n, m = anthropic_result, nano_result, mini_result

    a_score = a.final_commercial_fit_score
    n_score = n.final_commercial_fit_score
    m_score = m.final_commercial_fit_score

    a_tier = a.commercial_tier or ""
    n_tier = n.commercial_tier or ""
    m_tier = m.commercial_tier or ""

    return {
        "company_name": row_input.get("company_name", ""),
        "domain": row_input.get("domain", ""),
        "input_country": row_input.get("input_country", ""),
        "anthropic_hq_classification": a.ai_hq_classification or "",
        "openai_nano_hq_classification": n.ai_hq_classification or "",
        "openai_mini_hq_classification": m.ai_hq_classification or "",
        "anthropic_parent_hq_country": a.ai_parent_hq_country or "",
        "openai_nano_parent_hq_country": n.ai_parent_hq_country or "",
        "openai_mini_parent_hq_country": m.ai_parent_hq_country or "",
        "anthropic_foreign_hq_score": a.sig_foreign_hq_score_for_next_scoring,
        "openai_nano_foreign_hq_score": n.sig_foreign_hq_score_for_next_scoring,
        "openai_mini_foreign_hq_score": m.sig_foreign_hq_score_for_next_scoring,
        "anthropic_final_score": a_score,
        "openai_nano_final_score": n_score,
        "openai_mini_final_score": m_score,
        "anthropic_tier": a_tier,
        "openai_nano_tier": n_tier,
        "openai_mini_tier": m_tier,
        "anthropic_model": a.ai_hq_model or "",
        "openai_nano_model": n.ai_hq_model or "",
        "openai_mini_model": m.ai_hq_model or "",
        "anthropic_input_tokens": a.ai_hq_input_tokens,
        "anthropic_output_tokens": a.ai_hq_output_tokens,
        "anthropic_total_tokens": a.ai_hq_total_tokens,
        "anthropic_estimated_cost_usd": a.ai_hq_estimated_cost_usd,
        "openai_nano_input_tokens": n.ai_hq_input_tokens,
        "openai_nano_output_tokens": n.ai_hq_output_tokens,
        "openai_nano_total_tokens": n.ai_hq_total_tokens,
        "openai_nano_estimated_cost_usd": n.ai_hq_estimated_cost_usd,
        "openai_mini_input_tokens": m.ai_hq_input_tokens,
        "openai_mini_output_tokens": m.ai_hq_output_tokens,
        "openai_mini_total_tokens": m.ai_hq_total_tokens,
        "openai_mini_estimated_cost_usd": m.ai_hq_estimated_cost_usd,
        "anthropic_ai_error": a.ai_hq_error or "",
        "openai_nano_ai_error": n.ai_hq_error or "",
        "openai_mini_ai_error": m.ai_hq_error or "",
    }


def run_triple_comparison(
    df: pd.DataFrame,
    *,
    company_column: str,
    domain_column: str,
    country_column: str = "",
    default_input_country: str = "Italy",
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL,
    openai_nano_model: str = DEFAULT_OPENAI_NANO_MODEL,
    openai_mini_model: str = DEFAULT_OPENAI_MINI_MODEL,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    openai_api_key: str = "",
    prioritize_fn=prioritize_single_lead,
) -> pd.DataFrame:
    """Run each row once per provider/model (Anthropic, OpenAI nano, OpenAI
    mini) and return the triple-comparison DataFrame.

    ``prioritize_fn`` is injectable for tests; live runs use
    ``prioritize_single_lead`` with ``calculate_commercial_score_flag=True``
    (HQ + commercial score only — no non-HQ enrichment, no C5).
    """
    records = []
    for _, raw in df.iterrows():
        row = raw.to_dict()
        company = _cell(row, company_column)
        domain = _cell(row, domain_column) or None
        country = _cell(row, country_column) or None
        lead = LeadInput(company_name=company, domain=domain, input_country=country)

        common = dict(
            serper_api_key=serper_api_key,
            anthropic_api_key=anthropic_api_key,
            openai_api_key=openai_api_key,
            default_input_country=default_input_country,
            calculate_commercial_score_flag=True,
        )
        anthropic_result = prioritize_fn(
            lead, ai_provider="anthropic", ai_model=anthropic_model, **common)
        nano_result = prioritize_fn(
            lead, ai_provider="openai", ai_model=openai_nano_model, **common)
        mini_result = prioritize_fn(
            lead, ai_provider="openai", ai_model=openai_mini_model, **common)

        records.append(build_triple_comparison_row(
            {"company_name": company, "domain": domain or "",
             "input_country": country or default_input_country},
            anthropic_result, nano_result, mini_result,
        ))

    return pd.DataFrame(records, columns=TRIPLE_COMPARISON_COLUMNS)


# ---------------------------------------------------------------------------
# Cost summary (audit only — never affects scoring, HQ, or export behavior)
# ---------------------------------------------------------------------------
# Company counts used for the "estimated cost per N companies" projection.
COST_PROJECTION_COMPANY_COUNTS = (100, 1_000, 10_000)

# (display_label, column_prefix) for the two-way and triple comparisons.
# column_prefix must match the "<prefix>_model" / "<prefix>_input_tokens" /
# "<prefix>_output_tokens" / "<prefix>_total_tokens" /
# "<prefix>_estimated_cost_usd" columns on the comparison DataFrame.
TWO_WAY_COST_PROVIDERS = [("anthropic", "anthropic"), ("openai", "openai")]
TRIPLE_COST_PROVIDERS = [
    ("anthropic", "anthropic"),
    ("openai_nano", "openai_nano"),
    ("openai_mini", "openai_mini"),
]


def build_provider_cost_rows(comparison_df: pd.DataFrame, providers) -> list:
    """One cost row per provider/model actually present in ``comparison_df``.

    ``providers`` is a list of ``(label, column_prefix)`` pairs. A provider
    whose cost column is entirely blank (unpriced model, or no rows) still
    gets a row with a blank ``estimated_cost_usd`` — never a guessed cost and
    never a crash.
    """
    n_rows = len(comparison_df)
    rows = []
    for label, prefix in providers:
        model_col = f"{prefix}_model"
        in_col = f"{prefix}_input_tokens"
        out_col = f"{prefix}_output_tokens"
        total_col = f"{prefix}_total_tokens"
        cost_col = f"{prefix}_estimated_cost_usd"

        model_name = ""
        if model_col in comparison_df.columns:
            names = comparison_df[model_col].dropna()
            names = names[names != ""]
            if len(names):
                model_name = str(names.iloc[0])

        input_tokens = (int(comparison_df[in_col].dropna().sum())
                        if in_col in comparison_df.columns else 0)
        output_tokens = (int(comparison_df[out_col].dropna().sum())
                          if out_col in comparison_df.columns else 0)
        total_tokens = (int(comparison_df[total_col].dropna().sum())
                        if total_col in comparison_df.columns
                        else input_tokens + output_tokens)

        cost_series = (comparison_df[cost_col].dropna()
                       if cost_col in comparison_df.columns else pd.Series(dtype=float))
        total_cost = round(float(cost_series.sum()), 6) if len(cost_series) else None
        cost_per_company = (round(total_cost / n_rows, 6)
                            if total_cost is not None and n_rows else None)

        rows.append({
            "provider": label,
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": total_cost,
            "cost_per_company_usd": cost_per_company,
        })
    return rows


def build_cost_totals(cost_rows: list, n_rows: int) -> dict:
    """Grand totals per provider plus projected cost at 100/1,000/10,000
    companies.

    The projection combines the per-company cost across every provider/model
    row that has a known cost (unpriced models are simply excluded — never
    guessed). Returns ``None`` for the projection when no provider has a
    known cost yet.
    """
    totals = {"companies_compared": n_rows}
    for row in cost_rows:
        totals[f"total_{row['provider']}_cost_usd"] = row["estimated_cost_usd"]

    known_per_company = [r["cost_per_company_usd"] for r in cost_rows
                         if r["cost_per_company_usd"] is not None]
    combined_per_company = round(sum(known_per_company), 6) if known_per_company else None
    for count in COST_PROJECTION_COMPANY_COUNTS:
        totals[f"estimated_cost_per_{count}_companies_usd"] = (
            round(combined_per_company * count, 2)
            if combined_per_company is not None else None
        )
    return totals


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the same leads through Anthropic and OpenAI HQ "
            "interpretation and write a comparison Excel."
        )
    )
    parser.add_argument("--input", required=True,
                        help="Input .xlsx or .csv with leads.")
    parser.add_argument("--output-xlsx", required=True,
                        help="Path for the comparison workbook.")
    parser.add_argument("--company-column", default="company_name")
    parser.add_argument("--domain-column", default="domain")
    parser.add_argument("--country-column", default="",
                        help="Optional input-country column.")
    parser.add_argument("--default-input-country", default="Italy")
    parser.add_argument("--anthropic-model", default=DEFAULT_ANTHROPIC_MODEL)
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL,
                        choices=list(SUPPORTED_OPENAI_MODELS))
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT,
                        help=f"Rows to compare (default {DEFAULT_ROW_LIMIT}; "
                             "keep small — 2 AI calls + 1 Serper call per row).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the run plan without any API calls.")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    input_path = Path(args.input)
    df = read_input_rows(input_path)
    if args.company_column not in df.columns:
        print(f"Company column {args.company_column!r} not found in "
              f"{input_path.name}. Columns: {list(df.columns)}", file=sys.stderr)
        return 1

    selected = select_rows(df, args.start_row, args.row_limit)

    if args.dry_run:
        print("DRY RUN — no API calls made.")
        print(f"input: {input_path}")
        print(f"rows_selected: {len(selected)} "
              f"(start_row={args.start_row}, row_limit={args.row_limit})")
        print(f"anthropic_model: {args.anthropic_model}")
        print(f"openai_model: {args.openai_model}")
        for _, row in selected.iterrows():
            print(f"  - {_cell(row.to_dict(), args.company_column)} "
                  f"({_cell(row.to_dict(), args.domain_column)})")
        return 0

    serper_key = os.environ.get("SERPER_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    for name, value in (("SERPER_API_KEY", serper_key),
                        ("ANTHROPIC_API_KEY", anthropic_key),
                        ("OPENAI_API_KEY", openai_key)):
        if not value:
            print(f"Missing environment variable: {name}", file=sys.stderr)
            return 1

    comparison = run_comparison(
        selected,
        company_column=args.company_column,
        domain_column=args.domain_column,
        country_column=args.country_column,
        default_input_country=args.default_input_country,
        anthropic_model=args.anthropic_model,
        openai_model=args.openai_model,
        serper_api_key=serper_key,
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
    )

    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        comparison.to_excel(writer, sheet_name="Provider Comparison", index=False)

    matches = int(comparison["classification_match"].sum())
    print(f"rows_compared: {len(comparison)}")
    print(f"classification_matches: {matches}/{len(comparison)}")
    print(f"output_xlsx: {args.output_xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
