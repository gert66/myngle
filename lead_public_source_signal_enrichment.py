"""Public Source Signal Enrichment for Lead Prioritizer v2 (optional, evidence-only).

Retrieves public company-level evidence for a user-configured signal
(``signal_query``, e.g. "vacancies") from a single user-configured public
source (``source_base_url``) via Firecrawl, and returns it as ``LeadEvidence``
items — nothing more. Off by default. Adds evidence only; never creates or
changes a score directly — ``signal_name="public_source_signal"`` is
deliberately NOT one of the five scored non-HQ signal names
(``lead_non_hq_signal_extractor.SUPPORTED_SIGNALS``), so
``extract_non_hq_signals`` ignores it entirely and it can never move
``final_commercial_fit_score`` on its own. It flows into the existing
Evidence sheet / app-summary logic exactly like any other ``LeadEvidence``.

Reuse, not reinvention
-----------------------
The Firecrawl request/response plumbing (``_firecrawl_scrape_page``) is
reused verbatim from ``deep_dive_runner`` — same v1 scrape REST call, same
hard-failure-vs-404 distinction, same ``usage_tracker`` audit — mirroring the
precedent set by ``lead_hq_firecrawl_source.py``. Host parsing reuses
``lead_hq_ai_interpreter._host_from``.

First prototype — guardrails deferred
---------------------------------------
Per the initial spec, this first version does NOT implement the fuller
guardrail system planned for a later iteration: no domain-match-to-company
checks, no quote verification, no relevance scoring beyond a simple
substring match. The one exception is a small, explicit block list of
social/professional-network platforms that are never an acceptable "public
source" boundary for this feature (``_BLOCKED_SOURCE_DOMAINS`` below) — kept
intentionally minimal and easy to replace once the fuller guardrails land.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from deep_dive_runner import _firecrawl_scrape_page
from hq_simple_detector import is_hosted_careers_platform_domain
from lead_hq_ai_interpreter import _host_from
from lead_output_schema import LeadEvidence

# Minimal, explicit block list — social/professional-network platforms are
# never an acceptable "public source" boundary for this feature (their
# content is not intended for this kind of retrieval, and they actively
# restrict it). This is NOT the fuller guardrail system (domain-match,
# quote verification, relevance scoring, ...) planned for a later iteration —
# see the module docstring.
_BLOCKED_SOURCE_DOMAINS = frozenset({
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "glassdoor.com", "indeed.com",
})

_SNIPPET_CONTEXT_CHARS = 100
_MAX_SNIPPET_CHARS = 240


def _is_blocked_public_source(source_base_url: str) -> bool:
    """True when ``source_base_url`` resolves to a known blocked platform —
    a social/professional network (``_BLOCKED_SOURCE_DOMAINS``) or an existing
    hosted careers/ATS platform (Workday, Greenhouse, ...; shared with the
    rest of the v2 pipeline via ``is_hosted_careers_platform_domain``)."""
    host = _host_from(source_base_url)
    if not host:
        return False
    labels = host.split(".")
    candidates = {host}
    if len(labels) >= 2:
        candidates.add(".".join(labels[-2:]))
    return bool(candidates & _BLOCKED_SOURCE_DOMAINS) or is_hosted_careers_platform_domain(
        source_base_url)


def _build_candidate_urls(source_base_url: str, company_name: str, max_pages: int) -> list[str]:
    """Candidate URLs to try, all under the configured public source boundary
    (``source_base_url``'s own host — never a different domain).

    Always includes the base URL itself. When ``max_pages`` allows more, adds
    a couple of common company-search query-string variants on the SAME base
    URL — many public directories/vacancy portals support ``?q=``/``?search=``.
    """
    base = (source_base_url or "").strip().rstrip("/")
    if not base:
        return []
    if "://" not in base:
        base = f"https://{base}"
    candidates = [base]
    if max_pages > 1 and company_name:
        q = urllib.parse.quote_plus(company_name.strip())
        if q:
            sep = "&" if "?" in base else "?"
            candidates.append(f"{base}{sep}q={q}")
            if max_pages > 2:
                candidates.append(f"{base}{sep}search={q}")
    return candidates[:max(1, max_pages)]


def _extract_snippet(text: str, keyword: str) -> Optional[str]:
    """Short passage around the first case-insensitive occurrence of
    ``keyword`` in ``text`` — showing why it matched. ``None`` when the
    keyword does not appear at all (caller treats that page as no match)."""
    if not text or not keyword:
        return None
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return None
    start = max(0, idx - _SNIPPET_CONTEXT_CHARS)
    end = min(len(text), idx + len(keyword) + _SNIPPET_CONTEXT_CHARS)
    snippet = text[start:end].strip()
    return snippet[:_MAX_SNIPPET_CHARS] or None


def collect_public_source_signal_evidence(
    company_name: str,
    domain: Optional[str],
    signal_query: str,
    source_base_url: str,
    firecrawl_api_key: str,
    source_label: str = "",
    max_pages: int = 3,
) -> list[LeadEvidence]:
    """Retrieve public company-level evidence for ``signal_query`` from the
    single configured public source (``source_base_url``), via Firecrawl only.

    ``domain`` is accepted (and passed through by callers) for forward
    compatibility with a future domain-match guardrail; this first prototype
    does not use it (see module docstring — guardrails deferred).

    Returns ``[]`` — never raises — when: required inputs are missing; the
    source resolves to a blocked social/professional-network or hosted-ATS
    platform; Firecrawl hard-fails (401/402/403/429 or a network error); or
    nothing useful matched. A Firecrawl hard failure on any candidate page
    abandons the whole attempt (mirrors the existing Firecrawl fallback
    contract in ``lead_hq_firecrawl_source.py`` / ``deep_dive_runner.py``) —
    it never breaks the row.
    """
    try:
        company_name = (company_name or "").strip()
        signal_query = (signal_query or "").strip()
        source_base_url = (source_base_url or "").strip()
        source_label = (source_label or "").strip()

        if not (company_name and signal_query and source_base_url and firecrawl_api_key):
            return []

        if _is_blocked_public_source(source_base_url):
            return []

        max_pages = max(1, int(max_pages or 3))
        candidates = _build_candidate_urls(source_base_url, company_name, max_pages)
        if not candidates:
            return []

        query_used = f"{company_name} | signal: {signal_query} | source: {source_base_url}"
        evidence: list[LeadEvidence] = []

        for url in candidates:
            result = _firecrawl_scrape_page(url, firecrawl_api_key)
            if result["hard_failure"]:
                # Bad/exhausted key or network outage — abandon entirely,
                # matching the existing Firecrawl fallback contract. Whatever
                # was already found on earlier candidates is still returned.
                return evidence
            if not result["ok"]:
                continue
            snippet = _extract_snippet(result["text"], signal_query)
            if not snippet:
                continue
            evidence.append(LeadEvidence(
                evidence_id=f"public_source_signal:public_source:{len(evidence) + 1}",
                signal_name="public_source_signal",
                query_used=query_used,
                source_url=url,
                source_title=source_label or None,
                source_snippet=snippet,
                source_type="public_source",
                parser_source="firecrawl_public_source",
                confidence="Medium",
                notes=(
                    f"source_label={source_label or '(none)'}; "
                    f"retrieval_status={result['status']}"
                ),
            ))

        return evidence
    except Exception:
        return []
