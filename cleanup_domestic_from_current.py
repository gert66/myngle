"""One-off cleanup: retroactively filter a country's published current/
Lovable export down to foreign-HQ companies only.

Standalone script, not part of the Streamlit app -- run this once, by hand,
against a specific country whose current/ data was published before the
"Scope" unification (see cloud_run_streamlit_app.py and
docs/cloud_run_workflow.md). Before that fix, the enrichment cost gate and
the Lovable export filter were two independently-toggleable settings, so a
run with the gate on but the export filter off could fully enrich AND
publish confirmed-domestic companies (e.g. "Molins" in the Spain export)
into current/.

Downloads gs://<bucket>/<country_folder>/current/{companies.list.json,
company-details-*.json}, drops every list item (and its matching detail
record) whose foreign_hq_detected_for_export is not True, re-buckets the
remainder, and re-uploads. Defaults to a dry run (prints a summary, writes
nothing) -- pass --apply to actually overwrite current/ and delete
now-unused bucket files.

Usage:
    python cleanup_domestic_from_current.py --country spain            # dry run
    python cleanup_domestic_from_current.py --country spain --apply    # writes
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import lovable_gcs_upload as lovable_gcs
from cloud_run_streamlit_app import (
    _download_existing_current_export,
    _gcloud_executable,
    build_list_command,
    list_existing_gcs_files,
    run_capture,
)

DEFAULT_PROJECT = "project-979d7166-1016-40ce-94c"


def filter_foreign_hq_only(
    list_items: list[dict], details: dict[str, dict],
) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """Drop every company whose foreign_hq_detected_for_export is not True.

    Pure and order-preserving. Returns ``(kept_list_items, kept_details,
    dropped_list_items)`` -- dropped items are returned in full (not just
    counted) so a caller can show exactly which companies were removed.
    """
    kept_items = [i for i in list_items if i.get("foreign_hq_detected_for_export")]
    dropped_items = [i for i in list_items if not i.get("foreign_hq_detected_for_export")]
    kept_ids = {i["company_id"] for i in kept_items}
    kept_details = {cid: d for cid, d in details.items() if cid in kept_ids}
    return kept_items, kept_details, dropped_items


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--country", required=True, help="Export country, e.g. 'spain'.")
    parser.add_argument("--bucket", default=lovable_gcs.DEFAULT_GCS_BUCKET)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--bucket-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true",
                         help="Actually overwrite current/ (default: dry run only).")
    args = parser.parse_args()

    country_folder = lovable_gcs.country_folder_slug(args.country)
    work_dir = Path(tempfile.mkdtemp(prefix="cleanup_domestic_"))

    print(f"Downloading gs://{args.bucket}/{country_folder}/current/ ...")
    list_items, details = _download_existing_current_export(
        args.bucket, country_folder, args.project, work_dir / "existing")
    if not list_items:
        print("Nothing found (or download failed) -- nothing to clean up. Exiting.")
        return

    kept_items, kept_details, dropped_items = filter_foreign_hq_only(list_items, details)
    print(f"Companies before:                     {len(list_items)}")
    print(f"Companies kept (foreign HQ):           {len(kept_items)}")
    print(f"Companies dropped (domestic/unclear):  {len(dropped_items)}")
    if dropped_items:
        print("Dropped companies:")
        for item in dropped_items:
            print(f"  - {item.get('company_id')}: {item.get('company_name')}")

    if not dropped_items:
        print("\nNothing to drop -- current/ is already foreign-HQ-only. Exiting without changes.")
        return

    if not args.apply:
        print("\nDry run -- nothing uploaded. Re-run with --apply to overwrite current/.")
        return

    merged_items, merged_buckets = lovable_gcs.rebucket_company_details(
        kept_items, kept_details, args.bucket_size)

    out_dir = work_dir / "cleaned"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "companies.list.json").write_text(
        json.dumps(merged_items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for bucket_file, bucket_data in merged_buckets.items():
        (out_dir / bucket_file).write_text(
            json.dumps(bucket_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    tool_cmd = lovable_gcs.resolve_gcs_upload_tool()
    if tool_cmd is None:
        print("ERROR: neither gcloud nor gsutil found on PATH -- cannot upload.", file=sys.stderr)
        sys.exit(1)

    filenames = ["companies.list.json"] + sorted(merged_buckets.keys())
    print(f"\nUploading {len(filenames)} file(s) to current/ ...")
    for filename in filenames:
        dest = lovable_gcs.gcs_current_path(args.bucket, country_folder, filename)
        result = lovable_gcs.upload_file(tool_cmd, str(out_dir / filename), dest)
        status = "OK" if result["success"] else f"FAILED: {result.get('stderr') or result.get('error')}"
        print(f"  {filename} -> {dest}: {status}")
        if not result["success"]:
            sys.exit(1)

    # Fewer companies after filtering can mean fewer bucket files than
    # before -- delete any pre-existing bucket file no longer referenced by
    # the new companies.list.json, so a removed (domestic) company's detail
    # record doesn't keep sitting in current/, still publicly fetchable.
    stale_glob = f"gs://{args.bucket}/{country_folder}/current/company-details-*.json"
    rc, listing = run_capture(build_list_command(stale_glob, args.project))
    existing_bucket_uris = list_existing_gcs_files(listing) if rc == 0 else []
    kept_bucket_names = set(merged_buckets.keys())
    stale_uris = [
        uri for uri in existing_bucket_uris if Path(uri).name not in kept_bucket_names
    ]
    if stale_uris:
        print(f"\nDeleting {len(stale_uris)} now-unused bucket file(s) ...")
        for uri in stale_uris:
            rc_rm, out_rm = run_capture(
                [_gcloud_executable(), "storage", "rm", uri, "--project", args.project])
            status = "OK" if rc_rm == 0 else f"FAILED: {out_rm.strip()[-500:]}"
            print(f"  {uri}: {status}")

    print("\nDone.")


if __name__ == "__main__":
    main()
