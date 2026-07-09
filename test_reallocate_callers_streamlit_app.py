"""Tests for the pure helpers behind reallocate_callers_streamlit_app.py.

Only the pure helpers are tested — no Streamlit/Plotly rendering. Streamlit
and Plotly are imported lazily inside ``main()`` (see the module docstring),
so this module is importable without them.
"""

from __future__ import annotations

import pandas as pd

from reallocate_callers_from_gcs import assign_callers
from reallocate_callers_streamlit_app import (
    caller_distribution_dataframe,
    movers_dataframe,
    parse_caller_input,
    validate_callers,
)
from test_reallocate_callers_from_gcs import make_item


class TestParseCallerInput:
    def test_splits_commas_and_newlines_dedupes(self):
        assert parse_caller_input("Ann, Bob\nCara,Ann") == ["Ann", "Bob", "Cara"]

    def test_blank_yields_empty(self):
        assert parse_caller_input("   \n , ") == []


class TestValidateCallers:
    def test_error_when_empty(self):
        assert validate_callers([]) is not None

    def test_ok_when_non_empty(self):
        assert validate_callers(["Ann"]) is None


class TestCallerDistributionDataframe:
    def test_before_after_rows_include_dropped_and_added_callers(self):
        original = [
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=8, rank=2, caller="Bob"),
        ]
        # New pool = [Zoe] -> everyone moves to Zoe; Ann/Bob drop to 0.
        assignment = assign_callers(original, ["Zoe"])
        new_items = [
            {**it, "assigned_cold_caller": assignment[it["company_id"]][0]}
            for it in original
        ]
        df = caller_distribution_dataframe(original, new_items)
        pivot = df.pivot(index="caller", columns="when", values="count").fillna(0)
        assert pivot.loc["Ann", "Huidig"] == 1
        assert pivot.loc["Ann", "Nieuw"] == 0
        assert pivot.loc["Zoe", "Nieuw"] == 2
        assert pivot.loc["Zoe", "Huidig"] == 0

    def test_blank_caller_labelled(self):
        original = [make_item("c1", score=1, rank=1, caller=None)]
        df = caller_distribution_dataframe(original, original)
        assert "— (geen)" in set(df["caller"])


class TestMoversDataframe:
    def test_lists_only_changed_callers(self):
        original = [
            make_item("c1", score=9, rank=1, caller="Ann"),
            make_item("c2", score=8, rank=2, caller="Bob"),
            make_item("c3", score=7, rank=3, caller="Cara"),
        ]
        assignment = assign_callers(original, ["Ann", "Bob"])  # c3 Cara->Ann
        df = movers_dataframe(original, assignment)
        assert list(df["company_id"]) == ["c3"]
        assert df.iloc[0]["caller_before"] == "Cara"
        assert df.iloc[0]["caller_after"] == "Ann"

    def test_empty_when_nothing_moves(self):
        original = [make_item("c1", score=9, rank=1, caller="Ann")]
        assignment = assign_callers(original, ["Ann"])
        df = movers_dataframe(original, assignment)
        assert isinstance(df, pd.DataFrame)
        assert df.empty
