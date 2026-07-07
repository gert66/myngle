"""Tests for lead_hq_firecrawl_source.py — own-domain Firecrawl HQ crawl.

Firecrawl is mocked via the reused ``deep_dive_runner.requests.post`` boundary
(``_firecrawl_scrape_page`` lives there), mirroring test_deep_dive_runner.py.
No live network.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from lead_hq_firecrawl_source import collect_own_domain_hq_pages


def _ok_resp(markdown: str):
    resp = Mock(status_code=200)
    resp.json.return_value = {"data": {"markdown": markdown}}
    return resp


class TestNoKeyOrDomain:
    def test_missing_key_returns_unused(self):
        out = collect_own_domain_hq_pages("acme.com", "")
        assert out == {"pages": [], "pages_crawled": [], "used": False}

    def test_missing_domain_returns_unused(self):
        out = collect_own_domain_hq_pages("", "fc-key")
        assert out["used"] is False
        assert out["pages"] == []

    def test_none_domain_returns_unused(self):
        out = collect_own_domain_hq_pages(None, "fc-key")
        assert out["used"] is False


class TestSuccessfulCrawl:
    def test_homepage_scraped_and_marked_own_domain(self):
        resp = _ok_resp("FUJIFILM Holdings Corporation, headquartered in Tokyo.")
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_own_domain_hq_pages(
                "fujifilmtilburg.nl", "fc-key", max_pages=1)
        assert out["used"] is True
        assert len(out["pages"]) == 1
        page = out["pages"][0]
        assert page["source_kind"] == "own_domain"
        assert page["retrieval_method"] == "firecrawl"
        assert page["url"] == "https://fujifilmtilburg.nl"
        assert "Tokyo" in page["text"]

    def test_scheme_preserved_when_domain_includes_it(self):
        resp = _ok_resp("content")
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_own_domain_hq_pages(
                "http://acme.com", "fc-key", max_pages=1)
        assert out["pages"][0]["url"] == "http://acme.com"

    def test_respects_max_pages(self):
        resp = _ok_resp("content")
        with patch("deep_dive_runner.requests.post", return_value=resp):
            out = collect_own_domain_hq_pages("acme.com", "fc-key", max_pages=2)
        assert len(out["pages"]) == 2

    def test_uses_custom_candidate_paths(self):
        resp = _ok_resp("content")
        with patch("deep_dive_runner.requests.post", return_value=resp) as post:
            out = collect_own_domain_hq_pages(
                "acme.com", "fc-key", max_pages=5, candidate_paths=("", "/x"))
        assert post.call_count == 2
        urls = [p["url"] for p in out["pages"]]
        assert urls == ["https://acme.com", "https://acme.com/x"]


class TestFailureModes:
    def test_hard_failure_returns_unused_and_stops(self):
        # A 401 (bad/exhausted key) is a hard failure: abandon Firecrawl.
        resp = Mock(status_code=401)
        with patch("deep_dive_runner.requests.post", return_value=resp) as post:
            out = collect_own_domain_hq_pages("acme.com", "fc-key")
        assert out["used"] is False
        assert out["pages"] == []
        # Stopped after the first (homepage) call — never kept probing.
        assert post.call_count == 1

    def test_network_error_is_hard_failure(self):
        with patch("deep_dive_runner.requests.post",
                   side_effect=Exception("boom")):
            out = collect_own_domain_hq_pages("acme.com", "fc-key")
        assert out["used"] is False

    def test_404_pages_are_skipped_not_fatal(self):
        # Homepage 404s, /about succeeds -> still usable, only the good page kept.
        home = Mock(status_code=404)
        about = _ok_resp("About us: part of a foreign group.")
        with patch("deep_dive_runner.requests.post",
                   side_effect=[home, about, home, home, home, home]):
            out = collect_own_domain_hq_pages("acme.com", "fc-key", max_pages=1)
        assert out["used"] is True
        assert len(out["pages"]) == 1
        assert out["pages"][0]["url"] == "https://acme.com/about"

    def test_all_pages_empty_returns_unused(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty):
            out = collect_own_domain_hq_pages("acme.com", "fc-key")
        assert out["used"] is False
        assert out["pages"] == []
        # pages_crawled still records what was attempted.
        assert len(out["pages_crawled"]) >= 1
