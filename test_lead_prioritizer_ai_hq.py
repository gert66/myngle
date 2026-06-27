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
