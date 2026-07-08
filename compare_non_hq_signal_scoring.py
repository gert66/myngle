"""Compare deterministic keyword scoring vs AI signal scoring (Onderdeel 2).

Runs each input row's non-HQ evidence collection ONCE (no duplicate Serper
calls), then scores that SAME evidence twice -- once with the deterministic
keyword-count extractor (``lead_non_hq_signal_extractor.extract_non_hq_signals``)
and once with the AI adjudicator (``lead_ai_signal_scorer.score_signals_with_ai``)
-- and writes a per-signal, side-by-side comparison so a sample of leads can
be validated before turning ``ai_signal_scoring=True`` on more broadly.

Comparison-only tooling: it changes no scoring, C4, C5, HQ, Serper, Excel, or
Lovable export behavior, and it never runs unless invoked explicitly. Each row
costs one round of non-HQ Serper queries (via ``collect_non_hq_enrichment_evidence``,
fixed at up to 4 queries -- ``company_size_complexity`` and ``sector_industry``
are Lusha-only since Stap 3/4 and get no live Serper evidence, see
``lead_non_hq_enrichment.build_non_hq_enrichment_queries``) plus one Anthropic
call -- keep --row-limit small.

Usage:
    python compare_non_hq_signal_scoring.py \
        --input leads.xlsx --output-xlsx non_hq_signal_comparison.xlsx \
        --default-input-country Brazil --row-limit 20

    # Dry run: show which rows would run, no API calls
    python compare_non_hq_signal_scoring.py \
        --input leads.xlsx --output-xlsx out.xlsx --dry-run

Environment: SERPER_API_KEY, ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from compare_ai_providers_lead_prioritizer import _cell, read_input_rows, select_rows
from lead_ai_signal_scorer import DEFAULT_AI_SIGNAL_SCORING_MODEL, score_signals_with_ai
from lead_non_hq_enrichment import collect_non_hq_enrichment_evidence
from lead_non_hq_signal_extractor import SUPPORTED_SIGNALS, extract_non_hq_signals

DEFAULT_ROW_LIMIT = 20

SIGNAL_COMPARISON_COLUMNS = [
    "company_name", "domain", "input_country", "signal_name",
    "keyword_score", "keyword_value", "keyword_reason",
    "ai_score", "ai_value", "ai_reason",
    "score_delta", "agreement",
    "ai_call_success", "ai_error",
]

LEAD_COST_COLUMNS = [
    "company_name", "domain",
    "ai_model", "ai_input_tokens", "ai_output_tokens", "ai_total_tokens",
    "ai_estimated_cost_usd",
]


def _signals_by_name(signals) -> dict:
    return {s.signal_name: s for s in (signals or [])}


def build_lead_comparison_rows(
    *,
    company_name: str,
    domain: str,
    input_country: str,
    keyword_signals,
    ai_result,
) -> list[dict]:
    """One row per supported signal for this lead (long format -- easy to
    pivot/aggregate per signal across a whole sample).

    ``SUPPORTED_SIGNALS`` still has 5 entries (``company_size_complexity`` is
    kept in the schema on purpose, Stap 4) but only 4 receive live Serper
    evidence -- ``company_size_complexity`` rows correctly come out with
    ``keyword_score``/``ai_score`` both ``None`` and ``agreement`` blank,
    which is expected, not a bug."""
    kw_by_name = _signals_by_name(keyword_signals)
    ai_by_name = _signals_by_name(ai_result.signals if ai_result.call_success else [])

    rows = []
    for signal_name in SUPPORTED_SIGNALS:
        kw = kw_by_name.get(signal_name)
        ai = ai_by_name.get(signal_name)

        kw_score = kw.signal_score if kw else None
        ai_score = ai.signal_score if ai else None
        delta = (
            round(float(ai_score) - float(kw_score), 4)
            if kw_score is not None and ai_score is not None else None
        )
        if kw_score is None or ai_score is None:
            agreement = ""
        else:
            agreement = "same" if float(kw_score) == float(ai_score) else "different"

        rows.append({
            "company_name": company_name,
            "domain": domain or "",
            "input_country": input_country,
            "signal_name": signal_name,
            "keyword_score": kw_score,
            "keyword_value": kw.signal_value if kw else None,
            "keyword_reason": kw.signal_reason if kw else "",
            "ai_score": ai_score,
            "ai_value": ai.signal_value if ai else None,
            "ai_reason": ai.signal_reason if ai else "",
            "score_delta": delta,
            "agreement": agreement,
            "ai_call_success": ai_result.call_success,
            "ai_error": ai_result.error or "",
        })
    return rows


def run_comparison(
    df: pd.DataFrame,
    *,
    company_column: str,
    domain_column: str,
    country_column: str = "",
    default_input_country: str = "Italy",
    ai_model: str = DEFAULT_AI_SIGNAL_SCORING_MODEL,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(signal_comparison_df, cost_df)``.

    Evidence is collected exactly once per lead and fed to BOTH scoring
    paths -- neither path triggers its own Serper call, matching
    ``lead_prioritizer_core.py``'s existing Step-2/Step-3 separation.
    """
    signal_rows: list[dict] = []
    cost_rows: list[dict] = []

    for _, raw in df.iterrows():
        row = raw.to_dict()
        company = _cell(row, company_column)
        domain = _cell(row, domain_column) or None
        country = _cell(row, country_column) or default_input_country

        evidence_items = collect_non_hq_enrichment_evidence(
            company_name=company,
            domain=domain,
            serper_api_key=serper_api_key,
            country=country,
        )

        keyword_signals = extract_non_hq_signals(evidence_items, company_domain=domain)
        ai_result = score_signals_with_ai(
            company_name=company,
            country=country,
            evidence_items=evidence_items,
            anthropic_api_key=anthropic_api_key,
            ai_model=ai_model,
        )

        signal_rows.extend(build_lead_comparison_rows(
            company_name=company, domain=domain or "", input_country=country,
            keyword_signals=keyword_signals, ai_result=ai_result,
        ))
        cost_rows.append({
            "company_name": company,
            "domain": domain or "",
            "ai_model": ai_result.model,
            "ai_input_tokens": ai_result.input_tokens,
            "ai_output_tokens": ai_result.output_tokens,
            "ai_total_tokens": ai_result.total_tokens,
            "ai_estimated_cost_usd": ai_result.estimated_cost_usd,
        })

    signal_df = pd.DataFrame(signal_rows, columns=SIGNAL_COMPARISON_COLUMNS)
    cost_df = pd.DataFrame(cost_rows, columns=LEAD_COST_COLUMNS)
    return signal_df, cost_df


def build_signal_agreement_summary(signal_df: pd.DataFrame) -> pd.DataFrame:
    """Per-signal agreement rate and mean |delta| -- the key "is the AI
    upgrade worth it" table, aggregated across the whole sample."""
    rows = []
    for signal_name in SUPPORTED_SIGNALS:
        sub = signal_df[signal_df["signal_name"] == signal_name]
        judged = sub[sub["agreement"] != ""]
        n_judged = len(judged)
        n_same = int((judged["agreement"] == "same").sum()) if n_judged else 0
        deltas = judged["score_delta"].dropna()
        rows.append({
            "signal_name": signal_name,
            "leads_compared": len(sub),
            "leads_judged_by_both": n_judged,
            "agreement_rate": round(n_same / n_judged, 4) if n_judged else None,
            "mean_abs_delta": round(deltas.abs().mean(), 4) if len(deltas) else None,
            "ai_call_failures": int((sub["ai_call_success"] == False).sum()),  # noqa: E712
        })
    return pd.DataFrame(rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a sample of leads through deterministic keyword scoring and "
            "AI signal scoring on the SAME evidence, and write a per-signal "
            "comparison Excel."
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
    parser.add_argument("--ai-model", default=DEFAULT_AI_SIGNAL_SCORING_MODEL)
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT,
                        help=f"Rows to compare (default {DEFAULT_ROW_LIMIT}; "
                             "keep small -- one non-HQ Serper round + one "
                             "Anthropic call per row).")
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
        print(f"ai_model: {args.ai_model}")
        for _, row in selected.iterrows():
            print(f"  - {_cell(row.to_dict(), args.company_column)} "
                  f"({_cell(row.to_dict(), args.domain_column)})")
        return 0

    serper_key = os.environ.get("SERPER_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    for name, value in (("SERPER_API_KEY", serper_key),
                        ("ANTHROPIC_API_KEY", anthropic_key)):
        if not value:
            print(f"Missing environment variable: {name}", file=sys.stderr)
            return 1

    signal_df, cost_df = run_comparison(
        selected,
        company_column=args.company_column,
        domain_column=args.domain_column,
        country_column=args.country_column,
        default_input_country=args.default_input_country,
        ai_model=args.ai_model,
        serper_api_key=serper_key,
        anthropic_api_key=anthropic_key,
    )
    summary_df = build_signal_agreement_summary(signal_df)

    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Signal Agreement Summary", index=False)
        signal_df.to_excel(writer, sheet_name="Per-Signal Comparison", index=False)
        cost_df.to_excel(writer, sheet_name="Cost per Lead", index=False)

    n_leads = len(selected)
    total_cost = cost_df["ai_estimated_cost_usd"].dropna().sum()
    print(f"leads_compared: {n_leads}")
    print(summary_df.to_string(index=False))
    print(f"total_estimated_ai_cost_usd: {round(float(total_cost), 6) if len(cost_df['ai_estimated_cost_usd'].dropna()) else 'unknown (model unpriced)'}")
    print(f"output_xlsx: {args.output_xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
