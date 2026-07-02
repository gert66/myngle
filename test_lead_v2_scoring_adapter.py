"""Tests for Lead Prioritizer v2 scoring adapter (Step 5).

Includes non-mocked tests through the real ``score_company`` and mocked tests
for profile/flag behavior. No network or live API keys required.
"""

from __future__ import annotations

from unittest.mock import patch

from lead_output_schema import LeadInput, LeadPrioritizationResult, HQDetectionResult
from lead_v2_scoring_adapter import (
    build_score_company_input_from_v2_result,
    score_lead_v2_result,
)
from lead_prioritizer_core import prioritize_single_lead


def _result(**kw) -> LeadPrioritizationResult:
    base = dict(company_name="Acme", domain="acme.com", input_country="Italy")
    base.update(kw)
    return LeadPrioritizationResult(**base)


# ---------------------------------------------------------------------------
# Input mapping
# ---------------------------------------------------------------------------

class TestInputMapping:
    def test_signal_mapping(self):
        r = _result(
            sig_foreign_hq_score_for_next_scoring=3.0,
            sig_international_profile_score=2.0,
            sig_onboarding_training_need_score=1.0,
            sig_icp_keyword_match_score=2.0,
            sig_company_size_complexity_score=2.0,
            sig_employer_branding_score=2.0,
        )
        row = build_score_company_input_from_v2_result(r)
        assert row["sig_foreign_hq_score"] == 3.0
        assert row["sig_intl_footprint_score"] == 2.0
        assert row["sig_lnd_onboarding_score"] == 1.0
        assert row["sig_explicit_lnd_score"] == 2.0
        assert row["sig_employer_branding_score"] == 2.0

    def test_company_size_complexity_not_used_as_employee_range(self):
        r = _result(sig_company_size_complexity_score=2.0)
        row = build_score_company_input_from_v2_result(r)
        assert row["employee_range"] == ""
        assert row["company_size"] == ""
        assert row["lusha_employee_range"] == ""
        assert row["lusha_api_employee_range"] == ""
        # complexity score must not leak into any size field
        assert 2.0 not in (
            row["employee_range"], row["company_size"],
            row["lusha_employee_range"], row["lusha_api_employee_range"],
        )

    def test_rapid_growth_is_zero(self):
        row = build_score_company_input_from_v2_result(_result())
        assert row["sig_rapid_growth_score"] == 0.0

    def test_ti_onboarding_zero(self):
        row = build_score_company_input_from_v2_result(_result())
        assert row["ti_onboarding_score"] == 0.0

    def test_employer_branding_maps_from_result_not_hardcoded(self):
        row_none = build_score_company_input_from_v2_result(_result())
        assert row_none["sig_employer_branding_score"] == 0.0

        row_scored = build_score_company_input_from_v2_result(
            _result(sig_employer_branding_score=2.0))
        assert row_scored["sig_employer_branding_score"] == 2.0

    def test_no_competitor_field_anywhere(self):
        row = build_score_company_input_from_v2_result(
            _result(sig_foreign_hq_score_for_next_scoring=3.0))
        for key in row:
            assert "competitor" not in key.lower()

    def test_none_signals_map_to_zero(self):
        row = build_score_company_input_from_v2_result(_result())
        assert row["sig_foreign_hq_score"] == 0.0
        assert row["sig_intl_footprint_score"] == 0.0
        assert row["sig_lnd_onboarding_score"] == 0.0
        assert row["sig_explicit_lnd_score"] == 0.0


# ---------------------------------------------------------------------------
# score_lead_v2_result
# ---------------------------------------------------------------------------

class TestScoreLeadV2Result:
    def test_calls_score_company_with_italy_profile(self):
        captured = {}

        def _fake(row, params=None):
            captured["params"] = params
            return {"final_commercial_fit_score": 5.0, "commercial_tier": "X"}

        with patch("commercial_fit_scoring.score_company", side_effect=_fake):
            out = score_lead_v2_result(_result(sig_foreign_hq_score_for_next_scoring=3.0))
        assert captured["params"] == {"scoring_profile": "italy_register_icp_only"}
        assert out["v2_scoring_profile_used"] == "italy_register_icp_only"
        assert "v2_score_input_mapping_note" in out

    def test_does_not_mutate_result(self):
        r = _result(sig_foreign_hq_score_for_next_scoring=3.0)
        _ = score_lead_v2_result(r)
        assert r.final_commercial_fit_score is None
        assert r.commercial_tier is None

    def test_real_score_company_hq_only(self):
        # Non-mocked: HQ signal only should still yield a usable numeric score.
        out = score_lead_v2_result(_result(sig_foreign_hq_score_for_next_scoring=3.0))
        assert isinstance(out["final_commercial_fit_score"], (int, float))
        assert out["commercial_tier"]
        # Audit input echoes the mapped HQ score.
        assert out["score_input_foreign_hq"] == 3.0


# ---------------------------------------------------------------------------
# Core flag gating
# ---------------------------------------------------------------------------

class TestCoreGating:
    _lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")

    def _run(self, **flags):
        p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                         return_value={"organic": []})
        p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai",
                     return_value=HQDetectionResult(
                         hq_structure_type="foreign_parent",
                         foreign_hq_simple=True,
                         sig_foreign_hq_score_for_next_scoring=3.0))
        with p_serper, p_ai:
            return prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake", **flags,
            )

    def test_default_does_not_score(self):
        r = self._run()
        assert r.final_commercial_fit_score is None
        assert r.scoring_profile is None

    def test_flag_true_scores_and_keeps_hq(self):
        r = self._run(calculate_commercial_score_flag=True)
        assert isinstance(r.final_commercial_fit_score, (int, float))
        assert r.scoring_profile == "italy_register_icp_only"
        assert r.score_input_foreign_hq == 3.0
        # HQ fields untouched.
        assert r.hq_structure_type == "foreign_parent"
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0

    def test_scores_with_only_hq_no_signals(self):
        r = self._run(calculate_commercial_score_flag=True)
        # No non-HQ signals extracted, but scoring still ran.
        assert r.signals == []
        assert r.final_commercial_fit_score is not None
