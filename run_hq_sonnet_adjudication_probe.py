"""C5 — run the Sonnet HQ adjudication probe over an HQ-only output workbook.

Reads an existing Lead Prioritizer v2 HQ-only workbook (default sheet
"Enriched Leads"), asks Sonnet the target-identity question per selected row,
and writes a NEW workbook with C5 adjudication + proposed-recommendation columns
appended. It never alters the input workbook, never calls Serper, and never
writes API keys.

Standalone / removable: uses only ``lead_hq_sonnet_adjudicator`` and reuses the
secret-safe key loader from ``run_v2_single_lead_validation``.

Usage:
    python run_hq_sonnet_adjudication_probe.py \
        --input lead_prioritizer_v2_hq_only_enriched.xlsx \
        --output lead_prioritizer_v2_hq_only_c5_adjudicated.xlsx \
        --sheet "Enriched Leads" --start-row 0 --row-limit 10 --only-manual-review
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import pandas as pd

from lead_hq_sonnet_adjudicator import (
    adjudicate_hq_with_sonnet,
    build_c5_recommendation,
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    C5_MODEL_TIERS,
    C5_MODEL_TIER_CHOICES,
)
from run_v2_single_lead_validation import load_api_keys, ANTHROPIC_KEY_NAME

_DEFAULT_SHEET = "Enriched Leads"
_OPUS_ROW_LIMIT_SOFT_CAP = 10

_OPUS_WARNING = (
    "WARNING: Opus is significantly more expensive than Sonnet. Use only for "
    "small manual probes. Recommended row_limit <= 10."
)


def resolve_c5_model(model_tier: str, model: Optional[str]):
    """Resolve the model to use. Returns (model_used, error_message).

    - explicit --model overrides the tier;
    - sonnet tier with no --model uses the baked Sonnet default;
    - opus tier with no --model is rejected (Opus ID must be explicit).
    """
    explicit = (model or "").strip()
    if explicit:
        return explicit, None
    baked = C5_MODEL_TIERS.get(model_tier)
    if baked:
        return baked, None
    return None, (
        f"--model-tier {model_tier} requires an explicit --model: the Opus API "
        "model ID must be supplied explicitly (no default is baked in). "
        "Pass e.g. --model <opus-model-id>."
    )


def check_opus_guardrail(model_tier: str, row_limit: int, confirm_expensive_opus: bool):
    """Return an error message when an Opus run needs explicit confirmation.

    Opus with row_limit 0 (all) or > soft cap requires --confirm-expensive-opus.
    """
    if model_tier != "opus":
        return None
    rl = int(row_limit)
    if (rl == 0 or rl > _OPUS_ROW_LIMIT_SOFT_CAP) and not confirm_expensive_opus:
        shown = "0 (all rows)" if rl == 0 else str(rl)
        return (
            f"Opus with row_limit {shown} requires --confirm-expensive-opus. "
            f"Recommended row_limit <= {_OPUS_ROW_LIMIT_SOFT_CAP}."
        )
    return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("yes", "true", "1")


def filter_probe_rows(
    df: pd.DataFrame,
    only_manual_review: bool,
    only_suppressed: bool,
    start_row: int,
    row_limit: int,
) -> pd.DataFrame:
    """Apply the C5 row filters, then start_row / row_limit (0 = all remaining).

    Preserves the original DataFrame index.
    """
    sub = df
    if only_manual_review and "needs_manual_review" in sub.columns:
        sub = sub[sub["needs_manual_review"].apply(_truthy)]
    if only_suppressed and "hq_positive_score_suppressed_for_review" in sub.columns:
        sub = sub[sub["hq_positive_score_suppressed_for_review"].apply(_truthy)]
    start = max(0, int(start_row))
    sub = sub.iloc[start:]
    if row_limit and int(row_limit) > 0:
        sub = sub.iloc[: int(row_limit)]
    return sub


def _cell(row: dict, key: str) -> str:
    val = row.get(key)
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() == "nan" else s


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="C5 Sonnet HQ adjudication probe.")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--sheet", default=_DEFAULT_SHEET)
    p.add_argument("--start-row", type=int, default=0)
    p.add_argument("--row-limit", type=int, default=10)
    p.add_argument("--model-tier", choices=list(C5_MODEL_TIER_CHOICES), default="sonnet",
                   help="Model tier: sonnet (default) or opus (expensive; requires --model).")
    p.add_argument("--model", default=None,
                   help="Explicit model ID. Overrides --model-tier. Required for opus tier.")
    p.add_argument("--confirm-expensive-opus", action="store_true",
                   help="Confirm an Opus run with row_limit 0 or > 10.")
    p.add_argument("--only-manual-review", action="store_true")
    p.add_argument("--only-suppressed", action="store_true")
    p.add_argument("--secrets-file", default=None)
    p.add_argument("--include-raw-json", action="store_true")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # ── Resolve model (tier + explicit override) ──────────────────────────────
    model_used, model_error = resolve_c5_model(args.model_tier, args.model)
    if model_error:
        print(f"ERROR: {model_error}", file=sys.stderr)
        return 2

    # ── Opus cost guardrail ───────────────────────────────────────────────────
    if args.model_tier == "opus":
        print(_OPUS_WARNING, file=sys.stderr)
    opus_error = check_opus_guardrail(
        args.model_tier, args.row_limit, args.confirm_expensive_opus)
    if opus_error:
        print(f"ERROR: {opus_error}", file=sys.stderr)
        return 2

    keys = load_api_keys(secrets_file=args.secrets_file)
    anthropic_key = keys.get(ANTHROPIC_KEY_NAME, "")
    print(f"{ANTHROPIC_KEY_NAME}: {'set' if anthropic_key else 'MISSING'}", file=sys.stderr)
    if not anthropic_key:
        print("ERROR: ANTHROPIC_API_KEY missing. Set env var or --secrets-file.", file=sys.stderr)
        return 2

    try:
        xls = pd.ExcelFile(args.input)
    except Exception as exc:
        print(f"ERROR: cannot read workbook: {exc}", file=sys.stderr)
        return 2
    sheet = args.sheet if args.sheet in xls.sheet_names else xls.sheet_names[0]
    df = xls.parse(sheet)

    selected = filter_probe_rows(
        df, args.only_manual_review, args.only_suppressed, args.start_row, args.row_limit)

    print(f"Input      : {args.input}", file=sys.stderr)
    print(f"Sheet      : {sheet}", file=sys.stderr)
    print(f"Total rows : {len(df)}", file=sys.stderr)
    print(f"Selected   : {len(selected)}", file=sys.stderr)
    print(f"Model tier : {args.model_tier}", file=sys.stderr)
    print(f"Model used : {model_used}", file=sys.stderr)

    out_rows: list[dict] = []
    n_foreign = n_domestic = n_unclear = n_review = 0
    for idx, row in selected.iterrows():
        original = row.to_dict()
        result = adjudicate_hq_with_sonnet(
            company_name=_cell(original, "company_name"),
            domain=_cell(original, "domain"),
            input_country=_cell(original, "input_country"),
            hq_detected_country=_cell(original, "hq_detected_country"),
            hq_detected_city=_cell(original, "hq_detected_city"),
            ai_parent_company=_cell(original, "ai_parent_company"),
            ai_parent_hq_country=_cell(original, "ai_parent_hq_country"),
            ai_parent_hq_city=_cell(original, "ai_parent_hq_city"),
            hq_evidence_url=_cell(original, "hq_evidence_url"),
            hq_evidence_quote=_cell(original, "hq_evidence_quote"),
            hq_reason=_cell(original, "hq_reason"),
            anthropic_api_key=anthropic_key,
            model=model_used,
            include_raw=args.include_raw_json,
        )
        rec = build_c5_recommendation(result)

        enriched = dict(original)
        enriched.update({
            "source_index": idx,
            "c5_adjudication": result.adjudication,
            "c5_confidence": result.confidence,
            "c5_target_company_match": result.target_company_match,
            "c5_parent_company": result.parent_company,
            "c5_parent_hq_country": result.parent_hq_country,
            "c5_parent_hq_city": result.parent_hq_city,
            "c5_reason": result.reason,
            "c5_sonnet_model": result.model,   # kept for backwards compatibility
            "c5_model_used": model_used,        # preferred general column
            "c5_model_tier": args.model_tier,
            "c5_call_attempted": result.call_attempted,
            "c5_call_success": result.call_success,
            "c5_error": result.error,
            "c5_recommended_hq_score": rec["c5_recommended_hq_score"],
            "c5_recommended_manual_review": rec["c5_recommended_manual_review"],
            "c5_recommendation_reason": rec["c5_recommendation_reason"],
        })
        if args.include_raw_json:
            enriched["c5_raw_json"] = result.raw_json
        out_rows.append(enriched)

        if result.adjudication == "foreign_parent_confirmed":
            n_foreign += 1
        elif result.adjudication == "domestic_confirmed":
            n_domestic += 1
        else:
            n_unclear += 1
        if rec["c5_recommended_manual_review"]:
            n_review += 1

    out_df = pd.DataFrame(out_rows)
    summary = pd.DataFrame([{
        "input_file": args.input,
        "sheet": sheet,
        "sonnet_model": model_used,   # kept for backwards compatibility
        "c5_model_used": model_used,
        "c5_model_tier": args.model_tier,
        "confirm_expensive_opus": args.confirm_expensive_opus,
        "total_rows": len(df),
        "selected_rows": len(selected),
        "adjudicated_rows": len(out_rows),
        "foreign_parent_confirmed": n_foreign,
        "domestic_confirmed": n_domestic,
        "unclear": n_unclear,
        "recommended_manual_review": n_review,
        "only_manual_review": args.only_manual_review,
        "only_suppressed": args.only_suppressed,
    }])

    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        out_df.to_excel(writer, sheet_name="C5 Adjudication", index=False)
        summary.to_excel(writer, sheet_name="C5 Summary", index=False)

    print(f"Output written: {args.output}", file=sys.stderr)
    print(f"foreign={n_foreign} domestic={n_domestic} unclear={n_unclear} "
          f"review={n_review}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
