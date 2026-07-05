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
from typing import Optional

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
    """Return up to 6 non-HQ enrichment query specs.

    Each spec is ``{"signal_name": str, "query": str}``.  Queries are built from
    the domain root when a usable domain exists, else from the company name.

    The ``sector_industry`` spec collects audit/app metadata evidence only — it
    is never turned into a commercial scoring signal.

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
            "signal_name": "company_size_complexity",
            "query": f"{root} employees revenue locations company profile",
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
        {
            "signal_name": "sector_industry",
            "query": f"{root} company industry sector products services "
                     "business activity company profile official",
        },
    ]


# ---------------------------------------------------------------------------
# Serper caller (defensive — mirrors the HQ helper)
# ---------------------------------------------------------------------------

def call_serper_for_enrichment(query: str, serper_api_key: str) -> dict:
    """Fire a single Serper search and return the raw JSON payload.

    Returns ``{}`` on any error so callers can treat it defensively; never
    raises on API / network failure.
    """
    import urllib.request

    if not serper_api_key or not query:
        return {}

    payload_bytes = json.dumps({"q": query, "num": 10}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload_bytes,
        headers={
            "X-API-KEY": serper_api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


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
) -> list[LeadEvidence]:
    """Build query specs, call Serper per query, extract evidence.

    Returns one flat list of ``LeadEvidence`` across all non-HQ signals.  No
    scores are produced.  At most 6 Serper queries are made (one per signal).
    """
    specs = build_non_hq_enrichment_queries(company_name, domain)
    evidence: list[LeadEvidence] = []
    for spec in specs:
        query = spec["query"]
        payload = call_serper_for_enrichment(query, serper_api_key)
        evidence.extend(
            extract_evidence_from_serper_payload(
                payload,
                signal_name=spec["signal_name"],
                query_used=query,
                max_items=max_evidence_per_signal,
            )
        )
    return evidence
