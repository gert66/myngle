"""Tests for deep_dive_schema.py — pure dataclass serialization, no I/O."""

from __future__ import annotations

from deep_dive_schema import (
    DEEP_DIVE_CATEGORIES,
    DEEP_DIVE_RETRIEVAL_METHODS,
    DEEP_DIVE_SOURCE_KINDS,
    DeepDiveClaim,
    DeepDiveResult,
)


class TestConstants:
    def test_five_fixed_categories(self):
        assert len(DEEP_DIVE_CATEGORIES) == 5
        assert DEEP_DIVE_CATEGORIES == (
            "hq_structure", "locations", "training_infrastructure",
            "workforce", "recent_developments",
        )

    def test_source_kinds_and_retrieval_methods(self):
        assert DEEP_DIVE_SOURCE_KINDS == ("own_domain", "parent_domain", "external")
        assert DEEP_DIVE_RETRIEVAL_METHODS == ("firecrawl", "serper_localized", "plain_fetch")


class TestDeepDiveClaim:
    def test_to_json_dict_roundtrips_all_fields(self):
        c = DeepDiveClaim(
            claim_id="hq_structure:1", category="hq_structure",
            statement="Acme is headquartered in Munich.",
            quote="Acme GmbH, headquartered in Munich",
            source_url="https://acme.de/about",
            source_title="About Acme",
            source_kind="own_domain",
            domain_verified=True,
            retrieval_method="firecrawl",
        )
        d = c.to_json_dict()
        assert d == {
            "claim_id": "hq_structure:1",
            "category": "hq_structure",
            "statement": "Acme is headquartered in Munich.",
            "quote": "Acme GmbH, headquartered in Munich",
            "source_url": "https://acme.de/about",
            "source_title": "About Acme",
            "source_kind": "own_domain",
            "domain_verified": True,
            "retrieval_method": "firecrawl",
        }

    def test_defaults(self):
        c = DeepDiveClaim(claim_id="x:1", category="workforce",
                          statement="s", quote="q", source_url="https://x.com")
        assert c.source_title is None
        assert c.source_kind == "external"
        assert c.domain_verified is False
        assert c.retrieval_method == "serper_localized"


class TestDeepDiveResult:
    def test_to_json_dict_with_claim_objects(self):
        claim = DeepDiveClaim(claim_id="workforce:1", category="workforce",
                              statement="s", quote="q", source_url="https://acme.com")
        r = DeepDiveResult(
            company_name="Acme", domain="acme.com", parent_domain=None,
            trigger_reason="score_threshold", claims=[claim],
            pages_crawled=[{"url": "https://acme.com", "status": "ok"}],
            firecrawl_used=True, localized_queries_used=[],
            error="", generated_at="2026-07-05T12:00:00+00:00",
        )
        d = r.to_json_dict()
        assert d["company_name"] == "Acme"
        assert d["domain"] == "acme.com"
        assert d["parent_domain"] is None
        assert d["trigger_reason"] == "score_threshold"
        assert d["claims"] == [claim.to_json_dict()]
        assert d["pages_crawled"] == [{"url": "https://acme.com", "status": "ok"}]
        assert d["firecrawl_used"] is True
        assert d["localized_queries_used"] == []
        assert d["error"] == ""
        assert d["generated_at"] == "2026-07-05T12:00:00+00:00"

    def test_to_json_dict_with_empty_defaults(self):
        r = DeepDiveResult(company_name="Acme")
        d = r.to_json_dict()
        assert d["claims"] == []
        assert d["pages_crawled"] == []
        assert d["localized_queries_used"] == []
        assert d["firecrawl_used"] is False
        assert d["error"] == ""

    def test_to_json_dict_accepts_plain_dict_claims(self):
        # Defensive: to_json_dict must not crash if claims were ever plain
        # dicts instead of DeepDiveClaim objects (e.g. round-tripped from
        # Excel/JSON).
        r = DeepDiveResult(company_name="Acme", claims=[{"claim_id": "x:1"}])
        assert r.to_json_dict()["claims"] == [{"claim_id": "x:1"}]

    def test_error_never_prevents_serialization(self):
        r = DeepDiveResult(company_name="Acme", error="deep_dive_failed: boom")
        d = r.to_json_dict()
        assert d["error"] == "deep_dive_failed: boom"
        assert d["claims"] == []
