"""Mocked tests for Lead Prioritizer v2 AI-first HQ strategy.

Tests four representative cases: Thales (foreign, High), Amplifon (domestic),
Burger King Italy (foreign, High), and an unclear/empty result.

All external calls (Serper + Anthropic) are mocked so no network is required.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from lead_output_schema import LeadEvidence, LeadInput, LeadPrioritizationResult
from lead_prioritizer_core import prioritize_single_lead


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_serper(payload: dict):
    """Return a context manager that patches call_serper_for_hq to return payload."""
    return patch(
        "lead_prioritizer_core.call_serper_for_hq",
        return_value=payload,
    )


def _ai_json(**kwargs) -> str:
    defaults = dict(
        classification="unclear",
        confidence="Low",
        parent_company="",
        parent_hq_country="",
        parent_hq_city="",
        evidence_url="",
        evidence_quote="",
        reason="",
    )
    defaults.update(kwargs)
    return json.dumps(defaults)


def _mock_anthropic(ai_json_str: str):
    """Patch the module-level _anthropic_lib.Anthropic in lead_hq_ai_interpreter."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=ai_json_str)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    mock_lib = MagicMock()
    mock_lib.Anthropic.return_value = mock_client
    return patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib)


# Minimal Serper payload (organic only) — content doesn't affect AI path
_EMPTY_SERPER: dict = {"organic": []}


# ---------------------------------------------------------------------------
# Test 1: Thales — French defense company, HQ in Paris → foreign_parent, High → score 3
# ---------------------------------------------------------------------------

class TestThales:
    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com", input_country="Italy")

    _ai = _ai_json(
        classification="foreign_parent",
        confidence="High",
        parent_company="Thales Group",
        parent_hq_country="France",
        parent_hq_city="Paris",
        evidence_url="https://www.thalesgroup.com/en/group/overview",
        evidence_quote="Thales is a French multinational company headquartered in Paris, France.",
        reason="Thales Group is headquartered in Paris, France; Italy entity is a subsidiary.",
    )

    def test_score_is_3(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0

    def test_foreign_hq_true(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.foreign_hq_simple is True

    def test_no_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.needs_manual_review is False

    def test_hq_country_is_france(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.hq_detected_country == "France"
        assert result.ai_parent_hq_country == "France"

    def test_ai_call_success(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_call_success == "Yes"
        assert result.ai_call_attempted == "Yes"


# ---------------------------------------------------------------------------
# Test 2: Amplifon — Italian hearing aids company, domestic → score 0
# ---------------------------------------------------------------------------

class TestAmplifon:
    _lead = LeadInput(company_name="Amplifon", domain="amplifon.com", input_country="Italy")

    _ai = _ai_json(
        classification="domestic",
        confidence="High",
        parent_company="Amplifon S.p.A.",
        parent_hq_country="Italy",
        parent_hq_city="Milan",
        evidence_url="https://www.amplifon.com/about-us",
        evidence_quote="Amplifon is headquartered in Milan, Italy.",
        reason="Amplifon Group headquarters is in Milan, Italy, same as input country.",
    )

    def test_score_is_0(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.sig_foreign_hq_score_for_next_scoring == 0.0

    def test_foreign_hq_false(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.foreign_hq_simple is False

    def test_no_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.needs_manual_review is False

    def test_hq_structure_domestic(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.hq_structure_type == "domestic"


# ---------------------------------------------------------------------------
# Test 3: Burger King Italy — US parent → foreign_parent, High → score 3
# ---------------------------------------------------------------------------

class TestBurgerKingItaly:
    _lead = LeadInput(
        company_name="Burger King Italia",
        domain="burgerking.it",
        input_country="Italy",
    )

    _ai = _ai_json(
        classification="foreign_parent",
        confidence="High",
        parent_company="Restaurant Brands International",
        parent_hq_country="United States",
        parent_hq_city="Miami",
        evidence_url="https://www.rbi.com/about",
        evidence_quote="Burger King is a subsidiary of Restaurant Brands International, headquartered in Miami, US.",
        reason="Ultimate parent RBI is headquartered in the United States, not Italy.",
    )

    def test_score_is_3(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0

    def test_foreign_hq_true(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.foreign_hq_simple is True

    def test_hq_country_united_states(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.hq_detected_country == "United States"

    def test_no_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.needs_manual_review is False


# ---------------------------------------------------------------------------
# Test 4: Unclear / empty AI response → needs_manual_review, score None
# ---------------------------------------------------------------------------

class TestUnclearResult:
    _lead = LeadInput(company_name="SomeObscureFirm", domain="someobscurefirm.it", input_country="Italy")

    _ai = _ai_json(
        classification="unclear",
        confidence="Low",
        parent_company="",
        parent_hq_country="",
        parent_hq_city="",
        evidence_url="",
        evidence_quote="",
        reason="No reliable evidence found in search results.",
    )

    def test_needs_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.needs_manual_review is True

    def test_score_is_none(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.sig_foreign_hq_score_for_next_scoring is None

    def test_ai_call_attempted(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_call_attempted == "Yes"

    def test_ai_call_success_no(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_call_success == "No"


# ---------------------------------------------------------------------------
# Shimano — hosted careers-platform input domain (Step 1 upstream fix).
#
# Before Step 1, build_simple_hq_query("Shimano Europe Group",
# "shimano.wd3.myworkdayjobs.com") returned ("myworkdayjobs",
# "myworkdayjobs headquarters"), so the query sent to the AI interpreter was
# about Workday, not Shimano — the AI would then (correctly, given the bad
# query) report Workday/United States as the "foreign parent HQ". The
# assertions below pin the query actually built and sent, plus the new
# domain_is_hosted_platform audit flag.
# ---------------------------------------------------------------------------

class TestShimanoHostedPlatformDomain:
    _lead = LeadInput(
        company_name="Shimano Europe Group",
        domain="shimano.wd3.myworkdayjobs.com",
        input_country="Netherlands",
    )

    _ai = _ai_json(
        classification="foreign_parent",
        confidence="High",
        parent_company="Shimano Inc.",
        parent_hq_country="Japan",
        parent_hq_city="Sakai",
        evidence_url="https://www.shimano.com/en/corporate/",
        evidence_quote="Shimano Inc. is headquartered in Sakai, Osaka, Japan.",
        reason="Shimano Inc. is the ultimate parent, headquartered in Japan.",
    )

    def test_query_uses_tenant_not_platform_label(self):
        with patch(
            "lead_prioritizer_core.call_serper_for_hq",
        ) as mock_serper, _mock_anthropic(self._ai):
            mock_serper.return_value = _EMPTY_SERPER
            prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        _, call_kwargs = mock_serper.call_args
        assert call_kwargs["domain_root"] == "shimano"
        assert call_kwargs["query"] == "shimano headquarters"
        assert "myworkdayjobs" not in call_kwargs["query"]

    def test_domain_is_hosted_platform_flag_set(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.domain_is_hosted_platform is True
        # Original domain must never be overwritten.
        assert result.domain == "shimano.wd3.myworkdayjobs.com"

    def test_hq_result_reflects_shimano_not_workday(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_parent_company == "Shimano Inc."
        assert result.hq_detected_country == "Japan"


class TestNonHostedPlatformDomainFlag:
    def test_domain_is_hosted_platform_false_for_normal_domain(self):
        lead = LeadInput(company_name="IBM", domain="ibm.com", input_country="Italy")
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()):
            result = prioritize_single_lead(
                lead, serper_api_key="fake-serper", anthropic_api_key="fake-anthropic",
            )
        assert result.domain_is_hosted_platform is False


# ---------------------------------------------------------------------------
# Experimental OpenAI provider (opt-in; default Anthropic behavior unchanged)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from lead_hq_ai_interpreter import estimate_ai_cost_usd, interpret_hq_with_ai


def _mock_openai(ai_json_str: str, prompt_tokens=800, completion_tokens=120):
    """Patch the module-level _openai_lib.OpenAI in lead_hq_ai_interpreter."""
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=ai_json_str))]
    mock_resp.usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp
    mock_lib = MagicMock()
    mock_lib.OpenAI.return_value = mock_client
    return patch("lead_hq_ai_interpreter._openai_lib", mock_lib)


class TestOpenAIProvider:
    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com",
                      input_country="Italy")

    _ai = _ai_json(
        classification="foreign_parent",
        confidence="High",
        parent_company="Thales Group",
        parent_hq_country="France",
        parent_hq_city="Paris",
        evidence_url="https://www.thalesgroup.com/en/group/overview",
        evidence_quote="Thales is a French multinational headquartered in Paris.",
        reason="Ultimate parent HQ is in France.",
    )

    def _run_openai(self, **kwargs):
        base = dict(
            serper_api_key="fake-serper",
            anthropic_api_key="",
            openai_api_key="fake-openai",
            ai_provider="openai",
            ai_model="gpt-5.4-nano",
        )
        base.update(kwargs)
        with _mock_serper(_EMPTY_SERPER), _mock_openai(self._ai):
            return prioritize_single_lead(self._lead, **base)

    def test_same_post_ai_scoring_as_anthropic_path(self):
        result = self._run_openai()
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0
        assert result.foreign_hq_simple is True
        assert result.hq_structure_type == "foreign_parent"
        assert result.ai_hq_classification == "foreign_parent"
        assert result.ai_parent_hq_country == "France"
        assert result.needs_manual_review is False

    def test_provider_and_model_recorded(self):
        result = self._run_openai()
        assert result.ai_hq_provider == "openai"
        assert result.ai_hq_model == "gpt-5.4-nano"

    def test_usage_tokens_recorded(self):
        result = self._run_openai()
        assert result.ai_hq_input_tokens == 800
        assert result.ai_hq_output_tokens == 120
        assert result.ai_hq_total_tokens == 920

    def test_cost_computed_for_priced_openai_model(self):
        # gpt-5.4-nano pricing: 800/1M*0.20 + 120/1M*1.25 = 0.00031
        result = self._run_openai()
        assert result.ai_hq_estimated_cost_usd == 0.00031

    def test_missing_openai_key_routes_to_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_openai(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",  # anthropic key must NOT be used
                openai_api_key="",
                ai_provider="openai",
            )
        assert result.ai_hq_error == "no_openai_api_key"
        assert result.needs_manual_review is True
        assert result.ai_call_attempted == "No"

    def test_openai_call_failure_is_isolated(self):
        mock_lib = MagicMock()
        mock_lib.OpenAI.side_effect = RuntimeError("boom")
        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._openai_lib", mock_lib):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                openai_api_key="fake-openai",
                ai_provider="openai",
            )
        assert result.ai_hq_error.startswith("openai_call_failed:")
        assert result.needs_manual_review is True

    def test_unknown_provider_is_rejected_safely(self):
        with _mock_serper(_EMPTY_SERPER):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
                ai_provider="gemini",
            )
        assert result.ai_hq_error.startswith("unknown_ai_provider:")
        assert result.needs_manual_review is True

    def test_default_provider_stays_anthropic(self):
        # No ai_provider argument → the Anthropic path, unchanged.
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_hq_provider == "anthropic"
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0

    def test_openai_uses_same_system_prompt_and_user_message(self):
        captured = {}

        def _create(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock(message=MagicMock(content=self._ai))]
            resp.usage = None
            return resp

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _create
        mock_lib = MagicMock()
        mock_lib.OpenAI.return_value = mock_client

        from lead_hq_ai_interpreter import _SYSTEM_PROMPT

        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._openai_lib", mock_lib):
            prioritize_single_lead(
                self._lead, serper_api_key="fake-serper",
                openai_api_key="fake-openai", ai_provider="openai",
                ai_model="gpt-5.4-nano",
            )
        messages = captured["messages"]
        assert messages[0] == {"role": "system", "content": _SYSTEM_PROMPT}
        assert "thalesgroup" in messages[1]["content"]
        assert captured["model"] == "gpt-5.4-nano"

    def test_usage_absent_leaves_token_fields_blank(self):
        def _create(**kwargs):
            resp = MagicMock()
            resp.choices = [MagicMock(message=MagicMock(content=self._ai))]
            resp.usage = None
            return resp

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _create
        mock_lib = MagicMock()
        mock_lib.OpenAI.return_value = mock_client
        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._openai_lib", mock_lib):
            result = prioritize_single_lead(
                self._lead, serper_api_key="fake-serper",
                openai_api_key="fake-openai", ai_provider="openai",
            )
        assert result.ai_hq_input_tokens is None
        assert result.ai_hq_output_tokens is None
        assert result.ai_hq_total_tokens is None
        assert result.ai_hq_estimated_cost_usd is None


def _mock_deepseek(ai_json_str: str, prompt_tokens=800, completion_tokens=120):
    """Patch the module-level _openai_lib.OpenAI in lead_hq_ai_interpreter —
    DeepSeek reuses the same openai client package, pointed at a different
    base_url (see _call_deepseek_hq)."""
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=ai_json_str))]
    mock_resp.usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp
    mock_lib = MagicMock()
    mock_lib.OpenAI.return_value = mock_client
    return patch("lead_hq_ai_interpreter._openai_lib", mock_lib)


class TestDeepSeekProvider:
    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com",
                      input_country="Italy")

    _ai = _ai_json(
        classification="foreign_parent",
        confidence="High",
        parent_company="Thales Group",
        parent_hq_country="France",
        parent_hq_city="Paris",
        evidence_url="https://www.thalesgroup.com/en/group/overview",
        evidence_quote="Thales is a French multinational headquartered in Paris.",
        reason="Ultimate parent HQ is in France.",
    )

    def _run_deepseek(self, **kwargs):
        base = dict(
            serper_api_key="fake-serper",
            anthropic_api_key="",
            deepseek_api_key="fake-deepseek",
            ai_provider="deepseek",
            ai_model="deepseek-v4-flash",
        )
        base.update(kwargs)
        with _mock_serper(_EMPTY_SERPER), _mock_deepseek(self._ai):
            return prioritize_single_lead(self._lead, **base)

    def test_same_post_ai_scoring_as_anthropic_path(self):
        result = self._run_deepseek()
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0
        assert result.foreign_hq_simple is True
        assert result.hq_structure_type == "foreign_parent"
        assert result.ai_hq_classification == "foreign_parent"
        assert result.ai_parent_hq_country == "France"
        assert result.needs_manual_review is False

    def test_provider_and_model_recorded(self):
        result = self._run_deepseek()
        assert result.ai_hq_provider == "deepseek"
        assert result.ai_hq_model == "deepseek-v4-flash"

    def test_usage_tokens_recorded(self):
        result = self._run_deepseek()
        assert result.ai_hq_input_tokens == 800
        assert result.ai_hq_output_tokens == 120
        assert result.ai_hq_total_tokens == 920

    def test_cost_computed_for_deepseek_flash(self):
        # deepseek-v4-flash: 800/1M*0.14 + 120/1M*0.28 = 0.000146
        result = self._run_deepseek()
        assert result.ai_hq_estimated_cost_usd == 0.000146

    def test_missing_deepseek_key_routes_to_manual_review(self):
        with _mock_serper(_EMPTY_SERPER), _mock_deepseek(self._ai):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",  # anthropic key must NOT be used
                deepseek_api_key="",
                ai_provider="deepseek",
            )
        assert result.ai_hq_error == "no_deepseek_api_key"
        assert result.needs_manual_review is True
        assert result.ai_call_attempted == "No"

    def test_deepseek_call_failure_is_isolated(self):
        mock_lib = MagicMock()
        mock_lib.OpenAI.side_effect = RuntimeError("boom")
        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._openai_lib", mock_lib):
            result = prioritize_single_lead(
                self._lead,
                serper_api_key="fake-serper",
                deepseek_api_key="fake-deepseek",
                ai_provider="deepseek",
            )
        assert result.ai_hq_error.startswith("deepseek_call_failed:")
        assert result.needs_manual_review is True

    def test_uses_openai_compatible_client_with_deepseek_base_url(self):
        from lead_hq_ai_interpreter import DEEPSEEK_BASE_URL

        with _mock_serper(_EMPTY_SERPER), _mock_deepseek(self._ai) as mock_lib:
            prioritize_single_lead(
                self._lead, serper_api_key="fake-serper",
                deepseek_api_key="fake-deepseek", ai_provider="deepseek",
                ai_model="deepseek-v4-flash",
            )
            mock_lib.OpenAI.assert_called_once_with(
                api_key="fake-deepseek", base_url=DEEPSEEK_BASE_URL)

    def test_deepseek_uses_same_system_prompt_and_user_message(self):
        captured = {}

        def _create(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock(message=MagicMock(content=self._ai))]
            resp.usage = None
            return resp

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _create
        mock_lib = MagicMock()
        mock_lib.OpenAI.return_value = mock_client

        from lead_hq_ai_interpreter import _SYSTEM_PROMPT

        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._openai_lib", mock_lib):
            prioritize_single_lead(
                self._lead, serper_api_key="fake-serper",
                deepseek_api_key="fake-deepseek", ai_provider="deepseek",
                ai_model="deepseek-v4-flash",
            )
        messages = captured["messages"]
        assert messages[0] == {"role": "system", "content": _SYSTEM_PROMPT}
        assert "thalesgroup" in messages[1]["content"]
        assert captured["model"] == "deepseek-v4-flash"


class TestAnthropicUsageAndCost:
    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com",
                      input_country="Italy")

    def test_anthropic_usage_and_cost_recorded(self):
        ai = _ai_json(classification="domestic", confidence="High",
                      parent_hq_country="Italy")
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=ai)]
        mock_msg.usage = SimpleNamespace(input_tokens=1000, output_tokens=100)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg
        mock_lib = MagicMock()
        mock_lib.Anthropic.return_value = mock_client

        with _mock_serper(_EMPTY_SERPER), \
             patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib):
            result = prioritize_single_lead(
                self._lead, serper_api_key="fake-serper",
                anthropic_api_key="fake-anthropic",
            )
        assert result.ai_hq_provider == "anthropic"
        assert result.ai_hq_input_tokens == 1000
        assert result.ai_hq_output_tokens == 100
        assert result.ai_hq_total_tokens == 1100
        # claude-haiku-4-5 pricing is known: 1000/1M*1.00 + 100/1M*5.00
        assert result.ai_hq_estimated_cost_usd == 0.0015
        # Backwards compatible: a usage object without cache attributes at all
        # (older SDK / no caching) must never crash and must leave the new
        # cache fields blank rather than guessed.
        assert result.ai_hq_cache_creation_tokens is None
        assert result.ai_hq_cache_read_tokens is None
        # With no cache activity, the cache-aware estimate must equal the
        # plain per-token estimate.
        assert result.ai_hq_estimated_cost_usd_with_cache == result.ai_hq_estimated_cost_usd


class TestAnthropicPromptCaching:
    """Prompt-caching wiring in _call_anthropic_hq: the cache_control-marked
    system block, and cache_creation/cache_read usage propagation through to
    HQDetectionResult."""

    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com",
                      input_country="Italy")

    def _interpret(self, ai_json_str, usage):
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=ai_json_str)]
        mock_msg.usage = usage
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg
        mock_lib = MagicMock()
        mock_lib.Anthropic.return_value = mock_client
        with patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib):
            result = interpret_hq_with_ai(
                lead_input=self._lead, domain_root="thalesgroup",
                query="thalesgroup headquarters", serper_payload=_EMPTY_SERPER,
                anthropic_api_key="fake-anthropic",
            )
        return result, mock_client

    def test_system_sent_as_single_cache_control_block(self):
        from lead_hq_ai_interpreter import (
            _SYSTEM_PROMPT, _USER_TEMPLATE_STATIC_INSTRUCTIONS,
        )
        ai = _ai_json(classification="domestic", confidence="High",
                      parent_hq_country="Italy")
        _, client = self._interpret(
            ai, SimpleNamespace(input_tokens=200, output_tokens=100))
        _, kwargs = client.messages.create.call_args
        system_blocks = kwargs["system"]
        assert isinstance(system_blocks, list)
        assert len(system_blocks) == 1
        block = system_blocks[0]
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}
        assert block["text"] == _SYSTEM_PROMPT + "\n\n" + _USER_TEMPLATE_STATIC_INSTRUCTIONS
        # The per-lead message must NOT be duplicated into the cached block.
        assert "thalesgroup" not in block["text"]
        # And the user message must stay per-lead-only (no static instructions).
        assert '"classification": one of' not in kwargs["messages"][0]["content"]

    def test_static_instructions_are_stable_across_calls(self):
        # A second, independent import-level read must be byte-for-byte equal
        # -- this is what makes Anthropic's prompt cache able to hit at all.
        import lead_hq_ai_interpreter as m
        first = m._SYSTEM_PROMPT + "\n\n" + m._USER_TEMPLATE_STATIC_INSTRUCTIONS
        second = m._SYSTEM_PROMPT + "\n\n" + m._USER_TEMPLATE_STATIC_INSTRUCTIONS
        assert first == second

    def test_cache_boundary_hash_is_pinned(self):
        # Hard regression test for the prompt-cache boundary (Lusha
        # enrichment plan, Stap 5): _SYSTEM_PROMPT +
        # _USER_TEMPLATE_STATIC_INSTRUCTIONS is the ONE block sent with
        # cache_control=ephemeral on every HQ call. Pinning its SHA256 (not
        # just comparing it to itself, as the test above does) means any
        # FUTURE accidental edit to either constant -- even a single
        # character, e.g. from a change meant only for the per-lead
        # template -- fails this test immediately instead of silently
        # turning every cheap cache read into an expensive cache write.
        # Recorded before Stap 5's change (which only touches
        # _USER_TEMPLATE_PER_LEAD, never these two) and confirmed
        # unchanged after it.
        import hashlib
        import lead_hq_ai_interpreter as m
        combined = m._SYSTEM_PROMPT + "\n\n" + m._USER_TEMPLATE_STATIC_INSTRUCTIONS
        assert len(combined) == 3361
        assert hashlib.sha256(combined.encode("utf-8")).hexdigest() == (
            "0f85a295e5abaea39235204b5fb79b3ac65c06f6b90a62a6224d3fc2355665c4"
        )

    def test_cache_creation_and_read_tokens_propagated(self):
        ai = _ai_json(classification="domestic", confidence="High",
                      parent_hq_country="Italy")
        usage = SimpleNamespace(
            input_tokens=200, output_tokens=100,
            cache_creation_input_tokens=1500, cache_read_input_tokens=0,
        )
        result, _ = self._interpret(ai, usage)
        assert result.ai_hq_input_tokens == 200
        assert result.ai_hq_output_tokens == 100
        assert result.ai_hq_cache_creation_tokens == 1500
        assert result.ai_hq_cache_read_tokens == 0
        # 200*1.00 + 100*5.00 + 1500*1.00*1.25, all /1e6
        assert result.ai_hq_estimated_cost_usd_with_cache == 0.002575
        # Plain (non-cache-aware) estimate is unaffected by cache fields.
        assert result.ai_hq_estimated_cost_usd == 0.0007

    def test_cache_read_hit_on_subsequent_call(self):
        ai = _ai_json(classification="domestic", confidence="High",
                      parent_hq_country="Italy")
        usage = SimpleNamespace(
            input_tokens=200, output_tokens=100,
            cache_creation_input_tokens=0, cache_read_input_tokens=1500,
        )
        result, _ = self._interpret(ai, usage)
        assert result.ai_hq_cache_creation_tokens == 0
        assert result.ai_hq_cache_read_tokens == 1500
        # 200*1.00 + 100*5.00 + 1500*1.00*0.1, all /1e6
        assert result.ai_hq_estimated_cost_usd_with_cache == 0.00085

    def test_usage_without_cache_attributes_at_all_is_backwards_compatible(self):
        # A plain SimpleNamespace with only input/output tokens (as an older
        # SDK response, or any response where caching was never used, would
        # look) must not crash and must leave the cache fields blank.
        ai = _ai_json(classification="domestic", confidence="High",
                      parent_hq_country="Italy")
        usage = SimpleNamespace(input_tokens=200, output_tokens=100)
        result, _ = self._interpret(ai, usage)
        assert result.ai_hq_input_tokens == 200
        assert result.ai_hq_output_tokens == 100
        assert result.ai_hq_cache_creation_tokens is None
        assert result.ai_hq_cache_read_tokens is None
        assert result.ai_hq_estimated_cost_usd_with_cache == result.ai_hq_estimated_cost_usd


class TestEstimateAiCostWithCache:
    """Manual worked examples for estimate_ai_cost_usd_with_cache — an
    instruction block of ~1500 tokens, cache write vs. cache read vs. no
    caching, against claude-haiku-4-5-20251001 pricing (1.00, 5.00 USD/MTOK)."""

    def test_no_cache_activity_matches_plain_estimate(self):
        from lead_hq_ai_interpreter import estimate_ai_cost_usd_with_cache
        assert estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", 1000, 100) == 0.0015
        assert estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", 1000, 100,
            cache_creation_input_tokens=None, cache_read_input_tokens=None,
        ) == estimate_ai_cost_usd("claude-haiku-4-5-20251001", 1000, 100)

    def test_cache_write_billed_at_1_25x_input_price(self):
        from lead_hq_ai_interpreter import estimate_ai_cost_usd_with_cache
        # First call for a batch: 1500-token instruction block written to
        # cache, 200 per-lead input tokens, 100 output tokens.
        # 200*1.00 + 100*5.00 + 1500*1.00*1.25, all /1e6 = 0.002575
        cost = estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", 200, 100,
            cache_creation_input_tokens=1500, cache_read_input_tokens=0,
        )
        assert cost == 0.002575

    def test_cache_read_billed_at_0_1x_input_price(self):
        from lead_hq_ai_interpreter import estimate_ai_cost_usd_with_cache
        # Every subsequent lead in the batch: same 1500-token instruction
        # block, now served from cache instead of rewritten.
        # 200*1.00 + 100*5.00 + 1500*1.00*0.1, all /1e6 = 0.00085
        cost = estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", 200, 100,
            cache_creation_input_tokens=0, cache_read_input_tokens=1500,
        )
        assert cost == 0.00085
        # A cache read is far cheaper than paying full input price for the
        # same 1500 tokens every time.
        full_price_equivalent = estimate_ai_cost_usd(
            "claude-haiku-4-5-20251001", 200 + 1500, 100)
        assert cost < full_price_equivalent

    def test_unknown_model_returns_none(self):
        from lead_hq_ai_interpreter import estimate_ai_cost_usd_with_cache
        assert estimate_ai_cost_usd_with_cache(
            "gpt-9-unreleased", 1000, 100, cache_creation_input_tokens=100,
        ) is None

    def test_missing_base_tokens_return_none(self):
        from lead_hq_ai_interpreter import estimate_ai_cost_usd_with_cache
        assert estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", None, 100,
            cache_creation_input_tokens=1500,
        ) is None
        assert estimate_ai_cost_usd_with_cache(
            "claude-haiku-4-5-20251001", 1000, None,
            cache_read_input_tokens=1500,
        ) is None


class TestEstimateAiCost:
    def test_known_model(self):
        assert estimate_ai_cost_usd("claude-haiku-4-5-20251001", 1000, 100) == 0.0015

    def test_unknown_model_returns_none(self):
        assert estimate_ai_cost_usd("gpt-9-unreleased", 1000, 100) is None

    def test_openai_nano_and_mini_pricing(self):
        # gpt-5.4-nano: 1000/1M*0.20 + 100/1M*1.25 = 0.000325
        assert estimate_ai_cost_usd("gpt-5.4-nano", 1000, 100) == 0.000325
        # gpt-5.4-mini: 1000/1M*0.75 + 100/1M*4.50 = 0.00120
        assert estimate_ai_cost_usd("gpt-5.4-mini", 1000, 100) == 0.0012

    def test_deepseek_flash_and_pro_pricing(self):
        # deepseek-v4-flash: 1000/1M*0.14 + 100/1M*0.28 = 0.000168
        assert estimate_ai_cost_usd("deepseek-v4-flash", 1000, 100) == 0.000168
        # deepseek-v4-pro: 1000/1M*0.435 + 100/1M*0.87 = 0.000522
        assert estimate_ai_cost_usd("deepseek-v4-pro", 1000, 100) == 0.000522

    def test_missing_tokens_return_none(self):
        assert estimate_ai_cost_usd("claude-haiku-4-5-20251001", None, 100) is None
        assert estimate_ai_cost_usd("claude-haiku-4-5-20251001", 1000, None) is None


# ---------------------------------------------------------------------------
# hq_evidence_urls — ALL usable HQ evidence URLs, mechanically validated
# against the Serper payload actually shown to the model (never an
# AI-invented URL); the existing hq_evidence_url is unchanged.
# ---------------------------------------------------------------------------

class TestHqEvidenceUrls:
    _lead = LeadInput(company_name="Acme Italia", domain="acme.com", input_country="Italy")

    _payload = {
        "organic": [
            {"title": "Acme HQ", "snippet": "Acme is headquartered in Munich.",
             "link": "https://acme.com/about"},
            {"title": "Acme on Wikipedia", "snippet": "Acme GmbH is a German company.",
             "link": "https://en.wikipedia.org/wiki/Acme"},
        ],
    }

    def _interpret(self, ai_json_str):
        from lead_hq_ai_interpreter import interpret_hq_with_ai
        with _mock_anthropic(ai_json_str):
            return interpret_hq_with_ai(
                lead_input=self._lead, domain_root="acme", query="acme headquarters",
                serper_payload=self._payload, anthropic_api_key="fake-anthropic",
            )

    def test_valid_urls_from_payload_are_kept_ordered(self):
        ai = _ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        )
        # evidence_urls is not part of _ai_json's defaults -- inject it directly.
        data = json.loads(ai)
        data["evidence_urls"] = ["https://acme.com/about", "https://en.wikipedia.org/wiki/Acme"]
        result = self._interpret(json.dumps(data))
        assert result.hq_evidence_urls == [
            "https://acme.com/about", "https://en.wikipedia.org/wiki/Acme",
        ]

    def test_first_element_equals_existing_singular_field(self):
        data = json.loads(_ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        ))
        data["evidence_urls"] = ["https://en.wikipedia.org/wiki/Acme", "https://acme.com/about"]
        result = self._interpret(json.dumps(data))
        assert result.hq_evidence_url == "https://acme.com/about"
        assert result.hq_evidence_urls[0] == result.hq_evidence_url

    def test_invented_url_not_in_payload_is_dropped(self):
        data = json.loads(_ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        ))
        data["evidence_urls"] = [
            "https://acme.com/about", "https://invented-by-model.com/fake",
        ]
        result = self._interpret(json.dumps(data))
        assert result.hq_evidence_urls == ["https://acme.com/about"]
        assert "invented-by-model" not in " ".join(result.hq_evidence_urls)

    def test_duplicate_urls_deduplicated(self):
        data = json.loads(_ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        ))
        data["evidence_urls"] = ["https://acme.com/about", "https://acme.com/about"]
        result = self._interpret(json.dumps(data))
        assert result.hq_evidence_urls == ["https://acme.com/about"]

    def test_missing_evidence_urls_key_falls_back_to_singular(self):
        # No "evidence_urls" key at all in the AI JSON (older-shape response).
        ai = _ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        )
        result = self._interpret(ai)
        assert result.hq_evidence_urls == ["https://acme.com/about"]

    def test_no_evidence_url_at_all_yields_empty_list(self):
        ai = _ai_json(classification="unclear", confidence="Low")
        result = self._interpret(ai)
        assert result.hq_evidence_urls == []

    def test_hq_evidence_urls_never_affects_classification_or_score(self):
        data_without = json.loads(_ai_json(
            classification="foreign_parent", confidence="High",
            parent_company="Acme GmbH", parent_hq_country="Germany",
            evidence_url="https://acme.com/about",
        ))
        data_with = dict(data_without)
        data_with["evidence_urls"] = [
            "https://acme.com/about", "https://en.wikipedia.org/wiki/Acme",
        ]
        r_without = self._interpret(json.dumps(data_without))
        r_with = self._interpret(json.dumps(data_with))
        assert r_without.sig_foreign_hq_score_for_next_scoring == r_with.sig_foreign_hq_score_for_next_scoring
        assert r_without.hq_structure_type == r_with.hq_structure_type
        assert r_without.needs_manual_review == r_with.needs_manual_review


# ---------------------------------------------------------------------------
# Firecrawl own-domain content as the PRIMARY HQ source (crawled_pages).
# ---------------------------------------------------------------------------

def _mock_anthropic_capture(ai_json_str: str):
    """Like _mock_anthropic but also returns the mock client so the caller can
    inspect the user message that was actually sent to the model."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=ai_json_str)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    mock_lib = MagicMock()
    mock_lib.Anthropic.return_value = mock_client
    return patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib), mock_client


def _user_message_sent(mock_client) -> str:
    _, kwargs = mock_client.messages.create.call_args
    return kwargs["messages"][0]["content"]


def _system_text_sent(mock_client) -> str:
    """The cache_control-marked system block's text (see _call_anthropic_hq) —
    ``_SYSTEM_PROMPT`` + ``_USER_TEMPLATE_STATIC_INSTRUCTIONS``, sent once
    per call as ``system=[{"type": "text", "text": ..., "cache_control": ...}]``."""
    _, kwargs = mock_client.messages.create.call_args
    return kwargs["system"][0]["text"]


class TestCrawledOwnDomainPrimarySource:
    _lead = LeadInput(company_name="Fujifilm NL", domain="fujifilmtilburg.nl",
                      input_country="Netherlands")
    _crawled = [{
        "url": "https://www.fujifilm.com/nl",
        "text": "FUJIFILM Holdings Corporation is headquartered in Tokyo, Japan.",
        "source_kind": "own_domain", "retrieval_method": "firecrawl",
    }]
    _serper = {"organic": [
        {"title": "LinkedIn", "snippet": "European HQ in Tilburg.",
         "link": "https://nl.linkedin.com/company/fujifilm-europe"},
    ]}

    def _interpret(self, ai_json_str, crawled_pages):
        mock_ctx, client = _mock_anthropic_capture(ai_json_str)
        with mock_ctx:
            result = interpret_hq_with_ai(
                lead_input=self._lead, domain_root="fujifilmtilburg",
                query="fujifilmtilburg headquarters", serper_payload=self._serper,
                anthropic_api_key="fake", crawled_pages=crawled_pages,
            )
        return result, client

    def test_crawled_text_included_as_primary_in_prompt(self):
        ai = _ai_json(classification="foreign_parent", confidence="High",
                      parent_hq_country="Japan", parent_hq_city="Tokyo")
        _, client = self._interpret(ai, self._crawled)
        msg = _user_message_sent(client)
        assert "PRIMARY SOURCE" in msg
        assert "FUJIFILM Holdings Corporation" in msg
        assert "https://www.fujifilm.com/nl" in msg

    def test_crawled_url_accepted_as_evidence(self):
        data = json.loads(_ai_json(classification="foreign_parent",
                                   confidence="High", parent_hq_country="Japan"))
        data["evidence_url"] = "https://www.fujifilm.com/nl"
        data["evidence_urls"] = ["https://www.fujifilm.com/nl"]
        result, _ = self._interpret(json.dumps(data), self._crawled)
        assert result.hq_evidence_url == "https://www.fujifilm.com/nl"
        assert "https://www.fujifilm.com/nl" in result.hq_evidence_urls

    def test_invented_url_still_rejected_even_with_crawl(self):
        data = json.loads(_ai_json(classification="foreign_parent",
                                   confidence="High", parent_hq_country="Japan"))
        data["evidence_url"] = "https://www.fujifilm.com/nl"
        data["evidence_urls"] = ["https://www.fujifilm.com/nl",
                                 "https://invented.example/fake"]
        result, _ = self._interpret(json.dumps(data), self._crawled)
        assert result.hq_evidence_urls == ["https://www.fujifilm.com/nl"]

    def test_no_crawled_pages_is_backward_compatible(self):
        # Serper-only path (empty crawled_pages) still classifies and the
        # prompt marks the primary source absent.
        ai = _ai_json(classification="foreign_parent", confidence="High",
                      parent_hq_country="Japan", parent_hq_city="Tokyo")
        result, client = self._interpret(ai, [])
        assert result.sig_foreign_hq_score_for_next_scoring == 3.0
        msg = _user_message_sent(client)
        assert "no own-website content was retrieved" in msg


class TestPrioritizeSingleLeadFirecrawlWiring:
    """prioritize_single_lead: Firecrawl own-domain crawl feeds the classifier
    and hq_location_summary is populated on the result."""

    _lead = LeadInput(company_name="Fujifilm NL", domain="fujifilmtilburg.nl",
                      input_country="Netherlands")
    _ai = _ai_json(classification="foreign_parent", confidence="High",
                   parent_company="FUJIFILM Holdings",
                   parent_hq_country="Japan", parent_hq_city="Tokyo",
                   evidence_url="https://www.fujifilm.com/nl")

    def _run(self, firecrawl_key, fc_return):
        crawl_patch = patch(
            "lead_prioritizer_core.collect_own_domain_hq_pages",
            return_value=fc_return)
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai), crawl_patch as m:
            result = prioritize_single_lead(
                self._lead, serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key=firecrawl_key)
        return result, m

    def test_crawl_invoked_when_key_present(self):
        fc = {"pages": [{"url": "https://www.fujifilm.com/nl", "text": "Tokyo HQ",
                         "source_kind": "own_domain", "retrieval_method": "firecrawl"}],
              "pages_crawled": [], "used": True}
        result, m = self._run("fc-key", fc)
        m.assert_called_once()
        assert result.hq_location_summary == "Parent company headquarters: Tokyo, Japan"

    def test_no_crawl_when_key_absent(self):
        fc = {"pages": [], "pages_crawled": [], "used": False}
        result, m = self._run("", fc)
        m.assert_not_called()
        # Still classifies from Serper-only path; summary still derived from AI fields.
        assert result.hq_location_summary == "Parent company headquarters: Tokyo, Japan"

    def test_hosted_platform_domain_is_not_crawled(self):
        hosted_lead = LeadInput(
            company_name="X", domain="jobs.lever.co", input_country="Netherlands")
        crawl_patch = patch(
            "lead_prioritizer_core.collect_own_domain_hq_pages",
            return_value={"pages": [], "pages_crawled": [], "used": False})
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai), crawl_patch as m:
            prioritize_single_lead(
                hosted_lead, serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key="fc-key")
        m.assert_not_called()


# ---------------------------------------------------------------------------
# Industry/sector derived by the HQ interpreter from the same material
# (primarily own-domain crawled content) — a free side product of the HQ
# call. See lead_prioritizer_core.py for the sector-detection fallback that
# consumes ai_hq_industry/ai_hq_sub_industry.
# ---------------------------------------------------------------------------

class TestHqDerivedIndustry:
    _lead = LeadInput(company_name="AEG Power Solutions", domain="aegps.com",
                      input_country="Germany")
    _crawled = [{
        "url": "https://www.aegps.com/about",
        "text": "AEG Power Solutions designs and manufactures power electronics "
                "and UPS systems for industrial and utility customers.",
        "source_kind": "own_domain", "retrieval_method": "firecrawl",
    }]

    def _interpret(self, ai_json_str, crawled_pages=None):
        mock_ctx, client = _mock_anthropic_capture(ai_json_str)
        with mock_ctx:
            result = interpret_hq_with_ai(
                lead_input=self._lead, domain_root="aegps",
                query="aegps headquarters", serper_payload={"organic": []},
                anthropic_api_key="fake",
                crawled_pages=crawled_pages if crawled_pages is not None else self._crawled,
            )
        return result, client

    def test_industry_and_sub_industry_populated(self):
        ai = _ai_json(classification="domestic", confidence="High",
                      industry="Power electronics", sub_industry="UPS systems")
        result, _ = self._interpret(ai)
        assert result.ai_hq_industry == "Power electronics"
        assert result.ai_hq_sub_industry == "UPS systems"

    def test_blank_industry_becomes_none(self):
        ai = _ai_json(classification="unclear", confidence="Low",
                      industry="", sub_industry="")
        result, _ = self._interpret(ai)
        assert result.ai_hq_industry is None
        assert result.ai_hq_sub_industry is None

    def test_industry_populated_even_when_classification_unclear(self):
        # Industry/sector is independent of the HQ classification succeeding.
        ai = _ai_json(classification="unclear", confidence="Low",
                      industry="Power electronics", sub_industry="")
        result, _ = self._interpret(ai)
        assert result.ai_hq_classification == "unclear"
        assert result.ai_hq_industry == "Power electronics"

    def test_prompt_asks_for_industry_and_known_categories(self):
        # The "industry"/"sub_industry" instructions are static across every
        # lead, so they now live in the cache_control-marked system block
        # (see _call_anthropic_hq / _USER_TEMPLATE_STATIC_INSTRUCTIONS)
        # instead of the per-lead user message.
        ai = _ai_json(classification="domestic", confidence="High",
                      industry="Power electronics")
        _, client = self._interpret(ai)
        system_text = _system_text_sent(client)
        assert '"industry"' in system_text
        assert '"sub_industry"' in system_text
        # Style-guidance vocabulary from the deterministic sector detector.
        assert "Chemicals" in system_text or "Manufacturing" in system_text

    def test_regex_fallback_recovers_industry(self):
        # Malformed JSON (unterminated "reason") -- core fields incl. industry
        # must still be recoverable via the regex fallback.
        from lead_hq_ai_interpreter import _parse_ai_response
        broken = (
            '{"classification": "domestic", "confidence": "High", '
            '"industry": "Power electronics", "sub_industry": "UPS systems", '
            '"reason": "unterminated'
        )
        parsed = _parse_ai_response(broken)
        assert parsed.get("industry") == "Power electronics"
        assert parsed.get("sub_industry") == "UPS systems"


class TestSectorFallbackToOwnDomainAi:
    """lead_prioritizer_core: when the deterministic keyword sector detector
    finds nothing, fall back to the HQ interpreter's AI-derived industry --
    but ONLY when a genuine own-domain crawl backs it."""

    _lead = LeadInput(company_name="AEG Power Solutions", domain="aegps.com",
                      input_country="Germany")
    _ai = _ai_json(classification="domestic", confidence="High",
                   industry="Power electronics", sub_industry="UPS systems")
    _fc_used = {
        "pages": [{"url": "https://www.aegps.com/about", "text": "...",
                  "source_kind": "own_domain", "retrieval_method": "firecrawl"}],
        "pages_crawled": [], "used": True,
    }
    _fc_not_used = {"pages": [], "pages_crawled": [], "used": False}

    def _run(self, fc_return, extra_kwargs=None):
        crawl_patch = patch(
            "lead_prioritizer_core.collect_own_domain_hq_pages",
            return_value=fc_return)
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai), crawl_patch:
            result = prioritize_single_lead(
                self._lead, serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key="fc-key",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
                **(extra_kwargs or {}),
            )
        return result

    def test_fallback_fires_when_keyword_detector_found_nothing(self):
        result = self._run(self._fc_used)
        assert result.detected_industry == "Power electronics"
        assert result.detected_sub_industry == "UPS systems"
        assert result.sector_source == "own_domain_ai"
        assert result.sector_confidence == "Medium"
        assert result.sector_evidence_url == "https://www.aegps.com/about"

    def test_fallback_does_not_fire_without_genuine_own_domain_crawl(self):
        # AI still returns an industry guess (Serper-only), but with no own-
        # domain crawl this must NOT be trusted as a sector source.
        result = self._run(self._fc_not_used)
        assert result.detected_industry is None
        assert result.sector_source is None

    def test_sector_industry_evidence_no_longer_consulted(self):
        # Lusha enrichment plan, Stap 4: the live Serper sector_industry
        # query/evidence tier is gone. Even a clean keyword hit in
        # sector_industry-tagged evidence is no longer consulted -- the
        # own-domain AI fallback (tier 2) wins whenever a genuine crawl
        # happened, exactly like test_fallback_fires_when_keyword_detector_
        # found_nothing above.
        with patch(
            "lead_prioritizer_core.collect_own_domain_hq_pages",
            return_value=self._fc_used,
        ), _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai), patch(
            "lead_prioritizer_core.collect_non_hq_enrichment_evidence",
            return_value=[
                LeadEvidence(
                    signal_name="sector_industry",
                    source_url="https://www.aegps.com/products",
                    source_title="AEG Power Solutions",
                    source_snippet="Supplier of industrial equipment and "
                                   "machinery for utility customers.",
                    source_type="organic",
                ),
            ],
        ):
            result = prioritize_single_lead(
                self._lead, serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key="fc-key",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.sector_source == "own_domain_ai"
        assert result.detected_industry == "Power electronics"

    def test_own_domain_ai_wins_over_lusha_text_fallback(self):
        # Tier 2 (own-domain Firecrawl+AI) must never be overwritten by the
        # lower-priority tier 3 (Lusha Description/Specialties keyword
        # match), even when both would independently produce a hit.
        lead = LeadInput(
            company_name="AEG Power Solutions", domain="aegps.com",
            input_country="Germany",
            lusha_description="A retail company with stores nationwide.",
        )
        with patch(
            "lead_prioritizer_core.collect_own_domain_hq_pages",
            return_value=self._fc_used,
        ), _mock_serper(_EMPTY_SERPER), _mock_anthropic(self._ai):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key="fc-key",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.sector_source == "own_domain_ai"
        assert result.detected_industry == "Power electronics"


# ---------------------------------------------------------------------------
# call_serper_for_hq: shared GCS enrichment cache (opt-in cache_index param).
# ---------------------------------------------------------------------------

class TestCallSerperForHqCache:
    def _mock_urlopen(self, payload: dict):
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        return patch("urllib.request.urlopen", return_value=response)

    def test_default_none_cache_index_always_calls_serper_live(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        with self._mock_urlopen({"organic": [{"title": "x"}]}) as mock_urlopen:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
            )
        mock_urlopen.assert_called_once()
        assert result == {"organic": [{"title": "x"}]}

    def test_cache_hit_skips_network_call(self):
        from lead_hq_ai_interpreter import call_serper_for_hq
        import enrichment_cache

        index: dict = {}
        enrichment_cache.put_cached(
            index, "serper", "acme.com", "hq", response={"organic": ["cached"]})

        with self._mock_urlopen({"organic": ["live"]}) as mock_urlopen:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                cache_index=index,
            )
        mock_urlopen.assert_not_called()
        assert result == {"organic": ["cached"]}

    def test_cache_miss_calls_live_and_populates_cache(self):
        from lead_hq_ai_interpreter import call_serper_for_hq
        import enrichment_cache

        index: dict = {}
        with self._mock_urlopen({"organic": ["live"]}) as mock_urlopen:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                cache_index=index,
            )
        mock_urlopen.assert_called_once()
        assert result == {"organic": ["live"]}
        cached = enrichment_cache.get_cached(
            index, "serper", "acme.com", "hq", ttl_days=120)
        assert cached == {"organic": ["live"]}

    def test_force_refresh_ignores_fresh_cache_entry(self):
        from lead_hq_ai_interpreter import call_serper_for_hq
        import enrichment_cache

        index: dict = {}
        enrichment_cache.put_cached(
            index, "serper", "acme.com", "hq", response={"organic": ["cached"]})

        with self._mock_urlopen({"organic": ["live"]}) as mock_urlopen:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                cache_index=index, force_refresh=True,
            )
        mock_urlopen.assert_called_once()
        assert result == {"organic": ["live"]}


# ---------------------------------------------------------------------------
# call_serper_for_hq: 429 retry/backoff (expected under concurrent load).
# ---------------------------------------------------------------------------

def _http_error_429(retry_after: str | None = None):
    headers = MagicMock()
    headers.get.return_value = retry_after
    return urllib.error.HTTPError("url", 429, "rate limited", headers, None)


class TestCallSerperForHq429Retry:
    def test_retries_then_succeeds(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        response = MagicMock()
        response.read.return_value = json.dumps({"organic": ["live"]}).encode()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[_http_error_429(), response]) as mock_urlopen, \
                patch("lead_hq_ai_interpreter.time.sleep") as mock_sleep:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
            )
        assert result == {"organic": ["live"]}
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()

    def test_exhausts_retries_then_returns_empty(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        with patch("urllib.request.urlopen", side_effect=_http_error_429()) as mock_urlopen, \
                patch("lead_hq_ai_interpreter.time.sleep"):
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                max_429_retries=2,
            )
        assert result == {}
        assert mock_urlopen.call_count == 3  # initial attempt + 2 retries

    def test_non_429_http_error_is_not_retried(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        headers = MagicMock()
        error = urllib.error.HTTPError("url", 500, "server error", headers, None)
        with patch("urllib.request.urlopen", side_effect=error) as mock_urlopen, \
                patch("lead_hq_ai_interpreter.time.sleep") as mock_sleep:
            result = call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
            )
        assert result == {}
        mock_urlopen.assert_called_once()
        mock_sleep.assert_not_called()

    def test_honors_retry_after_header(self):
        from lead_hq_ai_interpreter import call_serper_for_hq
        import api_retry

        response = MagicMock()
        response.read.return_value = json.dumps({"organic": ["live"]}).encode()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[_http_error_429("2.5"), response]), \
                patch("lead_hq_ai_interpreter.time.sleep") as mock_sleep:
            call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
            )
        mock_sleep.assert_called_once_with(2.5)


# ---------------------------------------------------------------------------
# call_serper_for_hq: gl/hl localization (Lusha enrichment plan, Stap 1).
# ---------------------------------------------------------------------------

class TestCallSerperForHqGlHl:
    def _capture_body(self, payload: dict = None):
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload or {}).encode()

        def _fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse()

        return captured, patch("urllib.request.urlopen", side_effect=_fake_urlopen)

    def test_gl_and_hl_included_when_given(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        captured, patcher = self._capture_body()
        with patcher:
            call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                gl="it", hl="it",
            )
        assert captured["body"] == {"q": "acme headquarters", "num": 10, "gl": "it", "hl": "it"}

    def test_gl_without_hl(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        captured, patcher = self._capture_body()
        with patcher:
            call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
                gl="ch", hl=None,
            )
        assert captured["body"] == {"q": "acme headquarters", "num": 10, "gl": "ch"}
        assert "hl" not in captured["body"]

    def test_omitted_gl_hl_matches_pre_existing_request_shape(self):
        from lead_hq_ai_interpreter import call_serper_for_hq

        captured, patcher = self._capture_body()
        with patcher:
            call_serper_for_hq(
                domain_root="acme.com", query="acme headquarters", serper_api_key="k",
            )
        assert captured["body"] == {"q": "acme headquarters", "num": 10}


# ---------------------------------------------------------------------------
# End-to-end: prioritize_single_lead resolves gl/hl and passes a country to
# the own-domain Firecrawl crawl from the lead's effective country.
# ---------------------------------------------------------------------------

class TestPrioritizeSingleLeadCountryLocalization:
    def test_serper_hq_call_receives_gl_hl_for_known_country(self):
        seen = {}

        def _fake_call_serper_for_hq(**kwargs):
            seen.update(kwargs)
            return {"organic": []}

        with patch("lead_prioritizer_core.call_serper_for_hq",
                   side_effect=_fake_call_serper_for_hq), \
             _mock_anthropic(_ai_json(classification="unclear")):
            prioritize_single_lead(
                LeadInput(company_name="Acme", domain="acme.it", input_country="Italy"),
                serper_api_key="s", anthropic_api_key="a",
            )
        assert seen.get("gl") == "it"
        assert seen.get("hl") == "it"

    def test_switzerland_sets_gl_but_not_hl(self):
        seen = {}

        def _fake_call_serper_for_hq(**kwargs):
            seen.update(kwargs)
            return {"organic": []}

        with patch("lead_prioritizer_core.call_serper_for_hq",
                   side_effect=_fake_call_serper_for_hq), \
             _mock_anthropic(_ai_json(classification="unclear")):
            prioritize_single_lead(
                LeadInput(company_name="Acme", domain="acme.ch", input_country="Switzerland"),
                serper_api_key="s", anthropic_api_key="a",
            )
        assert seen.get("gl") == "ch"
        assert seen.get("hl") is None

    def test_unknown_country_omits_gl_hl(self):
        seen = {}

        def _fake_call_serper_for_hq(**kwargs):
            seen.update(kwargs)
            return {"organic": []}

        with patch("lead_prioritizer_core.call_serper_for_hq",
                   side_effect=_fake_call_serper_for_hq), \
             _mock_anthropic(_ai_json(classification="unclear")):
            prioritize_single_lead(
                LeadInput(company_name="Acme", domain="acme.com", input_country="Narnia"),
                serper_api_key="s", anthropic_api_key="a",
            )
        assert seen.get("gl") is None
        assert seen.get("hl") is None

    def test_own_domain_firecrawl_crawl_receives_effective_country(self):
        seen = {}

        def _fake_collect(domain, key, **kwargs):
            seen.update(kwargs)
            return {"pages": [], "pages_crawled": [], "used": False}

        with patch("lead_prioritizer_core.collect_own_domain_hq_pages",
                   side_effect=_fake_collect), \
             _mock_anthropic(_ai_json(classification="unclear")):
            prioritize_single_lead(
                LeadInput(company_name="Acme", domain="acme.it", input_country="Italy"),
                serper_api_key="s", anthropic_api_key="a", firecrawl_api_key="fc-key",
            )
        assert seen.get("country") == "Italy"

    def test_non_lusha_caller_without_input_country_falls_back_to_default(self):
        # Verification (Stap 1/2 audit): a caller with no input_country and
        # no Lusha fields at all (e.g. an existing CLI-style caller) must
        # keep exactly the pre-Stap-1 fallback: default_input_country, and
        # gl/hl derived from THAT value -- never a regression to "no
        # localization at all" just because the row itself is bare.
        lead = LeadInput(company_name="Acme", domain="acme.com")
        seen = {}

        def _fake_call_serper_for_hq(**kwargs):
            seen.update(kwargs)
            return {"organic": []}

        with patch("lead_prioritizer_core.call_serper_for_hq",
                   side_effect=_fake_call_serper_for_hq), \
             _mock_anthropic(_ai_json(classification="unclear")):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                default_input_country="Italy",
            )
        assert result.input_country == "Italy"
        assert seen.get("gl") == "it"
        assert seen.get("hl") == "it"
        assert result.lusha_main_industry is None


# ---------------------------------------------------------------------------
# Sector priority chain (Lusha enrichment plan, Stap 2): Lusha mapping (tier
# 1, highest) -> Serper evidence keyword match (existing) -> own-domain AI
# (existing) -> Lusha Description/Specialties text (tier 3, last resort).
# lusha_main_industry/lusha_sub_industry audit fields are always populated
# from the input regardless of which tier (if any) won.
# ---------------------------------------------------------------------------

class TestSectorPriorityChain:
    _lead_no_lusha = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")

    def test_lusha_mapping_wins_over_serper_evidence(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_main_industry="Manufacturing",
            lusha_sub_industry="Industrial Machinery & Equipment",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_sector_industry",
            return_value={
                "detected_industry": "Software", "detected_sub_industry": None,
                "detected_company_type": None, "sector_confidence": "High",
                "sector_reason": "should not win", "sector_evidence_url": "https://x",
                "sector_evidence_quote": None, "sector_source_title": None,
                "sector_source": "keyword_match",
            },
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a")
        assert result.detected_industry == "Industrial equipment and machinery"
        assert result.sector_source == "lusha_mapped"

    def test_no_lusha_mapping_falls_through_to_serper_evidence_unchanged(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_sector_industry",
            return_value={
                "detected_industry": "Software", "detected_sub_industry": None,
                "detected_company_type": None, "sector_confidence": "High",
                "sector_reason": "matched", "sector_evidence_url": "https://x",
                "sector_evidence_quote": None, "sector_source_title": None,
                "sector_source": "keyword_match",
            },
        ):
            result = prioritize_single_lead(
                self._lead_no_lusha, serper_api_key="s", anthropic_api_key="a")
        assert result.detected_industry == "Software"
        assert result.sector_source == "keyword_match"

    def test_lusha_text_fallback_only_when_evidence_and_ai_tiers_empty(self):
        empty_sector = {
            "detected_industry": None, "detected_sub_industry": None,
            "detected_company_type": None, "sector_confidence": None,
            "sector_reason": None, "sector_evidence_url": None,
            "sector_evidence_quote": None, "sector_source_title": None,
            "sector_source": None,
        }
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_description="We manufacture industrial machinery worldwide.",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_sector_industry", return_value=empty_sector,
        ):
            result = prioritize_single_lead(lead, serper_api_key="s", anthropic_api_key="a")
        assert result.detected_industry == "Industrial equipment and machinery"
        assert result.sector_source == "lusha_text_fallback"

    def test_no_sector_data_anywhere_stays_none_exactly_as_before(self):
        empty_sector = {
            "detected_industry": None, "detected_sub_industry": None,
            "detected_company_type": None, "sector_confidence": None,
            "sector_reason": None, "sector_evidence_url": None,
            "sector_evidence_quote": None, "sector_source_title": None,
            "sector_source": None,
        }
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_sector_industry", return_value=empty_sector,
        ):
            result = prioritize_single_lead(
                self._lead_no_lusha, serper_api_key="s", anthropic_api_key="a")
        assert result.detected_industry is None
        assert result.sector_source is None

    def test_lusha_audit_fields_always_populated_regardless_of_winning_tier(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_main_industry="Some Unmapped Label",
            lusha_sub_industry="Also Unmapped",
        )
        empty_sector = {
            "detected_industry": None, "detected_sub_industry": None,
            "detected_company_type": None, "sector_confidence": None,
            "sector_reason": None, "sector_evidence_url": None,
            "sector_evidence_quote": None, "sector_source_title": None,
            "sector_source": None,
        }
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_sector_industry", return_value=empty_sector,
        ):
            result = prioritize_single_lead(lead, serper_api_key="s", anthropic_api_key="a")
        # Neither Lusha label mapped to anything -- detected_industry stays
        # empty -- but the raw audit values are still preserved verbatim.
        assert result.detected_industry is None
        assert result.lusha_main_industry == "Some Unmapped Label"
        assert result.lusha_sub_industry == "Also Unmapped"

    def test_lusha_audit_fields_none_when_no_lusha_input(self):
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()):
            result = prioritize_single_lead(
                self._lead_no_lusha, serper_api_key="s", anthropic_api_key="a")
        assert result.lusha_main_industry is None
        assert result.lusha_sub_industry is None


# ---------------------------------------------------------------------------
# company_size_complexity priority chain (Lusha enrichment plan, Stap 3):
# Lusha employee/revenue data (new, highest priority) -> existing
# deterministic Serper-keyword signal (unchanged, fallback only). The
# Serper query/evidence/extraction for this signal is never removed --
# only overridden when usable Lusha data is present.
# ---------------------------------------------------------------------------

class TestCompanySizeComplexityPriorityChain:
    def _serper_signal(self):
        from lead_output_schema import LeadSignal
        return LeadSignal(
            signal_name="company_size_complexity",
            signal_value="positive_evidence", signal_score=2.0,
            signal_confidence="High", signal_reason="Serper keyword match.",
            evidence_url="https://acme.com/about", parser_source="serper_organic_1",
        )

    def _other_signal(self, name):
        from lead_output_schema import LeadSignal
        return LeadSignal(signal_name=name, signal_value="positive_evidence", signal_score=2.0)

    def test_lusha_data_wins_over_serper_signal(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_employees="201-500", lusha_revenue="$50M - $100M",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_non_hq_signals",
            return_value=[self._serper_signal()],
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.company_size_complexity_source == "lusha"
        assert result.sig_company_size_complexity_score == 2.0
        assert "Lusha company size data" in result.company_size_complexity_reason
        assert result.lusha_employees == "201-500"
        assert result.lusha_revenue == "$50M - $100M"

    def test_missing_lusha_data_yields_no_signal(self):
        # No Serper fallback exists anymore (Lusha enrichment plan, Stap 4,
        # supersedes the earlier Stap-3 "permanent Serper fallback" design)
        # -- missing Lusha data simply means no company_size_complexity
        # signal at all; the score/reason/evidence fields stay None,
        # exactly as the schema always allowed.
        lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_non_hq_signals", return_value=[],
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.company_size_complexity_source is None
        assert result.sig_company_size_complexity_score is None
        assert result.company_size_complexity_reason is None
        assert result.lusha_employees is None

    def test_unparseable_lusha_data_yields_no_signal(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_employees="garbage", lusha_revenue="also garbage",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_non_hq_signals", return_value=[],
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.company_size_complexity_source is None
        assert result.sig_company_size_complexity_score is None
        # Raw (unusable) Lusha values are still preserved for audit.
        assert result.lusha_employees == "garbage"
        # Raw (unusable) Lusha values are still preserved for audit.
        assert result.lusha_employees == "garbage"

    def test_neither_lusha_nor_serper_produced_anything(self):
        lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_non_hq_signals", return_value=[],
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.company_size_complexity_source is None
        assert result.sig_company_size_complexity_score is None

    def test_signal_extraction_flag_off_stays_unchanged_regardless_of_lusha_data(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_employees="201-500", lusha_revenue="$50M - $100M",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                extract_non_hq_signals_flag=False,
            )
        assert result.company_size_complexity_source is None
        assert result.sig_company_size_complexity_score is None

    def test_lusha_size_win_does_not_affect_other_signals(self):
        lead = LeadInput(
            company_name="Acme", domain="acme.com", input_country="Italy",
            lusha_employees="201-500",
        )
        with _mock_serper(_EMPTY_SERPER), _mock_anthropic(_ai_json()), patch(
            "lead_prioritizer_core.extract_non_hq_signals",
            return_value=[self._serper_signal(), self._other_signal("international_profile")],
        ):
            result = prioritize_single_lead(
                lead, serper_api_key="s", anthropic_api_key="a",
                collect_non_hq_evidence=True, extract_non_hq_signals_flag=True,
            )
        assert result.company_size_complexity_source == "lusha"
        assert result.sig_international_profile_score == 2.0


# ---------------------------------------------------------------------------
# _build_user_message: ADDITIONAL CONTEXT section for Lusha Description/
# Specialties (Lusha enrichment plan, Stap 5). Per-lead template only --
# the cached system+static block is covered by
# TestAnthropicPromptCaching.test_cache_boundary_hash_is_pinned above.
# ---------------------------------------------------------------------------

class TestBuildUserMessageLushaContext:
    """NOTE (Stap 5 scope correction): none of these tests pass
    ``crawled_pages``, so ``crawled_pages=None`` -> own-site content counts
    as thin -> the ADDITIONAL CONTEXT section is always considered here.
    ``TestOwnSiteContentThinGating`` below covers the rich-vs-thin gate
    itself (section present vs. entirely absent)."""

    def _build(self, **kwargs):
        from lead_hq_ai_interpreter import _build_user_message
        return _build_user_message(
            domain_root="acme", input_country="Italy", query="acme headquarters",
            serper_payload=_EMPTY_SERPER, **kwargs,
        )

    def test_no_lusha_fields_matches_pre_stap5_output_exactly(self):
        # Every existing caller (no lusha_description/lusha_specialties
        # passed at all) must get byte-for-byte the same prompt as before
        # this parameter existed -- confirmed against a message built
        # without the new kwargs, and separately that the section reads
        # "(none)", identical to every other empty per-lead section.
        msg_omitted = self._build()
        msg_explicit_none = self._build(lusha_description=None, lusha_specialties=None)
        assert msg_omitted == msg_explicit_none
        assert "ADDITIONAL CONTEXT" in msg_omitted
        assert "  (none)" in msg_omitted

    def test_blank_strings_also_render_as_none(self):
        msg = self._build(lusha_description="", lusha_specialties="   ")
        assert "ADDITIONAL CONTEXT" in msg
        section = msg.split("ADDITIONAL CONTEXT")[1]
        assert "(none)" in section

    def test_description_only(self):
        msg = self._build(lusha_description="A global manufacturer of industrial machinery.")
        assert "Description: A global manufacturer of industrial machinery." in msg
        assert "Specialties:" not in msg

    def test_specialties_only(self):
        msg = self._build(lusha_specialties="machinery, engineering, exports")
        assert "Specialties: machinery, engineering, exports" in msg
        assert "Description:" not in msg

    def test_both_description_and_specialties(self):
        msg = self._build(
            lusha_description="A global manufacturer of industrial machinery.",
            lusha_specialties="machinery, engineering",
        )
        assert "Description: A global manufacturer of industrial machinery." in msg
        assert "Specialties: machinery, engineering" in msg

    def test_description_truncated_at_800_chars(self):
        long_desc = "x" * 2000
        msg = self._build(lusha_description=long_desc)
        section = msg.split("ADDITIONAL CONTEXT")[1]
        desc_line = next(l for l in section.splitlines() if l.strip().startswith("Description:"))
        rendered = desc_line.split("Description:", 1)[1].strip()
        assert len(rendered) == 800

    def test_specialties_truncated_at_300_chars(self):
        long_spec = "y" * 1000
        msg = self._build(lusha_specialties=long_spec)
        section = msg.split("ADDITIONAL CONTEXT")[1]
        spec_line = next(l for l in section.splitlines() if l.strip().startswith("Specialties:"))
        rendered = spec_line.split("Specialties:", 1)[1].strip()
        assert len(rendered) == 300

    def test_labelled_as_lower_authority_than_primary_and_secondary(self):
        msg = self._build(lusha_description="Some description.")
        assert "PRIMARY SOURCE" in msg
        assert "SECONDARY SOURCE" in msg
        assert "ADDITIONAL CONTEXT" in msg
        # Ordering: additional context comes after both existing sources,
        # never renamed/reprioritised ahead of them.
        assert msg.index("PRIMARY SOURCE") < msg.index("SECONDARY SOURCE") < msg.index("ADDITIONAL CONTEXT")
        assert "lower authority" in msg[msg.index("ADDITIONAL CONTEXT"):]

    def test_interpret_hq_with_ai_omitted_lusha_params_matches_existing_behavior(self):
        # End-to-end: interpret_hq_with_ai without the new kwargs (every
        # existing caller) sends the identical user message as before.
        ai = _ai_json(classification="unclear")
        captured = {}
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=ai)]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_msg
        mock_lib = MagicMock()
        mock_lib.Anthropic.return_value = mock_client

        def _capture_create(**kwargs):
            captured.update(kwargs)
            return mock_msg

        mock_client.messages.create.side_effect = _capture_create

        with patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib):
            interpret_hq_with_ai(
                lead_input=LeadInput(company_name="Acme", domain="acme.com", input_country="Italy"),
                domain_root="acme", query="acme headquarters",
                serper_payload=_EMPTY_SERPER, anthropic_api_key="fake",
            )
        user_content = captured["messages"][0]["content"]
        assert "ADDITIONAL CONTEXT" in user_content
        assert "  (none)" in user_content

    def test_interpret_hq_with_ai_passes_lusha_fields_into_user_message(self):
        ai = _ai_json(classification="unclear")
        captured = {}
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=ai)]
        mock_client = MagicMock()

        def _capture_create(**kwargs):
            captured.update(kwargs)
            return mock_msg

        mock_client.messages.create.side_effect = _capture_create
        mock_lib = MagicMock()
        mock_lib.Anthropic.return_value = mock_client

        with patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib):
            interpret_hq_with_ai(
                lead_input=LeadInput(company_name="Acme", domain="acme.com", input_country="Italy"),
                domain_root="acme", query="acme headquarters",
                serper_payload=_EMPTY_SERPER, anthropic_api_key="fake",
                lusha_description="A global manufacturer of industrial machinery.",
                lusha_specialties="machinery, engineering",
            )
        user_content = captured["messages"][0]["content"]
        assert "Description: A global manufacturer of industrial machinery." in user_content
        assert "Specialties: machinery, engineering" in user_content


# ---------------------------------------------------------------------------
# _own_site_content_is_thin / thin-vs-rich gating (Lusha enrichment plan,
# Stap 5 scope correction): the ADDITIONAL CONTEXT section is only
# considered when the own-domain Firecrawl content is thin/absent. A
# well-evidenced company gets byte-for-byte the pre-Stap-5 prompt -- the
# section is entirely omitted, not even rendered as "(none)".
# ---------------------------------------------------------------------------

def _page(text: str, url: str = "https://acme.com/about") -> dict:
    return {"url": url, "text": text, "source_kind": "own_domain", "retrieval_method": "firecrawl"}


class TestOwnSiteContentThinGating:
    def test_no_crawled_pages_counts_as_thin(self):
        from lead_hq_ai_interpreter import _own_site_content_is_thin
        assert _own_site_content_is_thin(None) is True
        assert _own_site_content_is_thin([]) is True

    def test_pages_with_only_blank_text_count_as_thin(self):
        from lead_hq_ai_interpreter import _own_site_content_is_thin
        assert _own_site_content_is_thin([_page("   ")]) is True

    def test_combined_length_under_threshold_is_thin(self):
        from lead_hq_ai_interpreter import _own_site_content_is_thin
        assert _own_site_content_is_thin([_page("x" * 399)]) is True

    def test_combined_length_at_or_above_threshold_is_not_thin(self):
        from lead_hq_ai_interpreter import _own_site_content_is_thin
        assert _own_site_content_is_thin([_page("x" * 400)]) is False
        assert _own_site_content_is_thin([_page("x" * 200), _page("y" * 200, "https://acme.com/x")]) is False

    def test_rich_own_site_content_omits_lusha_section_entirely(self):
        from lead_hq_ai_interpreter import _build_user_message
        rich_pages = [_page("x" * 5000)]
        msg = _build_user_message(
            domain_root="acme", input_country="Italy", query="acme headquarters",
            serper_payload=_EMPTY_SERPER, crawled_pages=rich_pages,
            lusha_description="A global manufacturer of industrial machinery.",
            lusha_specialties="machinery, engineering",
        )
        assert "ADDITIONAL CONTEXT" not in msg
        assert "A global manufacturer" not in msg
        assert "machinery, engineering" not in msg

    def test_thin_own_site_content_includes_lusha_section(self):
        from lead_hq_ai_interpreter import _build_user_message
        thin_pages = [_page("x" * 50)]
        msg = _build_user_message(
            domain_root="acme", input_country="Italy", query="acme headquarters",
            serper_payload=_EMPTY_SERPER, crawled_pages=thin_pages,
            lusha_description="A global manufacturer of industrial machinery.",
            lusha_specialties="machinery, engineering",
        )
        assert "ADDITIONAL CONTEXT" in msg
        assert "Description: A global manufacturer of industrial machinery." in msg
        assert "Specialties: machinery, engineering" in msg

    def test_rich_own_site_with_no_lusha_data_matches_pre_stap5_output(self):
        # Rich content + no Lusha data at all: message must be identical
        # whether or not the (empty) lusha_* kwargs are passed.
        from lead_hq_ai_interpreter import _build_user_message
        rich_pages = [_page("x" * 5000)]
        msg_with_kwargs = _build_user_message(
            domain_root="acme", input_country="Italy", query="acme headquarters",
            serper_payload=_EMPTY_SERPER, crawled_pages=rich_pages,
            lusha_description=None, lusha_specialties=None,
        )
        msg_without_kwargs = _build_user_message(
            domain_root="acme", input_country="Italy", query="acme headquarters",
            serper_payload=_EMPTY_SERPER, crawled_pages=rich_pages,
        )
        assert msg_with_kwargs == msg_without_kwargs
        assert "ADDITIONAL CONTEXT" not in msg_with_kwargs

    def test_threshold_boundary_via_interpret_hq_with_ai(self):
        # End-to-end: rich crawled_pages -> no ADDITIONAL CONTEXT reaches
        # the actual Anthropic call's user message.
        ai = _ai_json(classification="unclear")
        captured = {}
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=ai)]
        mock_client = MagicMock()

        def _capture_create(**kwargs):
            captured.update(kwargs)
            return mock_msg

        mock_client.messages.create.side_effect = _capture_create
        mock_lib = MagicMock()
        mock_lib.Anthropic.return_value = mock_client

        with patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib):
            interpret_hq_with_ai(
                lead_input=LeadInput(company_name="Acme", domain="acme.com", input_country="Italy"),
                domain_root="acme", query="acme headquarters",
                serper_payload=_EMPTY_SERPER, anthropic_api_key="fake",
                crawled_pages=[_page("x" * 5000)],
                lusha_description="A global manufacturer of industrial machinery.",
                lusha_specialties="machinery, engineering",
            )
        user_content = captured["messages"][0]["content"]
        assert "ADDITIONAL CONTEXT" not in user_content
        assert "A global manufacturer" not in user_content
