"""Tests for cloud_merge_results.py: merging part outputs into one final Excel.

Everything here uses local paths only (never gs://), so no GCS client is
ever constructed and no live calls happen. An autouse fixture clears every
cloud-related env var so tests never pick up leftover config from the host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloud_merge_results as cmr
from cloud_job_runner import ROW_INDEX_COL

_CLOUD_ENV_VARS = ["RUN_ID", "OUTPUT_GCS_DIR", "EXPECTED_TASK_COUNT", "FINAL_OUTPUT_NAME"]


@pytest.fixture(autouse=True)
def _clean_cloud_env(monkeypatch):
    for name in _CLOUD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_part(path: Path, names: list[str], indices: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"company_name": names, ROW_INDEX_COL: indices}).to_excel(path, index=False)


def _write_status(output_dir: Path, task_index: int, status: str) -> None:
    """Mirror what cloud_job_runner.py actually writes: a status/*_{done,failed}.json
    for every task, whether or not that task produced a parts/*.xlsx (a task with an
    empty row-shard writes a "done" status but no part file)."""
    status_dir = output_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    payload = {"task_index": task_index, "status": status}
    (status_dir / f"part_{task_index:04d}_{status}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ── 1. Merge restores original row order from out-of-order parts ───────────

def test_merge_restores_original_row_order_from_out_of_order_parts(tmp_path):
    output_dir = tmp_path / "out"
    # part_0000 (read first, alphabetically) deliberately holds the *later*
    # original rows, and part_0001 holds the earlier ones — proves the merge
    # sorts by _cloud_original_row_index rather than trusting file/read order.
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["D", "E"], [3, 4])
    _write_part(output_dir / "parts" / "part_0001.xlsx", ["A", "B", "C"], [0, 1, 2])
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "done")

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "merge-order", "--expected-task-count", "2"])

    assert rc == 0
    final_path = output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME
    assert final_path.exists()
    final_df = pd.read_excel(final_path)
    assert list(final_df["company_name"]) == ["A", "B", "C", "D", "E"]
    assert list(final_df[ROW_INDEX_COL]) == [0, 1, 2, 3, 4]

    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "done"
    assert manifest["parts_merged"] == 2
    assert manifest["row_count"] == 5
    assert manifest["error"] is None


def test_final_output_name_is_honored_and_xlsx_suffix_is_added(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")

    rc = cmr.main([
        "--output-dir", str(output_dir),
        "--run-id", "custom-name",
        "--expected-task-count", "1",
        "--final-output-name", "switzerland_prioritized",
    ])

    assert rc == 0
    assert (output_dir / "final" / "switzerland_prioritized.xlsx").exists()


# ── 2. Missing/failed task status ────────────────────────────────────────────

def test_task_still_running_fails_clearly(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")
    # task 1 has no status file at all yet — still running or never started.

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "missing-part", "--expected-task-count", "2"])

    assert rc == 1
    assert not (output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME).exists()

    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "Expected 2 task(s) to report status but found 1" in manifest["error"]
    assert manifest["parts_merged"] == 0


def test_failed_task_blocks_merge(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "failed")  # a failed task never writes a part file

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "one-failed", "--expected-task-count", "2"])

    assert rc == 1
    assert not (output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME).exists()

    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "1 task(s) failed" in manifest["error"]
    assert "part_0001_failed.json" in manifest["error"]


def test_more_tasks_than_rows_merges_only_nonempty_parts(tmp_path):
    """TASK_COUNT=10 against a 3-row file (the documented "first safe test"
    setting): 7 tasks get an empty row-shard, write a "done" status, and never
    write a parts/*.xlsx — that must not be mistaken for a missing/failed task."""
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")
    for task_index in range(1, 3):
        _write_status(output_dir, task_index, "done")  # empty shard, no part file

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "sparse-parts", "--expected-task-count", "3"])

    assert rc == 0
    final_df = pd.read_excel(output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME)
    assert list(final_df["company_name"]) == ["A"]
    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["parts_merged"] == 1


def test_no_parts_found_fails_clearly(tmp_path):
    output_dir = tmp_path / "out"
    (output_dir / "parts").mkdir(parents=True)  # empty parts dir, no expected count given

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "no-parts"])

    assert rc == 1
    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "No part files found" in manifest["error"]


# ── 3. Zero-row part ──────────────────────────────────────────────────────────
#
# Note on scope: cloud_job_runner.py never *writes* a parts/part_XXXX.xlsx for
# a task with no rows assigned to it (row_start >= total_rows) — it only writes
# a status JSON with rows_processed=0. So with more tasks than rows (e.g. the
# documented TASK_COUNT=10 first-test setting against a small file),
# EXPECTED_TASK_COUNT (the raw task count) is legitimately higher than the
# number of part files that ever get created. The "did every task report in"
# check above is therefore based on status files (_done.json/_failed.json),
# not part files — see test_more_tasks_than_rows_merges_only_nonempty_parts.
#
# The scenario below instead covers a part *file that exists* but happens to
# contain zero data rows (e.g. an edge case in the enrichment output) — the
# merge tolerates that as a no-op contribution.

def test_merge_tolerates_zero_row_part_file(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A", "B"], [0, 1])
    pd.DataFrame({"company_name": [], ROW_INDEX_COL: []}).to_excel(
        output_dir / "parts" / "part_0001.xlsx", index=False
    )
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "done")

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "zero-row-part", "--expected-task-count", "2"])

    assert rc == 0
    final_df = pd.read_excel(output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME)
    assert list(final_df["company_name"]) == ["A", "B"]

    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["parts_merged"] == 2
    assert manifest["row_count"] == 2


# ── Direct unit tests for the pure/local-IO helpers ─────────────────────────

def test_list_part_files_returns_sorted_paths(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0002.xlsx", ["C"], [2])
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_part(output_dir / "parts" / "part_0001.xlsx", ["B"], [1])

    result = cmr.list_part_files(str(output_dir))
    assert [Path(p).name for p in result] == ["part_0000.xlsx", "part_0001.xlsx", "part_0002.xlsx"]


def test_list_part_files_missing_dir_returns_empty_list(tmp_path):
    assert cmr.list_part_files(str(tmp_path / "does_not_exist")) == []


def test_merge_part_dataframes_sorts_by_row_index(tmp_path):
    p1 = tmp_path / "b.xlsx"
    p2 = tmp_path / "a.xlsx"
    _write_part(p1, ["Z"], [1])
    _write_part(p2, ["Y"], [0])

    merged = cmr.merge_part_dataframes([p1, p2])
    assert list(merged["company_name"]) == ["Y", "Z"]


def test_merge_part_dataframes_empty_list_returns_empty_dataframe():
    merged = cmr.merge_part_dataframes([])
    assert merged.empty
