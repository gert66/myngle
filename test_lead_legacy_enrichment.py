"""Tests for lead_legacy_enrichment.py (comparison feature, no live calls).

The Anthropic client is mocked at the module level, same pattern as
test_lead_icp_context_composer.py / test_lead_ai_signal_scorer.py. Serper is
mocked via call_serper_for_enrichment.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lead_legacy_enrichment import (
    DEFAULT_LEGACY_ENRICHMENT_MODEL,
    build_legacy_queries,
    run_legacy_enrichment,
)


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_legacy_enrichment._anthropic_lib", lib)


_BASE_KWARGS = dict(
    company_name="Acme Global BV",
    domain="acmeglobal.com",
    country="Netherlands",
    serper_api_key="fake-serper-key",
    anthropic_api_key="fake-key",
)


def _serper_payload(organic=None):
    return {"organic": organic or []}


# ---------------------------------------------------------------------------
# Query builder — 4 queries, Q1-Q4 of the original 5, no competitor query.
# ---------------------------------------------------------------------------

class TestBuildLegacyQueries:
    def test_returns_exactly_four_queries(self):
        queries = build_legacy_queries("Acme", "acme.com")
        assert len(queries) == 4

    def test_no_competitor_or_online_learning_terms(self):
        queries = build_legacy_queries("Acme", "acme.com")
        blob = " ".join(q["query"] for q in queries).lower()
        for term in ("preply", "learnlight", "speexx", "gofluent", "learnship",
                    "voxy", "berlitz", "talaera", "busuu", "duolingo", "lms"):
            assert term not in blob

    def test_site_anchor_used_when_domain_present(self):
        queries = build_legacy_queries("Acme", "acme.com")
        assert queries[0]["query"].startswith("site:acme.com OR ")

    def test_no_site_anchor_without_domain(self):
        queries = build_legacy_queries("Acme", None)
        assert not queries[0]["query"].startswith("site:")

    def test_empty_company_and_domain_yields_no_queries(self):
        assert build_legacy_queries("", None) == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestSuccessfulRun:
    def test_high_score_maps_to_9(self):
        payload = json.dumps({
            "lead_score": "High",
            "buying_signals": "International footprint, Explicit learning and development focus",
            "evidence": "Found international offices and an L&D academy.",
            "likely_training_interest": "Language training / Business English",
            "why_relevant": "Clear international L&D need.",
            "potential_buyer_function": "HR",
        })
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload([{"title": "Acme", "link": "https://acme.com",
                                                  "snippet": "Global offices."}])), \
             _mock_anthropic(payload):
            result = run_legacy_enrichment(**_BASE_KWARGS)

        assert result.call_attempted is True
        assert result.call_success is True
        assert result.error == ""
        assert result.icp_lead_score == "High"
        assert result.legacy_score == 9.0
        assert result.legacy_tier == "High"
        assert result.icp_buying_signals == \
            "International footprint, Explicit learning and development focus"
        assert result.icp_evidence == "Found international offices and an L&D academy."
        assert result.icp_likely_training_interest == "Language training / Business English"
        assert result.icp_why_relevant == "Clear international L&D need."
        assert result.icp_potential_buyer_function == "HR"
        assert len(result.queries_used) == 4
        assert result.model == DEFAULT_LEGACY_ENRICHMENT_MODEL

    def test_medium_score_maps_to_6(self):
        payload = json.dumps({"lead_score": "Medium", "buying_signals": "", "evidence": "",
                              "likely_training_interest": "", "why_relevant": "",
                              "potential_buyer_function": ""})
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), _mock_anthropic(payload):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        assert result.legacy_score == 6.0
        assert result.legacy_tier == "Medium"

    def test_low_score_maps_to_3(self):
        payload = json.dumps({"lead_score": "Low", "buying_signals": "", "evidence": "",
                              "likely_training_interest": "", "why_relevant": "",
                              "potential_buyer_function": ""})
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), _mock_anthropic(payload):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        assert result.legacy_score == 3.0
        assert result.legacy_tier == "Low"

    def test_unrecognized_lead_score_maps_to_0(self):
        payload = json.dumps({"lead_score": "Very High", "buying_signals": "", "evidence": "",
                              "likely_training_interest": "", "why_relevant": "",
                              "potential_buyer_function": ""})
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), _mock_anthropic(payload):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        assert result.legacy_score == 0.0
        assert result.legacy_tier == "Very High"

    def test_no_competitor_fields_in_output(self):
        payload = json.dumps({"lead_score": "Low", "buying_signals": "", "evidence": "",
                              "likely_training_interest": "", "why_relevant": "",
                              "potential_buyer_function": ""})
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), _mock_anthropic(payload):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        for attr in ("icp_competitor_signal", "icp_direct_language_competitor_signal",
                    "icp_online_language_learning_signal", "icp_broader_lnd_platform_signal"):
            assert not hasattr(result, attr)

    def test_no_competitor_terms_in_prompt(self):
        payload = json.dumps({"lead_score": "Low", "buying_signals": "", "evidence": "",
                              "likely_training_interest": "", "why_relevant": "",
                              "potential_buyer_function": ""})
        captured = {}

        def _fake_create(**kwargs):
            captured["system"] = kwargs.get("system")
            msg = MagicMock()
            msg.content = [MagicMock(text=payload)]
            return msg

        client = MagicMock()
        client.messages.create.side_effect = _fake_create
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), \
             patch("lead_legacy_enrichment._anthropic_lib", lib):
            run_legacy_enrichment(**_BASE_KWARGS)

        system_lower = captured["system"].lower()
        for term in ("competitor", "gofluent", "learnlight", "speexx", "preply",
                    "berlitz", "myngle"):
            assert term not in system_lower


# ---------------------------------------------------------------------------
# Hosted-platform guard (quality only, applied before the prompt)
# ---------------------------------------------------------------------------

class TestHostedPlatformGuard:
    def test_hosted_platform_hits_excluded_from_prompt(self):
        captured = {}

        def _fake_create(**kwargs):
            captured["messages"] = kwargs.get("messages")
            msg = MagicMock()
            msg.content = [MagicMock(text=json.dumps({
                "lead_score": "Low", "buying_signals": "", "evidence": "",
                "likely_training_interest": "", "why_relevant": "",
                "potential_buyer_function": "",
            }))]
            return msg

        client = MagicMock()
        client.messages.create.side_effect = _fake_create
        lib = MagicMock()
        lib.Anthropic.return_value = client

        payload = _serper_payload([
            {"title": "Careers at Acme", "link": "https://acme.wd3.myworkdayjobs.com/jobs",
             "snippet": "Apply now."},
            {"title": "Acme About", "link": "https://acme.com/about",
             "snippet": "Global company."},
        ])
        with patch("lead_legacy_enrichment.call_serper_for_enrichment", return_value=payload), \
             patch("lead_legacy_enrichment._anthropic_lib", lib):
            run_legacy_enrichment(**_BASE_KWARGS)

        prompt_text = captured["messages"][0]["content"]
        assert "myworkdayjobs.com" not in prompt_text
        assert "acme.com/about" in prompt_text


# ---------------------------------------------------------------------------
# Failure paths — never raise, error field records why
# ---------------------------------------------------------------------------

class TestFailureFallback:
    def test_no_api_key_never_attempts_call(self):
        kwargs = dict(_BASE_KWARGS)
        kwargs["anthropic_api_key"] = ""
        result = run_legacy_enrichment(**kwargs)
        assert result.call_attempted is False
        assert result.call_success is False
        assert result.error == "no_anthropic_api_key"

    def test_client_exception_yields_error_not_raise(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), \
             patch("lead_legacy_enrichment._anthropic_lib", lib):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        assert result.call_attempted is True
        assert result.call_success is False
        assert result.error.startswith("legacy_enrichment_call_failed")
        assert "connection reset" in result.error
        assert result.icp_lead_score == ""
        assert result.legacy_score == 0.0

    def test_unparseable_response_yields_error(self):
        with patch("lead_legacy_enrichment.call_serper_for_enrichment",
                   return_value=_serper_payload()), \
             _mock_anthropic("I cannot help with that."):
            result = run_legacy_enrichment(**_BASE_KWARGS)
        assert result.call_success is False
        assert result.error == "legacy_enrichment_parse_failed"

    def test_no_company_name_or_domain_yields_no_queries_error(self):
        kwargs = dict(_BASE_KWARGS)
        kwargs["company_name"] = ""
        kwargs["domain"] = None
        result = run_legacy_enrichment(**kwargs)
        assert result.call_attempted is False
        assert result.error == "no_queries"
