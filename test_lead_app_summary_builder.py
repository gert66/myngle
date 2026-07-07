"""Tests for Lead Prioritizer v2 deterministic app/evidence summary builder (Step 4).

No network / live keys. The builders are pure; the core is exercised with the
Serper/AI/collector calls mocked.
"""

from __future__ import annotations

from unittest.mock import patch

from lead_output_schema import LeadEvidence, LeadSignal, LeadInput, HQDetectionResult
from lead_app_summary_builder import (
    build_evidence_summary_app,
    build_key_source_links_app,
    build_advanced_notes_app,
    build_app_summary_fields,
)
from lead_prioritizer_core import prioritize_single_lead


def _sig(name, score=2.0, conf="High", reason="Evidence indicates presence.",
         url="https://s.example/1", title="Sig title", review=False):
    return LeadSignal(
        signal_name=name, signal_value="positive_evidence", signal_score=score,
        signal_confidence=conf, signal_reason=reason, evidence_url=url,
        evidence_quote="snippet", evidence_title=title, query_used="q",
        parser_source="serper_organic_1", needs_manual_review=review,
    )


def _ev(name, title="Ev title", url="https://e.example/1"):
    return LeadEvidence(
        signal_name=name, source_title=title, source_snippet="snippet",
        source_url=url, query_used="q", parser_source="serper_organic_1",
        source_type="organic",
    )


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_all_none_when_empty(self):
        fields = build_app_summary_fields([], [])
        assert fields == {
            "evidence_summary_app": None,
            "key_source_links_app": None,
            "advanced_notes_app": None,
        }


# ---------------------------------------------------------------------------
# evidence_summary_app
# ---------------------------------------------------------------------------

class TestEvidenceSummary:
    def test_includes_supported_signal_reason(self):
        sigs = [_sig("international_profile", reason="global / international presence.")]
        text = build_evidence_summary_app(sigs, [])
        assert "International profile" in text
        assert "score 2" in text
        assert "High confidence" in text
        assert "global / international presence." in text

    def test_one_line_per_signal(self):
        sigs = [_sig("international_profile"), _sig("company_size_complexity", score=1.0, conf="Medium")]
        text = build_evidence_summary_app(sigs, [])
        assert len(text.splitlines()) == 2

    def test_none_when_no_signals(self):
        # Evidence present but no signals → no signal lines → None.
        assert build_evidence_summary_app([], [_ev("international_profile")]) is None


# ---------------------------------------------------------------------------
# key_source_links_app
# ---------------------------------------------------------------------------

class TestKeySourceLinks:
    def test_none_when_no_urls(self):
        assert build_key_source_links_app([], []) is None

    def test_dedup_preserves_order_signals_first(self):
        sigs = [_sig("international_profile", url="https://a.example")]
        evs = [
            _ev("international_profile", url="https://a.example"),  # dup of signal url
            _ev("company_size_complexity", url="https://b.example"),
        ]
        text = build_key_source_links_app(sigs, evs)
        lines = text.splitlines()
        assert lines[0].endswith("https://a.example")
        assert any(l.endswith("https://b.example") for l in lines)
        # a.example appears exactly once
        assert sum(1 for l in lines if l.endswith("https://a.example")) == 1

    def test_respects_max_links(self):
        evs = [_ev("icp_keyword_match", url=f"https://x.example/{i}") for i in range(10)]
        text = build_key_source_links_app([], evs, max_links=3)
        assert len(text.splitlines()) == 3

    def test_unsupported_names_ignored(self):
        evs = [
            _ev("competitor", url="https://competitor.example"),
            _ev("international_profile", url="https://ok.example"),
        ]
        text = build_key_source_links_app([], evs)
        assert "competitor.example" not in text
        assert "ok.example" in text

    def test_does_not_invent_urls(self):
        # Signal with no url and no evidence → None (nothing fabricated).
        sigs = [_sig("international_profile", url=None)]
        assert build_key_source_links_app(sigs, []) is None


# ---------------------------------------------------------------------------
# advanced_notes_app
# ---------------------------------------------------------------------------

class TestAdvancedNotes:
    def test_counts_evidence_and_signals(self):
        sigs = [_sig("international_profile"), _sig("company_size_complexity")]
        evs = [_ev("international_profile"), _ev("company_size_complexity"), _ev("icp_keyword_match")]
        text = build_advanced_notes_app(sigs, evs)
        assert "Non-HQ evidence items: 3." in text
        assert "Extracted signals: 2." in text
        assert "international_profile" in text and "company_size_complexity" in text

    def test_low_confidence_and_review_flags(self):
        sigs = [
            _sig("international_profile", score=0.0, conf="Low", reason="none"),
            _sig("icp_keyword_match", review=True),
        ]
        text = build_advanced_notes_app(sigs, [])
        assert "Low-confidence or zero-score signals: international_profile." in text
        assert "Manual review flagged: icp_keyword_match." in text

    def test_none_when_nothing(self):
        assert build_advanced_notes_app([], []) is None

    def test_competitor_evidence_not_counted(self):
        text = build_advanced_notes_app([], [_ev("competitor"), _ev("international_profile")])
        assert "Non-HQ evidence items: 1." in text


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

    def test_default_does_not_build_summary(self):
        r = self._run(
            [_ev("international_profile", url="https://ok.example")],
            collect_non_hq_evidence=True,
            extract_non_hq_signals_flag=True,
        )
        assert r.evidence_summary_app is None
        assert r.key_source_links_app is None
        assert r.advanced_notes_app is None

    def test_flag_true_builds_summary(self):
        r = self._run(
            [_ev("international_profile", title="Global reach",
                 url="https://ok.example")],
            collect_non_hq_evidence=True,
            extract_non_hq_signals_flag=True,
            build_app_summary_fields_flag=True,
        )
        assert r.evidence_summary_app and "International profile" in r.evidence_summary_app
        assert r.key_source_links_app and "https://ok.example" in r.key_source_links_app
        assert r.advanced_notes_app and "Non-HQ evidence items: 1." in r.advanced_notes_app
        # HQ untouched.
        assert r.hq_structure_type == "domestic"

    def test_summary_from_evidence_without_signals(self):
        # Build flag on, evidence present, but signal extraction OFF → no signals.
        r = self._run(
            [_ev("international_profile", url="https://ok.example")],
            collect_non_hq_evidence=True,
            build_app_summary_fields_flag=True,
        )
        assert r.signals == []
        # evidence_summary_app is signal-based → None, but links/notes come from evidence.
        assert r.evidence_summary_app is None
        assert r.key_source_links_app and "https://ok.example" in r.key_source_links_app
        assert r.advanced_notes_app and "Extracted signals: 0." in r.advanced_notes_app


# ---------------------------------------------------------------------------
# Regression: KeyError 'employer_branding' (full enrichment crash)
# ---------------------------------------------------------------------------

class TestEmployerBrandingLabelRegression:
    """Step 4 crashed with KeyError: 'employer_branding' when the fifth signal
    (added to SUPPORTED_SIGNALS) had no entry in the local label map."""

    def test_employer_branding_signal_does_not_crash_summary(self):
        fields = build_app_summary_fields(
            [_sig("employer_branding", url="https://x.example/careers")], [])
        assert "Employer branding" in fields["evidence_summary_app"]
        assert "https://x.example/careers" in fields["key_source_links_app"]
        assert "employer_branding" in fields["advanced_notes_app"]

    def test_employer_branding_evidence_only_does_not_crash_links(self):
        fields = build_app_summary_fields(
            [], [_ev("employer_branding", url="https://x.example/culture")])
        assert "Employer branding" in fields["key_source_links_app"]

    def test_signals_without_employer_branding_still_work(self):
        fields = build_app_summary_fields(
            [_sig("international_profile"), _sig("icp_keyword_match",
                                                 url="https://y.example/2")], [])
        assert "International profile" in fields["evidence_summary_app"]
        assert "employer" not in fields["evidence_summary_app"].lower()

    def test_unknown_supported_signal_gets_fallback_label(self):
        # Defensive: a future signal name must never crash summary building.
        from lead_app_summary_builder import _signal_label
        assert _signal_label("some_future_signal") == "Some future signal"

    def test_full_enrichment_row_with_employer_branding_evidence(self):
        # End-to-end through the core: the row must enrich, not fall back to
        # an hq_only error row.
        r = self._run_full(
            [_ev("employer_branding", title="Careers",
                 url="https://acme.example/careers"),
             _ev("international_profile", url="https://acme.example/global")],
        )
        assert r.advanced_notes_app is not None
        assert r.key_source_links_app and "Employer branding" in r.key_source_links_app
        # Full enrichment completed: score calculated, employer branding fields
        # emitted with their default (0.0 — evidence had no branding keywords).
        assert r.final_commercial_fit_score is not None
        assert r.sig_employer_branding_score == 0.0

    def _run_full(self, collected_evidence):
        p_serper = patch("lead_prioritizer_core.call_serper_for_hq",
                         return_value={"organic": []})
        p_ai = patch("lead_prioritizer_core.interpret_hq_with_ai",
                     return_value=HQDetectionResult(
                         hq_structure_type="foreign_parent",
                         foreign_hq_simple=True,
                         sig_foreign_hq_score_for_next_scoring=3.0))
        p_collect = patch("lead_prioritizer_core.collect_non_hq_enrichment_evidence",
                          return_value=collected_evidence)
        with p_serper, p_ai, p_collect:
            return prioritize_single_lead(
                LeadInput(company_name="Acme", domain="acme.com",
                          input_country="Uruguay"),
                serper_api_key="fake", anthropic_api_key="fake",
                collect_non_hq_evidence=True,
                extract_non_hq_signals_flag=True,
                build_app_summary_fields_flag=True,
                build_caller_app_fields_flag=True,
                calculate_commercial_score_flag=True,
            )
