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

    def test_c4_safety_fields_in_enriched_leads(self):
        result = _sample_result(
            hq_positive_score_suppressed_for_review="Yes",
            hq_evidence_domain_mismatch_warning="Yes",
            hq_review_reason="risky domain root and evidence URL does not match lead domain",
            hq_query_risk_flag="Yes",
            hq_evidence_domain_match="No",
        )
        row = flatten_result_for_excel(result, {"c": "Acme"}, 0, True, "")
        assert row["hq_positive_score_suppressed_for_review"] == "Yes"
        assert row["hq_evidence_domain_mismatch_warning"] == "Yes"
        assert row["hq_review_reason"].startswith("risky domain root")
        assert row["hq_query_risk_flag"] == "Yes"
        assert row["hq_evidence_domain_match"] == "No"

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


# ---------------------------------------------------------------------------
# C5 batch integration (optional, country-agnostic) — adjudicator mocked
# ---------------------------------------------------------------------------

from lead_output_schema import HQDetectionResult  # noqa: E402
from lead_hq_sonnet_adjudicator import SonnetHQAdjudicationResult  # noqa: E402
from lead_prioritizer_batch_core import (  # noqa: E402
    apply_c5_adjudication,
    add_c5_summary_fields,
    row_selected_for_c5,
)


def _enriched_rows():
    """Two rows: one HQ score-3 foreign, one score-0 domestic (manual review)."""
    return [
        {"company_name": "Nissan do Brasil", "domain": "nissan.com.br",
         "input_country": "Brazil", "sig_foreign_hq_score_for_next_scoring": 3.0,
         "needs_manual_review": False, "hq_positive_score_suppressed_for_review": "No",
         "hq_detected_country": "Japan", "ai_parent_hq_country": "Japan"},
        {"company_name": "Empresa Uruguaya", "domain": "empresa.com.uy",
         "input_country": "Uruguay", "sig_foreign_hq_score_for_next_scoring": 0.0,
         "needs_manual_review": True, "hq_positive_score_suppressed_for_review": "No",
         "hq_detected_country": "", "ai_parent_hq_country": ""},
    ]


def _mk_result(adjudication="unclear", confidence="Low", target="unclear",
               call_success=True, error="", model="claude-sonnet-5"):
    return SonnetHQAdjudicationResult(
        adjudication=adjudication, confidence=confidence, target_company_match=target,
        parent_company="", parent_hq_country="", parent_hq_city="",
        reason="r", model=model, call_attempted=True,
        call_success=call_success, error=error)


def _patch_adjudicator(result_or_fn):
    # apply_c5_adjudication reuses adjudicate_row from the probe, which calls
    # adjudicate_hq_with_sonnet in the probe's namespace.
    if callable(result_or_fn):
        return patch("run_hq_sonnet_adjudication_probe.adjudicate_hq_with_sonnet",
                     side_effect=result_or_fn)
    return patch("run_hq_sonnet_adjudication_probe.adjudicate_hq_with_sonnet",
                 return_value=result_or_fn)


class TestC5RowSelection:
    def test_scope_variants(self):
        rows = _enriched_rows()
        assert [row_selected_for_c5(r, "all_rows") for r in rows] == [True, True]
        assert [row_selected_for_c5(r, "score_3_only") for r in rows] == [True, False]
        assert [row_selected_for_c5(r, "score_3_or_manual_review") for r in rows] == [True, True]
        assert [row_selected_for_c5(r, "manual_review_or_suppressed") for r in rows] == [False, True]

    def test_suppressed_selected(self):
        r = {"sig_foreign_hq_score_for_next_scoring": 0.0, "needs_manual_review": False,
             "hq_positive_score_suppressed_for_review": "Yes"}
        assert row_selected_for_c5(r, "manual_review_or_suppressed") is True


class TestC5Disabled:
    def test_run_batch_alone_has_no_c5_columns(self):
        df = pd.DataFrame({"company": ["A"], "domain": ["a.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="hq_only", row_limit=1)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   return_value=_sample_result()):
            out = run_batch_dataframe(df, cfg, "k1", "k2")
        assert not any(c.startswith("c5_") for c in out["enriched_leads"].columns)


class TestC5AppendOnly:
    def test_scores_unchanged_fields_added(self):
        rows = _enriched_rows()
        with _patch_adjudicator(_mk_result(adjudication="domestic_confirmed",
                                           confidence="High", target="yes")):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="claude-sonnet-5",
                model_tier="sonnet", scoring_behavior="append_only", scope="all_rows")
        # original scores untouched
        assert out[0]["sig_foreign_hq_score_for_next_scoring"] == 3.0
        assert out[1]["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert out[0]["needs_manual_review"] is False
        # C5 fields present
        assert out[0]["c5_adjudication"] == "domestic_confirmed"
        assert out[0]["c5_call_attempted"] is True
        assert "c5_possible_foreign_parent_for_review" in out[0]
        assert counts["c5_rows_attempted"] == 2


class TestC5Conservative:
    def test_confirms_old_score_3(self):
        rows = [_enriched_rows()[0]]  # score 3
        with _patch_adjudicator(_mk_result("foreign_parent_confirmed", "High", "yes")):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scoring_behavior="conservative_adjustment", scope="all_rows")
        assert out[0]["sig_foreign_hq_score_for_next_scoring"] == 3.0
        assert counts["c5_downgraded_score_3_count"] == 0

    def test_downgrades_old_score_3_when_domestic(self):
        rows = [_enriched_rows()[0]]
        with _patch_adjudicator(_mk_result("domestic_confirmed", "High", "yes")):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scoring_behavior="conservative_adjustment", scope="all_rows")
        assert out[0]["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert out[0]["needs_manual_review"] is True
        assert counts["c5_downgraded_score_3_count"] == 1
        assert "downgraded" in out[0]["hq_reason"].lower()

    def test_does_not_auto_upgrade_old_score_0(self):
        rows = [_enriched_rows()[1]]  # score 0
        with _patch_adjudicator(_mk_result("foreign_parent_confirmed", "High", "yes")):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scoring_behavior="conservative_adjustment", scope="all_rows")
        assert out[0]["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert out[0]["c5_possible_foreign_parent_for_review"] is True
        assert out[0]["needs_manual_review"] is True
        assert counts["c5_possible_foreign_parent_for_review_count"] == 1

    def test_failure_old_3_downgrades_old_0_stays_with_error(self):
        rows = _enriched_rows()  # [score3, score0]
        fail = _mk_result(adjudication="unclear", call_success=False,
                          error="sonnet_parse_failed")
        with _patch_adjudicator(fail):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scoring_behavior="conservative_adjustment", scope="all_rows")
        # old score 3 → 0 + review
        assert out[0]["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert out[0]["needs_manual_review"] is True
        assert out[0]["c5_error"] == "sonnet_parse_failed"
        # old score 0 → stays 0 + review, error present
        assert out[1]["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert out[1]["needs_manual_review"] is True
        assert out[1]["c5_error"] == "sonnet_parse_failed"
        assert counts["c5_error_count"] == 2


class TestC5ScopeFiltering:
    def test_score_3_only_sends_one(self):
        rows = _enriched_rows()
        with _patch_adjudicator(_mk_result()):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scope="score_3_only")
        assert out[0]["c5_call_attempted"] is True
        assert out[1]["c5_call_attempted"] is False   # not sent → blank
        assert out[1]["c5_possible_foreign_parent_for_review"] is False
        assert counts["c5_rows_attempted"] == 1

    def test_all_rows_sends_all(self):
        rows = _enriched_rows()
        with _patch_adjudicator(_mk_result()):
            out, counts = apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scope="all_rows")
        assert counts["c5_rows_attempted"] == 2


class TestC5CountryAgnostic:
    def test_input_country_passed_through(self):
        rows = _enriched_rows()  # Brazil + Uruguay
        seen = []

        def _capture(**kwargs):
            seen.append(kwargs.get("input_country"))
            return _mk_result()

        with _patch_adjudicator(_capture):
            apply_c5_adjudication(
                rows, anthropic_api_key="k", model_used="m", model_tier="sonnet",
                scope="all_rows")
        assert "Brazil" in seen
        assert "Uruguay" in seen
        # no hardcoded country substituted
        assert set(seen) == {"Brazil", "Uruguay"}


class TestC5RunSummary:
    def test_summary_includes_settings_and_counts(self):
        base = build_run_summary_dataframe(
            BatchRunConfig(company_name_column="c", domain_column="d",
                           run_mode="hq_only"),
            total_input_rows=2, selected_rows=2, processed_rows=2,
            success_count=2, error_count=0)
        counts = {
            "c5_rows_attempted": 2, "c5_success_count": 2, "c5_error_count": 0,
            "c5_foreign_parent_confirmed_count": 1, "c5_domestic_confirmed_count": 1,
            "c5_unclear_count": 0, "c5_recommended_score_3_count": 1,
            "c5_possible_foreign_parent_for_review_count": 0,
            "c5_downgraded_score_3_count": 0,
        }
        summ = add_c5_summary_fields(
            base, c5_enabled=True, c5_scoring_behavior="conservative_adjustment",
            c5_scope="all_rows", c5_model_tier="sonnet", c5_model_used="claude-sonnet-5",
            counts=counts)
        rec = summ.iloc[0].to_dict()
        assert rec["c5_enabled"] is True
        assert rec["c5_scoring_behavior"] == "conservative_adjustment"
        assert rec["c5_scope"] == "all_rows"
        assert rec["c5_model_tier"] == "sonnet"
        assert rec["c5_model_used"] == "claude-sonnet-5"
        assert rec["c5_rows_attempted"] == 2
        assert rec["c5_foreign_parent_confirmed_count"] == 1
        assert rec["c5_downgraded_score_3_count"] == 0
