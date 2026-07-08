"""Tests for lead_public_source_signal_enrichment.py — optional, evidence-only
Public Source Signal Enrichment.

Firecrawl is mocked via the reused ``deep_dive_runner.requests.post`` boundary
(``_firecrawl_scrape_page`` lives there), mirroring test_lead_hq_firecrawl_source.py
and test_deep_dive_runner.py. No live network.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from lead_output_schema import LeadEvidence
from lead_public_source_signal_enrichment import (
    collect_public_source_signal_evidence,
    _is_blocked_public_source,
    _build_candidate_urls,
    _extract_snippet,
)


def _ok_resp(markdown: str):
    resp = Mock(status_code=200)
    resp.json.return_value = {"data": {"markdown": markdown}}
    return resp


# ---------------------------------------------------------------------------
# 1. Feature disabled / missing required inputs -> no evidence
# ---------------------------------------------------------------------------

class TestMissingRequiredInputs:
    def test_missing_company_name_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "", "acme.com", "vacancies", "https://kvk.nl/zoeken", "fc-key")
        assert out == []

    def test_missing_signal_query_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "Acme BV", "acme.com", "", "https://kvk.nl/zoeken", "fc-key")
        assert out == []

    def test_missing_firecrawl_key_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken", "")
        assert out == []

    def test_no_network_call_made_when_inputs_missing(self):
        with patch("deep_dive_runner.requests.post") as post:
            out = collect_public_source_signal_evidence(
                "", None, "vacancies", "https://kvk.nl/zoeken", "fc-key")
        assert out == []
        post.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Missing source URL -> no evidence
# ---------------------------------------------------------------------------

class TestMissingSourceUrl:
    def test_blank_source_base_url_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "Acme BV", "acme.com", "vacancies", "", "fc-key")
        assert out == []

    def test_none_source_base_url_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "Acme BV", "acme.com", "vacancies", None, "fc-key")
        assert out == []

    def test_whitespace_only_source_base_url_returns_no_evidence(self):
        out = collect_public_source_signal_evidence(
            "Acme BV", "acme.com", "vacancies", "   ", "fc-key")
        assert out == []


# ---------------------------------------------------------------------------
# 3. Blocked social/professional-network source -> no evidence, no call made
# ---------------------------------------------------------------------------

class TestBlockedSocialSource:
    def test_linkedin_is_not_blocked(self):
        # Deliberately removed from _BLOCKED_SOURCE_DOMAINS in a later commit
        # ("Remove 'linkedin.com' from blocked source domains") -- documented
        # here so the exception is visible, not silently untested.
        assert not _is_blocked_public_source("https://www.linkedin.com/company/acme")

    def test_facebook_is_blocked(self):
        assert _is_blocked_public_source("https://facebook.com/acme")

    def test_glassdoor_is_blocked(self):
        assert _is_blocked_public_source("https://www.glassdoor.com/acme")

    def test_hosted_ats_platform_is_blocked(self):
        # Reuses the existing hosted-careers-platform guard from the rest
        # of the v2 pipeline (Workday, Greenhouse, ...).
        assert _is_blocked_public_source("https://acme.wd3.myworkdayjobs.com")

    def test_ordinary_public_source_is_not_blocked(self):
        assert not _is_blocked_public_source("https://kvk.nl/zoeken")

    def test_glassdoor_source_returns_no_evidence_and_makes_no_call(self):
        with patch("deep_dive_runner.requests.post") as post:
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies",
                "https://www.glassdoor.com/acme", "fc-key")
        assert out == []
        post.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Firecrawl hard failure -> no evidence, never raises
# ---------------------------------------------------------------------------

class TestFirecrawlHardFailure:
    def test_401_returns_no_evidence(self):
        resp = Mock(status_code=401)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "bad-key")
        assert out == []

    def test_403_returns_no_evidence(self):
        resp = Mock(status_code=403)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key")
        assert out == []

    def test_429_returns_no_evidence(self):
        resp = Mock(status_code=429)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key")
        assert out == []

    def test_network_error_returns_no_evidence_without_raising(self):
        with patch("deep_dive_runner.requests.post",
                   side_effect=Exception("network boom")):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key")
        assert out == []

    def test_hard_failure_stops_after_first_candidate(self):
        resp = Mock(status_code=401)
        with patch("deep_dive_runner.requests.post", return_value=resp) as post:
            collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key", max_pages=3)
        assert post.call_count == 1  # abandoned entirely, never kept probing


# ---------------------------------------------------------------------------
# 5. Successful retrieval -> LeadEvidence with signal_name public_source_signal
# ---------------------------------------------------------------------------

class TestSuccessfulRetrieval:
    def test_match_creates_lead_evidence_with_expected_fields(self):
        resp = _ok_resp("Acme BV currently has 5 open vacancies in Amsterdam.")
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key", source_label="KvK register", max_pages=1)
        assert len(out) == 1
        ev = out[0]
        assert isinstance(ev, LeadEvidence)
        assert ev.signal_name == "public_source_signal"
        assert ev.source_type == "public_source"
        assert ev.parser_source == "firecrawl_public_source"
        assert ev.source_url == "https://kvk.nl/zoeken"
        assert "vacancies" in ev.source_snippet.lower()
        assert "Acme BV" in ev.query_used
        assert "vacancies" in ev.query_used
        assert "https://kvk.nl/zoeken" in ev.query_used
        assert "KvK register" in ev.notes
        assert "retrieval_status=ok" in ev.notes
        assert ev.source_title == "KvK register"

    def test_no_match_returns_no_evidence_without_error(self):
        resp = _ok_resp("This page has nothing relevant at all.")
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key")
        assert out == []

    def test_404_candidate_is_skipped_not_fatal(self):
        home = Mock(status_code=404)
        match = _ok_resp("Acme BV: 3 vacancies open now.")
        with patch("deep_dive_runner.requests.post",
                   side_effect=[home, match]):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key", max_pages=2)
        assert len(out) == 1

    def test_respects_max_pages_candidate_cap(self):
        resp = _ok_resp("no match here")
        with patch("deep_dive_runner.requests.post", return_value=resp) as post:
            collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key", max_pages=2)
        assert post.call_count == 2

    def test_never_raises_on_unexpected_exception(self):
        # Defensive top-level guard: even a totally unexpected exception path
        # must yield [] rather than propagate.
        with patch("deep_dive_runner.requests.post",
                   side_effect=RuntimeError("unexpected")):
            out = collect_public_source_signal_evidence(
                "Acme BV", "acme.com", "vacancies", "https://kvk.nl/zoeken",
                "fc-key")
        assert out == []


# ---------------------------------------------------------------------------
# Helper unit coverage: candidate URL building stays within the configured
# public source boundary; snippet extraction.
# ---------------------------------------------------------------------------

class TestBuildCandidateUrls:
    def test_base_url_always_included(self):
        urls = _build_candidate_urls("https://kvk.nl/zoeken", "Acme BV", 1)
        assert urls == ["https://kvk.nl/zoeken"]

    def test_adds_scheme_when_missing(self):
        urls = _build_candidate_urls("kvk.nl/zoeken", "Acme BV", 1)
        assert urls == ["https://kvk.nl/zoeken"]

    def test_all_candidates_share_the_configured_host(self):
        urls = _build_candidate_urls("https://kvk.nl/zoeken", "Acme BV", 3)
        assert len(urls) == 3
        for u in urls:
            assert u.startswith("https://kvk.nl/zoeken")

    def test_max_pages_one_yields_single_candidate_even_with_company_name(self):
        urls = _build_candidate_urls("https://kvk.nl/zoeken", "Acme BV", 1)
        assert len(urls) == 1


class TestExtractSnippet:
    def test_returns_none_when_keyword_absent(self):
        assert _extract_snippet("nothing relevant here", "vacancies") is None

    def test_returns_context_around_match(self):
        snippet = _extract_snippet(
            "Acme BV currently has 5 open vacancies in Amsterdam right now.",
            "vacancies")
        assert snippet is not None
        assert "vacancies" in snippet.lower()

    def test_empty_text_returns_none(self):
        assert _extract_snippet("", "vacancies") is None
