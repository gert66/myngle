"""Tests for Lead Prioritizer v2 deterministic non-HQ signal extraction (Step 3).

No network / live keys. Serper is mocked at the core level; the extractor is
pure and needs no mocking.
"""

from __future__ import annotations

from unittest.mock import patch

from lead_output_schema import LeadEvidence, LeadInput, HQDetectionResult
from lead_non_hq_signal_extractor import (
    extract_non_hq_signals,
    summarize_non_hq_signals_for_result,
    SUPPORTED_SIGNALS,
)
from lead_prioritizer_core import prioritize_single_lead


def _ev(signal_name, title="", snippet="", url="https://x.example/p", query="q"):
    return LeadEvidence(
        signal_name=signal_name, source_title=title, source_snippet=snippet,
        source_url=url, query_used=query, parser_source="serper_organic_1",
        source_type="organic",
    )


# ---------------------------------------------------------------------------
# Pure extractor behavior
# ---------------------------------------------------------------------------

class TestExtractor:
    def test_no_evidence_no_signals(self):
        assert extract_non_hq_signals([]) == []

    def test_one_keyword_gives_score_1(self):
        ev = [_ev("international_profile", snippet="A company with global reach.")]
        sigs = extract_non_hq_signals(ev)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.signal_score == 1.0
        assert s.signal_value == "weak_evidence"
        assert s.signal_confidence == "Medium"  # score 1.0 + url

    def test_two_distinct_keywords_give_score_2(self):
        ev = [_ev("international_profile",
                  snippet="Global company with offices in many countries.")]
        sigs = extract_non_hq_signals(ev)
        s = sigs[0]
        assert s.signal_score == 2.0
        assert s.signal_value == "positive_evidence"
        assert s.signal_confidence == "High"  # score 2.0 + url

    def test_evidence_but_no_keywords_gives_score_0(self):
        ev = [_ev("company_size_complexity", snippet="Nothing relevant here at all.")]
        sigs = extract_non_hq_signals(ev)
        s = sigs[0]
        assert s.signal_score == 0.0
        assert s.signal_value == "no_positive_match"
        assert s.signal_confidence == "Low"

    def test_score_2_without_url_is_low_confidence(self):
        ev = [_ev("international_profile",
                  snippet="Global company with offices worldwide.", url="")]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 2.0
        assert s.signal_confidence == "Low"  # no source_url

    def test_evidence_fields_copied_verbatim(self):
        ev = [_ev("onboarding_training_need",
                  title="Careers at Acme", snippet="Training and onboarding academy.",
                  url="https://acme.com/careers", query="acme careers training")]
        s = extract_non_hq_signals(ev)[0]
        assert s.evidence_title == "Careers at Acme"
        assert s.evidence_quote == "Training and onboarding academy."
        assert s.evidence_url == "https://acme.com/careers"
        assert s.query_used == "acme careers training"
        assert s.parser_source == "serper_organic_1"
        assert s.needs_manual_review is False

    def test_employer_branding_two_keywords_give_score_2(self):
        ev = [_ev("employer_branding",
                  snippet="Great place to work with strong employee satisfaction.")]
        sigs = extract_non_hq_signals(ev)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.signal_name == "employer_branding"
        assert s.signal_score == 2.0
        assert s.signal_value == "positive_evidence"

    def test_only_supported_signals_and_no_competitor(self):
        # An evidence item tagged with a competitor-like name must NOT yield a signal.
        ev = [
            _ev("international_profile", snippet="global markets"),
            _ev("competitor", snippet="Berlitz is an alternative provider"),
        ]
        sigs = extract_non_hq_signals(ev)
        names = {s.signal_name for s in sigs}
        assert names == {"international_profile"}
        assert all(n in SUPPORTED_SIGNALS for n in names)
        assert "competitor" not in names


# ---------------------------------------------------------------------------
# Summary mapping
# ---------------------------------------------------------------------------

class TestSummary:
    def test_missing_signals_map_to_none(self):
        summary = summarize_non_hq_signals_for_result([])
        for key in (
            "sig_international_profile_score", "sig_onboarding_training_need_score",
            "sig_company_size_complexity_score", "sig_icp_keyword_match_score",
            "sig_employer_branding_score",
            "international_profile_reason", "icp_keyword_match_evidence_url",
            "employer_branding_reason", "employer_branding_evidence_url",
            "employer_branding_evidence_quote",
        ):
            assert summary[key] is None

    def test_present_signal_maps_fields(self):
        ev = [_ev("icp_keyword_match", snippet="corporate training for global teams")]
        sigs = extract_non_hq_signals(ev)
        summary = summarize_non_hq_signals_for_result(sigs)
        assert summary["sig_icp_keyword_match_score"] == 2.0
        assert summary["icp_keyword_match_evidence_url"] == "https://x.example/p"
        assert "corporate training" in summary["icp_keyword_match_reason"]

    def test_employer_branding_signal_maps_fields(self):
        ev = [_ev("employer_branding",
                  snippet="Employee satisfaction and workplace culture are strong.",
                  url="https://acme.com/careers")]
        sigs = extract_non_hq_signals(ev)
        summary = summarize_non_hq_signals_for_result(sigs)
        assert summary["sig_employer_branding_score"] == 2.0
        assert summary["employer_branding_evidence_url"] == "https://acme.com/careers"
        assert summary["employer_branding_evidence_quote"] == \
            "Employee satisfaction and workplace culture are strong."
        assert "keyword match" in summary["employer_branding_reason"]


# ---------------------------------------------------------------------------
# Core flag gating
# ---------------------------------------------------------------------------

class TestCoreGating:
    _lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")

    def _run(self, collected_evidence, **flags):
        p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                         return_value={"organic": []})
        p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai",
                     return_value=HQDetectionResult(
                         hq_structure_type="domestic",
                         sig_foreign_hq_score_for_next_scoring=0.0))
        p_collect = patch("lead_prioritizer_core.collect_non_hq_enrichment_evidence",
                          return_value=collected_evidence)
        with p_serper, p_ai, p_collect:
            return prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake", **flags,
            )

    def test_default_does_not_extract(self):
        r = self._run([_ev("international_profile", snippet="global offices")])
        assert r.signals == []
        assert r.sig_international_profile_score is None

    def test_extract_flag_true_produces_signals(self):
        r = self._run(
            [_ev("international_profile", snippet="global offices worldwide")],
            collect_non_hq_evidence=True,
            extract_non_hq_signals_flag=True,
        )
        assert len(r.signals) == 1
        assert r.sig_international_profile_score == 2.0
        # HQ fields untouched.
        assert r.hq_structure_type == "domestic"
        assert r.sig_foreign_hq_score_for_next_scoring == 0.0

    def test_extract_without_collect_yields_empty_signals(self):
        # Flag on, but no evidence collected → empty signals, no implicit Serper.
        r = self._run([], extract_non_hq_signals_flag=True)
        assert r.signals == []
        assert r.sig_international_profile_score is None

    def test_employer_branding_flows_through_result(self):
        r = self._run(
            [_ev("employer_branding",
                 snippet="Great place to work with strong employee satisfaction.",
                 url="https://acme.com/careers")],
            collect_non_hq_evidence=True,
            extract_non_hq_signals_flag=True,
        )
        assert r.sig_employer_branding_score == 2.0
        assert r.employer_branding_evidence_url == "https://acme.com/careers"
        assert r.employer_branding_evidence_quote == \
            "Great place to work with strong employee satisfaction."
        assert "keyword match" in r.employer_branding_reason
