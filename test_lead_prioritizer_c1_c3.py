"""Mocked tests for Lead Prioritizer v2 changes C1–C3.

Covers:
  C1 — robust AI parser (markdown fences, prose, truncated/malformed `reason`,
       regex fallback, and genuinely unusable responses).
  C2 — default input country (blank/None → "Italy").
  C3 — audit fields (ai_hq_raw_json, domain_root, query_used, parser_source)
       and the competitor-exclusion note.

All external calls (Serper + Anthropic) are mocked — no network or live keys.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lead_output_schema import LeadInput
from lead_prioritizer_core import prioritize_single_lead


# ---------------------------------------------------------------------------
# Helpers (same mocking strategy as test_lead_prioritizer_ai_hq.py)
# ---------------------------------------------------------------------------

_EMPTY_SERPER: dict = {"organic": []}


def _mock_serper(payload: dict):
    return patch("lead_prioritizer_core.call_serper_for_hq", return_value=payload)


def _mock_anthropic(ai_text: str):
    """Patch the module-level _anthropic_lib so the model returns ai_text verbatim."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=ai_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    mock_lib = MagicMock()
    mock_lib.Anthropic.return_value = mock_client
    return patch("lead_hq_ai_interpreter._anthropic_lib", mock_lib)


def _run(lead: LeadInput, ai_text: str, **kwargs):
    with _mock_serper(_EMPTY_SERPER), _mock_anthropic(ai_text):
        return prioritize_single_lead(
            lead,
            serper_api_key="fake-serper",
            anthropic_api_key="fake-anthropic",
            **kwargs,
        )


# ---------------------------------------------------------------------------
# C1 — robust parser
# ---------------------------------------------------------------------------

class TestC1RobustParser:
    _lead = LeadInput(company_name="Ricoh Italia", domain="ricoh.it", input_country="Italy")

    def test_truncated_reason_still_scores(self):
        # Valid core fields, then a truncated/unterminated `reason` → json.loads
        # fails, regex fallback must recover classification/confidence/country.
        raw = (
            '{"classification": "foreign_parent", "confidence": "High", '
            '"parent_company": "Ricoh Company, Ltd.", "parent_hq_country": "Japan", '
            '"parent_hq_city": "Tokyo", "evidence_url": "https://ricoh.com", '
            '"evidence_quote": "HQ in Tokyo", '
            '"reason": "Ricoh is headquartered in Tokyo and the Italian entity'
        )
        r = _run(self._lead, raw)
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
        assert r.foreign_hq_simple is True
        assert r.needs_manual_review is False
        assert r.hq_detected_country == "Japan"

    def test_markdown_fenced_with_prose(self):
        raw = (
            "Here is the analysis:\n```json\n"
            '{"classification": "foreign_parent", "confidence": "Medium", '
            '"parent_hq_country": "Germany", "parent_hq_city": "Munich", '
            '"reason": "German parent."}\n'
            "```\nHope that helps!"
        )
        r = _run(self._lead, raw)
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
        assert r.hq_detected_country == "Germany"

    def test_unusable_response_routes_to_review(self):
        raw = "I'm sorry, I cannot determine the headquarters from these results."
        r = _run(self._lead, raw)
        assert r.needs_manual_review is True
        assert r.sig_foreign_hq_score_for_next_scoring is None


# ---------------------------------------------------------------------------
# C2 — default input country
# ---------------------------------------------------------------------------

class TestC2DefaultCountry:
    def test_blank_country_defaults_to_italy_foreign(self):
        # input_country None → effective "Italy"; German parent differs → score 3.
        lead = LeadInput(company_name="BMW Italia", domain="bmw.it", input_country=None)
        # bmw is a short (risky) root, so include matching evidence so the C4
        # safety layer keeps the confirmed foreign HQ score.
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_company="BMW AG", parent_hq_country="Germany",
            parent_hq_city="Munich", evidence_url="https://www.bmw.it/",
            evidence_quote="", reason="r",
        ))
        r = _run(lead, ai)
        assert r.input_country == "Italy"
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0

    def test_blank_country_defaults_to_italy_domestic(self):
        # Parent in Italy + defaulted input Italy → domestic → score 0.
        lead = LeadInput(company_name="Amplifon", domain="amplifon.com", input_country="")
        ai = json.dumps(dict(
            classification="domestic", confidence="High",
            parent_company="Amplifon S.p.A.", parent_hq_country="Italy",
            parent_hq_city="Milan", evidence_url="", evidence_quote="", reason="r",
        ))
        r = _run(lead, ai)
        assert r.input_country == "Italy"
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.hq_structure_type == "domestic"

    def test_custom_default_country(self):
        lead = LeadInput(company_name="Acme", domain="acme.de", input_country=None)
        ai = json.dumps(dict(
            classification="domestic", confidence="High",
            parent_hq_country="Germany", reason="r",
        ))
        r = _run(lead, ai, default_input_country="Germany")
        assert r.input_country == "Germany"
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0


# ---------------------------------------------------------------------------
# C3 — audit fields
# ---------------------------------------------------------------------------

class TestC3AuditFields:
    _lead = LeadInput(company_name="Thales Italia", domain="thalesgroup.com", input_country="Italy")
    _ai = json.dumps(dict(
        classification="foreign_parent", confidence="High",
        parent_company="Thales Group", parent_hq_country="France",
        parent_hq_city="Paris", evidence_url="https://thalesgroup.com",
        evidence_quote="HQ in Paris", reason="French parent.",
    ))

    def test_provenance_fields_mapped(self):
        r = _run(self._lead, self._ai)
        assert r.domain_root == "thalesgroup"
        assert r.query_used == "thalesgroup headquarters"
        assert r.parser_source == "ai_first"

    def test_raw_json_populated(self):
        r = _run(self._lead, self._ai)
        assert r.ai_hq_raw_json and "foreign_parent" in r.ai_hq_raw_json

    def test_competitor_excluded_note(self):
        r = _run(self._lead, self._ai)
        assert r.competitor_signal_excluded_from_next_scoring == (
            "Competitor evidence is audit-only and excluded from HQ scoring."
        )


# ---------------------------------------------------------------------------
# Canonical scoring edges (taxonomy unchanged: 4 classes)
# ---------------------------------------------------------------------------

class TestScoringEdges:
    _lead = LeadInput(company_name="Foo", domain="foo.it", input_country="Italy")

    def test_low_confidence_foreign_parent(self):
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="Low",
            parent_hq_country="Germany", reason="weak",
        ))
        r = _run(self._lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.needs_manual_review is True
        assert r.foreign_hq_simple is True

    def test_country_normalization_treats_italia_as_domestic(self):
        # foreign_parent label but parent country normalizes equal to input → domestic 0.
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Italia", reason="same country",
        ))
        r = _run(self._lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.hq_structure_type == "domestic"

    def test_regional_branch_only(self):
        ai = json.dumps(dict(
            classification="regional_branch_only", confidence="High",
            parent_hq_country="Germany", reason="branch",
        ))
        r = _run(self._lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.foreign_hq_simple is False


class TestBrazilNormalization:
    """Brazil aliases (Brasil / BR / BRA) must normalize equal to input Brazil,
    so a domestic Brazilian company is not misclassified as foreign."""

    _br = LeadInput(company_name="Foo Brasil Ltda", domain="foo.com.br",
                    input_country="Brazil")

    def test_brasil_alias_is_domestic(self):
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Brasil", reason="same country",
        ))
        r = _run(self._br, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.hq_structure_type == "domestic"

    def test_br_alias_scores_zero(self):
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="BR", reason="same country",
        ))
        r = _run(self._br, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0

    def test_bra_alias_scores_zero(self):
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="BRA", reason="same country",
        ))
        r = _run(self._br, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0

    def test_default_country_brazil_domestic(self):
        lead = LeadInput(company_name="Foo Brasil Ltda", domain="foo.com.br",
                         input_country=None)
        ai = json.dumps(dict(
            classification="domestic", confidence="High",
            parent_hq_country="Brasil", reason="domestic",
        ))
        r = _run(lead, ai, default_input_country="Brazil")
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.input_country == "Brazil"

    def test_genuine_foreign_parent_still_scores_3(self):
        # foo is a short (risky) root, so include matching evidence so the C4
        # safety layer keeps the confirmed foreign HQ score.
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Germany",
            evidence_url="https://www.foo.com.br/", reason="German parent",
        ))
        r = _run(self._br, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0


class TestHQPositiveScoreSafety:
    """C4 — a risky short/generic domain root with blank/mismatched evidence
    must not blindly score a domestic company as foreign; route to review."""

    def test_fiap_false_positive_suppressed(self):
        lead = LeadInput(company_name="FIAP", domain="fiap.com.br", input_country="Brazil")
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Luxembourg",
            evidence_url="https://www.fiap.net/", reason="matched fiap.net",
        ))
        r = _run(lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.needs_manual_review is True
        assert r.hq_positive_score_suppressed_for_review == "Yes"
        assert r.hq_evidence_domain_mismatch_warning == "Yes"
        # classification and parent HQ stay visible
        assert r.ai_hq_classification == "foreign_parent"
        assert r.ai_parent_hq_country == "Luxembourg"
        assert r.hq_structure_type == "foreign_parent"

    def test_bild_generic_root_suppressed(self):
        lead = LeadInput(company_name="Bild Desenvolvimento Imobiliário",
                         domain="bild.com.br", input_country="Brazil")
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Germany",
            evidence_url="https://www.bild.de/", reason="matched bild.de",
        ))
        r = _run(lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.needs_manual_review is True
        assert r.hq_positive_score_suppressed_for_review == "Yes"

    def test_risky_root_same_domain_evidence_not_suppressed(self):
        lead = LeadInput(company_name="SH", domain="sh.com.br", input_country="Brazil")
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="United States",
            evidence_url="https://www.sh.com.br/about", reason="same domain",
        ))
        r = _run(lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
        assert r.needs_manual_review is False
        assert r.hq_positive_score_suppressed_for_review == "No"
        assert r.hq_evidence_domain_match == "Yes"

    def test_non_risky_valid_foreign_hq_scores_3(self):
        lead = LeadInput(company_name="Nissan do Brasil", domain="nissan.com.br",
                         input_country="Brazil")
        ai = json.dumps(dict(
            classification="foreign_parent", confidence="High",
            parent_hq_country="Japan",
            evidence_url="https://www.nissan.com.br/", reason="Japanese parent",
        ))
        r = _run(lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 3.0
        assert r.needs_manual_review is False
        assert r.hq_positive_score_suppressed_for_review == "No"
        assert r.hq_query_risk_flag == "No"

    def test_domestic_brazil_remains_zero_no_suppression(self):
        lead = LeadInput(company_name="Banco BMG", domain="bancobmg.com.br",
                         input_country="Brazil")
        ai = json.dumps(dict(
            classification="domestic", confidence="High",
            parent_hq_country="Brasil",
            evidence_url="https://www.bancobmg.com.br/", reason="domestic",
        ))
        r = _run(lead, ai)
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0
        assert r.hq_structure_type == "domestic"
        # domestic path never sets the suppression flag
        assert r.hq_positive_score_suppressed_for_review is None
