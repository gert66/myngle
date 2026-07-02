"""Tests for the Lead Prioritizer v2 shared batch core.

``prioritize_single_lead`` is mocked — no live APIs, no real keys.
"""

from __future__ import annotations

import time
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

    def test_employer_branding_fields_included(self):
        result = _sample_result(
            sig_employer_branding_score=2.0,
            employer_branding_reason="2 distinct keyword match(es) in evidence: "
                                      "employer branding, employee satisfaction",
            employer_branding_evidence_url="https://acme.com/careers",
            employer_branding_evidence_quote="Recognized as a great place to work.",
        )
        row = flatten_result_for_excel(result, {"c": "Acme"}, 0, True, "")
        assert row["sig_employer_branding_score"] == 2.0
        assert row["employer_branding_reason"].startswith("2 distinct")
        assert row["employer_branding_evidence_url"] == "https://acme.com/careers"
        assert row["employer_branding_evidence_quote"] == \
            "Recognized as a great place to work."

    def test_sector_fields_included(self):
        result = _sample_result(
            detected_industry="Consumer goods",
            detected_sub_industry="Consumer electronics",
            detected_company_type="Subsidiary",
            sector_confidence="High",
            sector_reason="Matched sector keyword(s): consumer electronics.",
            sector_evidence_url="https://acme.com/about",
            sector_evidence_quote="Acme is a consumer electronics company.",
            sector_source_title="About Acme",
        )
        row = flatten_result_for_excel(result, {"c": "Acme"}, 0, True, "")
        assert row["detected_industry"] == "Consumer goods"
        assert row["detected_sub_industry"] == "Consumer electronics"
        assert row["detected_company_type"] == "Subsidiary"
        assert row["sector_confidence"] == "High"
        assert row["sector_reason"].startswith("Matched sector")
        assert row["sector_evidence_url"] == "https://acme.com/about"
        assert row["sector_evidence_quote"] == "Acme is a consumer electronics company."
        assert row["sector_source_title"] == "About Acme"

    def test_sector_industry_evidence_rows_flattened(self):
        result = _sample_result(evidence_items=[
            LeadEvidence(evidence_id="sector_industry:organic:1",
                         signal_name="sector_industry",
                         source_url="https://acme.com/about",
                         source_title="About", source_snippet="a retail company",
                         query_used="acme company industry sector",
                         parser_source="serper_organic_1", source_type="organic"),
        ])
        rows = flatten_evidence_for_excel(result, source_index=2)
        assert len(rows) == 1
        assert rows[0]["signal_name"] == "sector_industry"
        assert rows[0]["source_snippet"] == "a retail company"

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
    run_batch_foreign_hq_only,
    run_batch_non_english_foreign_hq_only,
    run_batch_dataframe_parallel,
    run_single_batch_unit,
    split_dataframe_into_chunks,
    aggregate_parallel_chunk_progress,
    classify_parent_hq_language_market,
    resolve_parent_hq_country_for_export,
    FOREIGN_HQ_ONLY_MODE,
    NON_ENGLISH_FOREIGN_HQ_ONLY_MODE,
    MAX_PARALLEL_WORKERS,
    SUPPORTED_RUN_MODES,
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


# ---------------------------------------------------------------------------
# "Full enrichment, confirmed foreign-HQ only" batch mode
# ---------------------------------------------------------------------------

def _fake_prioritize_for_foreign_hq(scores: dict):
    """side_effect for prioritize_single_lead: hq_only calls report `scores`;
    full calls (run_full_v2_pipeline=True) report the same score plus a
    final_commercial_fit_score marker so tests can detect enrichment ran."""
    def _fake(lead_input, **kwargs):
        score = scores.get(lead_input.company_name, 0.0)
        if kwargs.get("run_full_v2_pipeline"):
            return _sample_result(
                company_name=lead_input.company_name, domain=lead_input.domain,
                input_country=lead_input.input_country or "Brazil",
                sig_foreign_hq_score_for_next_scoring=score,
                final_commercial_fit_score=99.0,
            )
        return _sample_result(
            company_name=lead_input.company_name, domain=lead_input.domain,
            input_country=lead_input.input_country or "Brazil",
            sig_foreign_hq_score_for_next_scoring=score,
            final_commercial_fit_score=None,
            evidence_items=[], signals=[],
        )
    return _fake


class TestForeignHQOnlyMode:
    _df = pd.DataFrame({
        "company": ["Confirmed Foreign Co", "Local Domestic Co"],
        "domain": ["confirmed.com.br", "local.com.br"],
    })
    _cfg = BatchRunConfig(
        company_name_column="company", domain_column="domain",
        run_mode=FOREIGN_HQ_ONLY_MODE, row_limit=2,
        default_input_country="Brazil",
    )

    def test_skips_rows_with_final_hq_score_0(self):
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            out = run_batch_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["Local Domestic Co", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["Local Domestic Co", "enrichment_skip_reason"] == "Not confirmed foreign HQ"
        assert pd.isna(rows.loc["Local Domestic Co", "final_commercial_fit_score"])

    def test_enriches_rows_with_final_hq_score_3(self):
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            out = run_batch_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["Confirmed Foreign Co", "enrichment_skipped"] == False  # noqa: E712
        assert rows.loc["Confirmed Foreign Co", "enrichment_skip_reason"] == ""
        assert rows.loc["Confirmed Foreign Co", "final_commercial_fit_score"] == 99.0

    def test_c5_conservative_downgrade_is_skipped(self):
        # Old HQ score 3, but C5 does not confirm → conservative_adjustment
        # downgrades to 0 → the row must be skipped, not enriched.
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake), \
             _patch_adjudicator(_mk_result("domestic_confirmed", "High", "yes")):
            out = run_batch_foreign_hq_only(
                self._df, self._cfg, "S", "A",
                c5_enabled=True, c5_scoring_behavior="conservative_adjustment",
                c5_scope="all_rows", c5_model_used="claude-sonnet-5", c5_model_tier="sonnet",
            )
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["Confirmed Foreign Co", "sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert rows.loc["Confirmed Foreign Co", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["Confirmed Foreign Co", "enrichment_skip_reason"] == "Not confirmed foreign HQ"
        assert pd.isna(rows.loc["Confirmed Foreign Co", "final_commercial_fit_score"])
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["c5_downgraded_score_3_count"] == 1

    def test_c5_possible_foreign_parent_review_still_skipped(self):
        # Old HQ score 0; C5 flags a possible foreign parent → conservative
        # mode never auto-upgrades → the row stays unconfirmed → still skipped.
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake), \
             _patch_adjudicator(_mk_result("foreign_parent_confirmed", "High", "yes")):
            out = run_batch_foreign_hq_only(
                self._df, self._cfg, "S", "A",
                c5_enabled=True, c5_scoring_behavior="conservative_adjustment",
                c5_scope="all_rows", c5_model_used="claude-sonnet-5", c5_model_tier="sonnet",
            )
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["Local Domestic Co", "sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert rows.loc["Local Domestic Co", "c5_possible_foreign_parent_for_review"] == True  # noqa: E712
        assert rows.loc["Local Domestic Co", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["Local Domestic Co", "enrichment_skip_reason"] == "Not confirmed foreign HQ"

    def test_output_columns_present_for_both_confirmed_and_skipped(self):
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            out = run_batch_foreign_hq_only(self._df, self._cfg, "S", "A")
        assert "enrichment_skipped" in out["enriched_leads"].columns
        assert "enrichment_skip_reason" in out["enriched_leads"].columns
        assert len(out["enriched_leads"]) == 2  # both confirmed and skipped rows present

    def test_run_summary_counts_and_mode(self):
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            out = run_batch_foreign_hq_only(self._df, self._cfg, "S", "A")
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["run_mode"] == FOREIGN_HQ_ONLY_MODE
        assert summary["total_processed_rows"] == 2
        assert summary["full_enrichment_attempted_count"] == 1
        assert summary["full_enrichment_skipped_count"] == 1
        assert summary["confirmed_foreign_hq_count"] == 1
        # C5 settings/counts columns are always present (enabled or not).
        assert summary["c5_enabled"] is False

    def test_country_agnostic_brazil_and_uruguay(self):
        df = pd.DataFrame({
            "company": ["Brazil Co", "Uruguay Co"],
            "domain": ["brco.com.br", "uyco.com.uy"],
            "country": ["Brazil", "Uruguay"],
        })
        cfg = BatchRunConfig(
            company_name_column="company", domain_column="domain",
            input_country_column="country", run_mode=FOREIGN_HQ_ONLY_MODE, row_limit=2,
        )
        seen_countries = []

        def _fake(lead_input, **kwargs):
            seen_countries.append(lead_input.input_country)
            score = 3.0 if lead_input.company_name == "Brazil Co" else 0.0
            if kwargs.get("run_full_v2_pipeline"):
                return _sample_result(
                    company_name=lead_input.company_name, domain=lead_input.domain,
                    input_country=lead_input.input_country,
                    sig_foreign_hq_score_for_next_scoring=score,
                    final_commercial_fit_score=99.0)
            return _sample_result(
                company_name=lead_input.company_name, domain=lead_input.domain,
                input_country=lead_input.input_country,
                sig_foreign_hq_score_for_next_scoring=score,
                final_commercial_fit_score=None, evidence_items=[], signals=[])

        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            run_batch_foreign_hq_only(df, cfg, "S", "A")
        assert "Brazil" in seen_countries
        assert "Uruguay" in seen_countries
        # no hardcoded country substituted
        assert set(seen_countries) == {"Brazil", "Uruguay"}

    def test_existing_run_modes_unchanged(self):
        # The new mode is deliberately excluded from SUPPORTED_RUN_MODES / the
        # CLI's --mode choices; resolve_pipeline_flags for existing modes and
        # run_batch_dataframe's own output shape are both untouched.
        assert FOREIGN_HQ_ONLY_MODE not in SUPPORTED_RUN_MODES
        assert resolve_pipeline_flags("full")["run_full_v2_pipeline"] is True
        assert all(v is False for v in resolve_pipeline_flags("hq_only").values())

        df = pd.DataFrame({"company": ["Acme"], "domain": ["acme.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="hq_only", row_limit=1)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   return_value=_sample_result()):
            out = run_batch_dataframe(df, cfg, "S", "A")
        assert "enrichment_skipped" not in out["enriched_leads"].columns
        assert "full_enrichment_attempted_count" not in out["run_summary"].columns

    # ── Progress reporting (phase-aware payloads) ────────────────────────────

    def test_progress_phases_nonzero_totals_and_company(self):
        payloads = []
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            run_batch_foreign_hq_only(self._df, self._cfg, "S", "A",
                                      progress_callback=payloads.append)
        assert payloads, "expected progress payloads"
        assert all("phase" in p for p in payloads)
        # Phase 1: HQ screening over ALL selected rows — never 0/0.
        p1 = [p for p in payloads if p["phase"] == 1]
        assert p1
        assert all(p["phase_total"] == 2 for p in p1)
        assert all(p["phase_label"] == "HQ screening" for p in p1)
        assert p1[-1]["phase_processed"] == 2
        assert all(p.get("current_company_name") for p in p1)
        # Phase 2 absent when C5 is disabled.
        assert not [p for p in payloads if p["phase"] == 2]
        # Phase 3: total is the confirmed count (1), not a running counter.
        p3 = [p for p in payloads if p["phase"] == 3]
        assert p3
        assert all(p["phase_total"] == 1 for p in p3)
        assert p3[-1]["phase_processed"] == 1
        assert p3[-1]["current_company_name"] == "Confirmed Foreign Co"
        assert p3[-1]["success_count"] == 1
        assert p3[-1]["error_count"] == 0

    def test_progress_phase2_present_when_c5_enabled(self):
        payloads = []
        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake), \
             _patch_adjudicator(_mk_result("foreign_parent_confirmed", "High", "yes")):
            run_batch_foreign_hq_only(
                self._df, self._cfg, "S", "A",
                c5_enabled=True, c5_scoring_behavior="append_only",
                c5_scope="all_rows", c5_model_used="claude-sonnet-5", c5_model_tier="sonnet",
                progress_callback=payloads.append)
        p2 = [p for p in payloads if p["phase"] == 2]
        assert p2
        assert all(p["phase_label"] == "C5 adjudication" for p in p2)
        assert all(p["phase_total"] == 2 for p in p2)  # scope all_rows → both rows
        assert p2[-1]["phase_processed"] == 2
        assert all(p.get("current_company_name") for p in p2)

    def test_progress_callback_exception_does_not_break_run(self):
        def _boom(_payload):
            raise RuntimeError("ui broke")

        fake = _fake_prioritize_for_foreign_hq(
            {"Confirmed Foreign Co": 3.0, "Local Domestic Co": 0.0})
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=fake):
            out = run_batch_foreign_hq_only(self._df, self._cfg, "S", "A",
                                            progress_callback=_boom)
        assert len(out["enriched_leads"]) == 2


# ---------------------------------------------------------------------------
# Parallel chunk processing
# ---------------------------------------------------------------------------

class TestSplitDataframeIntoChunks:
    def test_splits_into_approximately_equal_chunks(self):
        df = pd.DataFrame({"c": list(range(10))})
        chunks = split_dataframe_into_chunks(df, 3)
        assert [len(c) for c in chunks] == [4, 3, 3]  # sizes differ by at most 1
        # order and original index preserved
        recombined = pd.concat(chunks)
        assert list(recombined["c"]) == list(range(10))
        assert list(recombined.index) == list(range(10))

    def test_never_produces_empty_chunks(self):
        df = pd.DataFrame({"c": [1, 2]})
        chunks = split_dataframe_into_chunks(df, 4)
        assert [len(c) for c in chunks] == [1, 1]

    def test_empty_frame_yields_no_chunks(self):
        assert split_dataframe_into_chunks(pd.DataFrame({"c": []}), 3) == []


def _fake_prioritize_named(lead_input, **kwargs):
    return _sample_result(company_name=lead_input.company_name,
                          domain=lead_input.domain)


class TestParallelBatch:
    _df9 = pd.DataFrame({
        "company": [f"Co{i}" for i in range(9)],
        "domain": [f"co{i}.com" for i in range(9)],
    })
    _cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                          run_mode="hq_only", row_limit=0)

    def test_parallel_disabled_path_unchanged(self):
        # The sequential entry point is untouched: same output shape as always,
        # no parallel fields on its Run Summary.
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            out = run_batch_dataframe(self._df9, self._cfg, "S", "A")
        assert len(out["enriched_leads"]) == 9
        assert "parallel_processing_enabled" not in out["run_summary"].columns
        assert "chunk_reports" not in out

    def test_workers_1_single_chunk_matches_sequential(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            seq = run_batch_dataframe(self._df9, self._cfg, "S", "A")
            par = run_batch_dataframe_parallel(self._df9, self._cfg, "S", "A", workers=1)
        assert list(par["enriched_leads"]["company_name"]) == \
            list(seq["enriched_leads"]["company_name"])
        summary = par["run_summary"].iloc[0].to_dict()
        assert summary["parallel_chunk_count"] == 1
        assert summary["parallel_workers"] == 1

    def test_workers_3_processes_all_rows_preserves_order(self):
        payloads = []
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            out = run_batch_dataframe_parallel(
                self._df9, self._cfg, "S", "A", workers=3,
                progress_callback=payloads.append)
        enriched = out["enriched_leads"]
        assert list(enriched["company_name"]) == [f"Co{i}" for i in range(9)]
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["processed_rows"] == 9
        assert summary["success_count"] == 9
        assert summary["error_count"] == 0
        assert summary["parallel_processing_enabled"] == True  # noqa: E712
        assert summary["parallel_workers"] == 3
        assert summary["parallel_chunk_count"] == 3
        assert summary["parallel_chunk_size_min"] == 3
        assert summary["parallel_chunk_size_max"] == 3
        assert summary["parallel_failed_chunk_count"] == 0
        assert summary["parallel_successful_chunk_count"] == 3
        # chunk progress is accurate: totals nonzero, completes at 3/3
        assert payloads
        assert all(p["parallel_chunks_total"] == 3 for p in payloads)
        assert payloads[-1]["parallel_chunks_completed"] == 3
        assert all(p["chunk_row_count"] == 3 for p in payloads)

    def test_combines_evidence_and_signals_from_chunks(self):
        # _sample_result carries 1 evidence item + 1 signal per row.
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            out = run_batch_dataframe_parallel(self._df9, self._cfg, "S", "A", workers=3)
        assert len(out["evidence"]) == 9
        assert len(out["signals"]) == 9
        assert set(out["evidence"]["source_index"]) == set(range(9))
        assert set(out["signals"]["source_index"]) == set(range(9))

    def test_failed_chunk_reported_without_losing_successes(self):
        original_unit = bc.run_single_batch_unit

        def _unit(chunk_df, cfg, s, a, **kw):
            if "Co4" in list(chunk_df["company"]):
                raise RuntimeError("chunk exploded")
            return original_unit(chunk_df, cfg, s, a, **kw)

        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named), \
             patch("lead_prioritizer_batch_core.run_single_batch_unit",
                   side_effect=_unit):
            out = run_batch_dataframe_parallel(self._df9, self._cfg, "S", "A", workers=3)

        enriched = out["enriched_leads"]
        assert len(enriched) == 9  # nothing discarded
        # middle chunk (Co3-Co5) becomes placeholder error rows, order intact
        assert list(enriched["company"]) == [f"Co{i}" for i in range(9)]
        failed_rows = enriched[enriched["run_success"] == False]  # noqa: E712
        assert list(failed_rows["company"]) == ["Co3", "Co4", "Co5"]
        assert all(str(e).startswith("parallel_chunk_failed: RuntimeError")
                   for e in failed_rows["run_error"])
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["parallel_failed_chunk_count"] == 1
        assert summary["parallel_successful_chunk_count"] == 2
        assert summary["processed_rows"] == 6
        assert summary["success_count"] == 6
        assert summary["error_count"] == 3
        reports = out["chunk_reports"]
        failed = [r for r in reports if r["success"] is False]
        assert len(failed) == 1 and "chunk exploded" in failed[0]["error"]
        # successful chunks' data is intact
        assert len(out["evidence"]) == 6

    def test_fho_and_c5_config_passed_through_unchanged(self):
        seen: dict = {}

        def _unit(chunk_df, cfg, s, a, **kw):
            seen.update(kw)
            seen["run_mode"] = cfg.run_mode
            seen["start_row"] = cfg.start_row
            seen["row_limit"] = cfg.row_limit
            seen["default_input_country"] = cfg.default_input_country
            seen["input_country_column"] = cfg.input_country_column
            return {
                "enriched_leads": pd.DataFrame([{"company": "X"}] * len(chunk_df)),
                "evidence": pd.DataFrame(),
                "signals": pd.DataFrame(),
                "run_summary": pd.DataFrame([{
                    "processed_rows": len(chunk_df),
                    "success_count": len(chunk_df), "error_count": 0,
                }]),
            }

        cfg = BatchRunConfig(
            company_name_column="company", domain_column="domain",
            input_country_column="country", default_input_country="Uruguay",
            run_mode=FOREIGN_HQ_ONLY_MODE, start_row=1, row_limit=6)
        df = pd.DataFrame({
            "company": [f"Co{i}" for i in range(8)],
            "domain": [f"co{i}.com" for i in range(8)],
            "country": ["Uruguay"] * 8,
        })
        with patch("lead_prioritizer_batch_core.run_single_batch_unit",
                   side_effect=_unit):
            out = run_batch_dataframe_parallel(
                df, cfg, "S", "A", workers=2,
                c5_enabled=True, c5_scoring_behavior="conservative_adjustment",
                c5_scope="all_rows", c5_model_used="claude-sonnet-5",
                c5_model_tier="sonnet")
        # C5 config forwarded verbatim to every chunk unit
        assert seen["c5_enabled"] is True
        assert seen["c5_scoring_behavior"] == "conservative_adjustment"
        assert seen["c5_scope"] == "all_rows"
        assert seen["c5_model_used"] == "claude-sonnet-5"
        assert seen["c5_model_tier"] == "sonnet"
        # FHO mode + country config forwarded; selection already applied
        assert seen["run_mode"] == FOREIGN_HQ_ONLY_MODE
        assert seen["default_input_country"] == "Uruguay"
        assert seen["input_country_column"] == "country"
        assert seen["start_row"] == 0 and seen["row_limit"] == 0
        # start_row=1 + row_limit=6 → 6 selected rows across chunks
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["selected_rows"] == 6
        assert summary["processed_rows"] == 6

    def test_fho_mode_end_to_end_parallel(self):
        def _fake(lead_input, **kwargs):
            score = 3.0 if lead_input.company_name in ("Co1", "Co4") else 0.0
            return _sample_result(
                company_name=lead_input.company_name, domain=lead_input.domain,
                sig_foreign_hq_score_for_next_scoring=score,
                final_commercial_fit_score=(
                    99.0 if kwargs.get("run_full_v2_pipeline") else None),
            )

        df = pd.DataFrame({
            "company": [f"Co{i}" for i in range(6)],
            "domain": [f"co{i}.com" for i in range(6)],
        })
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=FOREIGN_HQ_ONLY_MODE, row_limit=0)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake):
            out = run_batch_dataframe_parallel(df, cfg, "S", "A", workers=2)
        enriched = out["enriched_leads"].set_index("company_name")
        assert enriched.loc["Co1", "enrichment_skipped"] == False  # noqa: E712
        assert enriched.loc["Co4", "enrichment_skipped"] == False  # noqa: E712
        assert enriched.loc["Co0", "enrichment_skipped"] == True   # noqa: E712
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["confirmed_foreign_hq_count"] == 2
        assert summary["full_enrichment_attempted_count"] == 2
        assert summary["full_enrichment_skipped_count"] == 4

    def test_workers_capped_at_4(self):
        assert MAX_PARALLEL_WORKERS == 4
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            out = run_batch_dataframe_parallel(self._df9, self._cfg, "S", "A",
                                               workers=10)
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["parallel_workers"] == 4
        assert summary["parallel_chunk_count"] == 4

    def test_chunk_result_callback_receives_successful_chunks(self):
        saved = []
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            run_batch_dataframe_parallel(
                self._df9, self._cfg, "S", "A", workers=3,
                chunk_result_callback=lambda rep, tab: saved.append((rep, tab)))
        assert len(saved) == 3
        assert sorted(rep["chunk_index"] for rep, _ in saved) == [1, 2, 3]
        assert all("enriched_leads" in tab for _, tab in saved)


# ---------------------------------------------------------------------------
# Australia non-English foreign-HQ mode
# ---------------------------------------------------------------------------

class TestClassifyParentHqLanguageMarket:
    @pytest.mark.parametrize("country,expected", [
        ("Germany", "non_english_speaking"),
        ("Japan", "non_english_speaking"),
        ("Brazil", "non_english_speaking"),
        ("United States", "english_speaking"),
        ("USA", "english_speaking"),
        ("UK", "english_speaking"),
        ("Canada", "english_speaking"),
        ("New Zealand", "english_speaking"),
        ("Ireland", "english_speaking"),
        ("Australia", "english_speaking"),
        ("Singapore", "review"),
        ("India", "review"),
        ("South Africa", "review"),
        ("United Arab Emirates", "review"),
        ("UAE", "review"),
        ("", "unclear"),
        (None, "unclear"),
        ("Atlantis", "unclear"),
    ])
    def test_classification(self, country, expected):
        assert classify_parent_hq_language_market(country) == expected

    def test_case_and_whitespace_insensitive(self):
        assert classify_parent_hq_language_market("  germany  ") == "non_english_speaking"
        assert classify_parent_hq_language_market("UNITED STATES") == "english_speaking"


class TestResolveParentHqCountryForExport:
    def test_c5_takes_priority(self):
        row = {"c5_parent_hq_country": "Germany", "ai_parent_hq_country": "France",
               "hq_detected_country": "Italy"}
        assert resolve_parent_hq_country_for_export(row) == "Germany"

    def test_falls_back_to_ai_then_detected(self):
        assert resolve_parent_hq_country_for_export(
            {"c5_parent_hq_country": "", "ai_parent_hq_country": "France",
             "hq_detected_country": "Italy"}) == "France"
        assert resolve_parent_hq_country_for_export(
            {"c5_parent_hq_country": "", "ai_parent_hq_country": "",
             "hq_detected_country": "Italy"}) == "Italy"

    def test_all_blank_returns_empty(self):
        assert resolve_parent_hq_country_for_export({}) == ""


def _au_result(company, score, parent_country, full=False):
    kw = dict(
        company_name=company, domain=company.lower() + ".com", input_country="Australia",
        hq_detected_country=parent_country,
        hq_structure_type="foreign_parent" if score == 3.0 else "domestic",
        sig_foreign_hq_score_for_next_scoring=score,
        ai_parent_hq_country=parent_country,
        domain_root=company.lower(), query_used=f"{company} headquarters", parser_source="ai",
        evidence_items=[], signals=[],
    )
    if full:
        kw["final_commercial_fit_score"] = 88.0
    return LeadPrioritizationResult(**kw)


def _fake_prioritize_for_au(scores: dict):
    def _fake(lead_input, **kwargs):
        score, parent = scores[lead_input.company_name]
        return _au_result(lead_input.company_name, score, parent,
                          full=bool(kwargs.get("run_full_v2_pipeline")))
    return _fake


class TestNonEnglishForeignHqOnlyMode:
    _rows = {
        "GermanCo": (3.0, "Germany"),        # confirmed + non-English -> enrich
        "USCo": (3.0, "United States"),       # confirmed but English -> skip
        "UKCo": (3.0, "United Kingdom"),      # confirmed but English -> skip
        "CanadaCo": (3.0, "Canada"),          # confirmed but English -> skip
        "NZCo": (3.0, "New Zealand"),         # confirmed but English -> skip
        "IrelandCo": (3.0, "Ireland"),        # confirmed but English -> skip
        "SGCo": (3.0, "Singapore"),           # confirmed but review -> skip
        "IndiaCo": (3.0, "India"),            # confirmed but review -> skip
        "SACo": (3.0, "South Africa"),        # confirmed but review -> skip
        "UAECo": (3.0, "UAE"),                # confirmed but review -> skip
        "DomesticCo": (0.0, "Australia"),     # not confirmed -> skip
    }
    _df = pd.DataFrame({"company": list(_rows), "domain": [c.lower() + ".com" for c in _rows]})
    _cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                          run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=0,
                          default_input_country="Australia")

    def test_only_confirmed_non_english_parent_gets_full_enrichment(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["GermanCo", "enrichment_skipped"] == False  # noqa: E712
        assert rows.loc["GermanCo", "recommended_for_non_english_foreign_hq_export"] == True  # noqa: E712
        # Backward-compat field: still True here since the row's country is Australia.
        assert rows.loc["GermanCo", "recommended_for_australia_export"] == True  # noqa: E712
        assert rows.loc["GermanCo", "final_commercial_fit_score"] == 88.0
        assert rows.loc["GermanCo", "parent_hq_language_market_type"] == "non_english_speaking"

    def test_english_speaking_parents_not_enriched(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        for co in ("USCo", "UKCo", "CanadaCo", "NZCo", "IrelandCo"):
            assert rows.loc[co, "enrichment_skipped"] == True, co  # noqa: E712
            assert rows.loc[co, "parent_hq_language_market_type"] == "english_speaking", co
            assert rows.loc[co, "enrichment_skip_reason"] == \
                "Parent HQ country is English-speaking/lower-priority for language/training export"
            assert pd.isna(rows.loc[co, "final_commercial_fit_score"])

    def test_review_markets_not_enriched(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        for co in ("SGCo", "IndiaCo", "SACo", "UAECo"):
            assert rows.loc[co, "enrichment_skipped"] == True, co  # noqa: E712
            assert rows.loc[co, "parent_hq_language_market_type"] == "review", co
            assert rows.loc[co, "enrichment_skip_reason"] == \
                "Parent HQ language market is review/nuanced"

    def test_not_confirmed_row_skipped(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        rows = out["enriched_leads"].set_index("company_name")
        assert rows.loc["DomesticCo", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["DomesticCo", "enrichment_skip_reason"] == "Not confirmed foreign HQ"

    def test_run_summary_counts(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["confirmed_foreign_hq_count"] == 10  # all but DomesticCo
        assert summary["non_english_foreign_hq_count"] == 1  # GermanCo only
        # english_speaking_parent_hq_count is a pure market-type tally (not
        # gated by confirmation): 5 confirmed English parents + DomesticCo
        # (parent country "Australia", not confirmed, market still english).
        assert summary["english_speaking_parent_hq_count"] == 6
        assert summary["review_parent_hq_count"] == 4
        assert summary["unclear_parent_hq_count"] == 0
        assert summary["full_enrichment_attempted_count"] == 1
        assert summary["full_enrichment_skipped_count"] == 10

    def test_output_includes_all_export_columns(self):
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(self._rows)):
            out = run_batch_non_english_foreign_hq_only(self._df, self._cfg, "S", "A")
        for col in (
            "foreign_hq_detected_for_export", "parent_hq_country_for_export",
            "parent_hq_language_market_type", "non_english_foreign_hq_detected",
            "non_english_foreign_hq_reason", "recommended_for_non_english_foreign_hq_export",
            "recommended_for_australia_export",
            "enrichment_skipped", "enrichment_skip_reason",
        ):
            assert col in out["enriched_leads"].columns

    def test_c5_conservative_downgrade_prevents_enrichment(self):
        rows = {"GermanCo": (3.0, "Germany")}
        df = pd.DataFrame({"company": ["GermanCo"], "domain": ["germanco.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=1,
                             default_input_country="Australia")
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(rows)), \
             _patch_adjudicator(_mk_result("domestic_confirmed", "High", "yes")):
            out = run_batch_non_english_foreign_hq_only(
                df, cfg, "S", "A", c5_enabled=True,
                c5_scoring_behavior="conservative_adjustment", c5_scope="all_rows",
                c5_model_used="claude-sonnet-5", c5_model_tier="sonnet")
        row = out["enriched_leads"].iloc[0]
        assert row["sig_foreign_hq_score_for_next_scoring"] == 0.0
        assert row["enrichment_skipped"] == True  # noqa: E712
        assert row["recommended_for_non_english_foreign_hq_export"] == False  # noqa: E712
        assert row["recommended_for_australia_export"] == False  # noqa: E712


class TestNonEnglishForeignHqOnlyModeCountryAgnostic:
    """Regression guard: the mode must NOT be Australia-hardcoded.

    A non-Australia row (e.g. Italy, New Zealand) with a confirmed non-English
    foreign parent must now be ELIGIBLE for full enrichment — this is exactly
    the restriction that was removed.
    """

    def _row_result(self, company, score, parent_country, input_country, full=False):
        r = _au_result(company, score, parent_country, full=full)
        r.input_country = input_country
        return r

    def test_italy_row_with_german_parent_now_eligible(self):
        df = pd.DataFrame({"company": ["ItalyCo"], "domain": ["italyco.com"],
                           "country": ["Italy"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             input_country_column="country",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=1)

        def _fake(lead_input, **kwargs):
            return self._row_result("ItalyCo", 3.0, "Germany", "Italy",
                                    full=bool(kwargs.get("run_full_v2_pipeline")))

        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_non_english_foreign_hq_only(df, cfg, "S", "A")
        row = out["enriched_leads"].iloc[0]
        assert row["enrichment_skipped"] == False  # noqa: E712
        assert row["enrichment_skip_reason"] == ""
        assert row["recommended_for_non_english_foreign_hq_export"] == True  # noqa: E712
        assert row["non_english_foreign_hq_detected"] == True  # noqa: E712
        # Old Australia-only flag correctly stays False (this isn't Australia).
        assert row["recommended_for_australia_export"] == False  # noqa: E712
        assert row["final_commercial_fit_score"] == 88.0

    # ── Acceptance test: New Zealand ────────────────────────────────────────

    _nz_rows = {
        "FranceCo": (3.0, "France"),         # confirmed non-English -> enrich
        "AustraliaCo": (3.0, "Australia"),   # confirmed but English -> skip
        "SingaporeCo": (3.0, "Singapore"),   # confirmed but review -> skip
        "DomesticCo": (3.0, "New Zealand"),  # same as input country -> skip
        "UnclearCo": (0.0, ""),              # not confirmed / unclear -> skip
    }

    def _nz_fake(self, lead_input, **kwargs):
        score, parent = self._nz_rows[lead_input.company_name]
        return self._row_result(lead_input.company_name, score, parent, "New Zealand",
                                full=bool(kwargs.get("run_full_v2_pipeline")))

    def _run_nz(self):
        df = pd.DataFrame({"company": list(self._nz_rows),
                           "domain": [f"{c.lower()}.com" for c in self._nz_rows]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=0,
                             default_input_country="New Zealand")
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=self._nz_fake):
            return run_batch_non_english_foreign_hq_only(df, cfg, "S", "A")

    def test_nz_france_parent_eligible(self):
        rows = self._run_nz()["enriched_leads"].set_index("company_name")
        assert rows.loc["FranceCo", "enrichment_skipped"] == False  # noqa: E712
        assert rows.loc["FranceCo", "recommended_for_non_english_foreign_hq_export"] == True  # noqa: E712
        assert rows.loc["FranceCo", "parent_hq_language_market_type"] == "non_english_speaking"

    def test_nz_australia_parent_skipped_english_speaking(self):
        rows = self._run_nz()["enriched_leads"].set_index("company_name")
        assert rows.loc["AustraliaCo", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["AustraliaCo", "parent_hq_language_market_type"] == "english_speaking"
        assert "English-speaking" in rows.loc["AustraliaCo", "enrichment_skip_reason"]

    def test_nz_singapore_parent_skipped_review(self):
        rows = self._run_nz()["enriched_leads"].set_index("company_name")
        assert rows.loc["SingaporeCo", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["SingaporeCo", "parent_hq_language_market_type"] == "review"
        assert "review/nuanced" in rows.loc["SingaporeCo", "enrichment_skip_reason"]

    def test_nz_domestic_parent_skipped(self):
        rows = self._run_nz()["enriched_leads"].set_index("company_name")
        assert rows.loc["DomesticCo", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["DomesticCo", "enrichment_skip_reason"] == \
            "Parent HQ country matches input country (not foreign)"

    def test_nz_unclear_result_skipped_manual_review(self):
        rows = self._run_nz()["enriched_leads"].set_index("company_name")
        assert rows.loc["UnclearCo", "enrichment_skipped"] == True  # noqa: E712
        assert rows.loc["UnclearCo", "enrichment_skip_reason"] == "Not confirmed foreign HQ"

    def test_missing_input_country_skipped(self):
        df = pd.DataFrame({"company": ["NoCountryCo"], "domain": ["nocountryco.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=1)

        def _fake(lead_input, **kwargs):
            return self._row_result("NoCountryCo", 3.0, "Germany", "",
                                    full=bool(kwargs.get("run_full_v2_pipeline")))

        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_non_english_foreign_hq_only(df, cfg, "S", "A")
        row = out["enriched_leads"].iloc[0]
        assert row["enrichment_skipped"] == True  # noqa: E712
        assert row["enrichment_skip_reason"] == "Input country is missing"

    def test_missing_parent_country_skipped(self):
        df = pd.DataFrame({"company": ["NoParentCo"], "domain": ["noparentco.com"],
                           "country": ["New Zealand"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             input_country_column="country",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=1)

        def _fake(lead_input, **kwargs):
            return self._row_result("NoParentCo", 3.0, "", "New Zealand",
                                    full=bool(kwargs.get("run_full_v2_pipeline")))

        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake):
            out = run_batch_non_english_foreign_hq_only(df, cfg, "S", "A")
        row = out["enriched_leads"].iloc[0]
        assert row["enrichment_skipped"] == True  # noqa: E712
        assert row["enrichment_skip_reason"] == "Parent HQ country is missing"


class TestExistingForeignHqOnlyModeUnaffected:
    """The FOREIGN_HQ_ONLY_MODE selection/output behavior must be byte-for-byte
    unchanged after adding the non-English mode (shared-helper refactor)."""

    def test_selection_and_output_unchanged(self):
        rows = {"GermanCo": (3.0, "Germany"), "USCo": (3.0, "United States"),
                "DomesticCo": (0.0, "Australia")}
        df = pd.DataFrame({"company": list(rows), "domain": [f"{c.lower()}.com" for c in rows]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=FOREIGN_HQ_ONLY_MODE, row_limit=0)
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(rows)):
            out = run_batch_foreign_hq_only(df, cfg, "S", "A")
        enriched = out["enriched_leads"].set_index("company_name")
        # ALL confirmed rows enrich regardless of parent-HQ language (no filter)
        assert enriched.loc["GermanCo", "enrichment_skipped"] == False  # noqa: E712
        assert enriched.loc["USCo", "enrichment_skipped"] == False  # noqa: E712
        assert enriched.loc["DomesticCo", "enrichment_skipped"] == True  # noqa: E712
        # no non-English-mode-only columns leak into this mode's output
        for col in ("parent_hq_language_market_type", "recommended_for_australia_export"):
            assert col not in out["enriched_leads"].columns
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["confirmed_foreign_hq_count"] == 2
        assert summary["full_enrichment_attempted_count"] == 2
        assert summary["full_enrichment_skipped_count"] == 1


class TestParallelNonEnglishMode:
    def test_run_single_batch_unit_dispatches_to_non_english_mode(self):
        rows = {"GermanCo": (3.0, "Germany"), "USCo": (3.0, "United States")}
        df = pd.DataFrame({"company": list(rows), "domain": [f"{c.lower()}.com" for c in rows]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=0,
                             default_input_country="Australia")
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(rows)):
            out = run_single_batch_unit(df, cfg, "S", "A")
        assert "recommended_for_australia_export" in out["enriched_leads"].columns

    def test_parallel_run_aggregates_non_english_counts(self):
        rows = {f"Co{i}": (3.0, "Germany") for i in range(4)}
        rows["USCo"] = (3.0, "United States")
        rows["DomesticCo"] = (0.0, "Australia")
        df = pd.DataFrame({"company": list(rows), "domain": [f"{c.lower()}.com" for c in rows]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode=NON_ENGLISH_FOREIGN_HQ_ONLY_MODE, row_limit=0,
                             default_input_country="Australia")
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_for_au(rows)):
            out = run_batch_dataframe_parallel(df, cfg, "S", "A", workers=3)
        assert len(out["enriched_leads"]) == 6
        summary = out["run_summary"].iloc[0].to_dict()
        assert summary["non_english_foreign_hq_count"] == 4
        assert summary["full_enrichment_attempted_count"] == 4
        assert summary["full_enrichment_skipped_count"] == 2
        assert summary["parallel_chunk_count"] == 3


# ---------------------------------------------------------------------------
# aggregate_parallel_chunk_progress (pure helper) + live parallel progress
# ---------------------------------------------------------------------------

class TestAggregateParallelChunkProgress:
    def test_running_chunks_use_live_snapshot(self):
        reports = [
            {"chunk_index": 1, "row_count": 10, "success": None, "error": ""},
            {"chunk_index": 2, "row_count": 10, "success": None, "error": ""},
        ]
        snapshot = {
            0: {"processed": 4, "success": 4, "error": 0, "selected": 10,
                "current_company_name": "Co4", "last_update": 5.0,
                "phase_label": None, "phase": None, "phase_processed": 0, "phase_total": 0},
            1: {"processed": 2, "success": 1, "error": 1, "selected": 10,
                "current_company_name": "Co12", "last_update": 9.0,
                "phase_label": None, "phase": None, "phase_processed": 0, "phase_total": 0},
        }
        agg = aggregate_parallel_chunk_progress(snapshot, reports, total_selected_rows=20)
        assert agg["processed_rows"] == 6
        assert agg["success_count"] == 5
        assert agg["error_count"] == 1
        assert agg["selected_rows"] == 20
        # most recently updated running chunk wins for "current company"
        assert agg["current_company_name"] == "Co12"
        assert agg["chunks_active_count"] == 2
        assert {c["chunk_index"] for c in agg["active_chunks"]} == {1, 2}

    def test_finished_successful_chunk_trusts_row_count(self):
        reports = [{"chunk_index": 1, "row_count": 10, "success": True, "error": ""}]
        snapshot = {0: {"processed": 10, "success": 9, "error": 1, "selected": 10,
                       "current_company_name": "LastCo", "last_update": 1.0}}
        agg = aggregate_parallel_chunk_progress(snapshot, reports)
        assert agg["processed_rows"] == 10
        assert agg["success_count"] == 9
        assert agg["error_count"] == 1
        assert agg["chunks_active_count"] == 0  # finished chunks are not "active"

    def test_finished_failed_chunk_counts_all_rows_as_errors(self):
        reports = [{"chunk_index": 1, "row_count": 10, "success": False, "error": "boom"}]
        agg = aggregate_parallel_chunk_progress({}, reports)
        assert agg["processed_rows"] == 10
        assert agg["error_count"] == 10
        assert agg["success_count"] == 0

    def test_phase_info_surfaced_for_active_chunks(self):
        reports = [{"chunk_index": 1, "row_count": 5, "success": None, "error": ""}]
        snapshot = {0: {"processed": 2, "success": 2, "error": 0, "selected": 5,
                       "current_company_name": "Acme", "last_update": 1.0,
                       "phase": 1, "phase_label": "HQ screening",
                       "phase_processed": 2, "phase_total": 5}}
        agg = aggregate_parallel_chunk_progress(snapshot, reports)
        assert agg["active_chunks"][0]["phase_label"] == "HQ screening"
        assert agg["active_chunks"][0]["phase_total"] == 5

    def test_empty_reports_yields_zeroed_aggregate(self):
        agg = aggregate_parallel_chunk_progress({}, [])
        assert agg["processed_rows"] == 0
        assert agg["success_count"] == 0
        assert agg["error_count"] == 0
        assert agg["active_chunks"] == []
        assert agg["current_company_name"] == ""


class TestParallelLiveProgress:
    """Regression guard: parallel mode must emit progress before every chunk
    finishes (row-level heartbeat), not just once per chunk completion."""

    def _slow_prioritize(self, sleep_seconds):
        def _fake(lead_input, **kwargs):
            time.sleep(sleep_seconds)
            return _sample_result(company_name=lead_input.company_name,
                                  domain=lead_input.domain)
        return _fake

    def test_heartbeat_and_row_level_events_fire_before_completion(self):
        df = pd.DataFrame({"company": [f"Co{i}" for i in range(6)],
                           "domain": [f"co{i}.com" for i in range(6)]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="hq_only", row_limit=0)
        events = []
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=self._slow_prioritize(0.05)):
            out = run_batch_dataframe_parallel(
                df, cfg, "S", "A", workers=2, progress_callback=events.append,
                heartbeat_interval_seconds=0.02,
            )
        assert len(events) > 2  # far more than the old "one event per chunk"
        heartbeats = [e for e in events if e.get("heartbeat")]
        completions = [e for e in events if not e.get("heartbeat")]
        assert heartbeats, "expected at least one heartbeat wake-up before chunks finished"
        assert len(completions) == 2  # one per chunk
        # heartbeat payloads carry live, incrementally-growing row counts —
        # not just a static "still waiting" message with no numbers.
        assert any(e["processed_rows"] > 0 for e in heartbeats)
        assert all("current_company_name" in e for e in events)
        assert all("parallel_workers" in e and e["parallel_workers"] == 2 for e in events)
        assert len(out["enriched_leads"]) == 6

    def test_progress_callback_never_called_from_worker_thread_unsafely(self):
        # The callback we pass to run_batch_dataframe_parallel must only ever
        # be invoked by the polling loop (main thread), never directly by a
        # chunk's internal row callback — verified by checking every payload
        # has the aggregate shape (not a bare per-row payload).
        df = pd.DataFrame({"company": ["A", "B", "C", "D"],
                           "domain": ["a.com", "b.com", "c.com", "d.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="hq_only", row_limit=0)
        events = []
        with patch("lead_prioritizer_batch_core.prioritize_single_lead",
                   side_effect=_fake_prioritize_named):
            run_batch_dataframe_parallel(
                df, cfg, "S", "A", workers=2, progress_callback=events.append,
                heartbeat_interval_seconds=0.05,
            )
        assert events
        for e in events:
            assert "parallel_chunks_total" in e
            assert "active_chunks" in e

    def test_chunk_failure_reported_without_hiding_error(self):
        df = pd.DataFrame({"company": ["Good1", "Good2", "Bad1", "Bad2"],
                           "domain": ["g1.com", "g2.com", "b1.com", "b2.com"]})
        cfg = BatchRunConfig(company_name_column="company", domain_column="domain",
                             run_mode="hq_only", row_limit=0)

        def _fake(lead_input, **kwargs):
            return _sample_result(company_name=lead_input.company_name)

        original_unit = run_single_batch_unit

        def _unit(chunk_df, cfg2, s, a, **kw):
            if "Bad1" in list(chunk_df["company"]):
                raise RuntimeError("chunk exploded")
            return original_unit(chunk_df, cfg2, s, a, **kw)

        events = []
        with patch("lead_prioritizer_batch_core.prioritize_single_lead", side_effect=_fake), \
             patch("lead_prioritizer_batch_core.run_single_batch_unit", side_effect=_unit):
            out = run_batch_dataframe_parallel(
                df, cfg, "S", "A", workers=2, progress_callback=events.append,
                heartbeat_interval_seconds=0.02,
            )
        failure_events = [e for e in events if e.get("chunk_success") is False]
        assert failure_events
        assert "chunk exploded" in failure_events[0]["chunk_error"]
        assert failure_events[0]["error_count"] >= 2  # failed chunk's rows counted
        assert len(out["enriched_leads"]) == 4  # nothing silently dropped
