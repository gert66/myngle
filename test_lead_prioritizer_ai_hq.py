"""Mocked tests for Lead Prioritizer v2 AI-first HQ strategy.

Tests four representative cases: Thales (foreign, High), Amplifon (domestic),
Burger King Italy (foreign, High), and an unclear/empty result.

All external calls (Serper + Anthropic) are mocked so no network is required.
"""

from __future__ import annotations

import json
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
        ai = _ai_json(classification="domestic", confidence="High",
                      industry="Power electronics")
        _, client = self._interpret(ai)
        msg = _user_message_sent(client)
        assert '"industry"' in msg
        assert '"sub_industry"' in msg
        # Style-guidance vocabulary from the deterministic sector detector.
        assert "Chemicals" in msg or "Manufacturing" in msg

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

    def test_keyword_match_always_wins_over_ai_fallback(self):
        # sector_industry evidence with a clean keyword hit must never be
        # overwritten by the AI-derived fallback, even when a crawl happened.
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
        assert result.sector_source == "keyword_match"
        assert result.detected_industry == "Industrial equipment and machinery"
