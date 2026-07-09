"""Merge a freshly-exported Lovable JSON batch into an already-published GCS
"current" export, instead of blindly overwriting it.

Motivating scenario: a large country list is processed in batches (e.g. a
1,000-row test batch is reviewed and approved, then the rest of the file
follows). Uploading each batch straight to ``gs://<bucket>/<prefix>/`` via
``lovable_gcs_upload.build_flat_upload_plan`` replaces ``companies.list.json``
outright, so the second batch would silently erase the first one. This module
adds the alternative: fetch what is already published, merge it with the new
batch, and upload the merged result.

Merge rule (deliberately simple, matches the manual/sequential-batch use
case this exists for): on a duplicate ``company_id`` the newest batch wins;
a company already published is never removed just because it is absent from
the new batch. This is not safe against concurrent/simultaneous uploads to
the same prefix (no locking) — it assumes one operator merging batches one
at a time, which is how the export UI is used.

Bucket files (``company-details-NNN.json``) already published are never
re-fetched or re-uploaded: the new batch's own bucket files are simply
renumbered to continue after the highest bucket number already in use, so
filenames never collide. An updated company's ``detail_bucket`` pointer in
the merged list moves to its new bucket file; the stale copy left behind in
its old bucket file is unreferenced and harmless.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from lovable_gcs_upload import build_flat_upload_plan, fetch_gcs_text, gcs_flat_path

_BUCKET_FILE_RE = re.compile(r"^company-details-(\d+)\.json$")


def load_local_export(output_dir) -> tuple[list[dict], dict[str, dict], dict]:
    """Load a freshly-written local export (companies.list.json + its
    company-details-NNN.json bucket files + export_manifest.json) back into
    memory, ready to be merged with an existing published export."""
    output_dir = Path(output_dir)
    list_items = json.loads(
        (output_dir / "companies.list.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (output_dir / "export_manifest.json").read_text(encoding="utf-8"))
    bucket_files = sorted({
        item.get("detail_bucket") for item in list_items if item.get("detail_bucket")
    })
    details_by_bucket = {
        bucket_file: json.loads((output_dir / bucket_file).read_text(encoding="utf-8"))
        for bucket_file in bucket_files
    }
    return list_items, details_by_bucket, manifest


def fetch_existing_flat_export(bucket: str, prefix: str) -> dict:
    """Fetch whatever companies.list.json + export_manifest.json is already
    published at ``gs://<bucket>/<prefix>/``, if anything.

    Returns ``{"exists", "list_items", "manifest", "error"}``.
    ``exists=False, error=None`` is the normal "nothing published here yet"
    case. ``exists=None`` means the check itself failed (missing CLI, auth,
    network) — callers should surface ``error`` rather than silently
    treating that as "nothing published", which would turn a merge into a
    silent overwrite.
    """
    list_result = fetch_gcs_text(gcs_flat_path(bucket, prefix, "companies.list.json"))
    if list_result["exists"] is False:
        return {"exists": False, "list_items": None, "manifest": None, "error": None}
    if not list_result["success"]:
        return {
            "exists": None, "list_items": None, "manifest": None,
            "error": list_result["error"] or "Could not check for an existing export.",
        }
    try:
        list_items = json.loads(list_result["text"])
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "exists": True, "list_items": None, "manifest": None,
            "error": f"Existing companies.list.json is not valid JSON: {exc}",
        }

    manifest = None
    manifest_result = fetch_gcs_text(gcs_flat_path(bucket, prefix, "export_manifest.json"))
    if manifest_result["success"]:
        try:
            manifest = json.loads(manifest_result["text"])
        except (json.JSONDecodeError, TypeError):
            manifest = None

    return {"exists": True, "list_items": list_items, "manifest": manifest, "error": None}


def next_bucket_start(existing_list_items: "list[dict] | None") -> int:
    """Next free ``company-details-NNN.json`` number, so a newly-written
    batch's bucket files never collide with ones already published."""
    highest = -1
    for item in existing_list_items or []:
        match = _BUCKET_FILE_RE.match(str(item.get("detail_bucket") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def renumber_new_export(
    new_list_items: list[dict],
    new_details_by_bucket: dict[str, dict],
    start_bucket_no: int,
) -> tuple[list[dict], dict[str, dict]]:
    """Renumber one export's bucket files starting at ``start_bucket_no``, so
    they never collide with buckets already published under the same
    prefix. Bucket contents and company data are unchanged — only bucket
    filenames/pointers."""
    old_to_new = {
        old_name: f"company-details-{start_bucket_no + i:03d}.json"
        for i, old_name in enumerate(sorted(new_details_by_bucket.keys()))
    }

    renumbered_items = []
    for item in new_list_items:
        item = dict(item)
        item["detail_bucket"] = old_to_new.get(item.get("detail_bucket"), item.get("detail_bucket"))
        renumbered_items.append(item)

    renumbered_details = {}
    for old_name, contents in new_details_by_bucket.items():
        new_name = old_to_new[old_name]
        renumbered_details[new_name] = {
            company_id: {**detail, "detail_bucket": new_name}
            for company_id, detail in contents.items()
        }

    return renumbered_items, renumbered_details


def merge_company_lists(
    existing_list_items: "list[dict] | None", new_list_items: list[dict],
) -> tuple[list[dict], dict]:
    """Merge two companies.list.json arrays: on a duplicate ``company_id``
    the newest batch (``new_list_items``) wins; a company only present in
    ``existing_list_items`` is always kept. Returns ``(merged_list, stats)``
    with ``stats = {"added", "updated", "kept_from_existing"}`` counts."""
    existing_by_id = {item.get("company_id"): item for item in (existing_list_items or [])}
    new_by_id = {item.get("company_id"): item for item in new_list_items}

    stats = {
        "added": sum(1 for cid in new_by_id if cid not in existing_by_id),
        "updated": sum(1 for cid in new_by_id if cid in existing_by_id),
        "kept_from_existing": sum(1 for cid in existing_by_id if cid not in new_by_id),
    }

    merged_by_id = {**existing_by_id, **new_by_id}
    # Existing-only companies keep their published order; new/updated
    # companies follow in this batch's own (score-sorted) order. Not a full
    # global re-sort across both batches — acceptable for a list the
    # frontend re-sorts by score anyway.
    merged_order = [cid for cid in existing_by_id if cid not in new_by_id]
    merged_order += [item.get("company_id") for item in new_list_items]
    merged_list = [merged_by_id[cid] for cid in merged_order]

    return merged_list, stats


def build_merged_manifest(
    new_manifest: dict,
    merged_list: list[dict],
    existing_manifest: Optional[dict],
    merge_stats: dict,
) -> dict:
    """Recompute manifest totals over the merged list; keeps the new
    export's own run metadata (generated_at, cold_callers, ...) and layers
    a ``merge`` bookkeeping block on top for auditability."""
    manifest = dict(new_manifest)
    manifest["rows_exported"] = len(merged_list)
    manifest["bucket_count"] = len({
        item.get("detail_bucket") for item in merged_list if item.get("detail_bucket")
    })
    manifest["foreign_hq_rows_exported"] = sum(
        1 for item in merged_list if item.get("foreign_hq_detected_for_export"))
    manifest["merge"] = {
        "merged_into_existing": existing_manifest is not None,
        "previous_rows_exported": (existing_manifest or {}).get("rows_exported"),
        "previous_generated_at": (existing_manifest or {}).get("generated_at"),
        "added": merge_stats["added"],
        "updated": merge_stats["updated"],
        "kept_from_existing": merge_stats["kept_from_existing"],
    }
    return manifest


def write_merge_output(
    output_dir,
    merged_list_items: list[dict],
    renumbered_details_by_bucket: dict[str, dict],
    merged_manifest: dict,
) -> list[str]:
    """Write the merge result (merged list + only the new/renumbered bucket
    files + merged manifest) to ``output_dir``. Returns the filenames
    written, in upload order — existing bucket files already published are
    never written here since they are never touched."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "companies.list.json").write_text(
        json.dumps(merged_list_items, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (output_dir / "export_manifest.json").write_text(
        json.dumps(merged_manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    filenames = ["companies.list.json", "export_manifest.json"]
    for bucket_file, contents in renumbered_details_by_bucket.items():
        (output_dir / bucket_file).write_text(
            json.dumps(contents, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        filenames.append(bucket_file)
    return filenames


def prepare_merge(local_output_dir, bucket: str, prefix: str, merge_output_dir=None) -> dict:
    """End-to-end: load a freshly-exported local batch, fetch whatever is
    already published at ``gs://<bucket>/<prefix>/``, merge them, write the
    merge result to ``merge_output_dir`` (defaults to
    ``<local_output_dir>/merged``), and build the upload job list.

    Returns ``{"fetch_error", "jobs", "stats", "merged_manifest",
    "output_dir"}``. ``fetch_error`` set (jobs/stats/merged_manifest all
    ``None``) means the existing-export check itself failed — the caller
    should surface that rather than silently falling back to an overwrite.
    """
    new_list_items, new_details_by_bucket, new_manifest = load_local_export(local_output_dir)

    existing = fetch_existing_flat_export(bucket, prefix)
    if existing["exists"] is None:
        return {
            "fetch_error": existing["error"], "jobs": None, "stats": None,
            "merged_manifest": None, "output_dir": None,
        }

    existing_list_items = existing["list_items"] or []
    start_bucket_no = next_bucket_start(existing_list_items)
    renumbered_items, renumbered_details = renumber_new_export(
        new_list_items, new_details_by_bucket, start_bucket_no)

    merged_list, stats = merge_company_lists(existing_list_items, renumbered_items)
    merged_manifest = build_merged_manifest(
        new_manifest, merged_list, existing["manifest"], stats)

    merge_output_dir = Path(merge_output_dir) if merge_output_dir else Path(local_output_dir) / "merged"
    filenames = write_merge_output(
        merge_output_dir, merged_list, renumbered_details, merged_manifest)
    jobs = build_flat_upload_plan(merge_output_dir, filenames, bucket, prefix)

    return {
        "fetch_error": None, "jobs": jobs, "stats": stats,
        "merged_manifest": merged_manifest, "output_dir": str(merge_output_dir),
    }
