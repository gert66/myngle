"""Merge Cloud Run Job part outputs into one final Excel.

Reads every ``parts/part_*.xlsx`` written by ``cloud_job_runner.py`` for a
run, concatenates them (sorted by ``_cloud_original_row_index`` when
present), and writes ``final/<name>.xlsx`` plus a ``final/manifest_done.json``
status file. Works against ``gs://`` paths and local directories.

Cloud usage:
    python cloud_merge_results.py
    (RUN_ID, OUTPUT_GCS_DIR, EXPECTED_TASK_COUNT, FINAL_OUTPUT_NAME from env)

Local usage:
    python cloud_merge_results.py --output-dir .\\out --expected-task-count 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from cloud_job_runner import (
    ROW_INDEX_COL,
    is_gcs_uri,
    join_path,
    list_gcs_uris,
    parse_gcs_uri,
    upload_output_file,
    write_status_json,
    _gcs_client,
)

DEFAULT_FINAL_OUTPUT_NAME = "lead_prioritizer_final.xlsx"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="mYngle Lead Prioritizer — merge Cloud Run Job part outputs into one final Excel",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None, help="gs://... dir or local dir (overrides OUTPUT_GCS_DIR)")
    parser.add_argument("--expected-task-count", type=int, default=None)
    parser.add_argument("--final-output-name", default=None)
    return parser


def resolve_merge_config(argv=None) -> dict:
    args = build_arg_parser().parse_args(argv)
    return {
        "run_id": args.run_id or os.environ.get("RUN_ID", ""),
        "output_dir": args.output_dir or os.environ.get("OUTPUT_GCS_DIR", ""),
        "expected_task_count": (
            args.expected_task_count if args.expected_task_count is not None
            else int(os.environ.get("EXPECTED_TASK_COUNT", "0")) or None
        ),
        "final_output_name": (
            args.final_output_name
            or os.environ.get("FINAL_OUTPUT_NAME", "")
            or DEFAULT_FINAL_OUTPUT_NAME
        ),
    }


def list_part_files(output_dir: str) -> list[str]:
    """Return part file locations (gs:// URIs or local paths), sorted."""
    parts_dir = join_path(output_dir, "parts")
    if is_gcs_uri(output_dir):
        return sorted(list_gcs_uris(parts_dir, suffix=".xlsx"))
    local_parts_dir = Path(parts_dir)
    if not local_parts_dir.is_dir():
        return []
    return sorted(str(p) for p in local_parts_dir.glob("part_*.xlsx"))


def _download_to_local(uri_or_path: str, local_path: Path) -> Path:
    if is_gcs_uri(uri_or_path):
        bucket_name, blob_name = parse_gcs_uri(uri_or_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _gcs_client().bucket(bucket_name).blob(blob_name).download_to_filename(str(local_path))
        return local_path
    return Path(uri_or_path)


def merge_part_dataframes(local_part_paths: list[Path]) -> pd.DataFrame:
    """Read and concatenate all part Excel files, sorted by original row index when present."""
    frames = []
    for path in local_part_paths:
        frames.append(pd.read_excel(path))
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if ROW_INDEX_COL in merged.columns:
        merged = merged.sort_values(ROW_INDEX_COL, kind="stable").reset_index(drop=True)
    return merged


def main(argv=None) -> int:
    cfg = resolve_merge_config(argv)
    started_at = _now_iso()

    if not cfg["output_dir"]:
        print("[cloud_merge_results] ERROR: no output dir specified (OUTPUT_GCS_DIR env var or --output-dir)", file=sys.stderr)
        return 1

    final_name = cfg["final_output_name"]
    if not final_name.lower().endswith(".xlsx"):
        final_name += ".xlsx"
    final_output_uri = join_path(cfg["output_dir"], "final", final_name)
    manifest_uri = join_path(cfg["output_dir"], "final", "manifest_done.json")

    print(f"[cloud_merge_results] run_id={cfg['run_id']} output_dir={cfg['output_dir']}", flush=True)

    part_locations = list_part_files(cfg["output_dir"])
    print(f"[cloud_merge_results] Found {len(part_locations)} part file(s).", flush=True)

    expected = cfg["expected_task_count"]
    if expected and len(part_locations) != expected:
        err_msg = (
            f"Expected {expected} part file(s) but found {len(part_locations)}. "
            f"Parts present: {[Path(p).name for p in part_locations]}"
        )
        print(f"[cloud_merge_results] ERROR: {err_msg}", file=sys.stderr)
        _write_manifest_failed(manifest_uri, cfg, started_at, err_msg)
        return 1

    if not part_locations:
        err_msg = "No part files found to merge."
        print(f"[cloud_merge_results] ERROR: {err_msg}", file=sys.stderr)
        _write_manifest_failed(manifest_uri, cfg, started_at, err_msg)
        return 1

    import tempfile
    tmp_dir = Path(tempfile.gettempdir()) / "cloud_merge_results" / (cfg["run_id"] or "run")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    local_paths = []
    for loc in part_locations:
        local_target = tmp_dir / Path(loc).name
        local_paths.append(_download_to_local(loc, local_target))

    try:
        merged_df = merge_part_dataframes(local_paths)
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        print(f"[cloud_merge_results] ERROR: could not merge part files: {err_msg}", file=sys.stderr)
        _write_manifest_failed(manifest_uri, cfg, started_at, err_msg)
        return 1

    local_final = tmp_dir / final_name
    merged_df.to_excel(local_final, index=False)
    print(f"[cloud_merge_results] Uploading final Excel -> {final_output_uri}", flush=True)
    upload_output_file(local_final, final_output_uri)

    finished_at = _now_iso()
    manifest = {
        "run_id": cfg["run_id"],
        "output_dir": cfg["output_dir"],
        "final_output_uri": final_output_uri,
        "expected_task_count": expected,
        "parts_merged": len(part_locations),
        "part_files": [Path(p).name for p in part_locations],
        "row_count": int(len(merged_df)),
        "status": "done",
        "started_at": started_at,
        "finished_at": finished_at,
        "error": None,
    }
    write_status_json(manifest_uri, manifest)
    print(f"[cloud_merge_results] Done. {len(merged_df)} rows merged from {len(part_locations)} part(s).", flush=True)
    return 0


def _write_manifest_failed(manifest_uri: str, cfg: dict, started_at: str, error: str) -> None:
    manifest = {
        "run_id": cfg["run_id"],
        "output_dir": cfg["output_dir"],
        "final_output_uri": None,
        "expected_task_count": cfg["expected_task_count"],
        "parts_merged": 0,
        "part_files": [],
        "row_count": 0,
        "status": "failed",
        "started_at": started_at,
        "finished_at": _now_iso(),
        "error": error,
    }
    try:
        write_status_json(manifest_uri, manifest)
    except Exception as write_exc:
        print(f"[cloud_merge_results] ERROR: could not write failed manifest: {write_exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
