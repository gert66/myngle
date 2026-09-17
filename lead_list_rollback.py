"""Prepare a selective rollback export for one protected lead-list batch.

This command never uploads anything. It turns current/, the immutable before
snapshot and ledger into a rollback candidate that can be reviewed first.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lead_list_safety import selective_rollback
from lovable_gcs_upload import rebucket_company_details


def load_current_export(path: Path) -> tuple[list[dict], dict[str, dict]]:
    items = json.loads((path / "companies.list.json").read_text(encoding="utf-8"))
    details: dict[str, dict] = {}
    for detail_file in sorted(path.glob("company-details-*.json")):
        details.update(json.loads(detail_file.read_text(encoding="utf-8")))
    return items, details


def prepare_rollback(current_dir: Path, safety_dir: Path, output_dir: Path, bucket_size: int = 500) -> dict:
    current_items, current_details = load_current_export(current_dir)
    before_dir = safety_dir / "before"
    before_items = json.loads((before_dir / "companies.list.json").read_text(encoding="utf-8"))
    before_details = json.loads((before_dir / "details.by-id.json").read_text(encoding="utf-8"))
    ledger = json.loads((safety_dir / "ledger.json").read_text(encoding="utf-8"))

    rollback = selective_rollback(
        current_items, current_details, before_items, before_details, ledger)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "batch_id": ledger.get("batch_id"),
        **rollback["summary"],
        "safe_to_apply": rollback["safe_to_apply"],
        "conflict_company_ids": rollback["conflict_company_ids"],
    }
    (output_dir / "rollback_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not rollback["safe_to_apply"]:
        return report

    items, buckets = rebucket_company_details(
        rollback["items"], rollback["details"], bucket_size)
    (output_dir / "companies.list.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for name, contents in buckets.items():
        (output_dir / name).write_text(
            json.dumps(contents, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-dir", required=True, type=Path)
    parser.add_argument("--safety-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--bucket-size", type=int, default=500)
    args = parser.parse_args(argv)
    report = prepare_rollback(args.current_dir, args.safety_dir, args.output_dir, args.bucket_size)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["safe_to_apply"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
