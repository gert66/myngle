"""Tests for deep_dive_schema.py — pure dataclass serialization, no I/O."""

from __future__ import annotations

from deep_dive_schema import (
    DEEP_DIVE_CATEGORIES,
    DEEP_DIVE_QUOTE_VERIFICATION_STATUSES,
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
            quote="headquartered in Munich",
            source_url="https://acme.de/about",
            source_title="About Acme",
            source_kind="own_domain",
            domain_verified=True,
            retrieval_method="firecrawl",
            quote_verified=True,
            quote_verification_status="verified",
            quote_match_score=1.0,
            quote_matched_snippet="headquartered in munich",
            original_quote="Acme GmbH, headquartered in Munich",
        )
        d = c.to_json_dict()
        assert d == {
            "claim_id": "hq_structure:1",
            "category": "hq_structure",
            "statement": "Acme is headquartered in Munich.",
            "quote": "headquartered in Munich",
            "source_url": "https://acme.de/about",
            "source_title": "About Acme",
            "source_kind": "own_domain",
            "domain_verified": True,
            "retrieval_method": "firecrawl",
            "quote_verified": True,
            "quote_verification_status": "verified",
            "quote_match_score": 1.0,
            "quote_matched_snippet": "headquartered in munich",
            "original_quote": "Acme GmbH, headquartered in Munich",
            "badge": "confirmed",
        }

    def test_defaults(self):
        c = DeepDiveClaim(claim_id="x:1", category="workforce",
                          statement="s", quote="q", source_url="https://x.com")
        assert c.source_title is None
        assert c.source_kind == "external"
        assert c.domain_verified is False
        assert c.retrieval_method == "serper_localized"
        # Quote-verification defaults are "safe unverified", never a false
        # "verified" for a claim built before this field existed.
        assert c.quote_verified is False
        assert c.quote_verification_status == "not_checked"
        assert c.quote_match_score == 0.0
        assert c.quote_matched_snippet == ""
        assert c.original_quote == ""
        assert c.badge == "unconfirmed"
        d = c.to_json_dict()
        assert d["quote_verification_status"] == "not_checked"
        assert d["badge"] == "unconfirmed"


class TestQuoteVerificationBadge:
    def _claim(self, **kw):
        base = dict(claim_id="x:1", category="workforce",
                    statement="s", quote="q", source_url="https://x.com")
        base.update(kw)
        return DeepDiveClaim(**base)

    def test_verified_is_confirmed(self):
        assert self._claim(quote_verification_status="verified").badge == "confirmed"

    def test_verified_corrected_is_confirmed(self):
        assert self._claim(quote_verification_status="verified_corrected").badge == "confirmed"

    def test_high_confidence_fuzzy_match_is_confirmed(self):
        c = self._claim(quote_verification_status="fuzzy_match", quote_match_score=0.9)
        assert c.badge == "confirmed"

    def test_low_confidence_fuzzy_match_is_unconfirmed(self):
        # Defensive: the matcher never actually returns fuzzy_match below
        # the threshold, but the badge logic must not assume that.
        c = self._claim(quote_verification_status="fuzzy_match", quote_match_score=0.5)
        assert c.badge == "unconfirmed"

    def test_not_found_is_unconfirmed(self):
        assert self._claim(quote_verification_status="not_found").badge == "unconfirmed"

    def test_fetch_failed_is_unconfirmed(self):
        assert self._claim(quote_verification_status="fetch_failed").badge == "unconfirmed"

    def test_not_checked_is_unconfirmed(self):
        assert self._claim(quote_verification_status="not_checked").badge == "unconfirmed"


class TestQuoteVerificationStatusesConstant:
    def test_all_six_statuses_present(self):
        assert DEEP_DIVE_QUOTE_VERIFICATION_STATUSES == (
            "verified", "verified_corrected", "fuzzy_match", "not_found",
            "fetch_failed", "not_checked",
        )


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
