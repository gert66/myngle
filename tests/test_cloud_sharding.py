"""Tests for the row-sharding logic used by cloud_job_runner.py.

Pure-function tests only (compute_row_range / add_row_index_column) — no
GCS, no subprocess, no live APIs, no real keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud_job_runner import ROW_INDEX_COL, add_row_index_column, compute_row_range


def _shard_all_indices(total_rows: int, task_count: int) -> list[int]:
    """Simulate every task's slice and collect all _cloud_original_row_index values."""
    df = pd.DataFrame({"company_name": [f"company_{i}" for i in range(total_rows)]})
    df = add_row_index_column(df)

    all_indices: list[int] = []
    for task_index in range(task_count):
        start, end = compute_row_range(total_rows, task_index, task_count)
        part = df.iloc[start:end]
        all_indices.extend(part[ROW_INDEX_COL].tolist())
    return all_indices


def test_100_rows_10_tasks_covers_every_row_exactly_once():
    indices = _shard_all_indices(total_rows=100, task_count=10)
    assert len(indices) == 100
    assert sorted(indices) == list(range(100))
    assert len(set(indices)) == 100  # no duplicates


def test_small_file_no_empty_or_duplicate_rows_across_tasks():
    # 3 rows, 10 tasks: most tasks get zero rows, but every row is covered
    # exactly once across the tasks that do get rows.
    indices = _shard_all_indices(total_rows=3, task_count=10)
    assert sorted(indices) == [0, 1, 2]
    assert len(set(indices)) == 3


def test_compute_row_range_is_contiguous_and_non_overlapping():
    total_rows, task_count = 47, 5
    ranges = [compute_row_range(total_rows, i, task_count) for i in range(task_count)]
    covered: list[int] = []
    for start, end in ranges:
        covered.extend(range(start, end))
    assert sorted(covered) == list(range(total_rows))
    assert len(set(covered)) == total_rows


def test_compute_row_range_beyond_total_rows_returns_empty_range():
    start, end = compute_row_range(total_rows=3, task_index=9, task_count=10)
    assert start >= 3
    assert end == 3


def test_compute_row_range_zero_rows():
    start, end = compute_row_range(total_rows=0, task_index=0, task_count=10)
    assert (start, end) == (0, 0)


def test_compute_row_range_task_count_clamped_to_at_least_one():
    # task_count=0 should behave like task_count=1, not raise ZeroDivisionError
    start, end = compute_row_range(total_rows=10, task_index=0, task_count=0)
    assert (start, end) == (0, 10)


def test_add_row_index_column_is_idempotent():
    df = pd.DataFrame({"company_name": ["a", "b", "c"]})
    once = add_row_index_column(df)
    assert list(once[ROW_INDEX_COL]) == [0, 1, 2]

    # Calling again on the already-indexed frame must not overwrite existing values.
    twice = add_row_index_column(once)
    assert list(twice[ROW_INDEX_COL]) == [0, 1, 2]


def test_add_row_index_column_does_not_mutate_input_when_column_missing():
    df = pd.DataFrame({"company_name": ["a", "b"]})
    result = add_row_index_column(df)
    assert ROW_INDEX_COL not in df.columns
    assert ROW_INDEX_COL in result.columns


@pytest.mark.parametrize("total_rows,task_count", [(1, 1), (1, 10), (17, 4), (500, 50)])
def test_shard_coverage_matrix(total_rows, task_count):
    indices = _shard_all_indices(total_rows=total_rows, task_count=task_count)
    assert sorted(indices) == list(range(total_rows))
