"""Tests for compare_non_hq_signal_scoring.py (no live Serper/Anthropic calls).

Serper is mocked via ``lead_non_hq_enrichment.call_serper_for_enrichment``;
Anthropic is mocked the same way as test_lead_ai_signal_scorer.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from compare_non_hq_signal_scoring import (
    build_signal_agreement_summary,
    run_comparison,
)
from lead_non_hq_signal_extractor import SUPPORTED_SIGNALS


def _serper_payload_for(signal_name: str) -> dict:
    """A payload whose organic snippet gives >=2 keyword hits for the
    deterministic extractor -- so both paths have real evidence to judge."""
    texts = {
        "international_profile": "International offices in 11 countries worldwide.",
        "onboarding_training_need": "Careers, training and onboarding academy for new employees.",
        "company_size_complexity": "Employees, revenue and locations across our company profile.",
        "icp_keyword_match": "Corporate training for global teams, sales and customer service.",
        "employer_branding": "Employer branding, employee satisfaction and great place to work.",
        "sector_industry": "A technology company providing software services.",
    }
    text = texts.get(signal_name, "")
    return {
        "organic": [
            {"title": "About us", "snippet": text, "link": "https://acme.com/about"},
        ],
    }


def _fake_call_serper_for_enrichment(query, serper_api_key, gl=None, hl=None, usage_kind="non_hq"):
    # query text encodes no signal name directly; infer from keyword presence
    # in the query itself (mirrors build_non_hq_enrichment_queries wording).
    if "international offices" in query:
        return _serper_payload_for("international_profile")
    if "careers training onboarding" in query:
        return _serper_payload_for("onboarding_training_need")
    if "employees revenue locations" in query:
        return _serper_payload_for("company_size_complexity")
    if "corporate training sales" in query:
        return _serper_payload_for("icp_keyword_match")
    if "employer branding" in query:
        return _serper_payload_for("employer_branding")
    return _serper_payload_for("sector_industry")


def _mock_anthropic_json(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=500, output_tokens=50,
                           cache_creation_input_tokens=0, cache_read_input_tokens=0)
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("lead_ai_signal_scorer._anthropic_lib", lib)


_AI_RESPONSE_ALL_POSITIVE = (
    '{"international_profile": {"verdict": "positive_evidence", "reason": "clear", "supporting_evidence_ids": ["international_profile:organic:1"]}, '
    '"onboarding_training_need": {"verdict": "positive_evidence", "reason": "clear", "supporting_evidence_ids": ["onboarding_training_need:organic:1"]}, '
    '"company_size_complexity": {"verdict": "weak_evidence", "reason": "thin", "supporting_evidence_ids": ["company_size_complexity:organic:1"]}, '
    '"icp_keyword_match": {"verdict": "positive_evidence", "reason": "clear", "supporting_evidence_ids": ["icp_keyword_match:organic:1"]}, '
    '"employer_branding": {"verdict": "no_positive_match", "reason": "no support", "supporting_evidence_ids": []}}'
)


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"company_name": "Acme BV", "domain": "acme.com"},
        {"company_name": "Globex NV", "domain": "globex.com"},
    ])


class TestRunComparison:
    def test_one_row_per_signal_per_lead(self):
        with patch("lead_non_hq_enrichment.call_serper_for_enrichment",
                   side_effect=_fake_call_serper_for_enrichment), \
             _mock_anthropic_json(_AI_RESPONSE_ALL_POSITIVE):
            signal_df, cost_df = run_comparison(
                _sample_df(), company_column="company_name", domain_column="domain",
                default_input_country="Netherlands",
                serper_api_key="fake", anthropic_api_key="fake",
            )
        assert len(signal_df) == len(_sample_df()) * len(SUPPORTED_SIGNALS)
        assert set(signal_df["signal_name"]) == set(SUPPORTED_SIGNALS)
        assert len(cost_df) == len(_sample_df())

    def test_keyword_and_ai_scores_both_present(self):
        with patch("lead_non_hq_enrichment.call_serper_for_enrichment",
                   side_effect=_fake_call_serper_for_enrichment), \
             _mock_anthropic_json(_AI_RESPONSE_ALL_POSITIVE):
            signal_df, _ = run_comparison(
                _sample_df().head(1), company_column="company_name", domain_column="domain",
                default_input_country="Netherlands",
                serper_api_key="fake", anthropic_api_key="fake",
            )
        row = signal_df[signal_df["signal_name"] == "international_profile"].iloc[0]
        assert row["keyword_score"] == 2.0
        assert row["ai_score"] == 2.0
        assert row["agreement"] == "same"

    def test_cost_recorded_per_lead(self):
        with patch("lead_non_hq_enrichment.call_serper_for_enrichment",
                   side_effect=_fake_call_serper_for_enrichment), \
             _mock_anthropic_json(_AI_RESPONSE_ALL_POSITIVE):
            _, cost_df = run_comparison(
                _sample_df(), company_column="company_name", domain_column="domain",
                default_input_country="Netherlands",
                serper_api_key="fake", anthropic_api_key="fake",
            )
        assert (cost_df["ai_input_tokens"] == 500).all()
        assert cost_df["ai_estimated_cost_usd"].notna().all()

    def test_ai_failure_leaves_ai_columns_blank_but_keyword_scores_intact(self):
        with patch("lead_non_hq_enrichment.call_serper_for_enrichment",
                   side_effect=_fake_call_serper_for_enrichment), \
             _mock_anthropic_json("not valid json"):
            signal_df, _ = run_comparison(
                _sample_df().head(1), company_column="company_name", domain_column="domain",
                default_input_country="Netherlands",
                serper_api_key="fake", anthropic_api_key="fake",
            )
        row = signal_df[signal_df["signal_name"] == "international_profile"].iloc[0]
        assert row["keyword_score"] == 2.0
        assert row["ai_score"] is None
        assert bool(row["ai_call_success"]) is False


class TestSignalAgreementSummary:
    def test_agreement_rate_and_delta_computed_per_signal(self):
        with patch("lead_non_hq_enrichment.call_serper_for_enrichment",
                   side_effect=_fake_call_serper_for_enrichment), \
             _mock_anthropic_json(_AI_RESPONSE_ALL_POSITIVE):
            signal_df, _ = run_comparison(
                _sample_df(), company_column="company_name", domain_column="domain",
                default_input_country="Netherlands",
                serper_api_key="fake", anthropic_api_key="fake",
            )
        summary = build_signal_agreement_summary(signal_df)
        assert len(summary) == len(SUPPORTED_SIGNALS)
        eb_row = summary[summary["signal_name"] == "employer_branding"].iloc[0]
        # keyword_score for employer_branding evidence above has 2 keyword
        # hits ("employer branding", "employee satisfaction", "great place
        # to work") -> keyword=2.0, AI verdict was "no_positive_match" -> 0.0.
        assert eb_row["agreement_rate"] == 0.0
