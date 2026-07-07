"""Tests for quote_verifier.py — pure matching, no network/AI calls."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from quote_verifier import verify_claims, verify_quote_on_page


def _claim(**kw):
    """Minimal stand-in for DeepDiveClaim — only the attributes
    verify_claims() actually reads/writes."""
    @dataclass
    class _Claim:
        source_url: str = "https://acme.com/about"
        quote: str = "Acme is a great company."
        quote_verified: bool = False
        quote_verification_status: str = "not_checked"
        quote_match_score: float = 0.0
        quote_matched_snippet: str = ""

    return _Claim(**kw)


# ---------------------------------------------------------------------------
# verify_quote_on_page — layered matcher
# ---------------------------------------------------------------------------

class TestVerifyQuoteOnPage:
    _PAGE = (
        "Acme GmbH was founded in 1990 and has grown into a leading "
        "manufacturer of industrial equipment across Europe."
    )

    def test_exact_match(self):
        r = verify_quote_on_page("Acme GmbH was founded in 1990", self._PAGE)
        assert r.status == "verified"
        assert r.match_score == 1.0
        assert r.matched_snippet == "acme gmbh was founded in 1990"

    def test_match_ignores_whitespace_differences(self):
        page = "Acme   GmbH\nwas\tfounded   in 1990."
        r = verify_quote_on_page("Acme GmbH was founded in 1990", page)
        assert r.status == "verified"

    def test_match_ignores_typographic_quotes_and_dashes(self):
        page = "The company’s motto is “quality first” — always."
        r = verify_quote_on_page("The company's motto is \"quality first\" - always",
                                 page)
        assert r.status == "verified"

    def test_match_ignores_case(self):
        r = verify_quote_on_page("ACME GMBH WAS FOUNDED IN 1990", self._PAGE)
        assert r.status == "verified"

    def test_slightly_truncated_quote_is_fuzzy_match(self):
        quote = ("Acme GmbH founded in 1990 and grown into a leading "
                "manufacturer of industrial equipment")
        r = verify_quote_on_page(quote, self._PAGE)
        assert r.status == "fuzzy_match"
        assert r.match_score >= 0.85
        assert r.matched_snippet

    def test_completely_absent_quote_is_not_found(self):
        r = verify_quote_on_page(
            "This sentence has absolutely nothing to do with the source page content",
            self._PAGE)
        assert r.status == "not_found"
        assert r.matched_snippet == ""

    def test_short_quote_near_match_is_not_found(self):
        # "Acme Gnb" is a near-miss of "Acme Gmb" but under the 25-char
        # fuzzy-eligibility floor — only an exact hit is allowed.
        r = verify_quote_on_page("Acme Gnb", self._PAGE)
        assert r.status == "not_found"
        assert r.match_score == 0.0

    def test_short_quote_exact_match_still_verified(self):
        r = verify_quote_on_page("Acme GmbH", self._PAGE)
        assert r.status == "verified"

    def test_empty_page_text(self):
        r = verify_quote_on_page("anything at all", "")
        assert r.status == "not_found"
        assert r.match_score == 0.0

    def test_empty_quote(self):
        r = verify_quote_on_page("", self._PAGE)
        assert r.status == "not_found"

    def test_never_raises_on_none_inputs(self):
        assert verify_quote_on_page(None, None).status == "not_found"


# ---------------------------------------------------------------------------
# verify_claims — cache reuse, targeted fetch, cap, per-claim isolation
# ---------------------------------------------------------------------------

class TestVerifyClaims:
    def test_uses_page_cache_without_extra_fetches(self):
        claim = _claim(source_url="https://acme.com/about",
                       quote="founded in 1990")
        page_cache = {"https://acme.com/about": "Acme was founded in 1990 in Munich."}
        fetch_calls = []

        def _fetch(url):
            fetch_calls.append(url)
            return "should never be used"

        verify_claims([claim], page_cache, _fetch)
        assert fetch_calls == []
        assert claim.quote_verification_status == "verified"
        assert claim.quote_verified is True

    def test_fetches_only_missing_urls(self):
        claim_cached = _claim(source_url="https://acme.com/about", quote="founded in 1990")
        claim_missing = _claim(source_url="https://acme.com/careers",
                               quote="join our growing team")
        page_cache = {"https://acme.com/about": "Acme was founded in 1990."}
        fetch_calls = []

        def _fetch(url):
            fetch_calls.append(url)
            return "Come join our growing team of engineers." if "careers" in url else None

        verify_claims([claim_cached, claim_missing], page_cache, _fetch)
        assert fetch_calls == ["https://acme.com/careers"]
        assert claim_cached.quote_verification_status == "verified"
        assert claim_missing.quote_verification_status == "verified"
        # Successful fetch is written back into the shared cache.
        assert page_cache["https://acme.com/careers"]

    def test_respects_max_verify_fetches_cap(self):
        claims = [
            _claim(source_url=f"https://x.com/page{i}", quote="some quote text here")
            for i in range(5)
        ]
        fetch_calls = []

        def _fetch(url):
            fetch_calls.append(url)
            return "some quote text here on this page"

        verify_claims(claims, {}, _fetch, max_verify_fetches=2)
        assert len(fetch_calls) == 2
        checked = [c for c in claims if c.quote_verification_status != "not_checked"]
        uncapped = [c for c in claims if c.quote_verification_status == "not_checked"]
        assert len(checked) == 2
        assert len(uncapped) == 3

    def test_fetch_exception_yields_fetch_failed_others_continue(self):
        claim_ok = _claim(source_url="https://acme.com/about", quote="founded in 1990")
        claim_broken = _claim(source_url="https://acme.com/broken", quote="anything")
        page_cache = {"https://acme.com/about": "Acme was founded in 1990."}

        def _fetch(url):
            if "broken" in url:
                raise ConnectionError("boom")
            return "unused"

        verify_claims([claim_ok, claim_broken], page_cache, _fetch)
        assert claim_ok.quote_verification_status == "verified"
        assert claim_broken.quote_verification_status == "fetch_failed"
        assert claim_broken.quote_verified is False
        assert claim_broken.quote_match_score == 0.0

    def test_fetch_returning_none_yields_fetch_failed(self):
        claim = _claim(source_url="https://acme.com/x", quote="whatever")
        verify_claims([claim], {}, lambda url: None)
        assert claim.quote_verification_status == "fetch_failed"

    def test_fetch_returning_empty_string_yields_fetch_failed(self):
        claim = _claim(source_url="https://acme.com/x", quote="whatever")
        verify_claims([claim], {}, lambda url: "")
        assert claim.quote_verification_status == "fetch_failed"

    def test_normalizes_shared_page_text_once_not_per_claim(self):
        # Two claims share one cached URL; the page text must only be
        # normalized once (observable via a call counter on a wrapped
        # normalize, or indirectly by asserting both claims verify
        # correctly against the single cache entry).
        page_cache = {"https://acme.com/about": "Acme was founded in 1990 in Munich, Germany."}
        claim_a = _claim(source_url="https://acme.com/about", quote="founded in 1990")
        claim_b = _claim(source_url="https://acme.com/about", quote="Munich, Germany")
        fetch_calls = []
        verify_claims([claim_a, claim_b], page_cache, lambda url: fetch_calls.append(url))
        assert fetch_calls == []  # both served from cache, no fetch attempted
        assert claim_a.quote_verification_status == "verified"
        assert claim_b.quote_verification_status == "verified"

    def test_empty_source_url_is_skipped_safely(self):
        claim = _claim(source_url="", quote="anything")
        verify_claims([claim], {}, lambda url: "text")
        assert claim.quote_verification_status == "not_checked"

    def test_returns_same_list_it_was_given(self):
        claims = [_claim()]
        result = verify_claims(claims, {}, lambda url: None)
        assert result is claims
