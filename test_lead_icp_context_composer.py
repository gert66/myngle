"""Tests for lead_icp_context_composer.py (rich ICP context, no live calls).

The Anthropic client is mocked at the module level, same pattern as
test_lead_caller_content_composer.py. Serper is mocked via
call_serper_for_enrichment.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lead_icp_context_composer import (
    DEFAULT_ICP_CONTEXT_MODEL,
    build_icp_context_prompt,
    build_icp_context_queries,
    collect_icp_context_evidence,
    compose_icp_context,
)
from lead_output_schema import LeadEvidence


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_icp_context_composer._anthropic_lib", lib)


_BASE_KWARGS = dict(
    company_name="Nordic Gear AB",
    country="Netherlands",
    curated_signals=[{
        "signal_name": "onboarding_training_need",
        "label": "Learning and development or onboarding needs",
        "evidence": "Careers page mentions an onboarding academy for new hires.",
        "source_url": "https://nordicgear.se/careers",
    }],
    extra_evidence=[{
        "label": "general_company_context",
        "evidence": "Nordic Gear AB is a mid-sized manufacturer based in Sweden.",
        "source_url": "https://nordicgear.se/about",
    }],
    anthropic_api_key="fake",
)


# ---------------------------------------------------------------------------
# Query builder — no competitor query, in the spirit of Q1/Q3/Q4 only.
# ---------------------------------------------------------------------------

class TestBuildIcpContextQueries:
    def test_returns_three_thematic_queries(self):
        specs = build_icp_context_queries("nordicgear")
        assert len(specs) == 3
        labels = {s["label"] for s in specs}
        assert labels == {
            "general_company_context", "lnd_employee_training", "language_global_teams",
        }
        for s in specs:
            assert "nordicgear" in s["query"]

    def test_no_competitor_terms_in_any_query(self):
        specs = build_icp_context_queries("acme")
        blob = " ".join(s["query"].lower() for s in specs)
        for term in ("preply", "learnlight", "speexx", "goFLUENT".lower(),
                     "duolingo", "competitor", "lms"):
            assert term not in blob

    def test_blank_root_returns_empty(self):
        assert build_icp_context_queries("") == []
        assert build_icp_context_queries(None) == []


# ---------------------------------------------------------------------------
# Evidence collection — hosted-platform guard applied before the prompt.
# ---------------------------------------------------------------------------

class TestCollectIcpContextEvidence:
    def test_collects_evidence_across_three_queries(self):
        def _fake_serper(query, key, **kwargs):
            return {"organic": [{"title": "T", "snippet": f"snippet for {query}",
                                  "link": "https://acme.com/page"}]}
        with patch("lead_icp_context_composer.call_serper_for_enrichment",
                   side_effect=_fake_serper):
            out = collect_icp_context_evidence("Acme", "acme.com", "fake-key")
        assert len(out) == 3
        for item in out:
            assert item["source_url"] == "https://acme.com/page"
            assert item["evidence"]

    def test_hosted_platform_evidence_excluded(self):
        def _fake_serper(query, key, **kwargs):
            return {"organic": [{"title": "T", "snippet": "some snippet",
                                  "link": "https://acme.wd3.myworkdayjobs.com/en-US/Careers"}]}
        with patch("lead_icp_context_composer.call_serper_for_enrichment",
                   side_effect=_fake_serper):
            out = collect_icp_context_evidence("Acme", "acme.com", "fake-key")
        assert out == []

    def test_no_serper_key_or_domain_still_safe(self):
        with patch("lead_icp_context_composer.call_serper_for_enrichment",
                   return_value={}):
            out = collect_icp_context_evidence("", None, "")
        assert out == []


# ---------------------------------------------------------------------------
# Prompt — must forbid competitor/alternative-provider content.
# ---------------------------------------------------------------------------

class TestPrompt:
    def test_system_prompt_forbids_competitor_content(self):
        from lead_icp_context_composer import _SYSTEM_PROMPT
        low = _SYSTEM_PROMPT.lower()
        assert "competitor" in low
        assert "alternative training providers" in low or "alternative-provider" in low

    def test_user_prompt_rules_forbid_competitor_content(self):
        prompt = build_icp_context_prompt(
            company_name="Acme", country="Netherlands",
            curated_signals=[], extra_evidence=[])
        assert "competitor" in prompt.lower()

    def test_prompt_includes_curated_and_extra_evidence(self):
        prompt = build_icp_context_prompt(**{
            k: v for k, v in _BASE_KWARGS.items()
            if k in ("company_name", "country", "curated_signals", "extra_evidence")
        })
        assert "onboarding academy for new hires" in prompt
        assert "mid-sized manufacturer based in Sweden" in prompt

    def test_prompt_empty_evidence_shows_none(self):
        prompt = build_icp_context_prompt(
            company_name="Acme", country=None, curated_signals=[], extra_evidence=[])
        assert "(none)" in prompt


# ---------------------------------------------------------------------------
# 1. Mocked successful API response — fields correctly mapped.
# ---------------------------------------------------------------------------

class TestSuccessfulComposition:
    def test_fields_mapped_from_clean_json(self):
        payload = json.dumps({
            "icp_buying_signals": "Active onboarding academy suggests near-term L&D investment.",
            "icp_likely_training_interest": "Onboarding and new-hire ramp-up.",
            "icp_potential_buyer_function": "HR / Talent Development",
        })
        with _mock_anthropic(payload):
            result = compose_icp_context(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is True
        assert result.error == ""
        assert result.buying_signals == (
            "Active onboarding academy suggests near-term L&D investment."
        )
        assert result.likely_training_interest == "Onboarding and new-hire ramp-up."
        assert result.potential_buyer_function == "HR / Talent Development"
        assert result.model == DEFAULT_ICP_CONTEXT_MODEL

    def test_fenced_json_is_stripped(self):
        payload = "```json\n" + json.dumps({
            "icp_buying_signals": "Fenced signals.",
            "icp_likely_training_interest": "Fenced interest.",
            "icp_potential_buyer_function": "Fenced function.",
        }) + "\n```"
        with _mock_anthropic(payload):
            result = compose_icp_context(**_BASE_KWARGS)
        assert result.call_success is True
        assert result.buying_signals == "Fenced signals."

    def test_prose_around_json_is_stripped(self):
        payload = (
            'Here you go:\n' + json.dumps({
                "icp_buying_signals": "x", "icp_likely_training_interest": "y",
                "icp_potential_buyer_function": "z",
            }) + '\nHope that helps.'
        )
        with _mock_anthropic(payload):
            result = compose_icp_context(**_BASE_KWARGS)
        assert result.call_success is True
        assert result.buying_signals == "x"


# ---------------------------------------------------------------------------
# 2. Mocked API error — silent fallback, audit note (error) present.
# ---------------------------------------------------------------------------

class TestApiErrorFallback:
    def test_client_exception_yields_silent_fallback_with_error(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_icp_context_composer._anthropic_lib", lib):
            result = compose_icp_context(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error.startswith("icp_context_call_failed")
        assert "connection reset" in result.error
        assert result.buying_signals is None
        assert result.likely_training_interest is None
        assert result.potential_buyer_function is None

    def test_no_api_key_never_attempts_call(self):
        kwargs = dict(_BASE_KWARGS)
        kwargs["anthropic_api_key"] = ""
        result = compose_icp_context(**kwargs)
        assert result.call_attempted is False
        assert result.call_success is False
        assert result.error == "no_anthropic_api_key"


# ---------------------------------------------------------------------------
# 3. Mocked unparseable response — silent fallback, audit note present.
# ---------------------------------------------------------------------------

class TestUnparseableResponseFallback:
    def test_prose_response_yields_silent_fallback_with_error(self):
        text = "I'm sorry, I cannot produce a JSON object for this request."
        with _mock_anthropic(text):
            result = compose_icp_context(**_BASE_KWARGS)
        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error == "icp_context_parse_failed"
        assert result.raw_json == text
        assert result.buying_signals is None

    def test_empty_response_yields_silent_fallback_with_error(self):
        with _mock_anthropic(""):
            result = compose_icp_context(**_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "icp_context_parse_failed"

    def test_non_dict_json_yields_silent_fallback_with_error(self):
        with _mock_anthropic("[1, 2, 3]"):
            result = compose_icp_context(**_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "icp_context_parse_failed"
