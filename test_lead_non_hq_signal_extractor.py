"""Tests for Lead Prioritizer v2 deterministic non-HQ signal extraction (Step 3).

No network / live keys. Serper is mocked at the core level; the extractor is
pure and needs no mocking.
"""

from __future__ import annotations

from unittest.mock import patch

from lead_output_schema import LeadEvidence, LeadInput, HQDetectionResult
from lead_non_hq_signal_extractor import (
    extract_non_hq_signals,
    extract_sector_industry,
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

    def test_sector_industry_evidence_yields_no_commercial_signal(self):
        # sector_industry is audit/app metadata only — never a scoring signal.
        ev = [
            _ev("sector_industry", snippet="Acme is a retail company."),
            _ev("international_profile", snippet="global markets"),
        ]
        sigs = extract_non_hq_signals(ev)
        names = {s.signal_name for s in sigs}
        assert names == {"international_profile"}
        assert "sector_industry" not in SUPPORTED_SIGNALS


# ---------------------------------------------------------------------------
# Step 2 — external-training and hosted-platform guards moved into the
# extraction layer, so the signal score and the exporter's displayed
# rationale can never disagree (previously: a high score with every driver
# shown as "Rejected").
# ---------------------------------------------------------------------------

class TestExternalTrainingAndHostedPlatformGuards:
    def test_samsung_external_installer_training_scores_zero(self):
        """External installer/partner training must not count as an internal
        onboarding_training_need or icp_keyword_match keyword hit."""
        ev = [_ev(
            "onboarding_training_need",
            title="Become a Samsung installer",
            snippet=(
                "Find out how to become a certified Samsung heat pump "
                "installer. Get the training you need to become a climate "
                "solutions partner."
            ),
            url="https://www.samsung.com/uk/installer-training",
        )]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 0.0
        assert s.signal_value == "no_positive_match"
        assert "external installer" in s.signal_reason.lower() \
            or "partner" in s.signal_reason.lower()

    def test_samsung_case_also_zero_for_icp_keyword_match(self):
        ev = [_ev(
            "icp_keyword_match",
            snippet=(
                "Become a certified installer and climate solutions partner "
                "with our product training and certification program."
            ),
        )]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 0.0

    def test_internal_onboarding_academy_scores_positively(self):
        """A genuine internal L&D case must still score normally."""
        ev = [_ev(
            "onboarding_training_need",
            title="Careers at Acme",
            snippet="Our employee onboarding academy and internal LMS support "
                    "every new hire's career development.",
        )]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 2.0
        assert s.signal_value == "positive_evidence"

    def test_mixed_evidence_counts_only_internal_keywords(self):
        """When both external-training and internal evidence exist for the
        same signal, only the internal evidence may drive the score."""
        ev = [
            _ev("onboarding_training_need", snippet=(
                "Become a certified installer and channel partner."
            ), url="https://x.example/installer"),
            _ev("onboarding_training_need", snippet=(
                "Our employee onboarding academy supports every new hire."
            ), url="https://x.example/careers"),
        ]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 2.0
        assert s.evidence_url == "https://x.example/careers"

    def test_hosted_platform_evidence_never_scores_any_signal(self):
        """A hosted careers-platform hit (Workday, Greenhouse, ...) is about
        the platform vendor, not the company, for ANY non-HQ signal — not
        just the L&D family."""
        ev = [_ev(
            "international_profile",
            snippet="Global company with offices in many countries worldwide.",
            url="https://shimano.wd3.myworkdayjobs.com/some-posting",
        )]
        s = extract_non_hq_signals(ev)[0]
        assert s.signal_score == 0.0
        assert "hosted careers-platform" in s.signal_reason.lower()

    def test_hosted_platform_evidence_kept_in_evidence_items_for_audit(self):
        """Rejected evidence must stay available for audit — only the score
        (never the raw evidence_items list) changes."""
        evidence_items = [_ev(
            "onboarding_training_need",
            snippet="Become a certified installer and channel partner.",
            url="https://shimano.wd3.myworkdayjobs.com/some-posting",
        )]
        sigs = extract_non_hq_signals(evidence_items)
        assert sigs[0].signal_score == 0.0
        # The evidence item itself is untouched by extraction.
        assert len(evidence_items) == 1
        assert evidence_items[0].source_url == "https://shimano.wd3.myworkdayjobs.com/some-posting"


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
# Sector / industry extraction (audit metadata only)
# ---------------------------------------------------------------------------

class TestSectorIndustry:
    def test_no_evidence_all_none(self):
        out = extract_sector_industry([])
        assert all(v is None for v in out.values())

    def test_non_sector_evidence_ignored(self):
        out = extract_sector_industry(
            [_ev("international_profile", snippet="A retail company.")])
        assert all(v is None for v in out.values())

    def test_detects_consumer_electronics_manufacturer(self):
        ev = [_ev("sector_industry",
                  title="TCL SEMP | LinkedIn",
                  snippet="TCL SEMP is a consumer electronics manufacturing "
                          "company based in Brazil.",
                  url="https://www.linkedin.com/company/tcl-semp")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Consumer goods"
        assert out["detected_sub_industry"] == "Consumer electronics"
        assert out["sector_evidence_url"] == "https://www.linkedin.com/company/tcl-semp"
        assert out["sector_source_title"] == "TCL SEMP | LinkedIn"
        assert "consumer electronics" in out["sector_reason"]
        # Manufacturing was also mentioned; reason should acknowledge it.
        assert "Manufacturing" in out["sector_reason"]

    def test_two_keywords_same_sector_high_confidence(self):
        ev = [_ev("sector_industry",
                  snippet="A financial services group focused on banking.")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Financial services"
        assert out["sector_confidence"] == "High"

    def test_no_keyword_match_leaves_industry_blank_low_confidence(self):
        ev = [_ev("sector_industry", snippet="Nothing recognizable here.")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] is None
        assert out["sector_confidence"] == "Low"
        assert "No clear sector keywords" in out["sector_reason"]

    def test_company_type_detected(self):
        ev = [_ev("sector_industry",
                  snippet="A pharmaceutical company and subsidiary of "
                          "a multinational group.")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Pharmaceuticals"
        assert out["detected_company_type"] == "Subsidiary"

    def test_public_sector_only_on_clear_government_terms(self):
        ev = [_ev("sector_industry",
                  snippet="A public company serving many customers.")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] != "Public sector / government"

    # -----------------------------------------------------------------
    # Representative B2B/industrial snippets (expanded keyword coverage).
    # -----------------------------------------------------------------

    def test_igm_resins_detects_chemicals(self):
        ev = [_ev("sector_industry",
                  title="IGM Resins | Company profile",
                  snippet="IGM Resins is a global producer of specialty "
                          "chemicals, photoinitiators and UV curing resins "
                          "for coatings, inks and adhesives.",
                  url="https://www.igmresins.com/about")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Chemicals"
        assert out["detected_sub_industry"] in (
            "Specialty chemicals", "Resins", "Coatings", "Adhesives",
            "Inks", "Polymers", "UV curing", "Photoinitiators",
        )
        assert out["sector_confidence"] == "High"
        # Evidence quote/URL/source title/reason are preserved, not invented.
        assert out["sector_evidence_url"] == "https://www.igmresins.com/about"
        assert out["sector_source_title"] == "IGM Resins | Company profile"
        assert "chemicals" in out["sector_reason"].lower() or \
            "specialty chemicals" in out["sector_reason"].lower()

    def test_bauwatch_detects_security_services_site_monitoring(self):
        ev = [_ev("sector_industry",
                  title="BauWatch | About us",
                  snippet="BauWatch provides mobile security services and "
                          "24/7 site monitoring for construction sites "
                          "across Europe.",
                  url="https://www.bauwatch.com/about")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Security services"
        assert out["detected_sub_industry"] == "Site monitoring"
        assert out["sector_confidence"] == "High"
        assert out["sector_evidence_url"] == "https://www.bauwatch.com/about"
        assert out["sector_source_title"] == "BauWatch | About us"

    def test_dorc_detects_healthcare_ophthalmic_devices(self):
        ev = [_ev("sector_industry",
                  title="DORC | Dutch Ophthalmic Research Center",
                  snippet="DORC is a medical technology company "
                          "specializing in ophthalmic devices for eye "
                          "surgery.",
                  url="https://www.dorc.nl/about")]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Healthcare"
        assert out["detected_sub_industry"] in (
            "Medical devices", "Ophthalmic devices", "Medical technology",
        )
        assert out["sector_confidence"] == "High"
        assert out["sector_evidence_url"] == "https://www.dorc.nl/about"
        assert out["sector_source_title"] == "DORC | Dutch Ophthalmic Research Center"

    def test_workday_hosted_platform_snippet_ignored_for_sector(self):
        """Sector evidence from a hosted careers platform (e.g. a Workday job
        posting) must never drive sector detection — it describes the
        platform vendor, not the company (Step 2 upstream fix)."""
        ev = [_ev(
            "sector_industry",
            title="Shimano Careers - Financial Analyst",
            snippet="Join our financial services team supporting banking "
                    "operations on the Workday platform.",
            url="https://shimano.wd3.myworkdayjobs.com/some-posting",
        )]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] is None
        assert out["sector_confidence"] == "Low"
        assert "hosted careers" in out["sector_reason"].lower()

    def test_workday_snippet_ignored_but_other_evidence_still_used(self):
        ev = [
            _ev("sector_industry",
                title="Shimano Careers - Financial Analyst",
                snippet="Join our financial services team on Workday.",
                url="https://shimano.wd3.myworkdayjobs.com/some-posting"),
            _ev("sector_industry",
                title="Shimano | Company profile",
                snippet="Shimano is a manufacturer of bicycle components "
                        "and fishing tackle.",
                url="https://www.shimano.com/en/corporate/"),
        ]
        out = extract_sector_industry(ev)
        assert out["detected_industry"] == "Manufacturing"
        assert out["sector_evidence_url"] == "https://www.shimano.com/en/corporate/"


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

    def test_sector_fields_flow_through_result(self):
        r = self._run(
            [_ev("sector_industry",
                 snippet="Acme is a retail company with stores nationwide.",
                 url="https://acme.com/about")],
            collect_non_hq_evidence=True,
        )
        assert r.detected_industry == "Retail"
        assert r.sector_evidence_url == "https://acme.com/about"
        # Sector detection must not create signals or scores.
        assert r.signals == []
        assert r.final_commercial_fit_score is None

    def test_no_evidence_means_no_sector_fields(self):
        r = self._run([])
        assert r.detected_industry is None
        assert r.sector_confidence is None

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
