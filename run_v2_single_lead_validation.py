"""Safe CLI runner for the Lead Prioritizer v2 single-lead live validation.

Runs ``prioritize_single_lead(..., run_full_v2_pipeline=True)`` for the six
documented validation cases (see ``LEAD_PRIORITIZER_V2_SINGLE_LEAD_VALIDATION.md``)
and writes a *compact*, secret-free JSON + CSV report to ``validation_outputs/``.

API keys
--------
``SERPER_API_KEY`` and ``ANTHROPIC_API_KEY`` are read from environment variables
first. As an optional fallback, ``--secrets-file PATH`` may point at a TOML file
(same shape as ``.streamlit/secrets.toml``) with those keys.

Safety guarantees
-----------------
- Key *values* are never printed, logged, or written to any output file.
- Output contains only a curated compact field set, counts, and at most six
  evidence URLs per lead.
- Raw AI JSON (``ai_hq_raw_json``), raw Serper payloads, and API keys are never
  included in the compact output.

Usage
-----
    export SERPER_API_KEY=...
    export ANTHROPIC_API_KEY=...
    python run_v2_single_lead_validation.py

    # or, using a secrets file as fallback:
    python run_v2_single_lead_validation.py --secrets-file .streamlit/secrets.toml
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lead_output_schema import LeadInput, LeadPrioritizationResult
from lead_prioritizer_core import prioritize_single_lead

# Names of the required secrets. Only names are ever referenced by string here —
# never values.
SERPER_KEY_NAME = "SERPER_API_KEY"
ANTHROPIC_KEY_NAME = "ANTHROPIC_API_KEY"

DEFAULT_INPUT_COUNTRY = "Italy"
DEFAULT_OUTPUT_DIR = "validation_outputs"

# Cap on evidence URLs surfaced per lead in the compact output.
MAX_EVIDENCE_URLS = 6


@dataclass(frozen=True)
class ValidationCase:
    company_name: str
    domain: str


# The six documented validation cases (input_country = Italy). Keep in sync with
# LEAD_PRIORITIZER_V2_SINGLE_LEAD_VALIDATION.md.
VALIDATION_CASES: list[ValidationCase] = [
    ValidationCase("BMW ITALIA S.P.A.", "bmw.it"),
    ValidationCase("KNORR-BREMSE RAIL SYSTEMS ITALIA S.R.L.", "knorr-bremse.com"),
    ValidationCase("DANFOSS S.R.L.", "danfoss.com"),
    ValidationCase("RICOH ITALIA S.R.L.", "ricoh.it"),
    ValidationCase("CANNON BONO S.P.A.", "cannonbono.com"),
    ValidationCase("IET", "iet.it"),
]

# Curated scalar fields for the compact output (and CSV columns). Deliberately
# excludes raw AI JSON, raw payloads, and long evidence quotes/snippets.
COMPACT_SCALAR_FIELDS: list[str] = [
    # Identity / run context
    "company_name",
    "domain",
    "input_country",
    "v2_pipeline_mode",
    # HQ detection (compact)
    "hq_detected_country",
    "hq_detected_city",
    "hq_confidence",
    "foreign_hq_simple",
    "hq_structure_type",
    "needs_manual_review",
    "sig_foreign_hq_score_for_next_scoring",
    # HQ AI audit (compact, no raw JSON)
    "ai_hq_classification",
    "ai_hq_confidence",
    "ai_parent_company",
    "ai_parent_hq_country",
    "ai_parent_hq_city",
    "ai_call_attempted",
    "ai_call_success",
    "ai_hq_error",
    # Non-HQ signal scores
    "sig_international_profile_score",
    "sig_onboarding_training_need_score",
    "sig_company_size_complexity_score",
    "sig_icp_keyword_match_score",
    # Commercial scoring
    "final_commercial_fit_score",
    "commercial_tier",
    "icp_similarity_score",
    "lean_model_prob",
    "scoring_profile",
    # Caller/app (compact flags only)
    "commercial_tier_app",
    "foreign_hq_signal_used_in_app",
    "foreign_hq_country_app",
    "foreign_hq_city_app",
]


def _read_keys_from_secrets_file(path: Path) -> dict[str, str]:
    """Read the two keys from a TOML secrets file. Returns {} on any problem.

    Never raises and never surfaces values; only the two known key names are
    extracted.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name in (SERPER_KEY_NAME, ANTHROPIC_KEY_NAME):
        val = data.get(name)
        if isinstance(val, str) and val.strip():
            out[name] = val.strip()
    return out


def load_api_keys(
    env: Optional[dict[str, str]] = None,
    secrets_file: Optional[str | os.PathLike[str]] = None,
) -> dict[str, str]:
    """Resolve API keys: environment first, then optional secrets-file fallback.

    Returns a dict with keys ``SERPER_API_KEY`` and ``ANTHROPIC_API_KEY`` mapped
    to their (possibly empty) values. Environment values take precedence over the
    secrets file. This function never prints key values.
    """
    env = os.environ if env is None else env

    resolved: dict[str, str] = {}
    for name in (SERPER_KEY_NAME, ANTHROPIC_KEY_NAME):
        resolved[name] = (env.get(name) or "").strip()

    # Only consult the secrets file for keys still missing from the environment.
    if secrets_file and not all(resolved.values()):
        file_keys = _read_keys_from_secrets_file(Path(secrets_file))
        for name in (SERPER_KEY_NAME, ANTHROPIC_KEY_NAME):
            if not resolved[name] and file_keys.get(name):
                resolved[name] = file_keys[name]

    return resolved


def _evidence_urls(result: LeadPrioritizationResult) -> list[str]:
    """Return up to ``MAX_EVIDENCE_URLS`` unique, non-empty evidence source URLs."""
    urls: list[str] = []
    seen: set[str] = set()
    for item in (result.evidence_items or []):
        url = getattr(item, "source_url", None)
        if isinstance(url, str):
            url = url.strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= MAX_EVIDENCE_URLS:
            break
    return urls


def to_compact_output(
    result: LeadPrioritizationResult,
    *,
    run_success: bool,
    run_error: Optional[str] = None,
) -> dict[str, Any]:
    """Convert a full result into the compact, secret-free output record.

    Includes only ``COMPACT_SCALAR_FIELDS``, evidence/signal counts, at most
    ``MAX_EVIDENCE_URLS`` evidence URLs, and the run status. Never includes raw
    AI JSON, raw Serper payloads, or API keys.
    """
    record: dict[str, Any] = {}
    for name in COMPACT_SCALAR_FIELDS:
        record[name] = getattr(result, name, None)

    record["evidence_count"] = len(result.evidence_items or [])
    record["signal_count"] = len(result.signals or [])
    record["evidence_urls"] = _evidence_urls(result)
    record["run_success"] = run_success
    record["run_error"] = run_error
    return record


def _failed_case_record(
    case: ValidationCase, run_error: str
) -> dict[str, Any]:
    """Compact record for a case that raised before producing a result."""
    record: dict[str, Any] = {name: None for name in COMPACT_SCALAR_FIELDS}
    record["company_name"] = case.company_name
    record["domain"] = case.domain
    record["input_country"] = DEFAULT_INPUT_COUNTRY
    record["evidence_count"] = 0
    record["signal_count"] = 0
    record["evidence_urls"] = []
    record["run_success"] = False
    record["run_error"] = run_error
    return record


def run_case(
    case: ValidationCase,
    *,
    serper_api_key: str,
    anthropic_api_key: str,
    default_input_country: str = DEFAULT_INPUT_COUNTRY,
) -> dict[str, Any]:
    """Run the full v2 pipeline for one case and return its compact record.

    Any exception is captured into ``run_error`` (with ``run_success=False``)
    rather than aborting the whole run.
    """
    try:
        result = prioritize_single_lead(
            LeadInput(
                company_name=case.company_name,
                domain=case.domain or None,
                input_country=None,
            ),
            serper_api_key=serper_api_key,
            anthropic_api_key=anthropic_api_key,
            run_full_v2_pipeline=True,
            default_input_country=default_input_country,
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the batch
        return _failed_case_record(case, f"{type(exc).__name__}: {exc}")

    return to_compact_output(result, run_success=True, run_error=None)


def _write_json(records: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_csv(records: list[dict[str, Any]], path: Path) -> None:
    columns = COMPACT_SCALAR_FIELDS + [
        "evidence_count",
        "signal_count",
        "evidence_urls",
        "run_success",
        "run_error",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = dict(rec)
            # Flatten the URL list into a single delimited cell.
            row["evidence_urls"] = " | ".join(rec.get("evidence_urls") or [])
            writer.writerow(row)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Lead Prioritizer v2 single-lead live validation and write a "
            "compact, secret-free JSON + CSV report."
        )
    )
    parser.add_argument(
        "--secrets-file",
        default=None,
        help=(
            "Optional TOML file (e.g. .streamlit/secrets.toml) used as a fallback "
            "for SERPER_API_KEY / ANTHROPIC_API_KEY when they are absent from the "
            "environment."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    keys = load_api_keys(secrets_file=args.secrets_file)
    serper_key = keys[SERPER_KEY_NAME]
    anthropic_key = keys[ANTHROPIC_KEY_NAME]

    # Presence only — never the values.
    print(f"{SERPER_KEY_NAME}: {'set' if serper_key else 'MISSING'}", file=sys.stderr)
    print(
        f"{ANTHROPIC_KEY_NAME}: {'set' if anthropic_key else 'MISSING'}",
        file=sys.stderr,
    )
    if not serper_key or not anthropic_key:
        print(
            "WARNING: one or both keys are missing — live cases will be recorded "
            "with run_success=false and a run_error. Set the environment "
            "variables or pass --secrets-file.",
            file=sys.stderr,
        )

    records: list[dict[str, Any]] = []
    for case in VALIDATION_CASES:
        print(f"Running: {case.company_name} ({case.domain})", file=sys.stderr)
        rec = run_case(
            case,
            serper_api_key=serper_key,
            anthropic_api_key=anthropic_key,
        )
        status = "ok" if rec["run_success"] else f"error ({rec['run_error']})"
        print(f"  -> {status}", file=sys.stderr)
        records.append(rec)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"v2_single_lead_validation_{stamp}.json"
    csv_path = out_dir / f"v2_single_lead_validation_{stamp}.csv"
    _write_json(records, json_path)
    _write_csv(records, csv_path)

    n_ok = sum(1 for r in records if r["run_success"])
    print(
        f"Wrote {len(records)} records ({n_ok} ok / {len(records) - n_ok} error) to:\n"
        f"  {json_path}\n  {csv_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
