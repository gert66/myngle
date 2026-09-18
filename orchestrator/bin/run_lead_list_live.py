#!/usr/bin/env python3
"""Protected VM route for Lead List preflight, Zyte enrichment and GCS merge."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
repo = Path(os.environ.get("MYNGLE_REPO_ROOT", str(ORCH_ROOT.parent)))
if not (repo / "lead_list_live.py").is_file():
    sibling = ORCH_ROOT.parent / "myngle"
    if sibling.is_dir():
        repo = sibling
sys.path.insert(0, str(repo))

from lead_list_live import (  # noqa: E402
    build_live_preflight,
    combine_enrichment_exports,
    protected_publish_export,
    run_zyte_enrichment,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "enrich", "publish"])
    parser.add_argument("--list-dir", required=True)
    parser.add_argument("--country", default="south-korea")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--confirm-batch-id", default="")
    parser.add_argument("--export-dir", default="")
    parser.add_argument("--list-key", default="")
    parser.add_argument("--list-name", default="")
    args = parser.parse_args(argv)

    preflight = build_live_preflight(
        args.list_dir, country_slug=args.country,
        batch_id=args.batch_id, caller=args.caller,
    )
    if args.action == "preflight":
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0 if preflight["safety_preflight"] == "GREEN" else 2
    if args.action == "enrich":
        outputs = run_zyte_enrichment(
            args.list_dir, preflight, allow_supplier_calls=True,
        )
        combined = combine_enrichment_exports(outputs, args.list_dir, preflight, args.caller)
        print(json.dumps({"enriched_outputs": outputs, "combined_export": combined}, indent=2))
        return 0
    if not args.export_dir:
        parser.error("publish requires --export-dir")
    result = protected_publish_export(
        args.export_dir, args.list_dir, preflight=preflight, caller=args.caller,
        confirm_batch_id=args.confirm_batch_id,
        list_key=args.list_key,
        list_name=args.list_name,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
