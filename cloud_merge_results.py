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

# Must match lead_prioritizer_batch_core._SHEET_NAMES / DEEP_DIVE_SHEET_NAME —
# every part file lead_prioritizer_batch_cli.py writes has its data under
# sheets with these exact names, and export_workbook_to_lovable_json() (the
# Cloud Run Lovable auto-export step) requires "Enriched Leads" to be
# present, plus reads Evidence/Signals/Run Summary/Deep Dive when present.
ENRICHED_LEADS_SHEET_NAME = "Enriched Leads"
EVIDENCE_SHEET_NAME = "Evidence"
SIGNALS_SHEET_NAME = "Signals"
RUN_SUMMARY_SHEET_NAME = "Run Summary"
DEEP_DIVE_SHEET_NAME = "Deep Dive"

# lead_prioritizer_batch_core.flatten_result_for_excel/flatten_evidence_for_excel/
# flatten_signals_for_excel/flatten_deep_dive_for_excel all stamp this column
# from the pandas index of lead_prioritizer_batch_cli.py's OWN input read —
# which, for a Cloud Run task, is a fresh 0-based RangeIndex LOCAL to that
# one task's row-shard (the shard was written with index=False, so nothing
# about the original file's row position survives except the separate
# ROW_INDEX_COL column cloud_job_runner.py adds). Concatenating Evidence/
# Signals/Deep Dive rows across tasks WITHOUT remapping this column would
# silently collide -- task 0's source_index=0 and task 7's source_index=0
# both claim "row 0", but are different companies. See merge_workbook_sheets.
SOURCE_INDEX_COL = "source_index"


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


def list_status_files(output_dir: str, suffix: str) -> list[str]:
    """Return status file locations (gs:// URIs or local paths) ending in suffix, sorted.

    A task whose row-shard is empty (task_count > row_count, e.g. the
    documented TASK_COUNT=10 first test against a small file) reports a
    "done" status but never writes a parts/*.xlsx file — that's by design,
    not a missing part. Counting status files (not part files) against
    expected_task_count is what actually tells us every task reported in.
    """
    status_dir = join_path(output_dir, "status")
    if is_gcs_uri(output_dir):
        return sorted(list_gcs_uris(status_dir, suffix=suffix))
    local_status_dir = Path(status_dir)
    if not local_status_dir.is_dir():
        return []
    return sorted(str(p) for p in local_status_dir.glob(f"*{suffix}"))


def resolve_task_states(output_dir: str) -> dict[str, str]:
    """Map each task label (e.g. "part_0098") to its terminal status.

    cloud_job_runner.py keys status files by task index alone — never by
    retry attempt — and never deletes an earlier attempt's marker. So a
    task that failed once and then succeeded on a Cloud Run-driven retry
    (or a manual rerun of just that task) leaves both a stale
    "_failed.json" and a fresh "_done.json" on GCS for the same label.
    "done" always wins over "failed" here, regardless of which file
    happens to sort first, so a superseded failure can't block the merge
    or get double-counted against expected_task_count.
    """
    failed_labels = {Path(p).name[: -len("_failed.json")] for p in list_status_files(output_dir, "_failed.json")}
    done_labels = {Path(p).name[: -len("_done.json")] for p in list_status_files(output_dir, "_done.json")}
    states: dict[str, str] = {label: "failed" for label in failed_labels}
    states.update({label: "done" for label in done_labels})
    return states


def _download_to_local(uri_or_path: str, local_path: Path) -> Path:
    if is_gcs_uri(uri_or_path):
        bucket_name, blob_name = parse_gcs_uri(uri_or_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _gcs_client().bucket(bucket_name).blob(blob_name).download_to_filename(str(local_path))
        return local_path
    return Path(uri_or_path)


def merge_part_dataframes(local_part_paths: list[Path]) -> pd.DataFrame:
    """Read and concatenate all part Excel files, sorted by original row index when present.

    Reads the "Enriched Leads" sheet explicitly rather than relying on it
    being sheet 0 -- true today only because of insertion order in
    lead_prioritizer_batch_core._SHEET_NAMES, and a fragile thing to depend
    on implicitly.
    """
    frames = []
    for path in local_part_paths:
        frames.append(pd.read_excel(path, sheet_name=ENRICHED_LEADS_SHEET_NAME))
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if ROW_INDEX_COL in merged.columns:
        merged = merged.sort_values(ROW_INDEX_COL, kind="stable").reset_index(drop=True)
    return merged


def _remap_source_index(df: pd.DataFrame, index_map: dict) -> pd.DataFrame:
    """Rewrite df[SOURCE_INDEX_COL] through index_map (local -> run-wide id),
    leaving any value with no mapping entry untouched rather than raising —
    a row this defensive lookup can't place is still worth keeping."""
    if df.empty or SOURCE_INDEX_COL not in df.columns or not index_map:
        return df
    df = df.copy()
    df[SOURCE_INDEX_COL] = df[SOURCE_INDEX_COL].map(
        lambda v: index_map.get(int(v), v) if pd.notna(v) else v)
    return df


def merge_workbook_sheets(local_part_paths: list[Path]) -> dict[str, pd.DataFrame]:
    """Merge EVERY sheet lead_prioritizer_batch_cli.py writes (Enriched
    Leads, Evidence, Signals, Run Summary, Deep Dive) across all part files —
    not just Enriched Leads, which merge_part_dataframes/the pre-existing
    behavior handled alone. Without this, export_workbook_to_lovable_json()
    (the Cloud Run "auto-export to Lovable" step) always saw an empty/missing
    Signals sheet, so every non-HQ Commercial Fit Driver showed "Not
    evidenced" regardless of the real sig_*_score values -- confirmed live
    against a real run (Excelian | Luxoft: sig_international_profile_score=2
    etc. with real reason text, yet the Lovable export showed all four
    non-HQ drivers as unevidenced).

    Also remaps SOURCE_INDEX_COL in Evidence/Signals/Deep Dive from each
    part's own locally-0-based value onto the run-wide-unique ROW_INDEX_COL
    every Enriched Leads row already carries (see SOURCE_INDEX_COL's
    docstring above) -- required for export_lead_prioritizer_to_lovable_json.py's
    source_index-keyed row matching to still link the right Evidence/Signals
    rows to the right company once multiple tasks' rows sit in one sheet.
    """
    enriched_frames: list[pd.DataFrame] = []
    evidence_frames: list[pd.DataFrame] = []
    signal_frames: list[pd.DataFrame] = []
    summary_frames: list[pd.DataFrame] = []
    deep_dive_frames: list[pd.DataFrame] = []

    for path in local_part_paths:
        with pd.ExcelFile(path) as xls:
            sheet_names = set(xls.sheet_names)
            if ENRICHED_LEADS_SHEET_NAME not in sheet_names:
                continue  # defensive; every real part file has this sheet
            enriched_part = xls.parse(ENRICHED_LEADS_SHEET_NAME)

            index_map: dict = {}
            if SOURCE_INDEX_COL in enriched_part.columns and ROW_INDEX_COL in enriched_part.columns:
                for local_idx, global_idx in zip(
                        enriched_part[SOURCE_INDEX_COL], enriched_part[ROW_INDEX_COL]):
                    if pd.notna(local_idx) and pd.notna(global_idx):
                        index_map[int(local_idx)] = int(global_idx)
                # Canonicalize: source_index becomes the same run-wide id
                # Evidence/Signals/Deep Dive rows get remapped onto below, so
                # every sheet agrees on one id scheme after the merge.
                enriched_part[SOURCE_INDEX_COL] = enriched_part[ROW_INDEX_COL]
            enriched_frames.append(enriched_part)

            if EVIDENCE_SHEET_NAME in sheet_names:
                evidence_frames.append(_remap_source_index(xls.parse(EVIDENCE_SHEET_NAME), index_map))
            if SIGNALS_SHEET_NAME in sheet_names:
                signal_frames.append(_remap_source_index(xls.parse(SIGNALS_SHEET_NAME), index_map))
            if DEEP_DIVE_SHEET_NAME in sheet_names:
                deep_dive_frames.append(_remap_source_index(xls.parse(DEEP_DIVE_SHEET_NAME), index_map))
            if RUN_SUMMARY_SHEET_NAME in sheet_names:
                summary_frames.append(xls.parse(RUN_SUMMARY_SHEET_NAME))

    def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    enriched = _concat(enriched_frames)
    if ROW_INDEX_COL in enriched.columns:
        enriched = enriched.sort_values(ROW_INDEX_COL, kind="stable").reset_index(drop=True)

    return {
        "enriched_leads": enriched,
        "evidence": _concat(evidence_frames),
        "signals": _concat(signal_frames),
        "run_summary": _concat(summary_frames),
        "deep_dive": _concat(deep_dive_frames),
    }


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
    if expected:
        task_states = resolve_task_states(cfg["output_dir"])
        reported = len(task_states)
        done_count = sum(1 for s in task_states.values() if s == "done")
        failed_labels = sorted(label for label, state in task_states.items() if state == "failed")
        if reported != expected:
            err_msg = (
                f"Expected {expected} task(s) to report status but found {reported} "
                f"({done_count} done, {len(failed_labels)} failed) — some tasks "
                f"are still running or never started."
            )
            print(f"[cloud_merge_results] ERROR: {err_msg}", file=sys.stderr)
            _write_manifest_failed(manifest_uri, cfg, started_at, err_msg)
            return 1
        if failed_labels:
            failed_filenames = [f"{label}_failed.json" for label in failed_labels]
            err_msg = f"{len(failed_filenames)} task(s) failed: {failed_filenames}"
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
        merged_sheets = merge_workbook_sheets(local_paths)
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        print(f"[cloud_merge_results] ERROR: could not merge part files: {err_msg}", file=sys.stderr)
        _write_manifest_failed(manifest_uri, cfg, started_at, err_msg)
        return 1
    merged_df = merged_sheets["enriched_leads"]

    local_final = tmp_dir / final_name
    with pd.ExcelWriter(local_final, engine="openpyxl") as writer:
        merged_df.to_excel(writer, sheet_name=ENRICHED_LEADS_SHEET_NAME, index=False)
        merged_sheets["evidence"].to_excel(writer, sheet_name=EVIDENCE_SHEET_NAME, index=False)
        merged_sheets["signals"].to_excel(writer, sheet_name=SIGNALS_SHEET_NAME, index=False)
        merged_sheets["run_summary"].to_excel(writer, sheet_name=RUN_SUMMARY_SHEET_NAME, index=False)
        # Match lead_prioritizer_batch_core.build_excel_workbook_bytes: an
        # empty Deep Dive sheet is omitted entirely rather than written blank
        # (most runs never enable --deep-dive at all).
        if not merged_sheets["deep_dive"].empty:
            merged_sheets["deep_dive"].to_excel(writer, sheet_name=DEEP_DIVE_SHEET_NAME, index=False)
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
