"""Tests for the Lead Prioritizer v2 full single-lead pipeline preset.

External calls (Serper HQ + non-HQ, Anthropic HQ interpretation) are mocked;
no live API keys required.
"""

from __future__ import annotations

from unittest.mock import patch

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
    with p_serper, p_ai, p_collect:
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
