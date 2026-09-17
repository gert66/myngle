"""Zyte own-site HQ evidence with lightweight smart link discovery.

Fetches the company homepage through Zyte, extracts visible text plus links,
then follows the strongest About/Company/Group/Imprint-style links. Only links
seen on the fetched company homepage are eligible for cross-domain follow-up,
which gives the HQ interpreter an explicit entity relationship rather than a
name-only web search guess.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

_ZYTE_ENDPOINT = "https://api.zyte.com/v1/extract"
_DEFAULT_MAX_PAGES = 3

_HIGH_TERMS = (
    "about", "about-us", "who-we-are", "company", "corporate", "group", "gruppe",
    "ueber-uns", "über-uns", "unternehmen", "impressum", "imprint", "legal-notice",
    "ownership", "owner", "parent", "holding", "a-propos", "chi-siamo", "over-ons",
)
_MEDIUM_TERMS = ("contact", "kontakt", "history", "geschichte", "profile", "profil")
_BLOCKED_HOST_FRAGMENTS = (
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "kununu.com", "wikipedia.org",
)

class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: Optional[str] = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            anchor = " ".join(self._anchor_parts).strip()
            self.links.append((self._href, anchor))
            self._href = None
            self._anchor_parts = []

    def handle_data(self, data):
        if self._skip:
            return
        s = re.sub(r"\s+", " ", data).strip()
        if not s:
            return
        self.parts.append(s)
        if self._href is not None:
            self._anchor_parts.append(s)


def _parse_html(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _PageParser()
    try:
        p.feed(html)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", " ".join(p.parts)).strip()
    return text, p.links


def _basic_auth(api_key: str) -> str:
    return base64.b64encode(f"{api_key}:".encode()).decode()


def _fetch(url: str, api_key: str, timeout: int = 60) -> dict:
    payload = json.dumps({
        "url": url,
        "httpResponseBody": True,
        "followRedirect": True,
    }).encode()
    req = urllib.request.Request(
        _ZYTE_ENDPOINT,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + _basic_auth(api_key)},
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                obj = json.loads(response.read().decode())
            body = obj.get("httpResponseBody") or ""
            html = base64.b64decode(body, validate=False).decode("utf-8", errors="replace") if body else ""
            text, links = _parse_html(html)
            ok = obj.get("statusCode") == 200 and bool(text)
            return {
                "ok": ok,
                "status": obj.get("statusCode"),
                "url": obj.get("url") or url,
                "text": text,
                "links": links,
            }
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 2:
                break
        time.sleep(1.5 * (attempt + 1))
    return {"ok": False, "status": last_error or "error", "url": url, "text": "", "links": []}


def fetch_page_via_zyte(url: str, zyte_api_key: str, *, timeout: int = 60) -> dict:
    """Public single-page Zyte fetch for other enrichment layers.

    Returns the same ``ok/status/url/text/links`` shape as the internal HQ
    fetcher and never exposes the API key in its result.
    """
    url = (url or "").strip()
    key = (zyte_api_key or "").strip()
    if not url or not key:
        return {"ok": False, "status": "missing_url_or_key", "url": url, "text": "", "links": []}
    return _fetch(url, key, timeout=timeout)


def _score_link(base_url: str, href: str, anchor: str) -> tuple[int, str] | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if any(blocked in host for blocked in _BLOCKED_HOST_FRAGMENTS):
        return None
    haystack = f"{parsed.path} {anchor}".lower().replace("_", "-")
    high_hits = sum(1 for term in _HIGH_TERMS if term in haystack)
    medium_hits = sum(1 for term in _MEDIUM_TERMS if term in haystack)
    if not high_hits and not medium_hits:
        return None
    base_host = urlsplit(base_url).netloc.lower().split(":")[0]
    score = high_hits * 100 + medium_hits * 20
    if host == base_host or host.endswith("." + base_host) or base_host.endswith("." + host):
        score += 40
    # Strong group/company links are allowed cross-domain only when the company
    # homepage itself links them, which is useful for subsidiary -> parent sites.
    if host != base_host and any(term in haystack for term in ("group", "gruppe", "holding", "parent", "company")):
        score += 30
    return score, absolute.split("#", 1)[0]


def _rank_discovered_links(base_url: str, links: list[tuple[str, str]]) -> list[str]:
    best: dict[str, int] = {}
    for href, anchor in links:
        scored = _score_link(base_url, href, anchor)
        if not scored:
            continue
        score, url = scored
        best[url] = max(score, best.get(url, -1))
    return [url for url, _ in sorted(best.items(), key=lambda item: (-item[1], item[0]))]


def collect_own_domain_hq_pages_zyte(
    domain: Optional[str],
    zyte_api_key: str,
    *,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> dict:
    """Return Zyte HQ pages in the same shape as the Firecrawl adapter."""
    domain = (domain or "").strip()
    if not domain or not zyte_api_key:
        return {"pages": [], "pages_crawled": [], "used": False}
    base = domain if "://" in domain else f"https://{domain}"
    base = base.rstrip("/")
    pages: list[dict] = []
    crawled: list[dict] = []

    home = _fetch(base, zyte_api_key)
    crawled.append({"url": base, "status": home.get("status")})
    if home.get("ok"):
        pages.append({
            "url": home.get("url") or base,
            "text": home.get("text") or "",
            "source_kind": "own_domain",
            "retrieval_method": "zyte",
        })
    if len(pages) >= max_pages:
        return {"pages": pages, "pages_crawled": crawled, "used": True}

    ranked = _rank_discovered_links(home.get("url") or base, home.get("links") or []) if home.get("ok") else []
    # If the homepage failed or exposed no useful links, preserve the existing
    # static-path safety net. These are only attempted until max_pages succeeds.
    fallbacks = [base + p for p in ("/about", "/about-us", "/company", "/company-profile", "/en/about")]
    candidates = ranked + [u for u in fallbacks if u not in ranked]
    seen = {base}
    for url in candidates:
        if len(pages) >= max_pages:
            break
        if url in seen:
            continue
        seen.add(url)
        result = _fetch(url, zyte_api_key)
        crawled.append({"url": url, "status": result.get("status")})
        if result.get("ok"):
            pages.append({
                "url": result.get("url") or url,
                "text": result.get("text") or "",
                "source_kind": "own_domain" if urlsplit(url).netloc == urlsplit(base).netloc else "linked_group_domain",
                "retrieval_method": "zyte",
            })
    return {"pages": pages, "pages_crawled": crawled, "used": bool(pages)}
