"""Tests for the Lead Prioritizer v2 full single-lead pipeline preset.

External calls (Serper HQ + non-HQ, Anthropic HQ interpretation) are mocked;
no live API keys required.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from lead_caller_content_composer import ComposedCallerContent
from lead_icp_context_composer import IcpContextResult
from lead_output_schema import LeadInput, HQDetectionResult, LeadEvidence
from lead_prioritizer_core import prioritize_single_lead


_HQ = HQDetectionResult(
    hq_structure_type="foreign_parent",
    foreign_hq_simple=True,
    hq_detected_country="Germany",
    hq_detected_city="Munich",
    hq_confidence="High",
    sig_foreign_hq_score_for_next_scoring=3.0,
)

# Non-HQ evidence with positive keywords across the supported signals.
_NON_HQ_EVIDENCE = [
    LeadEvidence(signal_name="international_profile", source_title="Global reach",
                 source_snippet="Global company with offices in many countries.",
                 source_url="https://acme.com/intl", query_used="q1",
                 parser_source="serper_organic_1", source_type="organic"),
    LeadEvidence(signal_name="onboarding_training_need", source_title="Careers",
                 source_snippet="Training and onboarding academy for employees.",
                 source_url="https://acme.com/careers", query_used="q2",
                 parser_source="serper_organic_1", source_type="organic"),
]

_LEAD = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")


def _run(**flags):
    p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                     return_value={"organic": []})
    p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai", return_value=_HQ)
    p_collect = patch("lead_prioritizer_core.collect_non_hq_enrichment_evidence",
                      return_value=list(_NON_HQ_EVIDENCE))
    # Rich ICP context's own broader-query evidence collection is independent
    # of Step 2 above and would otherwise make a live Serper call; default to
    # no extra evidence unless a test overrides it.
    p_icp_evidence = patch("lead_prioritizer_core.collect_icp_context_evidence",
                          return_value=[])
    with p_serper, p_ai, p_collect, p_icp_evidence:
        return prioritize_single_lead(
            _LEAD, serper_api_key="fake", anthropic_api_key="fake", **flags)


# ---------------------------------------------------------------------------
# Pipeline mode
# ---------------------------------------------------------------------------

class TestPipelineMode:
    def test_default_is_hq_only(self):
        assert _run().v2_pipeline_mode == "hq_only"

    def test_partial_when_one_flag(self):
        r = _run(collect_non_hq_evidence=True)
        assert r.v2_pipeline_mode == "partial_v2"

    def test_partial_when_scoring_only(self):
        r = _run(calculate_commercial_score_flag=True)
        assert r.v2_pipeline_mode == "partial_v2"

    def test_full_preset_mode(self):
        assert _run(run_full_v2_pipeline=True).v2_pipeline_mode == "full_v2_single_lead"


# ---------------------------------------------------------------------------
# Full preset activates every step
# ---------------------------------------------------------------------------

class TestFullPresetActivatesAllSteps:
    def test_all_steps_populated(self):
        r = _run(run_full_v2_pipeline=True)
        # Step 2: evidence
        assert r.evidence_items
        # Step 3: signals
        assert r.signals
        assert r.sig_international_profile_score is not None
        # Step 4: app summaries
        assert r.key_source_links_app
        assert r.advanced_notes_app
        # Step 5: commercial score
        assert r.final_commercial_fit_score is not None
        assert r.scoring_profile == "italy_register_icp_only"
        # Step 6: caller/app fields
        assert r.foreign_hq_signal_used_in_app == "Yes"
        assert r.what_is_hot_app
        assert r.call_starter_app
        # HQ canonical fields untouched
        assert r.hq_structure_type == "foreign_parent"
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
        assert r.input_country == "Italy"

    def test_full_preset_needs_no_extra_flags(self):
        # Passing only run_full_v2_pipeline (no individual flags) is enough.
        r = _run(run_full_v2_pipeline=True)
        assert r.evidence_items and r.signals and r.final_commercial_fit_score is not None
        assert r.what_is_hot_app is not None

    def test_individual_flag_behavior_unchanged_when_preset_off(self):
        # Explicit single flag with preset off keeps prior behavior: only that
        # step runs, mode is partial_v2, later steps stay empty.
        r = _run(collect_non_hq_evidence=True)
        assert r.evidence_items          # step 2 ran
        assert r.signals == []           # step 3 did not
        assert r.final_commercial_fit_score is None  # step 5 did not
        assert r.what_is_hot_app is None             # step 6 did not
        assert r.v2_pipeline_mode == "partial_v2"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

class TestGuardrails:
    _FORBIDDEN = ["competitor", "competing", "rival", "rapid growth",
                  "fast growing", "fastest growing", "alternative provider"]

    def test_no_forbidden_terms_in_full_pipeline_app_fields(self):
        r = _run(run_full_v2_pipeline=True)
        blob = " ".join(str(v) for v in [
            r.what_is_hot_app, r.what_is_not_app, r.why_relevant_app,
            r.caller_angle_app, r.call_starter_app, r.caution_app,
            r.evidence_summary_app, r.advanced_notes_app, r.key_source_links_app,
        ]).lower()
        for term in self._FORBIDDEN:
            assert term not in blob, f"forbidden term {term!r} present"

    def test_order_hq_before_non_hq(self):
        # HQ interpret must be called before non-HQ collection.
        calls = []
        p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                         return_value={"organic": []})
        p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai",
                     side_effect=lambda **k: calls.append("hq") or _HQ)
        p_collect = patch("lead_prioritizer_core.collect_non_hq_enrichment_evidence",
                          side_effect=lambda **k: calls.append("non_hq") or list(_NON_HQ_EVIDENCE))
        with p_serper, p_ai, p_collect:
            prioritize_single_lead(_LEAD, serper_api_key="fake",
                                   anthropic_api_key="fake", run_full_v2_pipeline=True)
        assert calls == ["hq", "non_hq"]


# ---------------------------------------------------------------------------
# Step 3 — AI caller-content composition (explicit opt-in only)
# ---------------------------------------------------------------------------

class TestComposeCallerContentFlag:
    def test_not_run_by_default(self):
        r = _run()
        assert r.composed_by_ai is None
        assert r.composed_why_relevant is None

    def test_full_preset_does_not_enable_it(self):
        # run_full_v2_pipeline turns on steps 2-6, but composition stays an
        # explicit, separate opt-in.
        r = _run(run_full_v2_pipeline=True)
        assert r.composed_by_ai is None
        assert r.composed_why_relevant is None

    def test_success_populates_composed_fields(self):
        composed = ComposedCallerContent(
            why_relevant="Acme is a Germany-linked company with clear international reach.",
            what_is_hot=["Global offices across many countries.", "Active onboarding academy."],
            cold_caller_summary="Acme looks like a strong first-conversation candidate.",
            caller_angle="Ask how they coordinate training across countries.",
            call_starter="I noticed Acme operates internationally — how do you handle onboarding?",
            driver_evidence={"international_profile": "Global company with offices in many countries."},
            call_attempted=True,
            call_success=True,
        )
        with patch("lead_prioritizer_core.compose_caller_content", return_value=composed):
            r = _run(run_full_v2_pipeline=True, compose_caller_content_flag=True)
        assert r.composed_by_ai is True
        assert r.composed_why_relevant == composed.why_relevant
        assert r.composed_what_is_hot == "\n".join(composed.what_is_hot)
        assert r.composed_cold_caller_summary == composed.cold_caller_summary
        assert r.composed_caller_angle == composed.caller_angle
        assert r.composed_call_starter == composed.call_starter
        assert json.loads(r.composed_driver_evidence_json) == composed.driver_evidence
        assert r.v2_pipeline_mode == "full_v2_single_lead"

    def test_failure_falls_back_silently_with_audit_note(self):
        composed = ComposedCallerContent(
            call_attempted=True, call_success=False, error="no_anthropic_api_key",
        )
        with patch("lead_prioritizer_core.compose_caller_content", return_value=composed):
            r = _run(run_full_v2_pipeline=True, compose_caller_content_flag=True)
        assert r.composed_by_ai is False
        assert r.composed_why_relevant is None
        assert r.composed_what_is_hot is None
        # Deterministic templates from Step 6 are still present (the fallback).
        assert r.why_relevant_app is not None
        assert r.what_is_hot_app is not None
        assert r.composed_content_note is not None
        assert "no_anthropic_api_key" in r.composed_content_note

    def test_flag_alone_marks_partial_v2(self):
        composed = ComposedCallerContent(call_attempted=True, call_success=False, error="no_anthropic_api_key")
        with patch("lead_prioritizer_core.compose_caller_content", return_value=composed):
            r = _run(compose_caller_content_flag=True)
        assert r.v2_pipeline_mode == "partial_v2"


# ---------------------------------------------------------------------------
# Rich ICP context (explicit opt-in only, INDEPENDENT of compose_caller_content)
# ---------------------------------------------------------------------------

class TestComposeIcpContextFlag:
    def test_not_run_by_default(self):
        r = _run()
        assert r.icp_context_by_ai is None
        assert r.icp_buying_signals is None

    def test_full_preset_does_not_enable_it(self):
        r = _run(run_full_v2_pipeline=True)
        assert r.icp_context_by_ai is None
        assert r.icp_buying_signals is None

    def test_success_populates_icp_fields(self):
        icp = IcpContextResult(
            buying_signals="Active onboarding academy suggests near-term L&D investment.",
            likely_training_interest="Onboarding and new-hire ramp-up.",
            potential_buyer_function="HR / Talent Development",
            call_attempted=True, call_success=True,
        )
        with patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(run_full_v2_pipeline=True, compose_icp_context=True)
        assert r.icp_context_by_ai is True
        assert r.icp_buying_signals == icp.buying_signals
        assert r.icp_likely_training_interest == icp.likely_training_interest
        assert r.icp_potential_buyer_function == icp.potential_buyer_function
        assert r.v2_pipeline_mode == "full_v2_single_lead"

    def test_failure_falls_back_silently_with_audit_note(self):
        icp = IcpContextResult(call_attempted=True, call_success=False, error="no_anthropic_api_key")
        with patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(run_full_v2_pipeline=True, compose_icp_context=True)
        assert r.icp_context_by_ai is False
        assert r.icp_buying_signals is None
        assert r.icp_likely_training_interest is None
        assert r.icp_potential_buyer_function is None
        assert r.icp_context_content_note is not None
        assert "no_anthropic_api_key" in r.icp_context_content_note

    def test_flag_alone_marks_partial_v2(self):
        icp = IcpContextResult(call_attempted=True, call_success=False, error="no_anthropic_api_key")
        with patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(compose_icp_context=True)
        assert r.v2_pipeline_mode == "partial_v2"


# ---------------------------------------------------------------------------
# Independence of the two opt-in composition flags — either may run without
# the other, and enabling one must never implicitly enable/require the other.
# ---------------------------------------------------------------------------

class TestComposeFlagsAreIndependent:
    def test_icp_context_alone_does_not_touch_caller_content_fields(self):
        icp = IcpContextResult(
            buying_signals="signals", likely_training_interest="interest",
            potential_buyer_function="function", call_attempted=True, call_success=True,
        )
        with patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(run_full_v2_pipeline=True, compose_icp_context=True)
        assert r.icp_context_by_ai is True
        assert r.composed_by_ai is None
        assert r.composed_why_relevant is None

    def test_caller_content_alone_does_not_touch_icp_context_fields(self):
        composed = ComposedCallerContent(
            why_relevant="why", call_attempted=True, call_success=True,
        )
        with patch("lead_prioritizer_core.compose_caller_content", return_value=composed):
            r = _run(run_full_v2_pipeline=True, compose_caller_content_flag=True)
        assert r.composed_by_ai is True
        assert r.icp_context_by_ai is None
        assert r.icp_buying_signals is None

    def test_both_flags_together_populate_both_families(self):
        composed = ComposedCallerContent(
            why_relevant="why", call_attempted=True, call_success=True,
        )
        icp = IcpContextResult(
            buying_signals="signals", call_attempted=True, call_success=True,
        )
        with patch("lead_prioritizer_core.compose_caller_content", return_value=composed), \
             patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(run_full_v2_pipeline=True,
                     compose_caller_content_flag=True, compose_icp_context=True)
        assert r.composed_by_ai is True
        assert r.icp_context_by_ai is True


# ---------------------------------------------------------------------------
# Score-invariance: rich ICP context must never affect evidence_items,
# signals, or final_commercial_fit_score (Hard Requirement #1, Part A).
# ---------------------------------------------------------------------------

class TestComposeIcpContextScoreInvariance:
    _SCORE_FIELDS = (
        "final_commercial_fit_score", "commercial_tier", "icp_similarity_score",
        "lean_model_prob", "lr_z_score", "scoring_profile",
        "sig_foreign_hq_score_for_next_scoring",
        "sig_international_profile_score", "sig_onboarding_training_need_score",
        "sig_company_size_complexity_score", "sig_icp_keyword_match_score",
    )

    def _snapshot(self, r):
        snap = {f: getattr(r, f) for f in self._SCORE_FIELDS}
        snap["evidence_items"] = list(r.evidence_items or [])
        snap["signals"] = list(r.signals or [])
        return snap

    def test_identical_scoring_with_and_without_icp_context(self):
        icp = IcpContextResult(
            buying_signals="signals", likely_training_interest="interest",
            potential_buyer_function="function", call_attempted=True, call_success=True,
        )
        # Non-empty extra evidence, to prove it never leaks into scoring even
        # when actually collected.
        extra_evidence = [{"label": "general_company_context",
                          "evidence": "Some broader context.", "source_url": "https://acme.com"}]

        r_without = _run(run_full_v2_pipeline=True, calculate_commercial_score_flag=True)

        with patch("lead_prioritizer_core.collect_icp_context_evidence",
                   return_value=extra_evidence), \
             patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r_with = _run(run_full_v2_pipeline=True, calculate_commercial_score_flag=True,
                          compose_icp_context=True)

        assert self._snapshot(r_without) == self._snapshot(r_with)
        # The composition itself did run and populated its own fields.
        assert r_with.icp_context_by_ai is True
        assert r_without.icp_context_by_ai is None

    def test_icp_context_failure_also_leaves_scoring_untouched(self):
        icp = IcpContextResult(call_attempted=True, call_success=False, error="icp_context_parse_failed")
        r_without = _run(run_full_v2_pipeline=True, calculate_commercial_score_flag=True)
        with patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r_with = _run(run_full_v2_pipeline=True, calculate_commercial_score_flag=True,
                          compose_icp_context=True)
        assert self._snapshot(r_without) == self._snapshot(r_with)


# ---------------------------------------------------------------------------
# Onderdeel 2: opt-in AI signal scoring. Unlike every other v2 opt-in flag,
# this ONE is explicitly allowed to change final_commercial_fit_score -- but
# ONLY when actually enabled and successful; off (the default) must stay
# byte-identical, and a failed AI call must fall back to the deterministic
# extractor rather than ever shipping a row with no signals.
# ---------------------------------------------------------------------------

class TestAiSignalScoringFlag:
    def test_off_by_default(self):
        r = _run(extract_non_hq_signals_flag=True)
        assert r.signal_scoring_mode == "deterministic"

    def test_full_preset_does_not_enable_it(self):
        r = _run(run_full_v2_pipeline=True)
        assert r.signal_scoring_mode == "deterministic"

    def test_flag_off_is_byte_identical_to_current_behavior(self):
        r_default = _run(extract_non_hq_signals_flag=True, calculate_commercial_score_flag=True)
        r_explicit_off = _run(extract_non_hq_signals_flag=True,
                              calculate_commercial_score_flag=True, ai_signal_scoring=False)
        assert r_default.signals == r_explicit_off.signals
        assert r_default.final_commercial_fit_score == r_explicit_off.final_commercial_fit_score
        assert r_explicit_off.signal_scoring_mode == "deterministic"

    def test_flag_alone_without_extract_flag_does_nothing(self):
        r = _run(ai_signal_scoring=True)
        assert r.signals == []
        assert r.signal_scoring_mode == "deterministic"

    def test_ai_success_replaces_signals_and_marks_mode(self):
        from lead_ai_signal_scorer import AiSignalScoringResult
        from lead_output_schema import LeadSignal

        ai_signal = LeadSignal(
            signal_name="international_profile", signal_value="positive_evidence",
            signal_score=2.0, signal_confidence="High",
            evidence_url="https://acme.com/intl", evidence_urls=["https://acme.com/intl"],
        )
        ai_result = AiSignalScoringResult(
            signals=[ai_signal], call_attempted=True, call_success=True)
        with patch("lead_prioritizer_core.score_signals_with_ai", return_value=ai_result):
            r = _run(extract_non_hq_signals_flag=True, ai_signal_scoring=True)

        assert r.signal_scoring_mode == "ai"
        assert r.signals == [ai_signal]
        assert r.sig_international_profile_score == 2.0
        assert r.international_profile_evidence_url == "https://acme.com/intl"
        assert r.international_profile_evidence_urls == "https://acme.com/intl"

    def test_ai_failure_falls_back_to_deterministic(self):
        from lead_ai_signal_scorer import AiSignalScoringResult

        ai_result = AiSignalScoringResult(
            call_attempted=True, call_success=False,
            error="ai_signal_scoring_call_failed: connection reset",
        )
        r_deterministic = _run(extract_non_hq_signals_flag=True)
        with patch("lead_prioritizer_core.score_signals_with_ai", return_value=ai_result):
            r_ai_failed = _run(extract_non_hq_signals_flag=True, ai_signal_scoring=True)

        assert r_ai_failed.signal_scoring_mode == "deterministic"
        assert [s.signal_name for s in r_ai_failed.signals] == \
            [s.signal_name for s in r_deterministic.signals]
        assert [s.signal_score for s in r_ai_failed.signals] == \
            [s.signal_score for s in r_deterministic.signals]

    def test_same_signal_input_produces_same_score_as_deterministic(self):
        """Same scoring formula/weights either way -- proven by feeding the
        deterministic run's own signals back in as the "AI" result and
        checking the final score matches exactly."""
        r_deterministic = _run(extract_non_hq_signals_flag=True,
                               calculate_commercial_score_flag=True)

        from lead_ai_signal_scorer import AiSignalScoringResult
        ai_result = AiSignalScoringResult(
            signals=list(r_deterministic.signals), call_attempted=True, call_success=True)
        with patch("lead_prioritizer_core.score_signals_with_ai", return_value=ai_result):
            r_ai = _run(extract_non_hq_signals_flag=True,
                       calculate_commercial_score_flag=True, ai_signal_scoring=True)

        assert r_ai.final_commercial_fit_score == r_deterministic.final_commercial_fit_score
        assert r_ai.signal_scoring_mode == "ai"
        assert r_deterministic.signal_scoring_mode == "deterministic"


# ---------------------------------------------------------------------------
# Legacy enrichment mode: a separate, parallel comparison score. Must never
# touch final_commercial_fit_score or signals, whether it succeeds or fails.
# ---------------------------------------------------------------------------

class TestLegacyEnrichmentModeFlag:
    _SCORE_FIELDS = (
        "final_commercial_fit_score", "commercial_tier", "icp_similarity_score",
        "lean_model_prob", "lr_z_score", "scoring_profile",
        "sig_foreign_hq_score_for_next_scoring",
        "sig_international_profile_score", "sig_onboarding_training_need_score",
        "sig_company_size_complexity_score", "sig_icp_keyword_match_score",
    )

    def _snapshot(self, r):
        snap = {f: getattr(r, f) for f in self._SCORE_FIELDS}
        snap["evidence_items"] = list(r.evidence_items or [])
        snap["signals"] = list(r.signals or [])
        return snap

    def test_off_by_default(self):
        r = _run()
        assert r.legacy_score is None
        assert r.legacy_tier is None
        assert r.legacy_icp_lead_score is None

    def test_full_preset_does_not_enable_it(self):
        r = _run(run_full_v2_pipeline=True)
        assert r.legacy_score is None
        assert r.legacy_icp_lead_score is None

    def test_success_populates_legacy_fields(self):
        from lead_legacy_enrichment import LegacyEnrichmentResult

        legacy = LegacyEnrichmentResult(
            icp_lead_score="High", icp_buying_signals="International footprint",
            icp_likely_training_interest="Language training / Business English",
            icp_potential_buyer_function="HR", icp_why_relevant="Clear fit.",
            icp_evidence="Found international offices.",
            legacy_score=9.0, legacy_tier="High",
            call_attempted=True, call_success=True,
        )
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy):
            r = _run(run_full_v2_pipeline=True, calculate_commercial_score_flag=True,
                     legacy_enrichment_mode=True)

        assert r.legacy_score == 9.0
        assert r.legacy_tier == "High"
        assert r.legacy_icp_lead_score == "High"
        assert r.legacy_icp_buying_signals == "International footprint"
        assert r.legacy_icp_likely_training_interest == \
            "Language training / Business English"
        assert r.legacy_icp_potential_buyer_function == "HR"
        assert r.legacy_icp_why_relevant == "Clear fit."
        assert r.legacy_icp_evidence == "Found international offices."
        assert r.legacy_enrichment_error is None
        # Real v2 tier stays completely independent, never overwritten or
        # renamed to match the legacy High/Medium/Low scale.
        assert r.commercial_tier != "High"

    def test_failure_leaves_fields_blank_with_error_recorded(self):
        from lead_legacy_enrichment import LegacyEnrichmentResult

        legacy = LegacyEnrichmentResult(
            call_attempted=True, call_success=False,
            error="legacy_enrichment_call_failed: connection reset",
        )
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy):
            r = _run(legacy_enrichment_mode=True)

        assert r.legacy_score is None
        assert r.legacy_tier is None
        assert r.legacy_icp_lead_score is None
        assert r.legacy_enrichment_error == "legacy_enrichment_call_failed: connection reset"

    def test_flag_alone_marks_partial_v2(self):
        legacy = __import__("lead_legacy_enrichment").LegacyEnrichmentResult(
            call_attempted=True, call_success=False, error="no_anthropic_api_key")
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy):
            r = _run(legacy_enrichment_mode=True)
        assert r.v2_pipeline_mode == "partial_v2"

    def test_never_touches_score_or_signals_on_success(self):
        from lead_legacy_enrichment import LegacyEnrichmentResult

        legacy = LegacyEnrichmentResult(
            icp_lead_score="High", legacy_score=9.0, legacy_tier="High",
            call_attempted=True, call_success=True,
        )
        r_without = _run(extract_non_hq_signals_flag=True,
                         calculate_commercial_score_flag=True)
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy):
            r_with = _run(extract_non_hq_signals_flag=True,
                          calculate_commercial_score_flag=True,
                          legacy_enrichment_mode=True)

        assert self._snapshot(r_without) == self._snapshot(r_with)
        assert r_with.legacy_score == 9.0
        assert r_without.legacy_score is None

    def test_never_touches_score_or_signals_on_failure(self):
        from lead_legacy_enrichment import LegacyEnrichmentResult

        legacy = LegacyEnrichmentResult(
            call_attempted=True, call_success=False, error="legacy_enrichment_parse_failed")
        r_without = _run(extract_non_hq_signals_flag=True,
                         calculate_commercial_score_flag=True)
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy):
            r_with = _run(extract_non_hq_signals_flag=True,
                          calculate_commercial_score_flag=True,
                          legacy_enrichment_mode=True)

        assert self._snapshot(r_without) == self._snapshot(r_with)

    def test_independent_of_rich_icp_context_and_ai_signal_scoring(self):
        from lead_legacy_enrichment import LegacyEnrichmentResult
        from lead_icp_context_composer import IcpContextResult

        legacy = LegacyEnrichmentResult(
            icp_lead_score="Medium", legacy_score=6.0, legacy_tier="Medium",
            call_attempted=True, call_success=True,
        )
        icp = IcpContextResult(
            buying_signals="rich icp signals", call_attempted=True, call_success=True)
        with patch("lead_prioritizer_core.run_legacy_enrichment", return_value=legacy), \
             patch("lead_prioritizer_core.run_icp_context_composition", return_value=icp):
            r = _run(run_full_v2_pipeline=True, legacy_enrichment_mode=True,
                     compose_icp_context=True)

        # The rich-ICP-context fields and the legacy_icp_* fields must stay
        # in their own separate namespace and not clobber each other.
        assert r.icp_buying_signals == "rich icp signals"
        assert r.legacy_icp_lead_score == "Medium"
        assert r.legacy_score == 6.0
