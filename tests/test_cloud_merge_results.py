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


# ── 1. Merge restores original row order from out-of-order parts ───────────

def test_merge_restores_original_row_order_from_out_of_order_parts(tmp_path):
    output_dir = tmp_path / "out"
    # part_0000 (read first, alphabetically) deliberately holds the *later*
    # original rows, and part_0001 holds the earlier ones — proves the merge
    # sorts by _cloud_original_row_index rather than trusting file/read order.
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["D", "E"], [3, 4])
    _write_part(output_dir / "parts" / "part_0001.xlsx", ["A", "B", "C"], [0, 1, 2])

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

    rc = cmr.main([
        "--output-dir", str(output_dir),
        "--run-id", "custom-name",
        "--expected-task-count", "1",
        "--final-output-name", "switzerland_prioritized",
    ])

    assert rc == 0
    assert (output_dir / "final" / "switzerland_prioritized.xlsx").exists()


# ── 2. Missing part ──────────────────────────────────────────────────────────

def test_missing_part_fails_clearly(tmp_path):
    output_dir = tmp_path / "out"
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])

    rc = cmr.main(["--output-dir", str(output_dir), "--run-id", "missing-part", "--expected-task-count", "2"])

    assert rc == 1
    assert not (output_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME).exists()

    manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "Expected 2 part file(s) but found 1" in manifest["error"]
    assert manifest["parts_merged"] == 0


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
# a task with no rows assigned to it (row_start >= total_rows) — it only
# writes a status JSON with rows_processed=0. So with more tasks than rows,
# EXPECTED_TASK_COUNT (the raw task count) will legitimately be higher than
# the number of part files that ever get created, and a merge using the raw
# task count as EXPECTED_TASK_COUNT will fail the "missing part" check above.
# Callers must pass the count of tasks that actually produced a part, not the
# raw task_count, when tasks may outnumber rows. This is an existing/documented
# contract, not something changed here.
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
