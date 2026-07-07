"""Tests for deep_dive_runner.py (Step B, no live network/API calls).

Firecrawl is mocked via ``requests.post``; Serper/plain-fetch via their own
module functions; Anthropic via ``_anthropic_lib``, same pattern as the
other composer test files.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from deep_dive_schema import DeepDiveClaim
from deep_dive_runner import (
    DEFAULT_DEEP_DIVE_MODEL,
    _apply_quote_correction,
    _classify_source_kind,
    _collect_pages_via_fallback,
    _collect_pages_via_firecrawl,
    _distill_claims,
    _fetch_page_for_verification,
    _firecrawl_scrape_page,
    _reextract_not_found_quotes,
    _self_heal_claims,
    _validate_and_build_claims,
    build_deep_dive_prompt,
    gl_hl_for_country,
    run_deep_dive,
)


def _mock_anthropic(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("deep_dive_runner._anthropic_lib", lib)


def _mock_anthropic_sequence(*texts: str):
    """Mock the Anthropic client to return a different response per call,
    in order — used for tests exercising distillation THEN re-extraction."""
    messages = []
    for text in texts:
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        messages.append(msg)
    client = MagicMock()
    client.messages.create.side_effect = messages
    lib = MagicMock()
    lib.Anthropic.return_value = client
    return patch("deep_dive_runner._anthropic_lib", lib), client


# ---------------------------------------------------------------------------
# gl/hl localization — never a hardcoded gl=us default.
# ---------------------------------------------------------------------------

class TestGlHlForCountry:
    def test_known_country(self):
        assert gl_hl_for_country("Italy") == ("it", "it")
        assert gl_hl_for_country("netherlands") == ("nl", "nl")

    def test_unknown_country_omits_gl_hl(self):
        assert gl_hl_for_country("Atlantis") == ("", "")
        assert gl_hl_for_country(None) == ("", "")
        assert gl_hl_for_country("") == ("", "")

    def test_case_and_whitespace_insensitive(self):
        assert gl_hl_for_country("  ITALY  ") == ("it", "it")


# ---------------------------------------------------------------------------
# Firecrawl page scraping — request/response shape, hard vs. soft failures.
# ---------------------------------------------------------------------------

class TestFirecrawlScrapePage:
    def test_success_returns_markdown_text(self):
        resp = Mock(status_code=200)
        resp.json.return_value = {"data": {"markdown": "# About Acme\nWe are Acme."}}
        with patch("deep_dive_runner.requests.post", return_value=resp):
            result = _firecrawl_scrape_page("https://acme.com/about", "fc-key")
        assert result == {"ok": True, "text": "# About Acme\nWe are Acme.",
                          "status": "ok", "hard_failure": False}

    def test_empty_markdown_is_not_ok(self):
        resp = Mock(status_code=200)
        resp.json.return_value = {"data": {"markdown": "   "}}
        with patch("deep_dive_runner.requests.post", return_value=resp):
            result = _firecrawl_scrape_page("https://acme.com/about", "fc-key")
        assert result["ok"] is False

    def test_404_is_soft_failure(self):
        resp = Mock(status_code=404)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            result = _firecrawl_scrape_page("https://acme.com/nope", "fc-key")
        assert result == {"ok": False, "text": "", "status": "404", "hard_failure": False}

    @pytest.mark.parametrize("code", [401, 402, 403, 429])
    def test_key_failure_codes_are_hard_failures(self, code):
        resp = Mock(status_code=code)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            result = _firecrawl_scrape_page("https://acme.com", "bad-key")
        assert result["hard_failure"] is True
        assert result["status"] == f"http_{code}"

    def test_other_http_error_is_soft_failure(self):
        resp = Mock(status_code=500)
        with patch("deep_dive_runner.requests.post", return_value=resp):
            result = _firecrawl_scrape_page("https://acme.com", "fc-key")
        assert result["hard_failure"] is False
        assert result["status"] == "http_500"

    def test_network_exception_is_hard_failure(self):
        with patch("deep_dive_runner.requests.post", side_effect=ConnectionError("boom")):
            result = _firecrawl_scrape_page("https://acme.com", "fc-key")
        assert result["hard_failure"] is True
        assert "boom" in result["status"]


# ---------------------------------------------------------------------------
# Firecrawl page collection (own + parent domain, capped, hard-failure abort)
# ---------------------------------------------------------------------------

class TestCollectPagesViaFirecrawl:
    def test_no_domains_returns_unused(self):
        out = _collect_pages_via_firecrawl(None, None, "fc-key", 6)
        assert out == {"pages": [], "pages_crawled": [], "used": False}

    def test_collects_pages_from_own_and_parent_domain(self):
        def _fake_scrape(url, key, timeout=15):
            if url.endswith("/about"):
                return {"ok": True, "text": f"content for {url}", "status": "ok", "hard_failure": False}
            return {"ok": False, "text": "", "status": "404", "hard_failure": False}

        with patch("deep_dive_runner._firecrawl_scrape_page", side_effect=_fake_scrape):
            out = _collect_pages_via_firecrawl("acme.com", "parentco.com", "fc-key", max_pages=6)

        assert out["used"] is True
        urls = {p["url"] for p in out["pages"]}
        assert "https://acme.com/about" in urls
        assert "https://parentco.com/about" in urls
        kinds = {p["url"]: p["source_kind"] for p in out["pages"]}
        assert kinds["https://acme.com/about"] == "own_domain"
        assert kinds["https://parentco.com/about"] == "parent_domain"
        assert all(p["retrieval_method"] == "firecrawl" for p in out["pages"])

    def test_hard_failure_marks_unused_even_with_partial_pages(self):
        # A hard failure mid-crawl must mark used=False regardless of
        # whatever pages were already collected -- the caller (run_deep_dive)
        # is responsible for ignoring `pages` entirely when used is False.
        calls = []

        def _fake_scrape(url, key, timeout=15):
            calls.append(url)
            if len(calls) == 1:
                return {"ok": True, "text": "homepage text", "status": "ok", "hard_failure": False}
            return {"ok": False, "text": "", "status": "http_402", "hard_failure": True}

        with patch("deep_dive_runner._firecrawl_scrape_page", side_effect=_fake_scrape):
            out = _collect_pages_via_firecrawl("acme.com", None, "fc-key", max_pages=6)
        assert out["used"] is False

    def test_respects_max_pages_cap(self):
        with patch("deep_dive_runner._firecrawl_scrape_page",
                   return_value={"ok": True, "text": "x", "status": "ok", "hard_failure": False}):
            out = _collect_pages_via_firecrawl("acme.com", "parentco.com", "fc-key", max_pages=2)
        assert len(out["pages"]) == 2

    def test_all_404_yields_unused(self):
        with patch("deep_dive_runner._firecrawl_scrape_page",
                   return_value={"ok": False, "text": "", "status": "404", "hard_failure": False}):
            out = _collect_pages_via_firecrawl("acme.com", None, "fc-key", max_pages=6)
        assert out["used"] is False
        assert out["pages"] == []


# ---------------------------------------------------------------------------
# Fallback collection: localized Serper + bare fetch.
# ---------------------------------------------------------------------------

class TestCollectPagesViaFallback:
    def test_uses_localized_gl_hl_from_country(self):
        seen_gl_hl = []

        def _fake_serper(query, key, gl="", hl=""):
            seen_gl_hl.append((gl, hl))
            return {}

        with patch("deep_dive_runner._call_serper_localized", side_effect=_fake_serper), \
             patch("deep_dive_runner._plain_fetch", return_value=""):
            _collect_pages_via_fallback(
                company_name="Acme", domain="acme.it", parent_domain=None,
                parent_company=None, country="Italy", serper_api_key="k", max_pages=6)
        assert seen_gl_hl and all(gh == ("it", "it") for gh in seen_gl_hl)

    def test_collects_evidence_and_homepage_fetch(self):
        def _fake_serper(query, key, gl="", hl=""):
            return {"organic": [{"title": "About Acme", "snippet": "Acme overview text.",
                                  "link": "https://acme.com/about"}]}

        with patch("deep_dive_runner._call_serper_localized", side_effect=_fake_serper), \
             patch("deep_dive_runner._plain_fetch", return_value="Acme homepage text."):
            out = _collect_pages_via_fallback(
                company_name="Acme", domain="acme.com", parent_domain=None,
                parent_company=None, country=None, serper_api_key="k", max_pages=6)

        methods = {p["retrieval_method"] for p in out["pages"]}
        assert "serper_localized" in methods
        assert "plain_fetch" in methods
        assert out["localized_queries_used"]

    def test_no_serper_key_still_attempts_plain_fetch(self):
        with patch("deep_dive_runner._call_serper_localized", return_value={}), \
             patch("deep_dive_runner._plain_fetch", return_value="Homepage text."):
            out = _collect_pages_via_fallback(
                company_name="Acme", domain="acme.com", parent_domain="parentco.com",
                parent_company="Parentco", country=None, serper_api_key="", max_pages=6)
        urls = {p["url"] for p in out["pages"]}
        assert any("acme.com" in u for u in urls)
        assert any("parentco.com" in u for u in urls)

    def test_respects_max_pages_cap(self):
        def _fake_serper(query, key, gl="", hl=""):
            return {"organic": [
                {"title": "T1", "snippet": "s1", "link": "https://x.com/1"},
                {"title": "T2", "snippet": "s2", "link": "https://x.com/2"},
            ]}

        with patch("deep_dive_runner._call_serper_localized", side_effect=_fake_serper), \
             patch("deep_dive_runner._plain_fetch", return_value=""):
            out = _collect_pages_via_fallback(
                company_name="Acme", domain=None, parent_domain=None,
                parent_company=None, country=None, serper_api_key="k", max_pages=2)
        assert len(out["pages"]) <= 2


# ---------------------------------------------------------------------------
# Source-kind classification (reuses lead_hq_ai_interpreter host-match logic)
# ---------------------------------------------------------------------------

class TestClassifySourceKind:
    def test_own_domain_match(self):
        assert _classify_source_kind("acme.com", "acme.com", None) == "own_domain"
        assert _classify_source_kind("www.acme.com".replace("www.", ""), "acme.com", None) == "own_domain"

    def test_parent_domain_match(self):
        assert _classify_source_kind("parentco.com", "acme.com", "parentco.com") == "parent_domain"

    def test_external_when_neither_matches(self):
        assert _classify_source_kind("thirdparty.com", "acme.com", "parentco.com") == "external"

    def test_subdomain_of_own_domain_matches(self):
        assert _classify_source_kind("careers.acme.com", "acme.com", None) == "own_domain"


# ---------------------------------------------------------------------------
# Claim validation — drop hallucinated URLs, invalid categories, hosted
# platform sources; correctly assign source_kind/domain_verified.
# ---------------------------------------------------------------------------

class TestValidateAndBuildClaims:
    _URLS = {
        "https://acme.com/about": "firecrawl",
        "https://parentco.com/about": "serper_localized",
        "https://news.example.com/article": "serper_localized",
    }
    _TITLES = {
        "https://acme.com/about": "About Acme",
        "https://parentco.com/about": None,
        "https://news.example.com/article": "Industry news",
    }

    def test_valid_claim_own_domain_verified(self):
        raw = [{"category": "hq_structure", "statement": "s", "quote": "q",
               "source_url": "https://acme.com/about"}]
        claims = _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", "parentco.com")
        assert len(claims) == 1
        c = claims[0]
        assert c.source_kind == "own_domain"
        assert c.domain_verified is True
        assert c.retrieval_method == "firecrawl"
        assert c.claim_id == "hq_structure:1"

    def test_parent_domain_claim(self):
        raw = [{"category": "workforce", "statement": "s", "quote": "q",
               "source_url": "https://parentco.com/about"}]
        claims = _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", "parentco.com")
        assert claims[0].source_kind == "parent_domain"
        assert claims[0].domain_verified is True

    def test_external_claim_not_domain_verified(self):
        raw = [{"category": "recent_developments", "statement": "s", "quote": "q",
               "source_url": "https://news.example.com/article"}]
        claims = _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", "parentco.com")
        assert claims[0].source_kind == "external"
        assert claims[0].domain_verified is False

    def test_invalid_category_dropped(self):
        raw = [{"category": "financials", "statement": "s", "quote": "q",
               "source_url": "https://acme.com/about"}]
        assert _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", None) == []

    def test_missing_required_field_dropped(self):
        raw = [{"category": "hq_structure", "statement": "", "quote": "q",
               "source_url": "https://acme.com/about"}]
        assert _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", None) == []

    def test_hallucinated_url_not_in_supplied_material_dropped(self):
        raw = [{"category": "hq_structure", "statement": "s", "quote": "q",
               "source_url": "https://invented-by-model.com/fake"}]
        assert _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", None) == []

    def test_hosted_platform_url_dropped_even_if_supplied(self):
        urls = dict(self._URLS)
        urls["https://acme.wd3.myworkdayjobs.com/en-US/Careers"] = "firecrawl"
        raw = [{"category": "workforce", "statement": "s", "quote": "q",
               "source_url": "https://acme.wd3.myworkdayjobs.com/en-US/Careers"}]
        assert _validate_and_build_claims(raw, urls, self._TITLES, "acme.com", None) == []

    def test_non_dict_items_ignored(self):
        assert _validate_and_build_claims(["not-a-dict"], self._URLS, self._TITLES, "acme.com", None) == []

    def test_claim_ids_increment_per_category(self):
        raw = [
            {"category": "workforce", "statement": "s1", "quote": "q1",
             "source_url": "https://acme.com/about"},
            {"category": "workforce", "statement": "s2", "quote": "q2",
             "source_url": "https://parentco.com/about"},
        ]
        claims = _validate_and_build_claims(raw, self._URLS, self._TITLES, "acme.com", "parentco.com")
        assert [c.claim_id for c in claims] == ["workforce:1", "workforce:2"]


# ---------------------------------------------------------------------------
# AI distillation — success / error / unparseable, same tolerant-parsing
# pattern as the other composer modules.
# ---------------------------------------------------------------------------

_PAGES = [
    {"url": "https://acme.com/about", "title": "About Acme",
     "text": "Acme GmbH is headquartered in Munich, Germany.",
     "source_kind": "own_domain", "retrieval_method": "firecrawl"},
]


class TestDistillClaims:
    def test_no_api_key(self):
        claims, error = _distill_claims(
            company_name="Acme", country="Germany", domain="acme.com",
            parent_company=None, parent_domain=None, pages=_PAGES,
            anthropic_api_key="", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert claims == [] and error == "no_anthropic_api_key"

    def test_successful_response_mapped(self):
        payload = json.dumps({"claims": [
            {"category": "hq_structure", "statement": "Acme is headquartered in Munich.",
             "quote": "headquartered in Munich, Germany", "source_url": "https://acme.com/about"},
        ]})
        with _mock_anthropic(payload):
            claims, error = _distill_claims(
                company_name="Acme", country="Germany", domain="acme.com",
                parent_company=None, parent_domain=None, pages=_PAGES,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert error == ""
        assert len(claims) == 1
        assert claims[0].category == "hq_structure"
        assert claims[0].source_kind == "own_domain"

    def test_fenced_json_is_stripped(self):
        payload = "```json\n" + json.dumps({"claims": [
            {"category": "workforce", "statement": "s", "quote": "q",
             "source_url": "https://acme.com/about"},
        ]}) + "\n```"
        with _mock_anthropic(payload):
            claims, error = _distill_claims(
                company_name="Acme", country=None, domain="acme.com",
                parent_company=None, parent_domain=None, pages=_PAGES,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert error == "" and len(claims) == 1

    def test_api_error_yields_call_failed(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("deep_dive_runner._anthropic_lib", lib):
            claims, error = _distill_claims(
                company_name="Acme", country=None, domain="acme.com",
                parent_company=None, parent_domain=None, pages=_PAGES,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert claims == []
        assert error.startswith("deep_dive_call_failed")
        assert "connection reset" in error

    def test_unparseable_response_yields_parse_failed(self):
        with _mock_anthropic("I cannot produce JSON for this."):
            claims, error = _distill_claims(
                company_name="Acme", country=None, domain="acme.com",
                parent_company=None, parent_domain=None, pages=_PAGES,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert claims == [] and error == "deep_dive_parse_failed"

    def test_claims_not_a_list_yields_parse_failed(self):
        with _mock_anthropic(json.dumps({"claims": "not-a-list"})):
            claims, error = _distill_claims(
                company_name="Acme", country=None, domain="acme.com",
                parent_company=None, parent_domain=None, pages=_PAGES,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert claims == [] and error == "deep_dive_parse_failed"


class TestBuildDeepDivePrompt:
    def test_prompt_includes_pages_and_categories(self):
        prompt = build_deep_dive_prompt(
            company_name="Acme", country="Germany", domain="acme.com",
            parent_company=None, parent_domain=None, pages=_PAGES)
        assert "Acme" in prompt
        assert "https://acme.com/about" in prompt
        assert "hq_structure" in prompt

    def test_prompt_with_no_pages_shows_none(self):
        prompt = build_deep_dive_prompt(
            company_name="Acme", country=None, domain=None,
            parent_company=None, parent_domain=None, pages=[])
        assert "(none)" in prompt


# ---------------------------------------------------------------------------
# run_deep_dive — end-to-end, never raises.
# ---------------------------------------------------------------------------

class TestRunDeepDive:
    def test_firecrawl_success_path(self):
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": list(_PAGES), "pages_crawled": [{"url": "x", "status": "ok"}],
                                "used": True}), \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s", "quote": "q",
                  "source_url": "https://acme.com/about"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", country="Germany",
                trigger_reason="score_threshold", serper_api_key="s",
                anthropic_api_key="fake", firecrawl_api_key="fc-key")
        assert result.firecrawl_used is True
        assert result.error == ""
        assert len(result.claims) == 1
        assert result.trigger_reason == "score_threshold"

    def test_no_firecrawl_key_uses_fallback(self):
        with patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": list(_PAGES), "localized_queries_used": ["q1"]}) as m_fb, \
             _mock_anthropic(json.dumps({"claims": []})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", country="Germany",
                trigger_reason="foreign_hq", serper_api_key="s",
                anthropic_api_key="fake", firecrawl_api_key="")
        m_fb.assert_called_once()
        assert result.firecrawl_used is False
        assert result.localized_queries_used == ["q1"]

    def test_firecrawl_hard_failure_falls_back(self):
        # Even though the mocked firecrawl collector returns a partial page
        # (collected before the hard failure), used=False means run_deep_dive
        # must discard it entirely and use only the fallback's pages.
        partial_page = {"url": "https://acme.com", "title": None, "text": "partial",
                        "source_kind": "own_domain", "retrieval_method": "firecrawl"}
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": [partial_page],
                                "pages_crawled": [{"url": "x", "status": "http_402"}],
                                "used": False}), \
             patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": list(_PAGES), "localized_queries_used": ["q1"]}) as m_fb, \
             patch("deep_dive_runner.build_deep_dive_prompt",
                   side_effect=lambda **kw: build_deep_dive_prompt(**kw)) as m_prompt, \
             _mock_anthropic(json.dumps({"claims": []})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", country=None,
                trigger_reason="manual", serper_api_key="s",
                anthropic_api_key="fake", firecrawl_api_key="fc-key")
        m_fb.assert_called_once()
        assert result.firecrawl_used is False
        prompted_urls = [p["url"] for p in m_prompt.call_args.kwargs["pages"]]
        assert "https://acme.com" not in prompted_urls  # the discarded partial page
        assert _PAGES[0]["url"] in prompted_urls

    def test_hosted_platform_page_excluded_before_distillation(self):
        pages = list(_PAGES) + [{
            "url": "https://acme.wd3.myworkdayjobs.com/en-US/Careers",
            "title": None, "text": "career listings", "source_kind": "own_domain",
            "retrieval_method": "firecrawl",
        }]
        captured_prompt = {}

        def _capture(**kwargs):
            captured_prompt.update(kwargs)
            return build_deep_dive_prompt(**kwargs)

        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             patch("deep_dive_runner.build_deep_dive_prompt", side_effect=_capture), \
             _mock_anthropic(json.dumps({"claims": []})):
            run_deep_dive(
                company_name="Acme", domain="acme.com", country=None,
                trigger_reason="manual", serper_api_key="s",
                anthropic_api_key="fake", firecrawl_api_key="fc-key")
        prompted_urls = [p["url"] for p in captured_prompt.get("pages", [])]
        assert "https://acme.wd3.myworkdayjobs.com/en-US/Careers" not in prompted_urls

    def test_no_pages_collected_yields_empty_claims_no_error(self):
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": [], "pages_crawled": [], "used": False}), \
             patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": [], "localized_queries_used": []}):
            result = run_deep_dive(
                company_name="Acme", domain=None, country=None,
                trigger_reason="manual", serper_api_key="", anthropic_api_key="")
        assert result.claims == []
        assert result.error == ""

    def test_no_anthropic_key_records_error(self):
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": [], "pages_crawled": [], "used": False}), \
             patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": list(_PAGES), "localized_queries_used": []}):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", country=None,
                trigger_reason="manual", serper_api_key="s", anthropic_api_key="")
        assert result.claims == []
        assert result.error == "no_anthropic_api_key"

    def test_unexpected_exception_never_raises(self):
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   side_effect=RuntimeError("unexpected boom")):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", country=None,
                trigger_reason="manual", serper_api_key="s", anthropic_api_key="a",
                firecrawl_api_key="fc-key")
        assert result.error.startswith("deep_dive_failed")
        assert "unexpected boom" in result.error
        assert result.claims == []

    def test_generated_at_is_set(self):
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": [], "pages_crawled": [], "used": False}), \
             patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": [], "localized_queries_used": []}):
            result = run_deep_dive(
                company_name="Acme", domain=None, country=None,
                trigger_reason="manual", serper_api_key="", anthropic_api_key="")
        assert result.generated_at
        assert result.company_name == "Acme"

    def test_result_never_raises_and_always_returns_deep_dive_result(self):
        from deep_dive_schema import DeepDiveResult
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   side_effect=Exception("anything")):
            result = run_deep_dive(company_name="Acme", firecrawl_api_key="fc")
        assert isinstance(result, DeepDiveResult)


# ---------------------------------------------------------------------------
# Quote verification wired into run_deep_dive — page-cache construction
# (serper_localized "pages" are snippets, not full text) and verify_quotes
# on/off.
# ---------------------------------------------------------------------------

_FULL_PAGE_TEXT = (
    "Acme GmbH was founded in 1990 and has grown into a leading "
    "manufacturer of industrial equipment across Europe."
)


class TestRunDeepDiveQuoteVerificationWiring:
    def test_verify_quotes_true_by_default_populates_status(self):
        pages = [{"url": "https://acme.com/about", "title": None, "text": _FULL_PAGE_TEXT,
                 "source_kind": "own_domain", "retrieval_method": "firecrawl"}]
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s",
                  "quote": "Acme GmbH was founded in 1990",
                  "source_url": "https://acme.com/about"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="fc-key")
        assert result.claims[0].quote_verification_status == "verified"
        assert result.claims[0].quote_verified is True

    def test_verify_quotes_false_leaves_not_checked(self):
        pages = [{"url": "https://acme.com/about", "title": None, "text": _FULL_PAGE_TEXT,
                 "source_kind": "own_domain", "retrieval_method": "firecrawl"}]
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s",
                  "quote": "Acme GmbH was founded in 1990",
                  "source_url": "https://acme.com/about"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="fc-key", verify_quotes=False)
        assert result.claims[0].quote_verification_status == "not_checked"
        assert result.claims[0].quote_verified is False

    def test_serper_localized_page_snippet_not_used_as_full_text_cache(self):
        # A serper_localized "page" only ever holds a short Serper snippet
        # (not the full page) -- verification must trigger a fresh fetch
        # for it rather than trusting the snippet as ground truth.
        snippet_pages = [{"url": "https://acme.com/careers", "title": "Careers",
                          "text": "Join our growing team today.",
                          "source_kind": "own_domain", "retrieval_method": "serper_localized"}]
        with patch("deep_dive_runner._collect_pages_via_fallback",
                   return_value={"pages": snippet_pages, "localized_queries_used": []}), \
             patch("deep_dive_runner._fetch_page_for_verification",
                   return_value=_FULL_PAGE_TEXT) as m_fetch, \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s",
                  "quote": "Acme GmbH was founded in 1990",
                  "source_url": "https://acme.com/careers"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="")
        m_fetch.assert_called_once()
        assert result.claims[0].quote_verification_status == "verified"


# ---------------------------------------------------------------------------
# Self-healing: fuzzy auto-correction, bundled not_found re-extraction,
# mechanical re-verification of the AI's own correction candidate.
# ---------------------------------------------------------------------------

def _healed_claim(**kw) -> DeepDiveClaim:
    base = dict(claim_id="hq_structure:1", category="hq_structure",
               statement="s", quote="q", source_url="https://acme.com/about")
    base.update(kw)
    return DeepDiveClaim(**base)


class TestApplyQuoteCorrection:
    def test_replaces_quote_and_preserves_original(self):
        from quote_verifier import QuoteVerification
        claim = _healed_claim(quote="AI paraphrase", quote_verification_status="fuzzy_match")
        verification = QuoteVerification(status="fuzzy_match", match_score=0.9,
                                         matched_snippet="the real page text")
        _apply_quote_correction(claim, verification)
        assert claim.quote == "the real page text"
        assert claim.original_quote == "AI paraphrase"
        assert claim.quote_verification_status == "verified_corrected"
        assert claim.quote_verified is True
        assert claim.quote_match_score == 0.9


class TestReextractNotFoundQuotes:
    def test_no_api_key_returns_empty(self):
        claim = _healed_claim(quote_verification_status="not_found")
        out = _reextract_not_found_quotes(
            company_name="Acme", not_found_claims=[claim],
            page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
            anthropic_api_key="", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {}

    def test_no_not_found_claims_returns_empty(self):
        out = _reextract_not_found_quotes(
            company_name="Acme", not_found_claims=[],
            page_text_by_url={}, anthropic_api_key="fake",
            ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {}

    def test_successful_reextraction_returns_candidate(self):
        claim = _healed_claim(quote_verification_status="not_found")
        with _mock_anthropic(json.dumps({
            "corrections": {"hq_structure:1": "Acme GmbH was founded in 1990"},
        })):
            out = _reextract_not_found_quotes(
                company_name="Acme", not_found_claims=[claim],
                page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {"hq_structure:1": "Acme GmbH was founded in 1990"}

    def test_null_correction_is_omitted(self):
        claim = _healed_claim(quote_verification_status="not_found")
        with _mock_anthropic(json.dumps({"corrections": {"hq_structure:1": None}})):
            out = _reextract_not_found_quotes(
                company_name="Acme", not_found_claims=[claim],
                page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {}

    def test_api_exception_returns_empty(self):
        claim = _healed_claim(quote_verification_status="not_found")
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("boom")
        lib = MagicMock()
        lib.Anthropic.return_value = client
        with patch("deep_dive_runner._anthropic_lib", lib):
            out = _reextract_not_found_quotes(
                company_name="Acme", not_found_claims=[claim],
                page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {}

    def test_bundles_multiple_claims_into_one_call(self):
        claim1 = _healed_claim(claim_id="hq_structure:1", quote_verification_status="not_found")
        claim2 = _healed_claim(claim_id="workforce:1", category="workforce",
                               quote_verification_status="not_found",
                               source_url="https://acme.com/careers")
        page_text_by_url = {
            "https://acme.com/about": _FULL_PAGE_TEXT,
            "https://acme.com/careers": "We employ over 500 people across Europe.",
        }
        with _mock_anthropic(json.dumps({"corrections": {
            "hq_structure:1": "founded in 1990",
            "workforce:1": "employ over 500 people",
        }})):
            out = _reextract_not_found_quotes(
                company_name="Acme", not_found_claims=[claim1, claim2],
                page_text_by_url=page_text_by_url,
                anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL)
        assert out == {"hq_structure:1": "founded in 1990", "workforce:1": "employ over 500 people"}


class TestSelfHealClaims:
    def test_fuzzy_match_auto_corrected(self):
        claim = _healed_claim(
            quote="Acme GmbH founded in 1990, grown into a leading manufacturer",
            quote_verification_status="fuzzy_match", quote_match_score=0.9,
            quote_matched_snippet="acme gmbh was founded in 1990 and has grown into a leading",
        )
        _self_heal_claims(
            claims=[claim], page_text_by_url={}, company_name="Acme",
            anthropic_api_key="", ai_model=DEFAULT_DEEP_DIVE_MODEL,
            auto_correct_quotes=True,
        )
        assert claim.quote == "acme gmbh was founded in 1990 and has grown into a leading"
        assert claim.original_quote == "Acme GmbH founded in 1990, grown into a leading manufacturer"
        assert claim.quote_verification_status == "verified_corrected"
        assert claim.quote_verified is True

    def test_auto_correct_disabled_leaves_fuzzy_match_untouched(self):
        claim = _healed_claim(
            quote="AI paraphrase of the page",
            quote_verification_status="fuzzy_match", quote_match_score=0.9,
            quote_matched_snippet="the real page text",
        )
        _self_heal_claims(
            claims=[claim], page_text_by_url={}, company_name="Acme",
            anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL,
            auto_correct_quotes=False,
        )
        assert claim.quote == "AI paraphrase of the page"
        assert claim.quote_verification_status == "fuzzy_match"
        assert claim.original_quote == ""

    def test_not_found_successful_reextraction_is_corrected(self):
        claim = _healed_claim(quote="hallucinated text", quote_verification_status="not_found")
        with _mock_anthropic(json.dumps({
            "corrections": {"hq_structure:1": "Acme GmbH was founded in 1990"},
        })):
            _self_heal_claims(
                claims=[claim], page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                company_name="Acme", anthropic_api_key="fake",
                ai_model=DEFAULT_DEEP_DIVE_MODEL, auto_correct_quotes=True,
            )
        assert claim.quote_verification_status == "verified_corrected"
        assert claim.quote_verified is True
        assert claim.original_quote == "hallucinated text"

    def test_not_found_reextraction_that_fails_mechanical_check_stays_not_found(self):
        claim = _healed_claim(quote="hallucinated text", quote_verification_status="not_found")
        with _mock_anthropic(json.dumps({
            "corrections": {"hq_structure:1": "this text is not on the page either"},
        })):
            _self_heal_claims(
                claims=[claim], page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                company_name="Acme", anthropic_api_key="fake",
                ai_model=DEFAULT_DEEP_DIVE_MODEL, auto_correct_quotes=True,
            )
        assert claim.quote_verification_status == "not_found"
        assert claim.quote == "hallucinated text"  # untouched
        assert claim.original_quote == ""

    def test_not_found_no_candidate_stays_not_found(self):
        claim = _healed_claim(quote="hallucinated text", quote_verification_status="not_found")
        with _mock_anthropic(json.dumps({"corrections": {"hq_structure:1": None}})):
            _self_heal_claims(
                claims=[claim], page_text_by_url={"https://acme.com/about": _FULL_PAGE_TEXT},
                company_name="Acme", anthropic_api_key="fake",
                ai_model=DEFAULT_DEEP_DIVE_MODEL, auto_correct_quotes=True,
            )
        assert claim.quote_verification_status == "not_found"
        assert claim.quote == "hallucinated text"

    def test_at_most_one_reextraction_call_for_multiple_not_found_claims(self):
        claim1 = _healed_claim(claim_id="hq_structure:1", quote_verification_status="not_found")
        claim2 = _healed_claim(claim_id="workforce:1", category="workforce",
                               quote_verification_status="not_found",
                               source_url="https://acme.com/careers")
        page_text_by_url = {
            "https://acme.com/about": _FULL_PAGE_TEXT,
            "https://acme.com/careers": "We employ over 500 people across Europe.",
        }
        with patch("deep_dive_runner._anthropic_lib") as lib:
            msg = MagicMock()
            msg.content = [MagicMock(text=json.dumps({"corrections": {
                "hq_structure:1": "founded in 1990",
                "workforce:1": "employ over 500 people",
            }}))]
            client = MagicMock()
            client.messages.create.return_value = msg
            lib.Anthropic.return_value = client
            _self_heal_claims(
                claims=[claim1, claim2], page_text_by_url=page_text_by_url,
                company_name="Acme", anthropic_api_key="fake",
                ai_model=DEFAULT_DEEP_DIVE_MODEL, auto_correct_quotes=True,
            )
            assert client.messages.create.call_count == 1
        assert claim1.quote_verification_status == "verified_corrected"
        assert claim2.quote_verification_status == "verified_corrected"

    def test_fetch_failed_never_corrected(self):
        claim = _healed_claim(quote="whatever", quote_verification_status="fetch_failed")
        _self_heal_claims(
            claims=[claim], page_text_by_url={}, company_name="Acme",
            anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL,
            auto_correct_quotes=True,
        )
        assert claim.quote_verification_status == "fetch_failed"
        assert claim.quote == "whatever"

    def test_not_checked_never_corrected(self):
        claim = _healed_claim(quote="whatever", quote_verification_status="not_checked")
        _self_heal_claims(
            claims=[claim], page_text_by_url={}, company_name="Acme",
            anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL,
            auto_correct_quotes=True,
        )
        assert claim.quote_verification_status == "not_checked"
        assert claim.quote == "whatever"

    def test_verified_never_touched(self):
        claim = _healed_claim(quote="already good", quote_verification_status="verified",
                              quote_verified=True, quote_match_score=1.0)
        _self_heal_claims(
            claims=[claim], page_text_by_url={}, company_name="Acme",
            anthropic_api_key="fake", ai_model=DEFAULT_DEEP_DIVE_MODEL,
            auto_correct_quotes=True,
        )
        assert claim.quote_verification_status == "verified"
        assert claim.quote == "already good"
        assert claim.original_quote == ""


class TestRunDeepDiveSelfHealingEndToEnd:
    def test_fuzzy_match_from_distillation_is_corrected_end_to_end(self):
        pages = [{"url": "https://acme.com/about", "title": None, "text": _FULL_PAGE_TEXT,
                 "source_kind": "own_domain", "retrieval_method": "firecrawl"}]
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s",
                  "quote": "Acme GmbH founded in 1990, grown into a leading manufacturer of industrial equipment",
                  "source_url": "https://acme.com/about"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="fc-key")
        claim = result.claims[0]
        assert claim.quote_verification_status == "verified_corrected"
        assert claim.quote_verified is True
        assert claim.original_quote

    def test_auto_correct_quotes_false_leaves_fuzzy_match_as_is(self):
        pages = [{"url": "https://acme.com/about", "title": None, "text": _FULL_PAGE_TEXT,
                 "source_kind": "own_domain", "retrieval_method": "firecrawl"}]
        original_quote = ("Acme GmbH founded in 1990, grown into a leading "
                          "manufacturer of industrial equipment")
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             _mock_anthropic(json.dumps({"claims": [
                 {"category": "hq_structure", "statement": "s", "quote": original_quote,
                  "source_url": "https://acme.com/about"},
             ]})):
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="fc-key", auto_correct_quotes=False)
        claim = result.claims[0]
        assert claim.quote_verification_status == "fuzzy_match"
        assert claim.quote == original_quote
        assert claim.original_quote == ""

    def test_not_found_end_to_end_reextraction_success(self):
        pages = [{"url": "https://acme.com/about", "title": None, "text": _FULL_PAGE_TEXT,
                 "source_kind": "own_domain", "retrieval_method": "firecrawl"}]
        distill_response = json.dumps({"claims": [
            {"category": "hq_structure", "statement": "s",
             "quote": "this text does not appear on the page at all",
             "source_url": "https://acme.com/about"},
        ]})
        reextract_response = json.dumps({"corrections": {
            "hq_structure:1": "founded in 1990",
        }})
        patched, client = _mock_anthropic_sequence(distill_response, reextract_response)
        with patch("deep_dive_runner._collect_pages_via_firecrawl",
                   return_value={"pages": pages, "pages_crawled": [], "used": True}), \
             patched:
            result = run_deep_dive(
                company_name="Acme", domain="acme.com", anthropic_api_key="fake",
                firecrawl_api_key="fc-key")
        assert client.messages.create.call_count == 2
        claim = result.claims[0]
        assert claim.quote_verification_status == "verified_corrected"
        assert claim.quote_verified is True
        assert claim.original_quote == "this text does not appear on the page at all"


class TestFetchPageForVerification:
    def test_uses_firecrawl_when_key_present(self):
        with patch("deep_dive_runner._firecrawl_scrape_page",
                   return_value={"ok": True, "text": "page text", "status": "ok",
                                "hard_failure": False}) as m_fc:
            text = _fetch_page_for_verification("https://acme.com/about", "fc-key")
        m_fc.assert_called_once_with("https://acme.com/about", "fc-key")
        assert text == "page text"

    def test_firecrawl_failure_yields_none(self):
        with patch("deep_dive_runner._firecrawl_scrape_page",
                   return_value={"ok": False, "text": "", "status": "404",
                                "hard_failure": False}):
            assert _fetch_page_for_verification("https://acme.com/about", "fc-key") is None

    def test_uses_plain_fetch_without_key(self):
        with patch("deep_dive_runner._plain_fetch", return_value="page text") as m_pf:
            text = _fetch_page_for_verification("https://acme.com/about", "")
        m_pf.assert_called_once_with("https://acme.com/about")
        assert text == "page text"

    def test_plain_fetch_failure_yields_none(self):
        with patch("deep_dive_runner._plain_fetch", return_value=""):
            assert _fetch_page_for_verification("https://acme.com/about", "") is None
