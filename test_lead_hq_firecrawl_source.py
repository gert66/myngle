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


class TestCountryLocalizedPaths:
    """Lusha enrichment plan, Stap 1: extra localized candidate paths appended
    for known countries, on top of (never instead of) the English defaults."""

    def test_no_country_keeps_default_six_paths(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.com", "fc-key")
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls == [
            "https://acme.com", "https://acme.com/about", "https://acme.com/about-us",
            "https://acme.com/company", "https://acme.com/company-profile",
            "https://acme.com/en/about",
        ]

    def test_unknown_country_keeps_default_six_paths(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.com", "fc-key", country="Narnia")
        assert post.call_count == 6

    def test_italy_appends_chi_siamo(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.it", "fc-key", country="Italy")
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls[-1] == "https://acme.it/chi-siamo"
        assert len(urls) == 7

    def test_netherlands_appends_over_ons(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.nl", "fc-key", country="Netherlands")
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls[-1] == "https://acme.nl/over-ons"

    def test_germany_appends_ueber_uns(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.de", "fc-key", country="Germany")
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls[-1] == "https://acme.de/ueber-uns"

    def test_france_appends_a_propos(self):
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages("acme.fr", "fc-key", country="France")
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls[-1] == "https://acme.fr/a-propos"

    def test_localized_path_found_within_max_pages_cap(self):
        # All English paths 404; the localized Italian path succeeds and is
        # still found because it's tried within the same max_pages budget.
        four_oh_four = Mock(status_code=404)
        ok = _ok_resp("Chi siamo: parte di un gruppo estero.")
        with patch("deep_dive_runner.requests.post",
                   side_effect=[four_oh_four] * 6 + [ok]):
            out = collect_own_domain_hq_pages(
                "acme.it", "fc-key", country="Italy", max_pages=3)
        assert out["used"] is True
        assert len(out["pages"]) == 1
        assert out["pages"][0]["url"] == "https://acme.it/chi-siamo"

    def test_explicit_candidate_paths_override_ignores_country(self):
        # Existing override behavior stays exactly as before -- country is
        # ignored whenever the caller passes candidate_paths explicitly.
        empty = _ok_resp("   ")
        with patch("deep_dive_runner.requests.post", return_value=empty) as post:
            collect_own_domain_hq_pages(
                "acme.it", "fc-key", country="Italy",
                candidate_paths=("", "/x"), max_pages=5)
        urls = [c.kwargs["json"]["url"] for c in post.call_args_list]
        assert urls == ["https://acme.it", "https://acme.it/x"]
