"""Command-line entry point.

    python -m hubspot_population_audit.cli run --snapshot <dir> --output-dir <dir> [options]

The snapshot load, reconciliation, and creation-cohort/bulk-import-wave
analysis are fully deterministic against the on-disk snapshot and never
perform a HubSpot call. The targeted evidence layer (``evidence.py``) adds
a deterministic offline sampling plan plus -- only when a token is present
in ``--token-env`` and ``--offline`` is not set -- cached, read-only
batch-read and association lookups via ``ReadOnlyHubSpotClient``. With no
token or ``--offline``, ``evidence.json`` is still written in full, with
``status: "skipped_offline"``. Bucket classification
(``population_map.json``, ``classification.py``) is a fully deterministic,
rule-based pass over the entire snapshot -- it consumes the cohort/wave
analysis and whatever evidence-layer samples are available, but never
performs a HubSpot call of its own.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from .classification import build_population_map
from .cohorts import build_cohort_analysis
from .evidence import DEFAULT_MAX_LOOKUPS, DEFAULT_TOKEN_ENV, run_evidence
from .progress import ProgressWriter, now_iso
from .reconcile import reconcile_all
from .report import generate_html_report
from .snapshot import Snapshot


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp_path, path)


def make_run_id() -> str:
    return time.strftime("pa-run-%Y%m%dT%H%M%SZ", time.gmtime())


def _dedupe_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _synthesize_gaps_and_next_actions(reconciliation: dict, evidence_result: dict, classification_result: dict) -> tuple[list, list]:
    """Build the top-level ``gaps.json``/``next_actions.json`` as one
    coherent, theme-grouped, deduplicated list -- not a raw concatenation
    of each phase's per-phase placeholder text. Every fact already
    captured by reconciliation, the evidence layer, or classification
    survives (grouped under a ``[theme]`` or ``[theme:object_type]``
    prefix so entries from the same theme sort together and a reader can
    tell at a glance which phase/object a line is about), and any
    identical line restated by two phases collapses into one.
    """
    gaps: list = []
    next_actions: list = []

    # Theme: reconciliation -- one line per object type, already specific
    # (names exactly which portal-total files were checked, and reports
    # any duplicate-ID occurrences found during this snapshot's resumed
    # extraction, which is why dedup-by-id-first-wins matters everywhere
    # else in this package).
    for object_type, result in reconciliation.items():
        if result["notes"]:
            gaps.append(f"[reconciliation:{object_type}] {'; '.join(result['notes'])}")

    # Theme: snapshot coverage -- facts about what this immutable snapshot
    # does and does not contain, stated once here rather than once per
    # phase that happens to touch them.
    gaps.append(
        "[snapshot:deals] not present in this snapshot (deals extraction probe failed and "
        "properties_deals returned 403); no deal-based cohort or reconciliation exists for "
        "this object type in the snapshot itself (targeted deal association evidence, where "
        "accessible, is in evidence.json)."
    )
    gaps.append(
        "[snapshot:properties] raw records carry only default properties (no hs_object_source, "
        "lifecyclestage, hubspot_owner_id, or associations); cohort characterization from the "
        "snapshot alone is limited to presence/recency/domain signals and cannot see source, "
        "lifecycle, owner, or association evidence directly -- see evidence.json for the "
        "targeted, sampled lookups that fill this gap."
    )

    # Theme: evidence layer -- property availability, sampling/lookup-budget
    # truncation, the offline-mode notice, and any denied object/association
    # endpoint, verbatim from evidence.py (already object/pair-specific).
    gaps.extend(f"[evidence] {item}" for item in evidence_result["gaps"])

    # Theme: classification -- the reconciliation-crosscheck invariant
    # (should never diverge), low-confidence waves, and uncertain-bucket
    # shares, verbatim from classification.py (already object-specific).
    gaps.extend(f"[classification] {item}" for item in classification_result["gaps"])

    next_actions.append(
        "[snapshot] Verified snapshot facts (see README 'Snapshot layout'): raw/<object>.jsonl "
        "envelopes shaped {record, extracted_at, page_index}; raw/_checkpoint.json; "
        "raw/owners.jsonl; raw/properties_<object>.json; no deals.jsonl (deals probe failed, "
        "properties_deals returned 403); run_status.json carries no portal_totals key and there "
        "is no portal_totals.json; raw records carry only default properties (no "
        "source/lifecycle/owner/associations)."
    )
    next_actions.extend(f"[evidence] {item}" for item in evidence_result["next_actions"])
    next_actions.extend(f"[classification] {item}" for item in classification_result["next_actions"])

    return _dedupe_preserve_order(gaps), _dedupe_preserve_order(next_actions)


def run_population_audit(
    snapshot_dir: str,
    output_dir: str,
    *,
    token_env: str = DEFAULT_TOKEN_ENV,
    offline: bool = False,
    max_lookups=None,
    lookup_rps: float = 3.0,
    sample_size: int = 200,
    full_fetch_threshold: int = 200,
    seed: int = 20260918,
    client=None,
) -> dict:
    run_id = make_run_id()
    os.makedirs(output_dir, exist_ok=True)
    reports_dir = os.path.join(output_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    progress = ProgressWriter(os.path.join(output_dir, "progress.json"))
    progress.set_phase("loading_snapshot")
    progress.set_subprocess_status("snapshot_load", "running")

    snap = Snapshot(snapshot_dir)
    object_types = snap.discover_object_types()
    for object_type in object_types:
        count = snap.count_raw_records(object_type)
        progress.set_counter(f"records_loaded.{object_type}", count)
    progress.set_subprocess_status("snapshot_load", "completed", object_types=list(object_types))

    progress.set_phase("reconciling")
    progress.set_subprocess_status("reconciliation", "running")
    reconciliation = reconcile_all(snap, object_types)
    progress.set_subprocess_status("reconciliation", "completed")

    progress.set_phase("cohort_analysis")
    progress.set_subprocess_status("cohort_analysis", "running")

    def _cohort_progress(object_type: str, scanned: int) -> None:
        progress.set_counter(f"records_scanned.{object_type}", scanned)

    cohort_analysis = build_cohort_analysis(
        snap, object_types, output_dir, progress_cb=_cohort_progress
    )
    for object_type in object_types:
        if object_type in cohort_analysis:
            progress.set_counter(
                f"waves_detected.{object_type}", cohort_analysis[object_type]["waves_detected"]
            )
    progress.set_subprocess_status("cohort_analysis", "completed")

    progress.set_phase("evidence")
    evidence_result = run_evidence(
        snap,
        cohort_analysis,
        output_dir,
        token_env=token_env,
        offline=offline,
        max_lookups=max_lookups,
        lookup_rps=lookup_rps,
        sample_size=sample_size,
        full_fetch_threshold=full_fetch_threshold,
        seed=seed,
        client=client,
        progress=progress,
    )
    evidence = evidence_result["evidence"]

    progress.set_phase("classification")
    progress.set_subprocess_status("classification", "running")

    def _classification_progress(object_type: str, processed: int, bucket_counts) -> None:
        progress.set_counter(f"classification.records_classified.{object_type}", processed)
        for bucket, count in bucket_counts.items():
            progress.set_counter(f"classification.bucket_counts.{object_type}.{bucket}", count)

    classification_result = build_population_map(
        snap,
        cohort_analysis,
        evidence,
        reconciliation,
        object_types,
        output_dir,
        progress_cb=_classification_progress,
    )
    population_map = classification_result["population_map"]
    progress.set_subprocess_status("classification", population_map["status"])

    gaps, next_actions = _synthesize_gaps_and_next_actions(
        reconciliation, evidence_result, classification_result
    )

    _write_json(os.path.join(output_dir, "population_map.json"), population_map)
    _write_json(os.path.join(output_dir, "cohort_analysis.json"), cohort_analysis)
    _write_json(os.path.join(output_dir, "evidence.json"), evidence)
    _write_json(os.path.join(output_dir, "gaps.json"), gaps)
    _write_json(os.path.join(output_dir, "next_actions.json"), next_actions)

    progress.set_phase("report_generation")
    progress.set_subprocess_status("report_generation", "running")
    report_path = os.path.join(reports_dir, "index.html")
    generate_html_report(
        {
            "run_id": run_id,
            "generated_at": now_iso(),
            "snapshot": snapshot_dir,
            "reconciliation": reconciliation,
            "cohort_analysis": cohort_analysis,
            "evidence": evidence,
            "population_map": population_map,
            "gaps": gaps,
        },
        report_path,
    )
    progress.set_subprocess_status("report_generation", "completed")

    progress.set_phase("completed")

    return {
        "run_id": run_id,
        "output_dir": output_dir,
        "report_path": report_path,
        "reconciliation": reconciliation,
        "population_map": population_map,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hubspot_population_audit", description="Read-only HubSpot population audit"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the population audit against a snapshot")
    run_parser.add_argument("--snapshot", required=True, help="Path to the immutable extraction snapshot")
    run_parser.add_argument("--output-dir", required=True, help="Directory to write audit artifacts into")
    run_parser.add_argument(
        "--no-live-lookups",
        action="store_true",
        help="Never perform live evidence lookups, even if a token is present (same effect as --offline).",
    )
    run_parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip all live evidence lookups; evidence.json is still written in full with "
        "status 'skipped_offline' plus the complete sampling plan.",
    )
    run_parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"Env var name holding a HubSpot private-app token (default: {DEFAULT_TOKEN_ENV}).",
    )
    run_parser.add_argument(
        "--max-lookups",
        type=int,
        default=DEFAULT_MAX_LOOKUPS,
        help="Cap on the total number of NEW live id/association lookups this run (cache hits never "
        "count against it). Default: None, meaning the effective cap covers the full sampling plan "
        "(every object id plus every implied association lookup) -- see evidence.json['lookup_budget']. "
        "Pass 0 to perform no fetches at all.",
    )
    run_parser.add_argument(
        "--lookup-rps", type=float, default=3.0, help="Max live lookup requests per second (default: 3.0)."
    )
    run_parser.add_argument(
        "--sample-size", type=int, default=200, help="Per-stratum sample size above the full-fetch threshold."
    )
    run_parser.add_argument(
        "--full-fetch-threshold", type=int, default=200, help="Strata at or below this size are fetched in full."
    )
    run_parser.add_argument(
        "--seed", type=int, default=20260918, help="Seed for the deterministic stratified sampling plan."
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        result = run_population_audit(
            snapshot_dir=args.snapshot,
            output_dir=args.output_dir,
            token_env=args.token_env,
            offline=args.offline or args.no_live_lookups,
            max_lookups=args.max_lookups,
            lookup_rps=args.lookup_rps,
            sample_size=args.sample_size,
            full_fetch_threshold=args.full_fetch_threshold,
            seed=args.seed,
        )
        print(f"Run complete: {result['run_id']}")
        print(f"Report: {result['report_path']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
