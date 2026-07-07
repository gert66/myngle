"""Tests for lead_ai_signal_scorer.py (Onderdeel 2, no live calls).

The Anthropic client is mocked at the module level, same pattern as
test_lead_icp_context_composer.py.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lead_ai_signal_scorer import (
    DEFAULT_AI_SIGNAL_SCORING_MODEL,
    build_ai_signal_scoring_prompt,
    score_signals_with_ai,
)
from lead_output_schema import LeadEvidence


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_ai_signal_scorer._anthropic_lib", lib)


def _ev(signal_name, evidence_id, title="", snippet="", url="https://acme.com/p"):
    return LeadEvidence(
        evidence_id=evidence_id, signal_name=signal_name,
        source_title=title, source_snippet=snippet, source_url=url,
        query_used="q", parser_source="serper_organic_1", source_type="organic",
    )


_BASE_KWARGS = dict(
    company_name="Acme Global BV",
    country="Netherlands",
    anthropic_api_key="fake-key",
)


# ---------------------------------------------------------------------------
# Prompt / evidence filtering
# ---------------------------------------------------------------------------

class TestPromptAndFiltering:
    def test_prompt_contains_only_usable_evidence(self):
        prompt = build_ai_signal_scoring_prompt(
            company_name="Acme", country="Italy",
            by_signal={"international_profile": [
                _ev("international_profile", "ip:1", snippet="Global reach across 11 countries."),
            ]},
        )
        assert "ip:1" in prompt
        assert "Global reach across 11 countries." in prompt
        assert "international_profile" in prompt

    def test_hosted_platform_evidence_excluded_from_call(self):
        calls = {"n": 0}

        def _capture(*a, **k):
            calls["n"] += 1
            raise RuntimeError("should not be called")

        ev = [_ev("international_profile", "ip:1", snippet="Global company.",
                  url="https://acme.wd3.myworkdayjobs.com/en-US/jobs")]
        with patch("lead_ai_signal_scorer._anthropic_lib") as lib:
            lib.Anthropic.side_effect = _capture
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)

        assert calls["n"] == 0
        assert result.call_attempted is False
        assert result.error == "no_usable_evidence"

    def test_external_training_evidence_excluded_from_call(self):
        ev = [_ev("onboarding_training_need", "ot:1",
                  snippet="Become an installer -- climate solutions partner training.")]
        result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        assert result.call_attempted is False
        assert result.error == "no_usable_evidence"

    def test_no_evidence_at_all_never_attempts_call(self):
        result = score_signals_with_ai(evidence_items=[], **_BASE_KWARGS)
        assert result.call_attempted is False
        assert result.error == "no_usable_evidence"
        assert result.signals == []

    def test_no_api_key_never_attempts_call(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach.")]
        kwargs = dict(_BASE_KWARGS)
        kwargs["anthropic_api_key"] = ""
        result = score_signals_with_ai(evidence_items=ev, **kwargs)
        assert result.call_attempted is False
        assert result.error == "no_anthropic_api_key"


# ---------------------------------------------------------------------------
# Verdicts translated into LeadSignal objects
# ---------------------------------------------------------------------------

class TestVerdictTranslation:
    def test_positive_verdict_with_valid_id_becomes_score_2(self):
        ev = [_ev("international_profile", "ip:1",
                  snippet="Opera in 11 paesi con sedi in tutta Europa.",
                  url="https://acme.com/about")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "positive_evidence",
                "reason": "Operates in 11 countries -- clearly international.",
                "supporting_evidence_ids": ["ip:1"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)

        assert result.call_success is True
        assert len(result.signals) == 1
        sig = result.signals[0]
        assert sig.signal_name == "international_profile"
        assert sig.signal_value == "positive_evidence"
        assert sig.signal_score == 2.0
        assert sig.signal_confidence == "High"
        assert sig.evidence_url == "https://acme.com/about"
        assert sig.evidence_urls == ["https://acme.com/about"]
        assert "11 countries" in sig.signal_reason

    def test_weak_verdict_becomes_score_1(self):
        ev = [_ev("employer_branding", "eb:1", snippet="Some mention of workplace culture.")]
        payload = json.dumps({
            "employer_branding": {
                "verdict": "weak_evidence", "reason": "Thin mention.",
                "supporting_evidence_ids": ["eb:1"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.signal_value == "weak_evidence"
        assert sig.signal_score == 1.0

    def test_no_positive_match_verdict_becomes_score_0(self):
        ev = [_ev("company_size_complexity", "cs:1", snippet="Unrelated marketing copy.")]
        payload = json.dumps({
            "company_size_complexity": {
                "verdict": "no_positive_match", "reason": "Nothing relevant.",
                "supporting_evidence_ids": [],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.signal_value == "no_positive_match"
        assert sig.signal_score == 0.0
        assert sig.evidence_urls == []

    def test_only_signals_with_usable_evidence_are_present_in_result(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach across countries.")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "positive_evidence", "reason": "Clear.",
                "supporting_evidence_ids": ["ip:1"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        names = {s.signal_name for s in result.signals}
        assert names == {"international_profile"}


# ---------------------------------------------------------------------------
# Mechanical validation -- the AI never gets the final say on which sources
# exist.
# ---------------------------------------------------------------------------

class TestMechanicalValidation:
    def test_nonexistent_evidence_id_is_dropped(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach across countries.",
                  url="https://acme.com/about")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "positive_evidence", "reason": "Clear.",
                "supporting_evidence_ids": ["ip:1", "ip:INVENTED"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.evidence_urls == ["https://acme.com/about"]

    def test_verdict_with_only_invented_ids_is_downgraded(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach across countries.")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "positive_evidence", "reason": "Clear.",
                "supporting_evidence_ids": ["ip:DOES-NOT-EXIST"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.signal_value == "no_positive_match"
        assert sig.signal_score == 0.0
        assert sig.evidence_urls == []

    def test_verdict_with_no_ids_at_all_is_downgraded(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach across countries.")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "weak_evidence", "reason": "Hmm.",
                "supporting_evidence_ids": [],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.signal_value == "no_positive_match"

    def test_evidence_id_from_a_different_signal_is_not_accepted(self):
        ev = [
            _ev("international_profile", "ip:1", snippet="Global reach.",
               url="https://acme.com/intl"),
            _ev("employer_branding", "eb:1", snippet="Great culture.",
               url="https://acme.com/careers"),
        ]
        payload = json.dumps({
            "international_profile": {
                "verdict": "positive_evidence", "reason": "Clear.",
                # eb:1 belongs to employer_branding, not international_profile.
                "supporting_evidence_ids": ["eb:1"],
            },
            "employer_branding": {
                "verdict": "no_positive_match", "reason": "Nothing.",
                "supporting_evidence_ids": [],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        by_name = {s.signal_name: s for s in result.signals}
        assert by_name["international_profile"].signal_value == "no_positive_match"

    def test_invalid_verdict_string_falls_back_to_no_positive_match(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach.")]
        payload = json.dumps({
            "international_profile": {
                "verdict": "extremely_positive", "reason": "Clear.",
                "supporting_evidence_ids": ["ip:1"],
            },
        })
        with _mock_anthropic(payload):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        sig = result.signals[0]
        assert sig.signal_value == "no_positive_match"


# ---------------------------------------------------------------------------
# API error / unparseable response -> caller falls back to deterministic
# ---------------------------------------------------------------------------

class TestFailureFallback:
    def test_client_exception_yields_failure_with_error(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach.")]
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_ai_signal_scorer._anthropic_lib", lib):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error.startswith("ai_signal_scoring_call_failed")
        assert result.signals == []

    def test_unparseable_response_yields_failure_with_error(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach.")]
        with _mock_anthropic("I cannot help with that."):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "ai_signal_scoring_parse_failed"
        assert result.signals == []

    def test_model_default_is_recorded(self):
        ev = [_ev("international_profile", "ip:1", snippet="Global reach.")]
        with _mock_anthropic("not json"):
            result = score_signals_with_ai(evidence_items=ev, **_BASE_KWARGS)
        assert result.model == DEFAULT_AI_SIGNAL_SCORING_MODEL
