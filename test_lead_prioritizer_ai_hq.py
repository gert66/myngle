"""Mocked tests for Lead Prioritizer v2 AI-first HQ strategy.

Tests four representative cases: Thales (foreign, High), Amplifon (domestic),
Burger King Italy (foreign, High), and an unclear/empty result.

All external calls (Serper + Anthropic) are mocked so no network is required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lead_output_schema import LeadInput, LeadPrioritizationResult
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
