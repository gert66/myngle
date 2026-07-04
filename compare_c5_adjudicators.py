"""Compare C5 HQ adjudicators: Sonnet vs OpenAI mini vs DeepSeek Pro.

Comparison-only tooling. It never runs unless invoked explicitly, and it never
touches production C5: ``adjudicate_hq_with_sonnet`` / ``build_c5_recommendation``
from ``lead_hq_sonnet_adjudicator.py`` are reused completely unchanged. This
script only adds two OpenAI-compatible adjudicator calls (OpenAI mini,
DeepSeek Pro) that ask the exact same C5 target-identity question, using the
same system prompt, user prompt, JSON schema, parser, and recommendation
mapper — so a side-by-side comparison is apples-to-apples.

Sonnet cost note: ``adjudicate_hq_with_sonnet`` does not currently return
token usage (adding that would mean touching the production C5 function,
which this script must not do). Sonnet's token/cost columns are therefore
always blank here — not a guess, just genuinely unavailable without changing
production behavior. OpenAI mini and DeepSeek Pro usage/cost use
``MODEL_PRICING_USD_PER_MTOK`` / ``estimate_ai_cost_usd`` from
``lead_hq_ai_interpreter.py`` (no duplicated pricing table).

Usage:
    python compare_c5_adjudicators.py \
        --input lead_prioritizer_output.xlsx --output-xlsx c5_comparison.xlsx \
        --row-limit 5

    # Dry run: show which rows/models would run, no API calls
    python compare_c5_adjudicators.py \
        --input lead_prioritizer_output.xlsx --output-xlsx out.xlsx --dry-run

Environment: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

try:
    import openai as _openai_lib
except ImportError:  # pragma: no cover
    _openai_lib = None  # type: ignore[assignment]

from lead_hq_ai_interpreter import MODEL_PRICING_USD_PER_MTOK, estimate_ai_cost_usd
from lead_hq_sonnet_adjudicator import (
    DEFAULT_SONNET_ADJUDICATION_MODEL,
    SonnetHQAdjudicationResult,
    adjudicate_hq_with_sonnet,
    build_adjudication_prompt,
    build_c5_recommendation,
    _ADJUDICATIONS,
    _CONFIDENCES,
    _MATCHES,
    _SYSTEM_PROMPT as C5_SYSTEM_PROMPT,
    _norm_enum,
    _parse_adjudication_response,
)

DEFAULT_ROW_LIMIT = 5
DEFAULT_OPENAI_MINI_MODEL = "gpt-5.4-mini"
DEFAULT_DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

PROVIDER_PREFIXES = ("sonnet", "openai_mini", "deepseek_pro")

BASE_COLUMNS = [
    "source_index", "company_name", "domain", "input_country",
    "base_hq_detected_country", "base_ai_parent_company",
    "base_ai_parent_hq_country", "base_hq_reason", "base_hq_evidence_url",
]

_PROVIDER_FIELD_SUFFIXES = [
    "adjudication", "confidence", "target_company_match", "parent_company",
    "parent_hq_country", "parent_hq_city", "recommended_hq_score",
    "recommended_manual_review", "recommendation_reason", "model",
    "call_attempted", "call_success", "error", "input_tokens",
    "output_tokens", "total_tokens", "estimated_cost_usd", "reason",
]

COMPARISON_FLAG_COLUMNS = [
    "sonnet_openai_mini_match", "sonnet_deepseek_pro_match",
    "openai_mini_deepseek_pro_match", "any_adjudication_disagreement",
    "any_score_disagreement", "any_parent_country_disagreement",
]

C5_COMPARISON_COLUMNS = (
    BASE_COLUMNS
    + [f"{prefix}_{suffix}" for prefix in PROVIDER_PREFIXES
       for suffix in _PROVIDER_FIELD_SUFFIXES]
    + COMPARISON_FLAG_COLUMNS
)

COST_SUMMARY_COLUMNS = [
    "provider", "model", "rows_compared", "input_tokens", "output_tokens",
    "total_tokens", "estimated_cost_usd", "cost_per_company_usd",
    "estimated_cost_100_companies_usd", "estimated_cost_1000_companies_usd",
    "estimated_cost_10000_companies_usd",
]


# ---------------------------------------------------------------------------
# Input reading / row selection
# ---------------------------------------------------------------------------

def read_c5_input_workbook(input_path: Path, sheet: "str | None" = None) -> tuple:
    """Read the input workbook. Prefers "Enriched Leads"; falls back to an
    explicit ``--sheet`` (if present) or the first sheet. Returns
    ``(dataframe, sheet_name_used)``."""
    xls = pd.ExcelFile(input_path)
    if sheet and sheet in xls.sheet_names:
        used = sheet
    elif "Enriched Leads" in xls.sheet_names:
        used = "Enriched Leads"
    else:
        used = xls.sheet_names[0]
    return xls.parse(used), used


def select_rows(df: pd.DataFrame, start_row: int, row_limit: int) -> pd.DataFrame:
    """Same selection semantics as the batch core: offset + limit (0 = all)."""
    sub = df.iloc[max(0, int(start_row)):]
    if row_limit and int(row_limit) > 0:
        sub = sub.iloc[:int(row_limit)]
    return sub


def _first(row: dict, *keys) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def resolve_c5_input_row(
    row: dict,
    *,
    company_column: str = "company_name",
    domain_column: str = "domain",
    country_column: str = "input_country",
) -> dict:
    """Map one workbook row to the base C5 input fields.

    Accepts either a normal Enriched Leads row or a provider-comparison
    workbook row (obvious equivalents like ``anthropic_parent_hq_country`` /
    ``openai_mini_parent_hq_country`` / ``deepseek_parent_hq_country`` are
    used as fallbacks for the parent-HQ-country field). Missing fields
    become blank strings rather than raising; only company_name/domain are
    expected to resolve to something usable.
    """
    company_name = _first(row, company_column, "company_name")
    domain = _first(row, domain_column, "domain")
    input_country = _first(row, country_column, "input_country")

    parent_hq_country = _first(
        row, "ai_parent_hq_country", "anthropic_parent_hq_country",
        "openai_mini_parent_hq_country", "deepseek_parent_hq_country",
    )
    reason_bits = [
        _first(row, "hq_reason"),
        _first(row, "anthropic_hq_classification"),
        _first(row, "anthropic_ai_error"),
    ]
    hq_reason = " | ".join(bit for bit in reason_bits if bit)

    return {
        "company_name": company_name,
        "domain": domain,
        "input_country": input_country,
        "hq_detected_country": _first(row, "hq_detected_country") or parent_hq_country,
        "hq_detected_city": _first(row, "hq_detected_city"),
        "ai_parent_company": _first(row, "ai_parent_company"),
        "ai_parent_hq_country": parent_hq_country,
        "ai_parent_hq_city": _first(row, "ai_parent_hq_city"),
        "hq_evidence_url": _first(row, "hq_evidence_url"),
        "hq_evidence_quote": _first(row, "hq_evidence_quote"),
        "hq_reason": hq_reason,
    }


# ---------------------------------------------------------------------------
# OpenAI-compatible adjudicator (OpenAI mini, DeepSeek Pro) — comparison only.
# Reuses the C5 system prompt, user prompt builder, parser, enums, and result
# dataclass shape from lead_hq_sonnet_adjudicator.py unchanged.
# ---------------------------------------------------------------------------

def _usage_field(usage_obj, *names):
    """First present integer attribute from a provider usage object, or None."""
    for name in names:
        value = getattr(usage_obj, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _call_openai_compatible_c5(
    api_key: str,
    model: str,
    prompt: str,
    *,
    base_url: "str | None" = None,
    max_tokens_kwarg: str = "max_completion_tokens",
) -> tuple:
    """One OpenAI-compatible chat-completions call using the C5 system
    prompt. Works for both OpenAI (default base_url) and DeepSeek
    (base_url="https://api.deepseek.com") since DeepSeek exposes an
    OpenAI-compatible API. Returns ``(raw_text, usage)``; raises on failure.
    """
    if _openai_lib is None:
        raise ImportError("openai package not installed")
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = _openai_lib.OpenAI(**client_kwargs)
    response = client.chat.completions.create(**{
        "model": model,
        max_tokens_kwarg: 512,
        "messages": [
            {"role": "system", "content": C5_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    })
    raw_text = (
        response.choices[0].message.content if getattr(response, "choices", None)
        else ""
    ) or ""
    usage_obj = getattr(response, "usage", None)
    input_tokens = _usage_field(usage_obj, "prompt_tokens", "input_tokens")
    output_tokens = _usage_field(usage_obj, "completion_tokens", "output_tokens")
    total = _usage_field(usage_obj, "total_tokens")
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    return raw_text, {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": total,
    }


def adjudicate_with_openai_compatible(
    *,
    company_name: str,
    domain: str,
    input_country: str,
    hq_detected_country: str = "",
    hq_detected_city: str = "",
    ai_parent_company: str = "",
    ai_parent_hq_country: str = "",
    ai_parent_hq_city: str = "",
    hq_evidence_url: str = "",
    hq_evidence_quote: str = "",
    hq_reason: str = "",
    api_key: str = "",
    model: str,
    base_url: "str | None" = None,
    max_tokens_kwarg: str = "max_completion_tokens",
    include_raw: bool = False,
) -> tuple:
    """Ask an OpenAI-compatible provider the same C5 target-identity question
    as Sonnet, using the identical prompt/parser/enum normalization. Returns
    ``(SonnetHQAdjudicationResult, usage_and_cost_dict)``. Never raises.
    """
    if not api_key:
        result = SonnetHQAdjudicationResult(
            model=model, call_attempted=False, call_success=False,
            error="no_api_key",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="ai_not_eligible: no API key",
        )
        return result, {"input_tokens": None, "output_tokens": None,
                        "total_tokens": None, "estimated_cost_usd": None}

    prompt = build_adjudication_prompt(
        company_name=company_name, domain=domain, input_country=input_country,
        hq_detected_country=hq_detected_country, hq_detected_city=hq_detected_city,
        ai_parent_company=ai_parent_company, ai_parent_hq_country=ai_parent_hq_country,
        ai_parent_hq_city=ai_parent_hq_city, hq_evidence_url=hq_evidence_url,
        hq_evidence_quote=hq_evidence_quote, hq_reason=hq_reason,
    )

    try:
        raw_text, usage = _call_openai_compatible_c5(
            api_key, model, prompt, base_url=base_url,
            max_tokens_kwarg=max_tokens_kwarg)
    except Exception as exc:
        result = SonnetHQAdjudicationResult(
            model=model, call_attempted=True, call_success=False,
            error=f"call_failed: {str(exc)[:200]}",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="ai_error",
        )
        return result, {"input_tokens": None, "output_tokens": None,
                        "total_tokens": None, "estimated_cost_usd": None}

    cost = estimate_ai_cost_usd(model, usage.get("input_tokens"), usage.get("output_tokens"))
    usage_with_cost = {**usage, "estimated_cost_usd": cost}

    data = _parse_adjudication_response(raw_text)
    if not data:
        result = SonnetHQAdjudicationResult(
            model=model, call_attempted=True, call_success=False,
            error="parse_failed",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="unparseable_response",
            raw_json=(raw_text or "")[:2000] if include_raw else None,
        )
        return result, usage_with_cost

    result = SonnetHQAdjudicationResult(
        model=model, call_attempted=True, call_success=True, error="",
        adjudication=_norm_enum(data.get("adjudication"), _ADJUDICATIONS, "unclear"),
        confidence=_norm_enum(data.get("confidence"), _CONFIDENCES, "Low"),
        target_company_match=_norm_enum(data.get("target_company_match"), _MATCHES, "unclear"),
        parent_company=str(data.get("parent_company") or "").strip(),
        parent_hq_country=str(data.get("parent_hq_country") or "").strip(),
        parent_hq_city=str(data.get("parent_hq_city") or "").strip(),
        reason=str(data.get("reason") or "").strip(),
        raw_json=(raw_text or "")[:2000] if include_raw else None,
    )
    return result, usage_with_cost


# ---------------------------------------------------------------------------
# Comparison row builder
# ---------------------------------------------------------------------------

def _provider_fields(prefix: str, result: SonnetHQAdjudicationResult,
                     rec: dict, usage: dict) -> dict:
    return {
        f"{prefix}_adjudication": result.adjudication,
        f"{prefix}_confidence": result.confidence,
        f"{prefix}_target_company_match": result.target_company_match,
        f"{prefix}_parent_company": result.parent_company,
        f"{prefix}_parent_hq_country": result.parent_hq_country,
        f"{prefix}_parent_hq_city": result.parent_hq_city,
        f"{prefix}_recommended_hq_score": rec["c5_recommended_hq_score"],
        f"{prefix}_recommended_manual_review": rec["c5_recommended_manual_review"],
        f"{prefix}_recommendation_reason": rec["c5_recommendation_reason"],
        f"{prefix}_model": result.model,
        f"{prefix}_call_attempted": result.call_attempted,
        f"{prefix}_call_success": result.call_success,
        f"{prefix}_error": result.error,
        f"{prefix}_input_tokens": usage.get("input_tokens"),
        f"{prefix}_output_tokens": usage.get("output_tokens"),
        f"{prefix}_total_tokens": usage.get("total_tokens"),
        f"{prefix}_estimated_cost_usd": usage.get("estimated_cost_usd"),
        f"{prefix}_reason": result.reason,
    }


def build_c5_comparison_row(
    source_index,
    base: dict,
    sonnet_result: SonnetHQAdjudicationResult, sonnet_rec: dict, sonnet_usage: dict,
    mini_result: SonnetHQAdjudicationResult, mini_rec: dict, mini_usage: dict,
    deepseek_result: SonnetHQAdjudicationResult, deepseek_rec: dict, deepseek_usage: dict,
) -> dict:
    """Flatten three per-adjudicator results into one comparison record."""
    row = {
        "source_index": source_index,
        "company_name": base.get("company_name", ""),
        "domain": base.get("domain", ""),
        "input_country": base.get("input_country", ""),
        "base_hq_detected_country": base.get("hq_detected_country", ""),
        "base_ai_parent_company": base.get("ai_parent_company", ""),
        "base_ai_parent_hq_country": base.get("ai_parent_hq_country", ""),
        "base_hq_reason": base.get("hq_reason", ""),
        "base_hq_evidence_url": base.get("hq_evidence_url", ""),
    }
    row.update(_provider_fields("sonnet", sonnet_result, sonnet_rec, sonnet_usage))
    row.update(_provider_fields("openai_mini", mini_result, mini_rec, mini_usage))
    row.update(_provider_fields("deepseek_pro", deepseek_result, deepseek_rec, deepseek_usage))

    s_adj, m_adj, d_adj = (sonnet_result.adjudication, mini_result.adjudication,
                          deepseek_result.adjudication)
    sonnet_mini_match = s_adj == m_adj
    sonnet_deepseek_match = s_adj == d_adj
    mini_deepseek_match = m_adj == d_adj

    scores = {sonnet_rec["c5_recommended_hq_score"], mini_rec["c5_recommended_hq_score"],
             deepseek_rec["c5_recommended_hq_score"]}
    countries = {
        (sonnet_result.parent_hq_country or "").strip().lower(),
        (mini_result.parent_hq_country or "").strip().lower(),
        (deepseek_result.parent_hq_country or "").strip().lower(),
    } - {""}

    row.update({
        "sonnet_openai_mini_match": sonnet_mini_match,
        "sonnet_deepseek_pro_match": sonnet_deepseek_match,
        "openai_mini_deepseek_pro_match": mini_deepseek_match,
        "any_adjudication_disagreement": not (
            sonnet_mini_match and sonnet_deepseek_match and mini_deepseek_match),
        "any_score_disagreement": len(scores) > 1,
        "any_parent_country_disagreement": len(countries) > 1,
    })
    return row


def run_c5_comparison(
    df: pd.DataFrame,
    *,
    company_column: str = "company_name",
    domain_column: str = "domain",
    country_column: str = "input_country",
    anthropic_api_key: str = "",
    openai_api_key: str = "",
    deepseek_api_key: str = "",
    sonnet_model: str = DEFAULT_SONNET_ADJUDICATION_MODEL,
    openai_mini_model: str = DEFAULT_OPENAI_MINI_MODEL,
    deepseek_pro_model: str = DEFAULT_DEEPSEEK_PRO_MODEL,
    include_raw: bool = False,
    sonnet_fn=adjudicate_hq_with_sonnet,
    openai_compatible_fn=adjudicate_with_openai_compatible,
) -> pd.DataFrame:
    """Run each row through all three C5 adjudicators and return the
    comparison DataFrame.

    ``sonnet_fn`` / ``openai_compatible_fn`` are injectable for tests; live
    runs use the real ``adjudicate_hq_with_sonnet`` (production, unchanged)
    and ``adjudicate_with_openai_compatible`` (this module).
    """
    records = []
    for source_index, raw in df.iterrows():
        row = raw.to_dict()
        base = resolve_c5_input_row(
            row, company_column=company_column, domain_column=domain_column,
            country_column=country_column)

        sonnet_result = sonnet_fn(
            company_name=base["company_name"], domain=base["domain"],
            input_country=base["input_country"],
            hq_detected_country=base["hq_detected_country"],
            hq_detected_city=base["hq_detected_city"],
            ai_parent_company=base["ai_parent_company"],
            ai_parent_hq_country=base["ai_parent_hq_country"],
            ai_parent_hq_city=base["ai_parent_hq_city"],
            hq_evidence_url=base["hq_evidence_url"],
            hq_evidence_quote=base["hq_evidence_quote"],
            hq_reason=base["hq_reason"],
            anthropic_api_key=anthropic_api_key,
            model=sonnet_model,
            include_raw=include_raw,
        )
        sonnet_rec = build_c5_recommendation(sonnet_result)
        # Sonnet token/cost intentionally blank — see module docstring.
        sonnet_usage = {"input_tokens": None, "output_tokens": None,
                        "total_tokens": None, "estimated_cost_usd": None}

        mini_result, mini_usage = openai_compatible_fn(
            company_name=base["company_name"], domain=base["domain"],
            input_country=base["input_country"],
            hq_detected_country=base["hq_detected_country"],
            hq_detected_city=base["hq_detected_city"],
            ai_parent_company=base["ai_parent_company"],
            ai_parent_hq_country=base["ai_parent_hq_country"],
            ai_parent_hq_city=base["ai_parent_hq_city"],
            hq_evidence_url=base["hq_evidence_url"],
            hq_evidence_quote=base["hq_evidence_quote"],
            hq_reason=base["hq_reason"],
            api_key=openai_api_key,
            model=openai_mini_model,
            base_url=None,
            max_tokens_kwarg="max_completion_tokens",
            include_raw=include_raw,
        )
        mini_rec = build_c5_recommendation(mini_result)

        deepseek_result, deepseek_usage = openai_compatible_fn(
            company_name=base["company_name"], domain=base["domain"],
            input_country=base["input_country"],
            hq_detected_country=base["hq_detected_country"],
            hq_detected_city=base["hq_detected_city"],
            ai_parent_company=base["ai_parent_company"],
            ai_parent_hq_country=base["ai_parent_hq_country"],
            ai_parent_hq_city=base["ai_parent_hq_city"],
            hq_evidence_url=base["hq_evidence_url"],
            hq_evidence_quote=base["hq_evidence_quote"],
            hq_reason=base["hq_reason"],
            api_key=deepseek_api_key,
            model=deepseek_pro_model,
            base_url=DEEPSEEK_BASE_URL,
            max_tokens_kwarg="max_tokens",
            include_raw=include_raw,
        )
        deepseek_rec = build_c5_recommendation(deepseek_result)

        records.append(build_c5_comparison_row(
            source_index, base,
            sonnet_result, sonnet_rec, sonnet_usage,
            mini_result, mini_rec, mini_usage,
            deepseek_result, deepseek_rec, deepseek_usage,
        ))

    return pd.DataFrame(records, columns=C5_COMPARISON_COLUMNS)


# ---------------------------------------------------------------------------
# Cost summary (audit only — never affects scoring, HQ, or C5 behavior)
# ---------------------------------------------------------------------------

def build_c5_cost_summary(comparison_df: pd.DataFrame) -> pd.DataFrame:
    """One cost row per adjudicator, with projected cost at 100/1,000/10,000
    companies. Sonnet's cost stays blank (see module docstring); a provider
    with no known cost still gets a row — never a guessed cost."""
    n_rows = len(comparison_df)
    rows = []
    for prefix in PROVIDER_PREFIXES:
        model_col = f"{prefix}_model"
        model_name = ""
        if model_col in comparison_df.columns:
            names = comparison_df[model_col].dropna()
            names = names[names != ""]
            if len(names):
                model_name = str(names.iloc[0])

        in_col, out_col, total_col, cost_col = (
            f"{prefix}_input_tokens", f"{prefix}_output_tokens",
            f"{prefix}_total_tokens", f"{prefix}_estimated_cost_usd",
        )
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

        def _projection(count, per_company=cost_per_company):
            return round(per_company * count, 2) if per_company is not None else None

        rows.append({
            "provider": prefix,
            "model": model_name,
            "rows_compared": n_rows,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": total_cost,
            "cost_per_company_usd": cost_per_company,
            "estimated_cost_100_companies_usd": _projection(100),
            "estimated_cost_1000_companies_usd": _projection(1000),
            "estimated_cost_10000_companies_usd": _projection(10000),
        })
    return pd.DataFrame(rows, columns=COST_SUMMARY_COLUMNS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare C5 HQ adjudicators (Sonnet vs OpenAI mini vs DeepSeek "
            "Pro) on the same rows and write a comparison Excel."
        )
    )
    parser.add_argument("--input", required=True,
                        help="Excel workbook (Enriched Leads or a provider "
                             "comparison workbook).")
    parser.add_argument("--output-xlsx", required=True,
                        help="Path for the C5 comparison workbook.")
    parser.add_argument("--sheet", default="",
                        help="Explicit sheet name (default: prefer "
                             "'Enriched Leads', else the first sheet).")
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT,
                        help=f"Rows to compare (default {DEFAULT_ROW_LIMIT}; "
                             "keep small — 3 AI calls per row).")
    parser.add_argument("--company-column", default="company_name")
    parser.add_argument("--domain-column", default="domain")
    parser.add_argument("--country-column", default="input_country")
    parser.add_argument("--include-raw", action="store_true", default=False,
                        help="Include truncated raw model JSON per adjudicator.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the run plan without any API calls.")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    input_path = Path(args.input)
    df, sheet_used = read_c5_input_workbook(input_path, args.sheet or None)
    selected = select_rows(df, args.start_row, args.row_limit)

    if args.dry_run:
        print("DRY RUN — no API calls made.")
        print(f"input: {input_path}")
        print(f"sheet_used: {sheet_used}")
        print(f"rows_selected: {len(selected)} "
              f"(start_row={args.start_row}, row_limit={args.row_limit})")
        print(f"sonnet_model: {DEFAULT_SONNET_ADJUDICATION_MODEL}")
        print(f"openai_mini_model: {DEFAULT_OPENAI_MINI_MODEL}")
        print(f"deepseek_pro_model: {DEFAULT_DEEPSEEK_PRO_MODEL}")
        for _, row in selected.iterrows():
            base = resolve_c5_input_row(
                row.to_dict(), company_column=args.company_column,
                domain_column=args.domain_column, country_column=args.country_column)
            print(f"  - {base['company_name']} ({base['domain']})")
        return 0

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    for name, value in (("ANTHROPIC_API_KEY", anthropic_key),
                        ("OPENAI_API_KEY", openai_key),
                        ("DEEPSEEK_API_KEY", deepseek_key)):
        if not value:
            print(f"Missing environment variable: {name}", file=sys.stderr)
            return 1

    comparison = run_c5_comparison(
        selected,
        company_column=args.company_column,
        domain_column=args.domain_column,
        country_column=args.country_column,
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
        deepseek_api_key=deepseek_key,
        include_raw=args.include_raw,
    )
    cost_summary = build_c5_cost_summary(comparison)

    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        comparison.to_excel(writer, sheet_name="C5 Comparison", index=False)
        cost_summary.to_excel(writer, sheet_name="Cost Summary", index=False)

    disagreements = int(comparison["any_adjudication_disagreement"].sum())
    print(f"rows_compared: {len(comparison)}")
    print(f"adjudication_disagreements: {disagreements}/{len(comparison)}")
    print("sonnet_cost_note: Sonnet token/cost tracking is intentionally "
          "blank (adjudicate_hq_with_sonnet is unchanged) — see Cost Summary.")
    print(f"output_xlsx: {args.output_xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
