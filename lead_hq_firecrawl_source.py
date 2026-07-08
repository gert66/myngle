"""Firecrawl own-domain crawl as the PRIMARY HQ evidence source.

Motivation
----------
Primary HQ detection used to rest on a SINGLE Serper query
(``"{domain_root} headquarters"``) whose single top snippet was handed to a
one-shot Haiku classifier. Serper is not deterministic for an identical query,
so the same company could land on ``foreign_parent`` one run and
``regional_branch_only`` the next (observed for FUJIFILM Manufacturing Europe
B.V.), flipping a fact that must never change run-to-run.

This module adds the company's OWN website content (homepage + a few
about/company-profile-style pages, redirects followed by Firecrawl) as a
first, most-trusted source fed to the classifier alongside the Serper snippets.
Own-domain page text is stable across runs, which removes the instability.

Reuse, not reinvention
----------------------
The Firecrawl request/response plumbing is reused verbatim from
``deep_dive_runner`` (``_firecrawl_scrape_page`` — same v1 scrape REST call,
same hard-failure vs. 404 distinction, same ``usage_tracker`` audit) and the
host helpers from ``lead_hq_ai_interpreter``. Only the small orchestration of
"crawl the own domain for HQ purposes" lives here.

Fallback contract (mirrors Deep Dive)
-------------------------------------
No ``firecrawl_api_key`` → nothing is crawled. A Firecrawl hard failure
(network error or a 401/402/403/429 key/quota status) discards everything from
that attempt and returns ``used=False`` so the caller falls back cleanly to
today's Serper-only behavior. A missing key is never an error.
"""

from __future__ import annotations

from typing import Optional

# Reuse the tested Firecrawl scrape helper rather than re-implementing it.
from deep_dive_runner import _firecrawl_scrape_page
from lead_country_config import normalize_country_for_hq as _normalize_country_for_hq

# Candidate own-domain pages tried in order, focused on where a company
# describes its group/parent/HQ (not the broader careers/newsroom set Deep
# Dive uses). Cheap and best-effort — a 404 just skips that one page.
_HQ_CANDIDATE_PAGE_PATHS: tuple[str, ...] = (
    "", "/about", "/about-us", "/company", "/company-profile", "/en/about",
)

# Extra localized "about us" paths tried in addition to the English ones
# above, keyed on the SAME normalized country key lead_country_config uses.
# Appended (never replacing) the English defaults, so an unknown/blank
# country keeps candidate_paths byte-identical to before this existed.
_COUNTRY_HQ_CANDIDATE_PATHS: dict[str, tuple[str, ...]] = {
    "italy": ("/chi-siamo",),
    "netherlands": ("/over-ons",),
    "germany": ("/ueber-uns",),
    "france": ("/a-propos",),
}

# Own-domain HQ crawls are deliberately small — a couple of pages are enough
# to describe a parent/group, and this runs on every row with a Firecrawl key.
_DEFAULT_MAX_HQ_PAGES = 3


def collect_own_domain_hq_pages(
    domain: Optional[str],
    firecrawl_api_key: str,
    *,
    max_pages: int = _DEFAULT_MAX_HQ_PAGES,
    candidate_paths: "tuple[str, ...] | None" = None,
    country: Optional[str] = None,
    cache_index: Optional[dict] = None,
    force_refresh: bool = False,
) -> dict:
    """Crawl the company's own domain for HQ classification material.

    Returns ``{"pages": [...], "pages_crawled": [...], "used": bool}``.

    Each entry in ``pages`` is ``{"url", "text", "source_kind": "own_domain",
    "retrieval_method": "firecrawl"}`` — the same page shape Deep Dive uses, so
    the AI-message formatter can consume either.

    ``used=False`` means Firecrawl could not be used at all (no key, no domain,
    or a hard failure) — the caller MUST ignore ``pages`` entirely and fall back
    to Serper-only, since a bad/exhausted key fails consistently and partial
    results would be misleading. Never raises.

    ``candidate_paths``, when given explicitly, is used verbatim (existing
    override behavior, unchanged). Otherwise the default is
    ``_HQ_CANDIDATE_PAGE_PATHS`` plus, when ``country`` maps to an entry in
    ``_COUNTRY_HQ_CANDIDATE_PATHS`` (e.g. Italy -> ``/chi-siamo``), that
    country's localized path(s) appended at the end — more candidates to try
    within the same ``max_pages`` cap, never fewer. A blank/unrecognised
    ``country`` (the default) keeps ``candidate_paths`` identical to before
    this parameter existed.

    ``cache_index``/``force_refresh`` (default ``None``/``False``) are passed
    straight through to ``_firecrawl_scrape_page`` (see ``enrichment_cache.py``)
    — this function has no caching logic of its own. ``None`` (the default)
    means every page hits Firecrawl live, exactly as before this parameter
    existed.
    """
    if candidate_paths is not None:
        paths = candidate_paths
    else:
        paths = _HQ_CANDIDATE_PAGE_PATHS + _COUNTRY_HQ_CANDIDATE_PATHS.get(
            _normalize_country_for_hq(country), ())
    domain = (domain or "").strip()
    if not firecrawl_api_key or not domain:
        return {"pages": [], "pages_crawled": [], "used": False}

    base = domain if "://" in domain else f"https://{domain}"
    base = base.rstrip("/")

    pages: list[dict] = []
    pages_crawled: list[dict] = []

    for path in paths:
        if len(pages) >= max_pages:
            break
        url = base + path
        result = _firecrawl_scrape_page(
            url, firecrawl_api_key,
            cache_index=cache_index, force_refresh=force_refresh,
        )
        pages_crawled.append({"url": url, "status": result["status"]})
        if result["hard_failure"]:
            # Bad/exhausted key or network outage: abandon Firecrawl entirely.
            return {"pages": pages, "pages_crawled": pages_crawled, "used": False}
        if result["ok"]:
            pages.append({
                "url": url,
                "text": result["text"],
                "source_kind": "own_domain",
                "retrieval_method": "firecrawl",
            })

    if not pages:
        return {"pages": pages, "pages_crawled": pages_crawled, "used": False}
    return {"pages": pages, "pages_crawled": pages_crawled, "used": True}
