"""Non-HQ enrichment evidence collector for Lead Prioritizer v2.

Step 2 of the v2 dataflow: after HQ detection, collect a small fixed set of
non-HQ evidence from Serper.  This module **collects evidence only** — it does
NOT produce scores, does NOT interpret with AI, and does NOT collect any
competitor / vendor-comparison / rapid-growth signals.

Independent of legacy enrichment: it does not import from
``hq_lookup_probe_app.py`` or ``enrich_clients_claude.py``.  It reuses only the
public ``derive_domain_root`` helper for domain-root-first query building.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import api_retry
from hq_simple_detector import derive_domain_root
from lead_output_schema import LeadEvidence


# ---------------------------------------------------------------------------
# Query builder (domain-root-first)
# ---------------------------------------------------------------------------

def _enrichment_root(company_name: str, domain: Optional[str]) -> str:
    """Domain-root-first: use the domain's registrable label when available,
    otherwise fall back to a normalized company name."""
    if domain and domain.strip():
        root = derive_domain_root(domain.strip())
        if root:
            return root
    return (company_name or "").strip().lower()


def build_non_hq_enrichment_queries(
    company_name: str,
    domain: Optional[str],
) -> list[dict]:
    """Return up to 4 non-HQ enrichment query specs.

    Each spec is ``{"signal_name": str, "query": str}``.  Queries are built from
    the domain root when a usable domain exists, else from the company name.

    ``company_size_complexity`` and ``sector_industry`` are deliberately NOT
    among these specs (Lusha enrichment plan, Stap 4): both are now covered
    without a live Serper call — company size/complexity directly from
    Lusha Company Number of Employees / Company Revenue (see
    ``lead_lusha_size_signal.py``), sector from Lusha Main/Sub Industry
    mapping, own-domain Firecrawl+AI, and a keyword match on Lusha
    Description/Specialties text as the last resort (see
    ``lead_lusha_sector_mapping.py``). Neither signal has a Serper fallback
    anymore.

    No competitor, alternative-provider, vendor-comparison or rapid-growth
    queries are produced.
    """
    root = _enrichment_root(company_name, domain)
    if not root:
        return []

    return [
        {
            "signal_name": "international_profile",
            "query": f"{root} international offices countries global presence",
        },
        {
            "signal_name": "onboarding_training_need",
            "query": f"{root} careers training onboarding academy learning development",
        },
        {
            "signal_name": "icp_keyword_match",
            "query": f"{root} corporate training sales customer service global teams",
        },
        {
            "signal_name": "employer_branding",
            "query": f"{root} employer branding employee satisfaction workplace "
                     "culture employee experience great place to work glassdoor",
        },
    ]


# ---------------------------------------------------------------------------
# Serper caller (defensive — mirrors the HQ helper)
# ---------------------------------------------------------------------------

# Country -> (gl, hl) for localized Serper results. Only countries with an
# unambiguous single primary language are mapped; unknown countries fall
# back to Serper's default (no gl/hl sent at all — current behavior).
_COUNTRY_TO_GL_HL: dict[str, tuple[str, str]] = {
    "netherlands": ("nl", "nl"),
    "italy": ("it", "it"),
    "germany": ("de", "de"),
    "france": ("fr", "fr"),
    "spain": ("es", "es"),
    "belgium": ("be", "nl"),
}


def gl_hl_for_country(country: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map a lead's input country to Serper's ``gl``/``hl`` params.

    Returns ``(None, None)`` for unmapped/unknown countries so callers can
    omit both params and keep exactly today's (unlocalized) behavior."""
    key = (country or "").strip().lower()
    return _COUNTRY_TO_GL_HL.get(key, (None, None))


def call_serper_for_enrichment(
    query: str,
    serper_api_key: str,
    gl: Optional[str] = None,
    hl: Optional[str] = None,
    usage_kind: str = "non_hq",
    domain: str = "",
    signal_name: str = "",
    cache_index: Optional[dict] = None,
    force_refresh: bool = False,
    max_429_retries: int = api_retry.DEFAULT_MAX_RETRIES,
) -> dict:
    """Fire a single Serper search and return the raw JSON payload.

    ``gl``/``hl`` (Serper's country/language params) are only included in the
    request when explicitly given, so callers that don't pass them keep the
    exact request shape used today. Returns ``{}`` on any error so callers can
    treat it defensively; never raises on API / network failure. A 429 (rate
    limited) is retried up to ``max_429_retries`` times with backoff before
    giving up and returning ``{}``, same as any other error.

    ``domain``/``signal_name`` + ``cache_index`` (all default blank/``None``)
    are an optional in-memory, GCS-backed shared cache lookup (see
    ``enrichment_cache.py``), keyed on domain + signal type. When
    ``cache_index`` is ``None`` — the default, and the case for every caller
    that doesn't build a cache-key pair (e.g. the rich-ICP-context composer,
    which has no single domain/signal_name to key on) — behavior is
    completely unchanged: every call hits Serper live. When provided
    (requires a non-blank ``domain``/``signal_name`` to actually key
    anything), a fresh-enough cached response is returned without a network
    call; a miss still calls Serper live and stores the result back into
    ``cache_index``.
    """
    import urllib.error
    import urllib.request
    import usage_tracker

    if not serper_api_key or not query:
        return {}

    use_cache = cache_index is not None and domain and signal_name
    if use_cache:
        import enrichment_cache
        cached = enrichment_cache.get_cached(
            cache_index, "serper", domain, signal_name,
            ttl_days=enrichment_cache.serper_ttl_days(signal_name),
            force_refresh=force_refresh,
        )
        if cached is not None:
            usage_tracker.record_cache_hit("serper")
            return cached
        usage_tracker.record_cache_miss("serper")

    usage_tracker.record_serper_call(usage_kind)
    request_payload: dict = {"q": query, "num": 10}
    if gl:
        request_payload["gl"] = gl
    if hl:
        request_payload["hl"] = hl

    payload_bytes = json.dumps(request_payload).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload_bytes,
        headers={
            "X-API-KEY": serper_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_429_retries:
                retry_after = api_retry.parse_retry_after(exc.headers.get("Retry-After"))
                time.sleep(api_retry.backoff_seconds(attempt, retry_after))
                attempt += 1
                continue
            return {}
        except Exception:
            return {}

    if use_cache and result:
        enrichment_cache.put_cached(
            cache_index, "serper", domain, signal_name, response=result)
    return result


# ---------------------------------------------------------------------------
# Evidence extractor (deterministic — no AI, no scoring, no invented quotes)
# ---------------------------------------------------------------------------

def _clean(value: object) -> str:
    return str(value or "").strip()


def extract_evidence_from_serper_payload(
    payload: dict,
    signal_name: str,
    query_used: str,
    max_items: int = 3,
) -> list[LeadEvidence]:
    """Convert a Serper payload into up to ``max_items`` LeadEvidence objects.

    Prioritises knowledgeGraph, then answerBox, then top organic results.  All
    snippets come verbatim from Serper fields — nothing is interpreted or
    invented.
    """
    if not isinstance(payload, dict) or max_items <= 0:
        return []

    out: list[LeadEvidence] = []

    def _add(source_type: str, parser_source: str, title: str, snippet: str, url: str) -> None:
        if len(out) >= max_items:
            return
        # Skip wholly empty evidence (no title, snippet, or url).
        if not (title or snippet or url):
            return
        out.append(LeadEvidence(
            evidence_id=f"{signal_name}:{source_type}:{len(out) + 1}",
            signal_name=signal_name,
            query_used=query_used,
            source_url=url or None,
            source_title=title or None,
            source_snippet=snippet or None,
            source_type=source_type,
            parser_source=parser_source,
            confidence=None,   # deterministic collector: no confidence assigned
            notes=None,
        ))

    # Knowledge graph (single)
    kg = payload.get("knowledgeGraph") or {}
    if isinstance(kg, dict) and kg:
        _add(
            source_type="knowledge_graph",
            parser_source="serper_knowledge_graph",
            title=_clean(kg.get("title")),
            snippet=_clean(kg.get("description")),
            url=_clean(kg.get("website") or kg.get("descriptionLink")),
        )

    # Answer box (single)
    ab = payload.get("answerBox") or {}
    if isinstance(ab, dict) and ab:
        _add(
            source_type="answer_box",
            parser_source="serper_answer_box",
            title=_clean(ab.get("title")),
            snippet=_clean(ab.get("answer") or ab.get("snippet")),
            url=_clean(ab.get("link")),
        )

    # Top organic results (fill remaining slots)
    organic = payload.get("organic") or []
    if isinstance(organic, list):
        for i, item in enumerate(organic, start=1):
            if len(out) >= max_items:
                break
            if not isinstance(item, dict):
                continue
            _add(
                source_type="organic",
                parser_source=f"serper_organic_{i}",
                title=_clean(item.get("title")),
                snippet=_clean(item.get("snippet")),
                url=_clean(item.get("link")),
            )

    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def collect_non_hq_enrichment_evidence(
    company_name: str,
    domain: Optional[str],
    serper_api_key: str,
    max_evidence_per_signal: int = 3,
    country: Optional[str] = None,
    cache_index: Optional[dict] = None,
    force_refresh: bool = False,
) -> list[LeadEvidence]:
    """Build query specs, call Serper per query, extract evidence.

    Returns one flat list of ``LeadEvidence`` across all non-HQ signals.  No
    scores are produced.  At most 4 Serper queries are made (one per signal;
    ``company_size_complexity`` and ``sector_industry`` have no Serper query
    — see ``build_non_hq_enrichment_queries``).
    ``country`` (the lead's effective input country) is mapped to Serper's
    ``gl``/``hl`` params for localized results; unmapped/unknown countries
    keep today's unlocalized behavior.

    ``cache_index`` (default ``None``) is an optional in-memory, GCS-backed
    shared cache index (see ``enrichment_cache.py``); when ``None`` — the
    default — every query hits Serper live, exactly as before this parameter
    existed. When provided, each of the (up to 4) queries is cached/looked up
    independently, keyed on ``domain`` + that query's own ``signal_name``.
    """
    gl, hl = gl_hl_for_country(country)
    specs = build_non_hq_enrichment_queries(company_name, domain)
    evidence: list[LeadEvidence] = []
    for spec in specs:
        query = spec["query"]
        payload = call_serper_for_enrichment(
            query, serper_api_key, gl=gl, hl=hl,
            domain=domain or "", signal_name=spec["signal_name"],
            cache_index=cache_index, force_refresh=force_refresh,
        )
        evidence.extend(
            extract_evidence_from_serper_payload(
                payload,
                signal_name=spec["signal_name"],
                query_used=query,
                max_items=max_evidence_per_signal,
            )
        )
    return evidence
