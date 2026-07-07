"""Lightweight tests for Lead Prioritizer v2 non-HQ evidence collection (Step 2).

No network / live keys: the Serper call is mocked. Covers the query builder,
the competitor-term exclusion guard, the evidence extractor, and the core flag
gating (default must NOT call non-HQ enrichment).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from lead_output_schema import LeadEvidence, LeadInput
from lead_non_hq_enrichment import (
    build_non_hq_enrichment_queries,
    extract_evidence_from_serper_payload,
    call_serper_for_enrichment,
    collect_non_hq_enrichment_evidence,
    gl_hl_for_country,
)
from lead_prioritizer_core import prioritize_single_lead


# Terms that must never appear in non-HQ enrichment queries.
_FORBIDDEN_TERMS = [
    "competitor", "competitors", "competing", "alternative", "alternatives",
    "vendor", "comparison", "vs ", "rival", "berlitz", "speexx", "learnlight",
    "rapid growth", "fast growing", "fastest growing",
]


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

class TestQueryBuilder:
    def test_uses_domain_root_not_legal_name(self):
        specs = build_non_hq_enrichment_queries("BMW Italia S.p.A.", "www.bmw.it")
        assert specs, "expected query specs"
        for spec in specs:
            assert spec["query"].startswith("bmw "), spec["query"]
            assert "italia" not in spec["query"].lower()
            assert "s.p.a" not in spec["query"].lower()

    def test_falls_back_to_company_name_without_domain(self):
        specs = build_non_hq_enrichment_queries("Some Company", None)
        assert specs
        for spec in specs:
            assert spec["query"].startswith("some company ")

    def test_at_most_six_queries(self):
        specs = build_non_hq_enrichment_queries("Acme", "acme.com")
        assert len(specs) <= 6
        assert {s["signal_name"] for s in specs} == {
            "international_profile", "onboarding_training_need",
            "company_size_complexity", "icp_keyword_match", "employer_branding",
            "sector_industry",
        }

    def test_employer_branding_is_fifth_query(self):
        specs = build_non_hq_enrichment_queries("Acme", "acme.com")
        assert specs[4]["signal_name"] == "employer_branding"
        q = specs[4]["query"].lower()
        assert "employer branding" in q
        assert "employee satisfaction" in q
        assert "workplace culture" in q
        assert "great place to work" in q
        assert "glassdoor" in q

    def test_sector_industry_is_sixth_query(self):
        specs = build_non_hq_enrichment_queries("Acme", "acme.com")
        assert len(specs) == 6
        assert specs[5]["signal_name"] == "sector_industry"
        q = specs[5]["query"].lower()
        assert q.startswith("acme ")
        assert "industry" in q
        assert "sector" in q
        assert "products" in q
        assert "services" in q
        assert "business activity" in q
        assert "company profile" in q

    def test_no_competitor_or_growth_terms(self):
        specs = build_non_hq_enrichment_queries("Acme", "acme.com")
        for spec in specs:
            q = spec["query"].lower()
            for term in _FORBIDDEN_TERMS:
                assert term not in q, f"forbidden term {term!r} in query: {q}"

    def test_empty_input_yields_no_queries(self):
        assert build_non_hq_enrichment_queries("", None) == []

    def test_hosted_platform_domain_uses_tenant_not_platform_label(self):
        """Shimano's Workday-hosted domain must never leak "myworkdayjobs"
        into the non-HQ enrichment queries (Step 1 upstream fix)."""
        specs = build_non_hq_enrichment_queries(
            "Shimano Europe Group", "shimano.wd3.myworkdayjobs.com",
        )
        assert specs
        for spec in specs:
            assert spec["query"].startswith("shimano "), spec["query"]
            assert "myworkdayjobs" not in spec["query"].lower()

    def test_bare_hosted_platform_domain_falls_back_to_company_name(self):
        """A bare hosted-platform domain with no recoverable tenant falls
        back to the company name rather than ever using the platform root."""
        specs = build_non_hq_enrichment_queries("Shimano Europe Group", "boards.greenhouse.io")
        assert specs
        for spec in specs:
            assert spec["query"].startswith("shimano europe group")
            assert "greenhouse" not in spec["query"].lower()


# ---------------------------------------------------------------------------
# Evidence extractor
# ---------------------------------------------------------------------------

class TestEvidenceExtractor:
    _payload = {
        "knowledgeGraph": {
            "title": "Acme Corp",
            "description": "Acme is a global manufacturer.",
            "website": "https://acme.com",
        },
        "answerBox": {
            "title": "Acme locations",
            "answer": "Acme has offices in 12 countries.",
            "link": "https://acme.com/locations",
        },
        "organic": [
            {"title": "Acme careers", "snippet": "Training and onboarding academy.",
             "link": "https://acme.com/careers"},
            {"title": "Acme profile", "snippet": "5,000 employees.",
             "link": "https://example.com/acme"},
        ],
    }

    def test_returns_lead_evidence(self):
        ev = extract_evidence_from_serper_payload(
            self._payload, signal_name="international_profile",
            query_used="acme international offices", max_items=3,
        )
        assert ev and all(isinstance(e, LeadEvidence) for e in ev)

    def test_respects_max_items_and_priority(self):
        ev = extract_evidence_from_serper_payload(
            self._payload, signal_name="international_profile",
            query_used="q", max_items=2,
        )
        assert len(ev) == 2
        # KG first, then answer box.
        assert ev[0].source_type == "knowledge_graph"
        assert ev[1].source_type == "answer_box"

    def test_fields_are_populated_and_verbatim(self):
        ev = extract_evidence_from_serper_payload(
            self._payload, signal_name="company_size_complexity",
            query_used="acme employees", max_items=5,
        )
        first = ev[0]
        assert first.signal_name == "company_size_complexity"
        assert first.query_used == "acme employees"
        assert first.source_snippet == "Acme is a global manufacturer."
        assert first.parser_source == "serper_knowledge_graph"
        assert first.confidence is None  # deterministic collector

    def test_empty_payload_returns_empty(self):
        assert extract_evidence_from_serper_payload({}, "x", "q") == []


# ---------------------------------------------------------------------------
# Onderdeel 3: gl/hl localization
# ---------------------------------------------------------------------------

class TestGlHlLocalization:
    def test_gl_hl_for_country_known_countries(self):
        assert gl_hl_for_country("Netherlands") == ("nl", "nl")
        assert gl_hl_for_country("Italy") == ("it", "it")
        assert gl_hl_for_country("Germany") == ("de", "de")
        assert gl_hl_for_country("France") == ("fr", "fr")
        assert gl_hl_for_country("Spain") == ("es", "es")
        assert gl_hl_for_country("Belgium") == ("be", "nl")

    def test_gl_hl_for_country_is_case_insensitive(self):
        assert gl_hl_for_country("italy") == ("it", "it")
        assert gl_hl_for_country("ITALY") == ("it", "it")

    def test_gl_hl_for_country_unknown_returns_none_none(self):
        assert gl_hl_for_country("Brazil") == (None, None)
        assert gl_hl_for_country(None) == (None, None)
        assert gl_hl_for_country("") == (None, None)

    def test_call_serper_for_enrichment_includes_gl_hl_when_given(self):
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            call_serper_for_enrichment("acme international", "fake-key", gl="it", hl="it")

        assert captured["body"] == {"q": "acme international", "num": 10, "gl": "it", "hl": "it"}

    def test_call_serper_for_enrichment_omits_gl_hl_when_not_given(self):
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            call_serper_for_enrichment("acme international", "fake-key")

        assert captured["body"] == {"q": "acme international", "num": 10}

    def test_collect_non_hq_enrichment_evidence_passes_gl_hl_for_known_country(self):
        calls = []

        def _fake_call(query, serper_api_key, gl=None, hl=None):
            calls.append((gl, hl))
            return {}

        with patch("lead_non_hq_enrichment.call_serper_for_enrichment", side_effect=_fake_call):
            collect_non_hq_enrichment_evidence(
                "Acme", "acme.com", "fake-key", country="Italy",
            )

        assert calls
        assert all(c == ("it", "it") for c in calls)

    def test_collect_non_hq_enrichment_evidence_unknown_country_passes_none(self):
        calls = []

        def _fake_call(query, serper_api_key, gl=None, hl=None):
            calls.append((gl, hl))
            return {}

        with patch("lead_non_hq_enrichment.call_serper_for_enrichment", side_effect=_fake_call):
            collect_non_hq_enrichment_evidence(
                "Acme", "acme.com", "fake-key", country="Brazil",
            )

        assert calls
        assert all(c == (None, None) for c in calls)


# ---------------------------------------------------------------------------
# Core flag gating
# ---------------------------------------------------------------------------

class TestCoreFlagGating:
    _lead = LeadInput(company_name="Acme", domain="acme.com", input_country="Italy")

    def _patches(self, collector_mock):
        return (
            patch("lead_prioritizer_core.call_serper_for_hq", return_value={"organic": []}),
            patch("lead_prioritizer_core.interpret_hq_with_ai",
                  return_value=__import__("lead_output_schema").HQDetectionResult(
                      hq_structure_type="domestic",
                      sig_foreign_hq_score_for_next_scoring=0.0,
                  )),
            patch("lead_prioritizer_core.collect_non_hq_enrichment_evidence",
                  side_effect=collector_mock),
        )

    def test_default_does_not_collect(self):
        calls = {"n": 0}

        def _collector(*a, **k):
            calls["n"] += 1
            return []

        p1, p2, p3 = self._patches(_collector)
        with p1, p2, p3:
            result = prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake",
            )
        assert calls["n"] == 0
        assert result.evidence_items == []

    def test_flag_true_collects(self):
        calls = {"n": 0}
        sample = [LeadEvidence(evidence_id="international_profile:organic:1",
                               signal_name="international_profile")]

        def _collector(*a, **k):
            calls["n"] += 1
            return sample

        p1, p2, p3 = self._patches(_collector)
        with p1, p2, p3:
            result = prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake",
                collect_non_hq_evidence=True,
            )
        assert calls["n"] == 1
        assert len(result.evidence_items) == 1
        # No scores produced by Step 2.
        assert result.sig_international_profile_score is None
        assert result.signals == []

    def test_effective_country_passed_to_collector(self):
        captured = {}

        def _collector(*a, **k):
            captured.update(k)
            return []

        p1, p2, p3 = self._patches(_collector)
        with p1, p2, p3:
            prioritize_single_lead(
                self._lead, serper_api_key="fake", anthropic_api_key="fake",
                collect_non_hq_evidence=True,
            )
        assert captured.get("country") == "Italy"
