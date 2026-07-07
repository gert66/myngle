"""Tests for usage_tracker: per-run API usage counting, cost estimate, history
CSV, run isolation, and the Streamlit render smoke path (stubbed streamlit)."""
from __future__ import annotations

import csv
import sys
import types
from unittest.mock import MagicMock

import usage_tracker as ut


def _seed_basic():
    ut.reset()
    ut.record_serper_call("hq")
    ut.record_serper_call("non_hq")
    ut.record_serper_call("non_hq")
    ut.record_serper_call("icp_context")
    ut.record_anthropic_call("claude-haiku-4-5-20251001", 1000, 200)
    ut.record_anthropic_call("claude-haiku-4-5-20251001", 2000, 400)
    ut.record_firecrawl_call()


def test_counts_and_breakdown():
    _seed_basic()
    s = ut.snapshot()
    assert s["serper_total"] == 4
    assert s["serper_by_kind"] == {"hq": 1, "non_hq": 2, "icp_context": 1}
    assert s["anthropic_calls"] == 2
    assert s["anthropic_input_tokens"] == 3000
    assert s["anthropic_output_tokens"] == 600
    assert s["anthropic_avg_input_tokens"] == 1500.0
    assert s["firecrawl_calls"] == 1


def test_cost_estimate_matches_pricing_table():
    _seed_basic()
    s = ut.snapshot()
    # Haiku 4.5 = (1.00, 5.00) USD/Mtok -> (3000*1 + 600*5)/1e6 = 0.006
    assert s["estimated_anthropic_usd"] == 0.006
    # serper 4 * 0.001 + firecrawl 1 * 0.001 + anthropic 0.006 = 0.011
    assert s["estimated_total_usd"] == round(0.006 + 4 * ut.SERPER_USD_PER_CALL
                                             + 1 * ut.FIRECRAWL_USD_PER_CALL, 6)
    assert s["estimated_total_eur"] == round(s["estimated_total_usd"] * ut.USD_TO_EUR, 4)


def test_unknown_kind_bucketed_as_other():
    ut.reset()
    ut.record_serper_call("something_new")
    assert ut.snapshot()["serper_by_kind"] == {"other": 1}


def test_unpriced_model_gives_none_anthropic_cost_but_keeps_tokens():
    ut.reset()
    ut.record_anthropic_call("some-unpriced-model", 1000, 500)
    s = ut.snapshot()
    assert s["anthropic_input_tokens"] == 1000
    assert s["estimated_anthropic_usd"] is None  # never a guessed cost
    # total cost still computable from serper/firecrawl (both zero here)
    assert s["estimated_total_usd"] == 0.0


def test_reset_isolates_runs():
    _seed_basic()
    ut.reset()
    s = ut.snapshot()
    assert s["serper_total"] == 0
    assert s["anthropic_calls"] == 0
    assert s["firecrawl_calls"] == 0


def test_record_anthropic_response_extracts_usage():
    ut.reset()
    resp = types.SimpleNamespace(
        usage=types.SimpleNamespace(input_tokens=123, output_tokens=45))
    ut.record_anthropic_response(resp, "claude-haiku-4-5-20251001", "hq")
    s = ut.snapshot()
    assert s["anthropic_input_tokens"] == 123
    assert s["anthropic_output_tokens"] == 45


def test_record_helpers_never_raise_on_garbage():
    ut.reset()
    ut.record_anthropic_response(object(), None)   # no usage attr -> still a call
    ut.record_anthropic_call(None, None, None)
    s = ut.snapshot()
    assert s["anthropic_calls"] == 2               # both counted, tokens stay 0
    assert s["anthropic_input_tokens"] == 0


def test_history_csv_grows(tmp_path):
    path = tmp_path / "usage_history.csv"
    _seed_basic()
    ut.append_history(companies=3, path=str(path))
    ut.append_history(companies=2, path=str(path))
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert rows[0][0] == "timestamp_utc"      # header once
    assert len(rows) == 3                       # header + 2 runs
    assert rows[1][1] == "3" and rows[2][1] == "2"


def test_format_summary_text_is_ascii_only():
    _seed_basic()
    text = ut.format_summary_text()
    text.encode("ascii")  # must not raise on a cp1252 console
    assert "API usage" in text and "TOTAL" in text


def test_render_usage_summary_smoke(monkeypatch):
    # Stub streamlit so the app's render function runs without a real UI.
    from lead_prioritizer_batch_app import render_usage_summary
    fake_st = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=None)
    cm.__exit__ = MagicMock(return_value=False)
    fake_st.expander.return_value = cm
    fake_st.columns.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
    monkeypatch.setitem(sys.modules, "streamlit", fake_st)
    _seed_basic()
    render_usage_summary(ut.snapshot())   # must not raise
    assert fake_st.expander.called
