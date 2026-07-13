"""Tests for the round-robin cohort window added to
caller_range_assignment.py: resolve_cohort_window and
assign_callers_round_robin_by_cohort_window."""

from __future__ import annotations

import pytest

import caller_range_assignment as cra


def _items(n: int) -> list[dict]:
    return [
        {"company_id": f"c{i}", "assigned_cold_caller_rank": i}
        for i in range(1, n + 1)
    ]


class TestResolveCohortWindow:
    def test_first_cohort(self):
        assert cra.resolve_cohort_window(100, 1, 1, 4642) == (1, 100)

    def test_multi_cohort_range(self):
        assert cra.resolve_cohort_window(500, 1, 2, 4642) == (1, 1000)

    def test_clamped_to_total(self):
        assert cra.resolve_cohort_window(500, 9, 10, 4642) == (4001, 4642)

    def test_entirely_outside_total_is_empty(self):
        start, end = cra.resolve_cohort_window(100, 50, 60, 300)
        assert start > end


class TestAssignCallersRoundRobinByCohortWindow:
    def test_window_covers_everything_matches_plain_round_robin(self):
        items = _items(8)
        callers = ["A", "B"]
        windowed = cra.assign_callers_round_robin_by_cohort_window(
            items, callers, cohort_size=100, cohort_start=1, cohort_end=1)
        plain = cra.compute_caller_ranks(items)
        for cid, rank in plain.items():
            assert windowed[cid] == (callers[(rank - 1) % len(callers)], rank)

    def test_outside_window_is_unassigned(self):
        items = _items(10)
        callers = ["A", "B"]
        result = cra.assign_callers_round_robin_by_cohort_window(
            items, callers, cohort_size=4, cohort_start=1, cohort_end=1,
            unassigned_label="— (none)")
        for cid, (caller, rank) in result.items():
            if rank <= 4:
                assert caller in callers
            else:
                assert caller == "— (none)"

    def test_caller_identity_stable_as_window_advances(self):
        # A company's caller must not change when a later cohort window is
        # opened up — only its assigned/unassigned status may change.
        items = _items(12)
        callers = ["A", "B", "C"]
        first_window = cra.assign_callers_round_robin_by_cohort_window(
            items, callers, cohort_size=4, cohort_start=1, cohort_end=1)
        wider_window = cra.assign_callers_round_robin_by_cohort_window(
            items, callers, cohort_size=4, cohort_start=1, cohort_end=2)
        for cid, (caller, rank) in first_window.items():
            if rank <= 4:
                assert wider_window[cid] == (caller, rank)

    def test_empty_caller_pool_raises(self):
        with pytest.raises(ValueError):
            cra.assign_callers_round_robin_by_cohort_window(
                _items(5), [], cohort_size=10, cohort_start=1, cohort_end=1)

    def test_every_company_present_including_unassigned(self):
        items = _items(10)
        result = cra.assign_callers_round_robin_by_cohort_window(
            items, ["A"], cohort_size=3, cohort_start=2, cohort_end=2)
        assert set(result.keys()) == {it["company_id"] for it in items}
