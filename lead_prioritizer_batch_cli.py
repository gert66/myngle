"""Command-line batch runner for Lead Prioritizer v2.

Thin CLI on top of the shared batch core (`lead_prioritizer_batch_core.py`).
It adds no enrichment logic and does not duplicate batch logic — it only reads
an Excel file, maps columns, runs the selected mode via the core, and writes an
enriched workbook.

Secret hygiene: API keys are read from the environment (then an optional
``--secrets-file`` fallback), passed straight to the core, and never printed or
written to output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from lead_prioritizer_batch_core import (
    BatchRunConfig,
    SUPPORTED_RUN_MODES,
    build_excel_workbook_bytes,
    run_batch_dataframe,
    select_batch_rows,
)
# Reuse the validated, secret-safe key loader instead of duplicating it.
from run_v2_single_lead_validation import (
    load_api_keys,
    SERPER_KEY_NAME,
    ANTHROPIC_KEY_NAME,
)

_CONFIRM_THRESHOLD = 50

# Optional key: missing is not an error, Deep Dive just uses its Serper/
# urllib fallback path instead of Firecrawl. Resolved locally (env, then an
# optional --secrets-file TOML fallback) rather than folded into the shared
# load_api_keys() above, which is reused by other CLI tools that know
# nothing about Firecrawl.
FIRECRAWL_KEY_NAME = "FIRECRAWL_API_KEY"


def load_firecrawl_key(secrets_file: Optional[str] = None) -> str:
    """Resolve the optional Firecrawl key: env first, then --secrets-file."""
    key = (os.environ.get(FIRECRAWL_KEY_NAME) or "").strip()
    if key:
        return key
    if not secrets_file:
        return ""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return ""
    try:
        with open(secrets_file, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return ""
    val = data.get(FIRECRAWL_KEY_NAME)
    return val.strip() if isinstance(val, str) and val.strip() else ""


class SheetResolutionError(ValueError):
    """Raised when the target sheet cannot be resolved unambiguously."""


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lead Prioritizer v2 batch runner (Excel in / Excel out).",
    )
    p.add_argument("--input", required=True, help="Path to the input .xlsx file.")
    p.add_argument("--company-column", required=True, help="Company name column.")
    p.add_argument("--domain-column", required=True, help="Domain column.")

    p.add_argument("--sheet", default=None,
                   help="Sheet name. Required only when the workbook has >1 sheet.")
    p.add_argument("--input-country-column", default=None,
                   help="Optional per-row input country column.")
    p.add_argument("--default-country", default="Italy",
                   help="Fallback input country when no column/value is present.")
    p.add_argument("--mode", default="full", choices=list(SUPPORTED_RUN_MODES),
                   help="Run mode (default: full).")
    p.add_argument("--start-row", type=int, default=0, help="First row offset (default: 0).")
    p.add_argument("--row-limit", type=int, default=10,
                   help="Max rows to process; 0 = all rows (default: 10).")
    p.add_argument("--output", default=None,
                   help="Output .xlsx path. Default: timestamped file next to input.")
    p.add_argument("--secrets-file", default=None,
                   help="Optional TOML fallback for API keys.")
    p.add_argument("--include-raw-ai-json", action="store_true",
                   help="Include ai_hq_raw_json in the Enriched Leads sheet.")
    p.add_argument("--stop-on-error", action="store_true",
                   help="Stop the batch on the first row error (default: continue).")
    p.add_argument("--yes", action="store_true",
                   help=f"Confirm running more than {_CONFIRM_THRESHOLD} rows.")
    p.add_argument("--compose-caller-content", action="store_true",
                   help="Opt-in Step 3: compose why_relevant/what_is_hot/"
                        "cold_caller_summary/caller_angle/call_starter via the "
                        "Anthropic API instead of deterministic templates. Falls "
                        "back to templates per-row on any failure (default: off).")
    p.add_argument("--rich-icp-context", action="store_true",
                   help="Opt-in: compose icp_buying_signals/"
                        "icp_likely_training_interest/icp_potential_buyer_function "
                        "via the Anthropic API using broader context evidence. "
                        "Independent of --compose-caller-content (either may be "
                        "used without the other); never affects scoring "
                        "(default: off).")
    p.add_argument("--ai-signal-scoring", action="store_true",
                   help="Opt-in Onderdeel 2: replace deterministic keyword-count "
                        "signal verdicts with one Anthropic call judging the same "
                        "guard-filtered evidence semantically. WARNING: unlike "
                        "--compose-caller-content/--rich-icp-context/--deep-dive, "
                        "this DOES change final_commercial_fit_score versus the "
                        "default (same scoring formula, AI-judged signal input). "
                        "Falls back to deterministic scoring on any AI failure. "
                        "Recorded per row via signal_scoring_mode (default: off).")
    p.add_argument("--legacy-enrichment-mode", action="store_true",
                   help="Opt-in comparison feature: reproduce the old "
                        "enrich_clients_claude.py Step-2 Serper+Claude evaluation "
                        "style (same holistic buying-signal judgment, minus the "
                        "competitor signal/query, no Jina page scraping) alongside "
                        "the normal v2 flow, for direct old-vs-new comparison. Fills "
                        "legacy_score/legacy_tier/legacy_icp_* columns; never "
                        "touches final_commercial_fit_score or signals either way "
                        "(default: off).")
    p.add_argument("--deep-dive", action="store_true",
                   help="Opt-in Step B: run a deeper, source-backed evidence "
                        "collection (Firecrawl if FIRECRAWL_API_KEY is set, "
                        "else localized Serper + plain fetches) for rows that "
                        "clear --deep-dive-min-score and/or a confirmed "
                        "foreign-HQ signal, AFTER scoring. Writes a 'Deep "
                        "Dive' sheet; never affects scoring. Independent of "
                        "--compose-caller-content and --rich-icp-context "
                        "(default: off).")
    p.add_argument("--deep-dive-min-score", type=float, default=8.0,
                   help="Minimum final_commercial_fit_score that triggers a "
                        "Deep Dive (default: 8.0).")
    p.add_argument("--deep-dive-max-pages", type=int, default=6,
                   help="Max pages collected per Deep Dive (default: 6).")
    p.add_argument("--no-verify-quotes", action="store_true",
                   help="Skip mechanical quote verification for Deep Dive "
                        "claims (on by default). Verification catches an AI "
                        "hallucinated/paraphrased quote by re-checking it "
                        "against the actual page text; leaves every claim "
                        "as quote_verification_status='not_checked' when "
                        "skipped.")
    p.add_argument("--no-auto-correct-quotes", action="store_true",
                   help="Disable automatic quote self-healing (on by "
                        "default, only meaningful when quote verification "
                        "is on): a fuzzy-matched quote stays as the AI's "
                        "original text instead of being corrected to the "
                        "real page text, and 'not_found' quotes never get "
                        "a re-extraction attempt.")
    p.add_argument("--no-deep-dive-on-foreign-hq", action="store_true",
                   help="Disable the confirmed-foreign-HQ Deep Dive trigger "
                        "(on by default, only meaningful with --deep-dive): "
                        "leaves --deep-dive-min-score as the only trigger.")
    p.add_argument("--use-enrichment-cache", action="store_true",
                   help="Opt-in: consult/update the shared, GCS-backed "
                        "Serper/Firecrawl enrichment cache (one index per "
                        "country present in the batch) instead of every run "
                        "starting cold. See enrichment_cache.py. Requires "
                        "--enrichment-cache-bucket (default: off).")
    p.add_argument("--enrichment-cache-bucket", default="",
                   help="GCS bucket for --use-enrichment-cache.")
    p.add_argument("--usage-output", default=None,
                   help="Optional path to write the per-run usage_tracker "
                        "snapshot as JSON (in addition to the printed "
                        "summary) — used by cloud_job_runner.py to fold "
                        "one task's usage into its status JSON so the Cloud "
                        "Run orchestrator can report combined usage/cache "
                        "stats across all tasks.")
    p.add_argument("--c5-enabled", action="store_true",
                   help="Opt-in Step C5: after normal batch processing, run "
                        "Sonnet HQ adjudication over score-3/manual-review "
                        "rows (fixed conservative_adjustment scoring "
                        "behavior, Sonnet model tier, no explicit model "
                        "override — the same defaults the local Streamlit "
                        "batch app uses day to day). Default: off.")
    p.add_argument("--gate-full-enrichment-on-foreign-hq", action="store_true",
                   help="Opt-in cost gate: run cheap HQ-only screening (1 "
                        "Serper call) for every row first, then run the full "
                        "non-HQ enrichment/scoring/caller-content pipeline "
                        "ONLY for rows confirmed foreign-HQ (HQ score == 3). "
                        "Non-confirmed rows are kept with "
                        "enrichment_skipped=True instead of paying the "
                        "~4 extra Serper calls + Firecrawl/Anthropic cost "
                        "for a row you were going to filter out anyway. "
                        "Works with every --mode. KNOWN LIMITATION when "
                        "combined with --c5-enabled: C5 runs AFTER this gate "
                        "decision as a flat post-step, so a row C5 later "
                        "confirms as foreign-HQ does NOT get pulled back "
                        "into full enrichment -- it only gets C5's own "
                        "fields. Default: off.")
    return p


# ---------------------------------------------------------------------------
# Small pure helpers (unit-testable without live APIs)
# ---------------------------------------------------------------------------

def generate_output_path(input_path: Path, mode: str, when: datetime) -> Path:
    ts = when.strftime("%Y%m%d_%H%M%S")
    return input_path.with_name(
        f"{input_path.stem}_lead_prioritizer_v2_{mode}_{ts}.xlsx"
    )


def resolve_sheet(sheet_names: list[str], sheet_arg: Optional[str]) -> str:
    if sheet_arg:
        if sheet_arg not in sheet_names:
            raise SheetResolutionError(
                f"Sheet {sheet_arg!r} not found. Available: {', '.join(sheet_names)}"
            )
        return sheet_arg
    if len(sheet_names) == 1:
        return sheet_names[0]
    raise SheetResolutionError(
        "Workbook has multiple sheets; pass --sheet. "
        f"Available: {', '.join(sheet_names)}"
    )


def check_required_columns(
    columns,
    company_col: str,
    domain_col: str,
    input_country_col: Optional[str] = None,
) -> None:
    cols = set(columns)
    missing = [c for c in (company_col, domain_col) if c not in cols]
    if input_country_col and input_country_col not in cols:
        missing.append(input_country_col)
    if missing:
        raise ValueError("missing required column(s): " + ", ".join(missing))


def config_from_args(args: argparse.Namespace) -> BatchRunConfig:
    return BatchRunConfig(
        company_name_column=args.company_column,
        domain_column=args.domain_column,
        input_country_column=args.input_country_column,
        default_input_country=args.default_country,
        run_mode=args.mode,
        start_row=args.start_row,
        row_limit=args.row_limit,
        continue_on_error=not args.stop_on_error,
        include_raw_ai_json=args.include_raw_ai_json,
        compose_caller_content=args.compose_caller_content,
        rich_icp_context=args.rich_icp_context,
        ai_signal_scoring=args.ai_signal_scoring,
        legacy_enrichment_mode=args.legacy_enrichment_mode,
        deep_dive=args.deep_dive,
        deep_dive_min_score=args.deep_dive_min_score,
        deep_dive_on_foreign_hq=not args.no_deep_dive_on_foreign_hq,
        deep_dive_max_pages=args.deep_dive_max_pages,
        verify_quotes=not args.no_verify_quotes,
        auto_correct_quotes=not args.no_auto_correct_quotes,
        use_enrichment_cache=args.use_enrichment_cache,
        enrichment_cache_bucket=args.enrichment_cache_bucket,
        gate_full_enrichment_on_foreign_hq=args.gate_full_enrichment_on_foreign_hq,
    )


def gate_c5_combo_warning(gate_enabled: bool, c5_enabled: bool) -> Optional[str]:
    """Warning text for the untested gate+C5 combination, or ``None`` when
    it doesn't apply. Pure/testable so the CLI's print behavior can be
    asserted without capturing stdout."""
    if not (gate_enabled and c5_enabled):
        return None
    return (
        "WARNING: --gate-full-enrichment-on-foreign-hq + --c5-enabled: C5 "
        "runs AFTER the foreign-HQ gate decision, so a row C5 later confirms "
        "as foreign-HQ stays enrichment_skipped=True (no full non-HQ "
        "enrichment) -- C5 only adds its own fields to whichever rows the "
        "plain HQ-score screening already selected. See "
        "lead_prioritizer_batch_cli.py --help for details."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # ── API keys (never printed as values) ────────────────────────────────────
    keys = load_api_keys(secrets_file=args.secrets_file)
    serper = keys.get(SERPER_KEY_NAME, "")
    anthropic = keys.get(ANTHROPIC_KEY_NAME, "")
    print(f"{SERPER_KEY_NAME}: {'set' if serper else 'missing'}")
    print(f"{ANTHROPIC_KEY_NAME}: {'set' if anthropic else 'missing'}")
    if not serper or not anthropic:
        print("ERROR: missing API key(s). Set env vars or pass --secrets-file.",
              file=sys.stderr)
        return 2

    # Optional: missing is not an error, only a Deep Dive fallback mode.
    firecrawl = load_firecrawl_key(args.secrets_file)
    print(f"{FIRECRAWL_KEY_NAME}: {'set' if firecrawl else 'not set (Deep Dive fallback mode)'}")

    # ── Load workbook ─────────────────────────────────────────────────────────
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2
    try:
        xls = pd.ExcelFile(input_path)
    except Exception as exc:
        print(f"ERROR: cannot read workbook: {exc}", file=sys.stderr)
        return 2

    try:
        sheet = resolve_sheet(list(xls.sheet_names), args.sheet)
    except SheetResolutionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    df = xls.parse(sheet)

    try:
        check_required_columns(
            df.columns, args.company_column, args.domain_column, args.input_country_column,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    config = config_from_args(args)
    selected_count = len(select_batch_rows(df, config))
    output_path = Path(args.output) if args.output else generate_output_path(
        input_path, args.mode, datetime.now())

    print(f"Input path    : {input_path}")
    print(f"Sheet         : {sheet}")
    print(f"Row count     : {len(df)}")
    print(f"Run mode      : {args.mode}")
    print(f"Foreign-HQ gate: {'on' if args.gate_full_enrichment_on_foreign_hq else 'off'}")
    print(f"Selected rows : {selected_count}")
    print(f"Output path   : {output_path}")

    warning = gate_c5_combo_warning(args.gate_full_enrichment_on_foreign_hq, args.c5_enabled)
    if warning:
        print(warning, file=sys.stderr)

    # ── Safety confirmation for large runs ────────────────────────────────────
    if selected_count > _CONFIRM_THRESHOLD and not args.yes:
        print(
            f"WARNING: {selected_count} selected rows exceeds {_CONFIRM_THRESHOLD}. "
            "Full mode makes multiple Serper + Anthropic calls per row, which has "
            "cost and time implications.",
            file=sys.stderr,
        )
        print("Re-run with --yes to confirm.", file=sys.stderr)
        return 3

    # ── Run batch via shared core ─────────────────────────────────────────────
    # Reset the per-run API usage tracker so the summary below reflects only
    # this invocation (the CLI runs one batch per process).
    import usage_tracker
    usage_tracker.reset()

    tables = run_batch_dataframe(df, config, serper, anthropic, firecrawl_api_key=firecrawl)

    # ── Optional C5 Sonnet HQ adjudication (after normal batch processing) ────
    # Fixed conservative_adjustment/score_3_or_manual_review/sonnet defaults —
    # the same choices the local Streamlit batch app uses day to day; not
    # exposed as separate flags to keep the cloud/CLI surface small.
    c5_model_used = ""
    c5_counts: dict = {}
    if args.c5_enabled:
        from lead_prioritizer_batch_core import apply_c5_adjudication
        from run_hq_sonnet_adjudication_probe import resolve_c5_model

        c5_model_used, c5_model_error = resolve_c5_model("sonnet", "")
        if c5_model_error:
            print(f"ERROR: {c5_model_error}", file=sys.stderr)
            return 2
        c5_rows, c5_counts = apply_c5_adjudication(
            tables["enriched_leads"],
            anthropic_api_key=anthropic,
            model_used=c5_model_used,
            model_tier="sonnet",
            scoring_behavior="conservative_adjustment",
            scope="score_3_or_manual_review",
            include_raw=args.include_raw_ai_json,
        )
        tables["enriched_leads"] = pd.DataFrame(c5_rows)

    # Always record the c5_enabled flag (and settings, when on) in the run
    # summary, matching the local Streamlit batch app's non-parallel path.
    from lead_prioritizer_batch_core import add_c5_summary_fields
    tables["run_summary"] = add_c5_summary_fields(
        tables["run_summary"],
        c5_enabled=args.c5_enabled,
        c5_scoring_behavior="conservative_adjustment" if args.c5_enabled else "",
        c5_scope="score_3_or_manual_review" if args.c5_enabled else "",
        c5_model_tier="sonnet" if args.c5_enabled else "",
        c5_model_used=c5_model_used if args.c5_enabled else "",
        counts=c5_counts,
    )

    data = build_excel_workbook_bytes(tables)
    output_path.write_bytes(data)

    summary_df = tables.get("run_summary")
    summary = summary_df.iloc[0].to_dict() if summary_df is not None and len(summary_df) else {}
    print(f"Processed rows: {summary.get('processed_rows')}")
    print(f"Success count : {summary.get('success_count')}")
    print(f"Error count   : {summary.get('error_count')}")
    if args.gate_full_enrichment_on_foreign_hq:
        print(f"Gate: full-enriched {summary.get('gated_full_enrichment_attempted_count')}, "
              f"skipped (not confirmed foreign-HQ) {summary.get('gated_full_enrichment_skipped_count')} "
              f"(~{summary.get('gated_estimated_serper_calls_saved')} Serper calls saved)")
    print(f"Output written: {output_path}")

    # ── Per-run API usage + estimated cost ────────────────────────────────────
    usage_snapshot = usage_tracker.snapshot()
    print()
    print(usage_tracker.format_summary_text(usage_snapshot))
    history_path = usage_tracker.append_history(
        companies=summary.get("processed_rows") or selected_count,
        snapshot=usage_snapshot)
    print(f"(usage appended to {history_path})")

    if args.usage_output:
        Path(args.usage_output).write_text(
            json.dumps(usage_snapshot, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
