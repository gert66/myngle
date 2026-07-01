"""Tests for the Lead Prioritizer v2 shared batch core.

``prioritize_single_lead`` is mocked — no live APIs, no real keys.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from lead_output_schema import LeadPrioritizationResult, LeadEvidence, LeadSignal
import lead_prioritizer_batch_core as bc
from lead_prioritizer_batch_core import (
    BatchRunConfig,
    resolve_pipeline_flags,
    select_batch_rows,
    flatten_result_for_excel,
    flatten_evidence_for_excel,
    flatten_signals_for_excel,
    build_run_summary_dataframe,
    build_excel_workbook_bytes,
    run_batch_dataframe,
)


def _sample_result(**kw) -> LeadPrioritizationResult:
    base = dict(
        company_name="Acme", domain="acme.com", input_country="Italy",
        v2_pipeline_mode="full_v2_single_lead",
        hq_detected_country="Germany", hq_structure_type="foreign_parent",
        sig_foreign_hq_score_for_next_scoring=3.0,
        sig_international_profile_score=2.0,
        final_commercial_fit_score=7.2, commercial_tier="🥇 Hot",
        what_is_hot_app="Foreign HQ signal: Germany",
        ai_hq_raw_json='{"classification":"foreign_parent"}',
        evidence_items=[
            LeadEvidence(evidence_id="international_profile:organic:1",
                         signal_name="international_profile",
                         source_url="https://acme.com/intl",
                         source_title="Global", source_snippet="global offices",
                         query_used="q", parser_source="serper_organic_1",
                         source_type="organic"),
        ],
        signals=[
            LeadSignal(signal_name="international_profile", signal_value="positive_evidence",
                       signal_score=2.0, signal_confidence="High",
                       signal_reason="2 hits", evidence_url="https://acme.com/intl"),
        ],
    )
    base.update(kw)
    return LeadPrioritizationResult(**base)


# ---------------------------------------------------------------------------
# resolve_pipeline_flags
# ---------------------------------------------------------------------------

class TestResolveFlags:
    def test_full(self):
        f = resolve_pipeline_flags("full")
        assert f["run_full_v2_pipeline"] is True
        assert all(not f[k] for k in f if k != "run_full_v2_pipeline")

    def test_hq_only(self):
        assert all(v is False for v in resolve_pipeline_flags("hq_only").values())

    def test_evidence_only(self):
        f = resolve_pipeline_flags("evidence_only")
        assert f["collect_non_hq_evidence"] is True
        assert f["extract_non_hq_signals_flag"] is False
        assert f["run_full_v2_pipeline"] is False

    def test_signals_no_score(self):
        f = resolve_pipeline_flags("signals_no_score")
        assert f["collect_non_hq_evidence"] is True
        assert f["extract_non_hq_signals_flag"] is True
        assert f["build_app_summary_fields_flag"] is True
        assert f["calculate_commercial_score_flag"] is False
        assert f["build_caller_app_fields_flag"] is False

    def test_full_no_score(self):
        f = resolve_pipeline_flags("full_no_score")
        assert f["collect_non_hq_evidence"] is True
        assert f["extract_non_hq_signals_flag"] is True
        assert f["build_app_summary_fields_flag"] is True
        assert f["build_caller_app_fields_flag"] is True
        assert f["calculate_commercial_score_flag"] is False
        assert f["run_full_v2_pipeline"] is False

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            resolve_pipeline_flags("nope")


# ---------------------------------------------------------------------------
# select_batch_rows
# ---------------------------------------------------------------------------

class TestSelectRows:
    _df = pd.DataFrame({"c": list(range(10)), "d": [f"d{i}" for i in range(10)]})

    def _cfg(self, **kw):
        base = dict(company_name_column="c", domain_column="d")
        base.update(kw)
        return BatchRunConfig(**base)

    def test_start_and_limit(self):
        sub = select_batch_rows(self._df, self._cfg(start_row=2, row_limit=3))
        assert list(sub["c"]) == [2, 3, 4]
        assert list(sub.index) == [2, 3, 4]  # original index preserved

    def test_limit_zero_means_all_remaining(self):
        sub = select_batch_rows(self._df, self._cfg(start_row=7, row_limit=0))
        assert list(sub["c"]) == [7, 8, 9]

    def test_limit_larger_than_frame(self):
        sub = select_batch_rows(self._df, self._cfg(start_row=0, row_limit=100))
        assert len(sub) == 10


# ---------------------------------------------------------------------------
# Flatten helpers
# ---------------------------------------------------------------------------

class TestFlatten:
    def test_result_includes_key_fields_and_originals(self):
        row = flatten_result_for_excel(
            _sample_result(), {"c": "Acme", "d": "acme.com", "extra": "x"},
            source_index=5, run_success=True, run_error="")
        # original columns preserved
        assert row["c"] == "Acme" and row["extra"] == "x"
        # metadata
        assert row["source_index"] == 5 and row["run_success"] is True
        # HQ / score / app
        assert row["hq_detected_country"] == "Germany"
        assert row["sig_foreign_hq_score_for_next_scoring"] == 3.0
        assert row["final_commercial_fit_score"] == 7.2
        assert row["commercial_tier"] == "🥇 Hot"
        assert row["what_is_hot_app"] == "Foreign HQ signal: Germany"
        assert row["v2_pipeline_mode"] == "full_v2_single_lead"
        assert row["evidence_count"] == 1 and row["signal_count"] == 1

    def test_raw_ai_json_excluded_by_default(self):
        row = flatten_result_for_excel(_sample_result(), {}, 0, True, "")
        assert "ai_hq_raw_json" not in row

    def test_raw_ai_json_included_when_flagged(self):
        row = flatten_result_for_excel(_sample_result(), {}, 0, True, "",
                                       include_raw_ai_json=True)
        assert row["ai_hq_raw_json"].startswith("{")

    def test_no_api_key_fields(self):
        row = flatten_result_for_excel(_sample_result(), {"c": "Acme"}, 0, True, "")
        for key in row:
            k = key.lower()
            assert "api_key" not in k and "apikey" not in k
            assert "serper" not in k and "anthropic_api" not in k
        # also no competitor field surfaced
        assert not any("competitor" in k.lower() for k in row)

    def test_error_row_has_metadata_only(self):
        row = flatten_result_for_excel(None, {"c": "Acme"}, 3, False, "Boom: x")
        assert row["run_success"] is False
        assert row["run_error"] == "Boom: x"
        assert row["evidence_count"] == 0 and row["signal_count"] == 0
        assert "hq_detected_country" not in row

    def test_evidence_one_row_per_item(self):
        rows = flatten_evidence_for_excel(_sample_result(), 2)
        assert len(rows) == 1
        assert rows[0]["source_index"] == 2
        assert rows[0]["signal_name"] == "international_profile"
        assert rows[0]["source_url"] == "https://acme.com/intl"

    def test_signals_one_row_per_signal(self):
        rows = flatten_signals_for_excel(_sample_result(), 4)
        assert len(rows) == 1
        assert rows[0]["source_index"] == 4
        assert rows[0]["signal_score"] == 2.0


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

class TestRunSummary:
    def test_summary_fields_no_keys(self):
        cfg = BatchRunConfig(company_name_column="c", domain_column="d",
                             run_mode="full", start_row=1, row_limit=5)
        df = build_run_summary_dataframe(cfg, total_input_rows=20, selected_rows=5,
                                         processed_rows=5, success_count=4, error_count=1)
        rec = df.iloc[0].to_dict()
        assert rec["run_mode"] == "full"
        assert rec["total_input_rows"] == 20
        assert rec["success_count"] == 4 and rec["error_count"] == 1
        for k in rec:
            assert "api_key" not in k.lower() and "serper" not in k.lower()


# ---------------------------------------------------------------------------
# run_batch_dataframe
# ---------------------------------------------------------------------------

class TestRunBatch:
    _df = pd.DataFrame({
        "company": ["Acme", "Beta", "Gamma"],
        "domain": ["acme.com", "beta.com", "gamma.com"],
    })

    def test_calls_pipeline_with_full_flags(self):
        seen = {}

        def _fake(lead_input, **kwargs):
            seen.update(kwargs)
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=1)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_dataframe(self._df, cfg, "SERP", "ANTH")
        assert seen.get("run_full_v2_pipeline") is True
        assert seen.get("default_input_country") == "Italy"
        # keys passed through but never surface in output
        assert out["enriched_leads"].shape[0] == 1
        blob = out["enriched_leads"].to_csv(index=False)
        assert "SERP" not in blob and "ANTH" not in blob

    def test_continue_on_error(self):
        calls = {"n": 0}

        def _fake(lead_input, **kwargs):
            calls["n"] += 1
            if lead_input.company_name == "Beta":
                raise RuntimeError("boom")
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=0, continue_on_error=True)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_dataframe(self._df, cfg, "k1", "k2")
        assert calls["n"] == 3  # did not stop at Beta
        enriched = out["enriched_leads"]
        assert enriched.shape[0] == 3
        assert (~enriched["run_success"]).sum() == 1
        assert out["run_summary"].iloc[0]["error_count"] == 1

    def test_progress_callback_once_per_row(self):
        payloads = []

        def _fake(lead_input, **kwargs):
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=0)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            run_batch_dataframe(self._df, cfg, "k1", "k2",
                                progress_callback=lambda p: payloads.append(p))
        assert len(payloads) == 3  # one per selected row
        last = payloads[-1]
        assert last["selected_rows"] == 3
        assert last["processed_rows"] == 3
        assert last["success_count"] == 3
        assert last["error_count"] == 0
        # secret-free payload
        for p in payloads:
            for k in p:
                assert "api_key" not in k.lower() and "serper" not in k.lower()
        assert "k1" not in str(payloads) and "k2" not in str(payloads)

    def test_progress_callback_called_for_error_rows(self):
        seen = []

        def _fake(lead_input, **kwargs):
            if lead_input.company_name == "Beta":
                raise RuntimeError("boom")
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=0, continue_on_error=True)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            run_batch_dataframe(self._df, cfg, "k1", "k2",
                                progress_callback=lambda p: seen.append(p))
        assert len(seen) == 3
        beta = [p for p in seen if p["current_company_name"] == "Beta"][0]
        assert beta["run_success"] is False
        assert "boom" in beta["run_error"]

    def test_progress_callback_exception_does_not_break_batch(self):
        def _fake(lead_input, **kwargs):
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=0)

        def _boom(_payload):
            raise ValueError("callback broke")

        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_dataframe(self._df, cfg, "k1", "k2", progress_callback=_boom)
        # Batch still completes fully despite the broken callback.
        assert out["enriched_leads"].shape[0] == 3
        assert out["run_summary"].iloc[0]["success_count"] == 3

    def test_backward_compatible_without_callback(self):
        def _fake(lead_input, **kwargs):
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=1)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_dataframe(self._df, cfg, "k1", "k2")  # no callback
        assert out["enriched_leads"].shape[0] == 1

    def test_stop_on_error_when_configured(self):
        def _fake(lead_input, **kwargs):
            if lead_input.company_name == "Beta":
                raise RuntimeError("boom")
            return _sample_result(company_name=lead_input.company_name)

        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="full", row_limit=0, continue_on_error=False)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_dataframe(self._df, cfg, "k1", "k2")
        # Acme (ok) + Beta (error) then stop → 2 rows, Gamma never processed.
        assert out["enriched_leads"].shape[0] == 2
        assert out["run_summary"].iloc[0]["processed_rows"] == 2


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------

class TestWorkbook:
    def test_returns_bytes_with_expected_sheets(self):
        tables = {
            "enriched_leads": pd.DataFrame([{"company_name": "Acme"}]),
            "evidence": pd.DataFrame([{"signal_name": "international_profile"}]),
            "signals": pd.DataFrame([{"signal_name": "international_profile"}]),
            "run_summary": pd.DataFrame([{"run_mode": "full"}]),
        }
        data = build_excel_workbook_bytes(tables)
        assert isinstance(data, (bytes, bytearray)) and len(data) > 0
        from openpyxl import load_workbook
        import io as _io
        wb = load_workbook(_io.BytesIO(data))
        assert wb.sheetnames == ["Enriched Leads", "Evidence", "Signals", "Run Summary"]

    def test_handles_empty_tables(self):
        data = build_excel_workbook_bytes({})
        from openpyxl import load_workbook
        import io as _io
        wb = load_workbook(_io.BytesIO(data))
        assert wb.sheetnames == ["Enriched Leads", "Evidence", "Signals", "Run Summary"]
