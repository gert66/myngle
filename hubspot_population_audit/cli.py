"""Command-line entry point.

    python -m hubspot_population_audit.cli run --snapshot <dir> --output-dir <dir> [--no-live-lookups]

This batch never performs a live HubSpot call: the snapshot load and
reconciliation are fully deterministic against the on-disk snapshot, and
every analysis not yet implemented (cohort detection, classification,
association/activity evidence) is written as an explicit
``"status": "not_yet_implemented"`` placeholder rather than silently
skipped. ``--no-live-lookups`` is accepted for forward interface
compatibility with the later batch that adds targeted live lookups; it is
currently a no-op because no live lookups exist yet.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from .progress import ProgressWriter, now_iso
from .reconcile import reconcile_all
from .report import generate_html_report
from .snapshot import Snapshot

NOT_YET_IMPLEMENTED = [
    "cohort_analysis: creation-cohort bucketing by day/week/month and bulk-import wave detection",
    "evidence: association evidence (company-contact, company/deal, contact/deal)",
    "evidence: activity/recency evidence",
    "population_map: evidence-based bucket classification "
    "(operational_customer, operational_prospect, active_other, historical_import, "
    "enrichment_or_bulk, legacy_or_obsolete_candidate, uncertain)",
    "live targeted lookups (association/activity evidence via ReadOnlyHubSpotClient)",
]


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp_path, path)


def make_run_id() -> str:
    return time.strftime("pa-run-%Y%m%dT%H%M%SZ", time.gmtime())


def run_population_audit(snapshot_dir: str, output_dir: str, live_lookups: bool = False) -> dict:
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

    # Not yet implemented in this batch -- explicit placeholders, never silently skipped.
    progress.set_subprocess_status("cohort_analysis", "not_yet_implemented")
    progress.set_subprocess_status("association_evidence", "not_yet_implemented")
    progress.set_subprocess_status("activity_evidence", "not_yet_implemented")
    progress.set_subprocess_status("classification", "not_yet_implemented")
    # Live lookups are never performed in this batch, regardless of the flag;
    # --no-live-lookups exists for forward interface compatibility only.
    progress.set_subprocess_status("live_lookups", "not_yet_implemented", requested=live_lookups)
    progress.set_counter("lookups_planned", 0)
    progress.set_counter("lookups_done", 0)
    progress.set_counter("cache_hits", 0)

    gaps = [
        f"'{ot}': {'; '.join(r['notes'])}" if r["notes"] else f"'{ot}': no gaps"
        for ot, r in reconciliation.items()
    ]
    gaps.extend(NOT_YET_IMPLEMENTED)

    population_map = {
        "schema_version": 1,
        "status": "not_yet_implemented",
        "note": (
            "Bucket classification (operational_customer, operational_prospect, "
            "active_other, historical_import, enrichment_or_bulk, "
            "legacy_or_obsolete_candidate, uncertain) is not implemented in this "
            "batch. The reconciliation totals below are the only currently "
            "known-good population counts; they are the baseline the future "
            "bucket counts must sum to exactly."
        ),
        "reconciliation": reconciliation,
    }
    cohort_analysis = {
        "schema_version": 1,
        "status": "not_yet_implemented",
        "note": "Creation-cohort bucketing by day/week/month and bulk-import wave detection (e.g. Sep 2023) is not implemented in this batch.",
    }
    evidence = {
        "schema_version": 1,
        "status": "partial",
        "observed_facts": [
            {
                "object_type": object_type,
                "kind": "portal_reconciliation",
                "counts": {
                    "recorded_portal_total": result["recorded_portal_total"],
                    "raw_record_count": result["raw_record_count"],
                    "unique_id_count": result["unique_id_count"],
                    "duplicate_count": result["duplicate_count"],
                    "delta": result["delta"],
                },
                "reconciled": result["reconciled"],
                "notes": result["notes"],
            }
            for object_type, result in reconciliation.items()
        ],
        "not_yet_implemented": [
            "association_evidence",
            "activity_recency_evidence",
            "cohort_characterization",
        ],
    }
    next_actions = [
        "Confirm the assumed snapshot layout (raw/<object>.jsonl envelopes, "
        "_checkpoint.json, portal_totals.json) against the actual live snapshot "
        "before running any cohort/classification analysis against it.",
        "Implement creation-cohort bucketing (day/week/month) and anomalous "
        "bulk-import wave detection, in particular around Sep 2023.",
        "Add targeted read-only association evidence (company-contact, "
        "company/deal, contact/deal) via ReadOnlyHubSpotClient.batch_read/search.",
        "Add targeted activity/recency evidence without re-fetching complete raw universes.",
        "Implement the evidence-based classification buckets so population_map.json "
        "bucket counts sum exactly to the reconciled portal population.",
    ]

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
            "not_yet_implemented": NOT_YET_IMPLEMENTED,
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
        help="Reserved for the future targeted-lookup batch; live lookups are never performed today.",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        result = run_population_audit(
            snapshot_dir=args.snapshot,
            output_dir=args.output_dir,
            live_lookups=not args.no_live_lookups,
        )
        print(f"Run complete: {result['run_id']}")
        print(f"Report: {result['report_path']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
