"""Tests for Lead Prioritizer v2 deterministic caller/app fields (Step 6).

Pure builder tests plus core flag gating (Serper/AI mocked). No live keys.
"""

from __future__ import annotations

from unittest.mock import patch

from lead_output_schema import (
    LeadInput, LeadPrioritizationResult, HQDetectionResult, LeadEvidence, LeadSignal,
)
from lead_caller_app_fields_builder import build_caller_app_fields
from lead_prioritizer_core import prioritize_single_lead


def _result(**kw) -> LeadPrioritizationResult:
    base = dict(company_name="Acme S.p.A.", domain="acme.com", input_country="Italy")
    base.update(kw)
    return LeadPrioritizationResult(**base)


_ALL_KEYS = {
    "commercial_fit_score_app", "commercial_tier_app", "what_is_hot_app",
    "what_is_not_app", "why_relevant_app", "caller_angle_app", "call_starter_app",
    "caution_app", "foreign_hq_signal_used_in_app", "foreign_hq_country_app",
    "foreign_hq_city_app",
}

# Terms that must never appear anywhere in the caller/app output.
_FORBIDDEN = ["competitor", "competing", "rival", "rapid growth", "fast growing",
              "fastest growing", "alternative provider", "berlitz", "speexx"]


class TestKeysAndEmpty:
    def test_returns_exact_keys(self):
        assert set(build_caller_app_fields(_result()).keys()) == _ALL_KEYS

    def test_minimal_result_factual_fallbacks(self):
        f = build_caller_app_fields(_result())
        assert f["commercial_fit_score_app"] is None
        assert f["commercial_tier_app"] is None
        assert f["what_is_hot_app"] is None            # nothing positive
        assert f["foreign_hq_signal_used_in_app"] == "No"
        assert f["foreign_hq_country_app"] is None
        # what_is_not is factual, not a failure frame
        assert "Commercial score not calculated" in f["what_is_not_app"]
        assert "No non-HQ evidence collected yet" in f["what_is_not_app"]
        # caller_angle always has a light-discovery fallback
        assert "light discovery" in f["caller_angle_app"]
        assert f["why_relevant_app"] is None


class TestScoreTier:
    def test_copy_score_and_tier(self):
        f = build_caller_app_fields(_result(final_commercial_fit_score=7.2,
                                            commercial_tier="🥇 Hot"))
        assert f["commercial_fit_score_app"] == 7.2
        assert f["commercial_tier_app"] == "🥇 Hot"


class TestForeignHQ:
    def test_positive_populates_hq_fields(self):
        f = build_caller_app_fields(_result(
            sig_foreign_hq_score_for_next_scoring=3.0,
            hq_detected_country="Germany", hq_detected_city="Munich"))
        assert f["foreign_hq_signal_used_in_app"] == "Yes"
        assert f["foreign_hq_country_app"] == "Germany"
        assert f["foreign_hq_city_app"] == "Munich"
        assert "Foreign HQ signal: Germany / Munich" in f["what_is_hot_app"]

    def test_no_foreign_hq_sets_no(self):
        f = build_caller_app_fields(_result(sig_foreign_hq_score_for_next_scoring=0.0))
        assert f["foreign_hq_signal_used_in_app"] == "No"
        assert f["foreign_hq_country_app"] is None


class TestWhatIsHotNot:
    def test_hot_includes_positive_signals(self):
        f = build_caller_app_fields(_result(
            sig_foreign_hq_score_for_next_scoring=3.0, hq_detected_country="Germany",
            sig_international_profile_score=2.0,
            sig_onboarding_training_need_score=1.0,
            sig_company_size_complexity_score=2.0,
            sig_icp_keyword_match_score=2.0))
        hot = f["what_is_hot_app"]
        assert "Foreign HQ signal: Germany" in hot
        assert "International profile evidence found" in hot
        assert "Onboarding/training need evidence found" in hot
        assert "Company complexity evidence found" in hot
        assert "ICP keyword evidence found" in hot

    def test_not_includes_zero_and_missing(self):
        f = build_caller_app_fields(_result(
            sig_onboarding_training_need_score=0.0,
            signals=[LeadSignal(signal_name="x")],   # signals present
            evidence_items=[LeadEvidence(evidence_id="e1")]))  # evidence present
        nt = f["what_is_not_app"]
        assert "No positive onboarding/training signal found" in nt
        assert "Commercial score not calculated" in nt
        assert "No non-HQ evidence collected yet" not in nt  # evidence present


class TestWhyRelevantPriority:
    def test_hq_plus_intl(self):
        f = build_caller_app_fields(_result(
            sig_foreign_hq_score_for_next_scoring=3.0,
            sig_international_profile_score=2.0))
        assert "foreign HQ signal and international profile" in f["why_relevant_app"]

    def test_hq_only(self):
        f = build_caller_app_fields(_result(sig_foreign_hq_score_for_next_scoring=3.0))
        assert "foreign parent/HQ signal outside" in f["why_relevant_app"]

    def test_intl_plus_onboarding(self):
        f = build_caller_app_fields(_result(
            sig_international_profile_score=2.0,
            sig_onboarding_training_need_score=1.0))
        assert "international and people-development" in f["why_relevant_app"]

    def test_icp_only(self):
        f = build_caller_app_fields(_result(sig_icp_keyword_match_score=2.0))
        assert "ICP-relevant keyword evidence" in f["why_relevant_app"]

    def test_score_only(self):
        f = build_caller_app_fields(_result(final_commercial_fit_score=4.0))
        assert "calculated commercial fit score" in f["why_relevant_app"]


class TestCallerAngleAndStarter:
    def test_angle_priority(self):
        assert "international HQ" in build_caller_app_fields(
            _result(sig_foreign_hq_score_for_next_scoring=3.0))["caller_angle_app"]
        onb_angle = build_caller_app_fields(
            _result(sig_onboarding_training_need_score=2.0))["caller_angle_app"]
        assert "onboarding, training" in onb_angle
        assert "centrally or locally" in onb_angle
        assert "international teams, sales" in build_caller_app_fields(
            _result(sig_icp_keyword_match_score=2.0))["caller_angle_app"]

    def test_call_starter_uses_company_name(self):
        f = build_caller_app_fields(_result(sig_foreign_hq_score_for_next_scoring=3.0))
        assert "Acme S.p.A." in f["call_starter_app"]

    def test_call_starter_fallback_company(self):
        f = build_caller_app_fields(LeadPrioritizationResult(company_name=""))
        assert "your company" in f["call_starter_app"]


class TestCaution:
    def test_manual_review_and_low_confidence(self):
        f = build_caller_app_fields(_result(needs_manual_review=True, hq_confidence="Low"))
        assert "Manual review recommended" in f["caution_app"]
        assert "HQ confidence is low." in f["caution_app"]

    def test_hq_error(self):
        f = build_caller_app_fields(_result(ai_hq_error="boom"))
        assert "HQ interpretation reported an error." in f["caution_app"]

    def test_hq_score_without_country(self):
        f = build_caller_app_fields(_result(
            sig_foreign_hq_score_for_next_scoring=3.0, hq_detected_country=None))
        assert "without a detected HQ country" in f["caution_app"]

    def test_missing_scoring_fields(self):
        f = build_caller_app_fields(_result(
            final_commercial_fit_score=3.0, missing_scoring_fields="sig_intl_footprint_score"))
        assert "missing signal defaults" in f["caution_app"]


class TestNoForbiddenTerms:
    def test_no_competitor_or_growth_terms_anywhere(self):
        f = build_caller_app_fields(_result(
            sig_foreign_hq_score_for_next_scoring=3.0, hq_detected_country="Germany",
            sig_international_profile_score=2.0, sig_onboarding_training_need_score=2.0,
            sig_company_size_complexity_score=2.0, sig_icp_keyword_match_score=2.0,
            final_commercial_fit_score=8.0, commercial_tier="🥇 Hot",
            needs_manual_review=True, hq_confidence="Low"))
        blob = " ".join(str(v) for v in f.values()).lower()
        for term in _FORBIDDEN:
            assert term not in blob, f"forbidden term {term!r} present"


class TestCoreGating:
    _lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")

    def _run(self, **flags):
        p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                         return_value={"organic": []})
        p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai",
                     return_value=HQDetectionResult(
                         hq_structure_type="foreign_parent", foreign_hq_simple=True,
                         hq_detected_country="Germany", hq_detected_city="Munich",
                         sig_foreign_hq_score_for_next_scoring=3.0))
        with p_serper, p_ai:
            return prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake", **flags)

    def test_default_does_not_build(self):
        r = self._run()
        assert r.what_is_hot_app is None
        assert r.foreign_hq_signal_used_in_app is None
        assert r.call_starter_app is None

    def test_flag_true_builds_and_keeps_hq(self):
        r = self._run(build_caller_app_fields_flag=True)
        assert r.foreign_hq_signal_used_in_app == "Yes"
        assert r.foreign_hq_country_app == "Germany"
        assert r.what_is_hot_app and "Foreign HQ signal: Germany / Munich" in r.what_is_hot_app
        assert r.call_starter_app and "Acme" in r.call_starter_app
        # HQ fields untouched.
        assert r.hq_structure_type == "foreign_parent"
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
