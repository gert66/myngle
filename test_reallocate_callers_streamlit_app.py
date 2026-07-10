"""Tests for the pure helpers behind reallocate_callers_streamlit_app.py
(the Caller Workload Manager).

Only the pure helpers are tested — no Streamlit/Plotly rendering. Streamlit
and Plotly are imported lazily inside ``main()`` (see the module docstring),
so this module is importable without them.
"""

from __future__ import annotations

import pytest

from reallocate_callers_streamlit_app import (
    FOCUS_MODES,
    OUT_OF_FOCUS_LABEL,
    UNASSIGNED_LABEL,
    allocation_map_segments,
    assignment_from_blocks,
    assignment_interleaved,
    blocks_coverage,
    blocks_with_feedback,
    caller_distribution_dataframe,
    even_blocks,
    focus_selection,
    format_position_ranges,
    movers_dataframe,
    per_caller_summary_dataframe,
    ranked_items,
    score_of,
    tier_counts,
    validate_callers,
)


def _item(cid: str, score: float, rank: int, caller: str = "old",
          tier: str = "🥉 Cool") -> dict:
    return {
        "company_id": cid,
        "company_name": f"Company {cid}",
        "commercial_fit_score": score,
        "commercial_tier": tier,
        "assigned_cold_caller": caller,
        "assigned_cold_caller_rank": rank,
    }


def _ranked_fixture(n: int = 10) -> list[dict]:
    """n items, scores 10.0 down to 10-0.5(n-1), ranks already 1..n."""
    return [_item(f"c{i}", 10.0 - 0.5 * i, i + 1) for i in range(n)]


class TestValidateCallers:
    def test_empty_pool_is_an_error(self):
        assert validate_callers([]) is not None

    def test_non_empty_pool_passes(self):
        assert validate_callers(["Ann"]) is None


class TestScoreOf:
    def test_valid_and_invalid_values(self):
        assert score_of({"commercial_fit_score": 7.5}) == 7.5
        assert score_of({"commercial_fit_score": "7.5"}) == 7.5
        assert score_of({"commercial_fit_score": None}) is None
        assert score_of({"commercial_fit_score": "n/a"}) is None
        assert score_of({}) is None


class TestRankedItems:
    def test_orders_by_stored_rank_when_not_reranking(self):
        items = [_item("b", 5.0, 2), _item("a", 9.0, 1), _item("c", 3.0, 3)]
        ranked = ranked_items(items, rerank_by_score=False)
        assert [it["company_id"] for it in ranked] == ["a", "b", "c"]

    def test_rerank_by_score_reorders_after_a_rescore(self):
        # Stored ranks contradict the (re-scored) scores; rerank fixes it.
        items = [_item("a", 4.0, 1), _item("b", 9.0, 2)]
        ranked = ranked_items(items, rerank_by_score=True)
        assert [it["company_id"] for it in ranked] == ["b", "a"]


class TestTierCounts:
    def test_counts_by_tier_label(self):
        items = [_item("a", 9, 1, tier="🥇 Hot"), _item("b", 8, 2, tier="🥇 Hot"),
                 _item("c", 5, 3, tier="🥉 Cool")]
        assert tier_counts(items) == {"🥇 Hot": 2, "🥉 Cool": 1}


class TestFocusSelection:
    def test_all_mode_selects_everything(self):
        ranked = _ranked_fixture(6)
        ids, summary = focus_selection(ranked, {"mode": "all"})
        assert len(ids) == 6
        assert summary["n_focus"] == 6
        assert summary["n_out"] == 0

    def test_top_n_takes_the_first_n_of_the_ranking(self):
        ranked = _ranked_fixture(10)
        ids, summary = focus_selection(ranked, {"mode": "top_n", "top_n": 3})
        assert ids == ["c0", "c1", "c2"]
        assert summary["n_focus"] == 3
        assert summary["n_out"] == 7
        assert summary["best_score"] == 10.0
        assert summary["worst_score"] == 9.0

    def test_score_band_reports_live_count_and_range(self):
        # THE feature request: "how many companies are within that range?"
        ranked = _ranked_fixture(10)  # scores 10.0, 9.5, ..., 5.5
        ids, summary = focus_selection(
            ranked, {"mode": "score_band", "min_score": 6.0, "max_score": 9.0})
        assert summary["n_focus"] == len(ids) == 7  # 9.0 .. 6.0
        assert summary["best_score"] == 9.0
        assert summary["worst_score"] == 6.0
        assert summary["n_total"] == 10

    def test_score_band_filters_on_value_not_rank_contiguity(self):
        # A stale (un-reranked) run can have scores out of rank order; the
        # band must still catch the right companies.
        items = [_item("a", 9.0, 1), _item("b", 3.0, 2), _item("c", 8.0, 3)]
        ids, summary = focus_selection(
            ranked_items(items, rerank_by_score=False),
            {"mode": "score_band", "min_score": 7.0, "max_score": 10.0})
        assert set(ids) == {"a", "c"}
        assert summary["n_focus"] == 2

    def test_band_bounds_are_inclusive(self):
        ranked = [_item("a", 6.0, 1), _item("b", 9.0, 2)]
        ids, _ = focus_selection(
            ranked, {"mode": "score_band", "min_score": 6.0, "max_score": 9.0})
        assert set(ids) == {"a", "b"}

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="focus mode"):
            focus_selection([], {"mode": "nope"})
        assert "nope" not in FOCUS_MODES


class TestEvenBlocks:
    def test_splits_focus_into_near_equal_contiguous_blocks(self):
        blocks = even_blocks(["Ann", "Bob", "Cara"], 10)
        assert blocks == [
            {"caller": "Ann", "start": 1, "end": 4},
            {"caller": "Bob", "start": 5, "end": 7},
            {"caller": "Cara", "start": 8, "end": 10},
        ]

    def test_empty_inputs_give_empty_blocks(self):
        assert even_blocks([], 10) == []
        assert even_blocks(["Ann"], 0) == []


class TestBlocksCoverage:
    def test_full_coverage_has_no_gaps_or_overlaps(self):
        blocks = even_blocks(["Ann", "Bob"], 10)
        cov = blocks_coverage(blocks, 10)
        assert cov["n_gaps"] == 0
        assert cov["n_overlaps"] == 0

    def test_gaps_and_overlaps_reported_as_compressed_ranges(self):
        blocks = [
            {"caller": "Ann", "start": 1, "end": 4},
            {"caller": "Bob", "start": 3, "end": 6},
        ]
        cov = blocks_coverage(blocks, 10)
        assert cov["gap_ranges"] == [(7, 10)]
        assert cov["n_gaps"] == 4
        assert cov["overlap_ranges"] == [(3, 4)]
        assert cov["n_overlaps"] == 2

    def test_blocks_outside_focus_are_clamped(self):
        cov = blocks_coverage([{"caller": "Ann", "start": 1, "end": 99}], 5)
        assert cov["n_gaps"] == 0
        assert cov["n_overlaps"] == 0


class TestFormatPositionRanges:
    def test_formats_runs_and_truncates(self):
        assert format_position_ranges([(1, 3), (7, 7)]) == "1–3, 7"
        long = [(i, i) for i in range(1, 12, 2)]
        assert format_position_ranges(long).endswith("…")


class TestAssignmentFromBlocks:
    def test_blocks_assign_contiguous_slices_with_global_ranks(self):
        ranked = _ranked_fixture(6)
        ids, _ = focus_selection(ranked, {"mode": "all"})
        assignment = assignment_from_blocks(
            ranked, ids, even_blocks(["Ann", "Bob"], 6))
        assert assignment["c0"] == ("Ann", 1)
        assert assignment["c2"] == ("Ann", 3)
        assert assignment["c3"] == ("Bob", 4)
        assert assignment["c5"] == ("Bob", 6)

    def test_out_of_focus_companies_get_unassigned_label(self):
        ranked = _ranked_fixture(6)
        ids, _ = focus_selection(ranked, {"mode": "top_n", "top_n": 4})
        assignment = assignment_from_blocks(
            ranked, ids, even_blocks(["Ann", "Bob"], 4))
        assert assignment["c4"] == (UNASSIGNED_LABEL, 5)
        assert assignment["c5"] == (UNASSIGNED_LABEL, 6)
        assert assignment["c3"] == ("Bob", 4)

    def test_uncovered_positions_get_unassigned_label(self):
        ranked = _ranked_fixture(4)
        ids, _ = focus_selection(ranked, {"mode": "all"})
        assignment = assignment_from_blocks(
            ranked, ids, [{"caller": "Ann", "start": 1, "end": 2}])
        assert assignment["c2"] == (UNASSIGNED_LABEL, 3)

    def test_overlap_last_block_wins(self):
        ranked = _ranked_fixture(4)
        ids, _ = focus_selection(ranked, {"mode": "all"})
        assignment = assignment_from_blocks(ranked, ids, [
            {"caller": "Ann", "start": 1, "end": 4},
            {"caller": "Bob", "start": 2, "end": 3},
        ])
        assert assignment["c0"][0] == "Ann"
        assert assignment["c1"][0] == "Bob"
        assert assignment["c3"][0] == "Ann"

    def test_positions_count_within_focus_not_global_rank(self):
        # With a score-band focus the in-play companies may not start at
        # global rank 1 — block position 1 must mean "best company IN PLAY".
        ranked = _ranked_fixture(10)
        ids, _ = focus_selection(
            ranked, {"mode": "score_band", "min_score": 6.0, "max_score": 8.0})
        assignment = assignment_from_blocks(
            ranked, ids, [{"caller": "Ann", "start": 1, "end": 2}])
        assert assignment[ids[0]][0] == "Ann"
        assert assignment[ids[1]][0] == "Ann"
        assert assignment[ids[2]][0] == UNASSIGNED_LABEL
        assert assignment["c0"] == (UNASSIGNED_LABEL, 1)  # score 10 — out of band


class TestAssignmentInterleaved:
    def test_matches_export_round_robin_when_focus_is_all(self):
        ranked = _ranked_fixture(5)
        ids, _ = focus_selection(ranked, {"mode": "all"})
        assignment = assignment_interleaved(ranked, ids, ["Ann", "Bob"])
        assert assignment["c0"] == ("Ann", 1)
        assert assignment["c1"] == ("Bob", 2)
        assert assignment["c2"] == ("Ann", 3)
        assert assignment["c4"] == ("Ann", 5)

    def test_interleaves_only_within_focus(self):
        ranked = _ranked_fixture(6)
        ids, _ = focus_selection(ranked, {"mode": "top_n", "top_n": 3})
        assignment = assignment_interleaved(ranked, ids, ["Ann", "Bob"])
        assert assignment["c0"][0] == "Ann"
        assert assignment["c1"][0] == "Bob"
        assert assignment["c2"][0] == "Ann"
        assert assignment["c3"] == (UNASSIGNED_LABEL, 4)

    def test_empty_caller_pool_raises(self):
        with pytest.raises(ValueError):
            assignment_interleaved([], [], [])


class TestBlocksWithFeedback:
    def test_reports_count_and_score_range_per_block(self):
        ranked = _ranked_fixture(10)
        enriched = blocks_with_feedback(
            [{"caller": "Ann", "start": 1, "end": 4}], ranked, 10)
        assert enriched[0]["companies"] == 4
        assert enriched[0]["best_score"] == 10.0
        assert enriched[0]["worst_score"] == 8.5

    def test_block_beyond_focus_is_clamped(self):
        ranked = _ranked_fixture(3)
        enriched = blocks_with_feedback(
            [{"caller": "Ann", "start": 2, "end": 99}], ranked, 3)
        assert enriched[0]["companies"] == 2

    def test_empty_block_reports_zero_and_none(self):
        enriched = blocks_with_feedback(
            [{"caller": "Ann", "start": 5, "end": 4}], [], 0)
        assert enriched[0]["companies"] == 0
        assert enriched[0]["best_score"] is None


class TestAllocationMapSegments:
    def test_segments_in_rank_order_with_out_of_focus_tail(self):
        blocks = [
            {"caller": "Ann", "start": 1, "end": 3},
            {"caller": "Bob", "start": 4, "end": 6},
        ]
        segments = allocation_map_segments(blocks, 6, 10)
        assert segments == [
            {"label": "Ann", "count": 3},
            {"label": "Bob", "count": 3},
            {"label": OUT_OF_FOCUS_LABEL, "count": 4},
        ]

    def test_gap_positions_labelled_unassigned(self):
        segments = allocation_map_segments(
            [{"caller": "Ann", "start": 1, "end": 2}], 4, 4)
        assert segments == [
            {"label": "Ann", "count": 2},
            {"label": UNASSIGNED_LABEL, "count": 2},
        ]

    def test_overlap_last_block_wins_in_the_map_too(self):
        segments = allocation_map_segments([
            {"caller": "Ann", "start": 1, "end": 4},
            {"caller": "Bob", "start": 3, "end": 4},
        ], 4, 4)
        assert segments == [
            {"label": "Ann", "count": 2},
            {"label": "Bob", "count": 2},
        ]


class TestPerCallerSummaryDataframe:
    def test_one_row_per_caller_with_scores_and_tiers(self):
        ranked = [
            _item("a", 9.0, 1, tier="🥇 Hot"),
            _item("b", 8.0, 2, tier="🥈 Warm"),
            _item("c", 5.0, 3, tier="🥉 Cool"),
        ]
        assignment = {
            "a": ("Ann", 1), "b": ("Ann", 2), "c": (UNASSIGNED_LABEL, 3)}
        df = per_caller_summary_dataframe(ranked, assignment)
        ann = df[df["caller"] == "Ann"].iloc[0]
        assert ann["companies"] == 2
        assert ann["best_score"] == 9.0
        assert ann["worst_score"] == 8.0
        assert ann["🥇 Hot"] == 1
        assert ann["🥈 Warm"] == 1
        # Unassigned bucket is always the last row.
        assert df.iloc[-1]["caller"] == UNASSIGNED_LABEL

    def test_callers_sorted_by_best_score(self):
        ranked = [_item("a", 9.0, 1), _item("b", 5.0, 2)]
        assignment = {"a": ("Bob", 1), "b": ("Ann", 2)}
        df = per_caller_summary_dataframe(ranked, assignment)
        assert list(df["caller"]) == ["Bob", "Ann"]

    def test_empty_assignment_returns_empty_dataframe(self):
        assert per_caller_summary_dataframe([], {}).empty


class TestCallerDistributionDataframe:
    def test_before_after_rows_include_dropped_and_added_callers(self):
        original = [_item("a", 9.0, 1, caller="Old"), _item("b", 8.0, 2, caller="Old")]
        new = [_item("a", 9.0, 1, caller="New"), _item("b", 8.0, 2, caller="New")]
        df = caller_distribution_dataframe(original, new)
        old_new = df[(df["caller"] == "Old") & (df["period"] == "New")]
        assert old_new["count"].iloc[0] == 0
        new_current = df[(df["caller"] == "New") & (df["period"] == "Current")]
        assert new_current["count"].iloc[0] == 0

    def test_blank_caller_labelled(self):
        original = [_item("a", 9.0, 1, caller="")]
        df = caller_distribution_dataframe(original, original)
        assert UNASSIGNED_LABEL in set(df["caller"])

    def test_ordered_by_new_workload_descending(self):
        original = [_item(f"c{i}", 9.0 - i, i + 1, caller="A") for i in range(3)]
        new = [
            _item("c0", 9.0, 1, caller="B"),
            _item("c1", 8.0, 2, caller="B"),
            _item("c2", 7.0, 3, caller="A"),
        ]
        df = caller_distribution_dataframe(original, new)
        assert list(dict.fromkeys(df["caller"])) == ["B", "A"]


class TestMoversDataframe:
    def test_lists_only_companies_whose_caller_changes(self):
        original = [_item("a", 9.0, 1, caller="Ann"), _item("b", 8.0, 2, caller="Bob")]
        assignment = {"a": ("Ann", 1), "b": ("Ann", 2)}
        df = movers_dataframe(original, assignment)
        assert list(df["company_id"]) == ["b"]
        assert df.iloc[0]["caller_before"] == "Bob"
        assert df.iloc[0]["caller_after"] == "Ann"

    def test_no_changes_yields_empty_dataframe(self):
        original = [_item("a", 9.0, 1, caller="Ann")]
        assert movers_dataframe(original, {"a": ("Ann", 1)}).empty
