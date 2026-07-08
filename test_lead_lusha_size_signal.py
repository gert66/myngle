"""Tests for lead_lusha_size_signal.py (Lusha enrichment plan, Stap 3).

Covers: employee-range normalization (incl. the pathological Lusha
top-bucket sentinel), revenue-bucket validity checks, blank/placeholder
handling, and the combined lusha_size_signal() entry point -- a hit
always scores positive_evidence/2.0/High with no evidence_url (structured
Lusha data, not a web snippet); no hit returns None so the caller falls
back to the existing Serper-based signal.
"""

from __future__ import annotations

import lead_lusha_size_signal as ls


class TestNormalizeEmployeeRangeLabel:
    def test_plain_range(self):
        assert ls.normalize_employee_range_label("201-500") == "201-500"

    def test_range_with_existing_separators(self):
        assert ls.normalize_employee_range_label("1,001-5,000") == "1,001-5,000"

    def test_pathological_top_bucket_collapses_to_open_ended(self):
        assert ls.normalize_employee_range_label("100001-10000000") == "100,001+"

    def test_already_open_ended_bucket(self):
        assert ls.normalize_employee_range_label("10001+") == "10,001+"

    def test_single_value(self):
        assert ls.normalize_employee_range_label("42") == "42"

    def test_blank_returns_none(self):
        assert ls.normalize_employee_range_label("") is None
        assert ls.normalize_employee_range_label(None) is None

    def test_placeholder_values_return_none(self):
        for v in ("Unknown", "N/A", "n/a", "-", "null", "NaN"):
            assert ls.normalize_employee_range_label(v) is None, v

    def test_garbage_text_returns_none(self):
        assert ls.normalize_employee_range_label("a lot of people") is None


class TestNormalizeRevenueLabel:
    def test_plain_bucket(self):
        assert ls.normalize_revenue_label("$50M - $100M") == "$50M - $100M"

    def test_open_ended_bucket(self):
        assert ls.normalize_revenue_label("$100B+") == "$100B+"

    def test_small_bucket(self):
        assert ls.normalize_revenue_label("$1 - $1M") == "$1 - $1M"

    def test_blank_returns_none(self):
        assert ls.normalize_revenue_label("") is None
        assert ls.normalize_revenue_label(None) is None

    def test_placeholder_values_return_none(self):
        for v in ("Unknown", "N/A", "-", "null"):
            assert ls.normalize_revenue_label(v) is None, v

    def test_garbage_text_returns_none(self):
        assert ls.normalize_revenue_label("lots of money") is None


class TestLushaSizeSignal:
    def test_employees_only(self):
        sig = ls.lusha_size_signal("201-500", "")
        assert sig is not None
        assert sig.signal_name == "company_size_complexity"
        assert sig.signal_value == "positive_evidence"
        assert sig.signal_score == 2.0
        assert sig.signal_confidence == "High"
        assert sig.evidence_url is None
        assert sig.parser_source == "lusha_size_data"
        assert "201-500 employees" in sig.signal_reason

    def test_revenue_only(self):
        sig = ls.lusha_size_signal("", "$1B - $10B")
        assert sig is not None
        assert "$1B - $10B revenue" in sig.signal_reason

    def test_both_employees_and_revenue(self):
        sig = ls.lusha_size_signal("100001-10000000", "$50M - $100M")
        assert sig is not None
        assert "100,001+ employees" in sig.signal_reason
        assert "$50M - $100M revenue" in sig.signal_reason

    def test_both_blank_returns_none(self):
        assert ls.lusha_size_signal("", "") is None
        assert ls.lusha_size_signal(None, None) is None

    def test_both_unparseable_returns_none(self):
        assert ls.lusha_size_signal("garbage", "also garbage") is None

    def test_placeholder_values_return_none(self):
        assert ls.lusha_size_signal("Unknown", "N/A") is None

    def test_one_unparseable_one_usable_still_returns_signal(self):
        sig = ls.lusha_size_signal("garbage", "$50M - $100M")
        assert sig is not None
        assert "revenue" in sig.signal_reason
        assert "employees" not in sig.signal_reason
