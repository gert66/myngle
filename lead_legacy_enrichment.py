"""Legacy-style enrichment mode for Lead Prioritizer v2 (comparison feature).

Reproduces the *evaluation style* of the old ``enrich_clients_claude.py``
Step-2 Serper+Claude scoring (``STEP2_STATIC_PREFIX`` around line 252,
``_build_serper_queries`` around line 2854, ``ICP_FIELDS`` around line 494)
so the new v2 pipeline can be compared against the old approach on the same
leads, side by side.

Deliberately excluded, matching the task's explicit scope:
- The 4 competitor/alternative-provider fields (``icp_competitor_signal``,
  ``icp_direct_language_competitor_signal``, ``icp_online_language_learning_signal``,
  ``icp_broader_lnd_platform_signal``) and their Q5 competitor-co-mention query.
  The buying-signal list below is renumbered 1-9, dropping the old #3
  competitor-signal entry entirely rather than replacing it with anything.
- Jina AI Reader / full-page scraping (the old Step 1). This mode works from
  Serper search snippets only, exactly like the rest of the v2 pipeline.

This is a brand-new, independent scoring path with its own ``legacy_score`` /
``legacy_tier`` fields -- NOT an extension of ``lead_icp_context_composer.py``
(similar prompt/parsing style, different purpose and a different, smaller set
of Serper queries: 4 here, matching the old Q1-Q4, vs. that composer's 3).

``enrich_clients_claude.py`` itself is never imported or modified (standing
project constraint) -- everything needed is reproduced locally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

try:
    import anthropic as _anthropic_lib
except ImportError:  # pragma: no cover
    _anthropic_lib = None  # type: ignore[assignment]

from hq_simple_detector import is_hosted_careers_platform_domain
from lead_non_hq_enrichment import call_serper_for_enrichment, gl_hl_for_country

DEFAULT_LEGACY_ENRICHMENT_MODEL = "claude-haiku-4-5-20251001"

# High/Medium/Low -> a numeric score on the same 0-10 scale final_commercial_fit_score
# uses, purely so the two systems' outputs are easy to eyeball side by side.
# Never fed into commercial_fit_scoring.py / lead_v2_scoring_adapter.py.
_LEAD_SCORE_TO_NUMERIC = {"high": 9.0, "medium": 6.0, "low": 3.0}


@dataclass
class LegacyEnrichmentResult:
    """Result of one legacy-style enrichment attempt."""
    icp_lead_score: str = ""
    icp_buying_signals: str = ""
    icp_likely_training_interest: str = ""
    icp_potential_buyer_function: str = ""
    icp_why_relevant: str = ""
    icp_evidence: str = ""
    legacy_score: float = 0.0
    legacy_tier: str = ""
    queries_used: list = field(default_factory=list)
    model: str = DEFAULT_LEGACY_ENRICHMENT_MODEL
    call_attempted: bool = False
    call_success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Queries -- exact opzet of _build_serper_queries() in enrich_clients_claude.py,
# Q1-Q4 only (Q5, the competitor/online-learning co-mention query, is dropped).
# ---------------------------------------------------------------------------

_LEGACY_QUERY_LABELS = [
    "General company context",
    "International footprint / HQ",
    "L&D / employee training",
    "Language / global teams",
]


def build_legacy_queries(company_name: str, domain: Optional[str]) -> list[dict]:
    """Return the 4 thematic queries (label + query), Q1-Q4 of the original
    5-query set -- everything except the dropped competitor query."""
    name = (company_name or domain or "").strip()
    if not name:
        return []
    domain = (domain or "").strip()
    site_q = f"site:{domain} OR " if domain else ""

    queries = [
        f'{site_q}"{name}" about company overview headquarters',
        f'"{name}" headquarters OR offices OR countries OR "international operations" '
        'OR "global presence" OR "regional HQ"',
        f'"{name}" "learning and development" OR training OR academy OR onboarding '
        'OR "talent development" OR L&D',
        f'"{name}" English OR "language training" OR "global teams" OR multilingual '
        'OR "language program" OR intercultural',
    ]
    return [{"label": label, "query": q} for label, q in zip(_LEGACY_QUERY_LABELS, queries)]


# ---------------------------------------------------------------------------
# Evidence collection + formatting (Serper only, hosted-platform guard applied
# before anything reaches the prompt)
# ---------------------------------------------------------------------------

def _collect_query_results(
    queries: list[dict], serper_api_key: str, gl: Optional[str], hl: Optional[str],
) -> list[tuple[str, list[dict]]]:
    """Run each query via the shared Serper enrichment helper (never
    duplicated) and return ``[(label, hits), ...]`` with hosted
    careers-platform hits already excluded."""
    groups: list[tuple[str, list[dict]]] = []
    for spec in queries:
        payload = call_serper_for_enrichment(spec["query"], serper_api_key, gl=gl, hl=hl)
        hits = []
        if isinstance(payload, dict):
            for hit in (payload.get("organic") or [])[:5]:
                if not isinstance(hit, dict):
                    continue
                if is_hosted_careers_platform_domain(hit.get("link")):
                    continue
                hits.append(hit)
        groups.append((spec["label"], hits))
    return groups


def _format_query_groups(groups: list[tuple[str, list[dict]]]) -> str:
    if not groups or not any(hits for _, hits in groups):
        return "(No web search results were found.)"
    blocks: list[str] = []
    for label, hits in groups:
        if not hits:
            continue
        lines = [f"=== {label} ==="]
        for i, hit in enumerate(hits, 1):
            title = (hit.get("title") or "(no title)").strip()
            link = hit.get("link") or ""
            snippet = (hit.get("snippet") or "").strip()[:180]
            lines.append(f"[{i}] {title}")
            lines.append(f"    URL: {link}")
            if snippet:
                lines.append(f"    snippet: {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(No web search results were found.)"


# ---------------------------------------------------------------------------
# Prompt -- same holistic-judgment style as STEP2_STATIC_PREFIX, minus the
# competitor-signal buying signal (#3 in the original 10) and its dedicated
# output fields/quality rule.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are analyzing companies to identify whether they may have a potential interest in \
language training, business English, communication training, leadership training, \
negotiation training, team-building, onboarding, or broader employee development.

Your task is to evaluate each company based on the 9 buying signals below. \
Prioritize the signals in this order:

1. International footprint - The company has offices, teams, subsidiaries, clients, \
production sites, or operations in multiple countries or regions.

2. Foreign headquarters, parent company, or group structure - The company operates in \
one country but has its headquarters, parent company, group ownership, regional HQ, or \
reporting lines in another country.

3. Merger, acquisition, integration, or new group ownership.

4. Explicit learning and development focus.

5. International customer base or client-facing international work.

6. Multicultural or multilingual workforce.

7. Employer branding and employee satisfaction.

8. Rapid growth, hiring, or expansion.

9. Leadership, management, sales, or negotiation-heavy roles.

Return ONLY a raw JSON object with exactly these fields and no others:
{"lead_score": "High or Medium or Low", \
"buying_signals": "comma-separated list of signal names actually supported by evidence", \
"evidence": "brief description of what was found and source types", \
"likely_training_interest": \
"comma-separated list from: Language training / Business English, \
Intercultural communication, Leadership training, Negotiation training, \
Sales or client communication, Team collaboration / team-building, \
Onboarding / employee development, Broader professional training", \
"why_relevant": "brief explanation", \
"potential_buyer_function": \
"most likely buyer such as HR, Learning and Development, Talent Development, \
People and Culture, Leadership Development, Sales Enablement, Customer Success, \
Operations, Procurement"}

Do not invent evidence. Do not wrap in markdown.\
"""


def build_legacy_enrichment_prompt(
    *, company_name: str, country: Optional[str], results_text: str,
) -> str:
    return (
        f"Now analyze this company based on the web search results provided below.\n\n"
        f"Company: {company_name}\n"
        f"Country: {country or '(unknown)'}\n\n"
        f"Web search results (retrieved via Serper Google Search, grouped by signal type):\n"
        f"{results_text}\n\n"
        "Evidence quality rules:\n"
        "- Only mark a buying signal as present when a result contains company-specific, "
        "contextual evidence — not just a keyword in a URL or a generic snippet.\n"
        "- Set lead_score to High only when two or more clearly distinct strong signals appear.\n"
        "- Base your analysis ONLY on the search results above. Do not invent or infer evidence."
    )


# ---------------------------------------------------------------------------
# Robust parsing (self-contained, mirrors lead_icp_context_composer.py /
# lead_ai_signal_scorer.py's tolerant-JSON-extraction pattern)
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"^```(?:json|JSON)?\s*", "", s).strip()
    s = re.sub(r"\s*```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start:end + 1].strip()
    return s


def _parse_response(raw: str) -> dict:
    raw = str(raw or "")
    for cand in (raw, _extract_json_object(raw)):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def extract_anthropic_text(response) -> str:
    """Concatenate the text from an Anthropic response, skipping non-text
    blocks (e.g. a leading ThinkingBlock on extended-thinking models)."""
    content = getattr(response, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: list = []
    try:
        blocks = list(content)
    except TypeError:
        return ""

    for block in blocks:
        if isinstance(block, dict):
            btype = str(block.get("type") or "").lower()
            if btype and btype != "text" and "text" not in block:
                continue
            val = block.get("text")
            if isinstance(val, str) and val:
                parts.append(val)
            continue
        val = getattr(block, "text", None)
        if isinstance(val, str) and val:
            parts.append(val)

    return "".join(parts)


def _legacy_score_for(lead_score: str) -> float:
    return _LEAD_SCORE_TO_NUMERIC.get(lead_score.strip().lower(), 0.0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_legacy_enrichment(
    *,
    company_name: str,
    domain: Optional[str],
    country: Optional[str],
    serper_api_key: str,
    anthropic_api_key: str,
    ai_model: str = DEFAULT_LEGACY_ENRICHMENT_MODEL,
) -> LegacyEnrichmentResult:
    """Reproduce the old enrich_clients_claude.py Step-2 evaluation style.

    Never raises: any failure (no key, call error, unparseable response)
    yields ``call_success=False`` with an ``error`` string and empty ICP
    fields, so the caller can simply leave the legacy_* fields blank.
    """
    queries = build_legacy_queries(company_name, domain)
    query_strings = [q["query"] for q in queries]

    if not anthropic_api_key or not queries:
        return LegacyEnrichmentResult(
            model=ai_model, queries_used=query_strings,
            call_attempted=False, call_success=False,
            error="no_anthropic_api_key" if not anthropic_api_key else "no_queries",
        )

    gl, hl = gl_hl_for_country(country)
    groups = _collect_query_results(queries, serper_api_key, gl, hl)
    results_text = _format_query_groups(groups)
    prompt = build_legacy_enrichment_prompt(
        company_name=company_name, country=country, results_text=results_text)

    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=ai_model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        import usage_tracker
        usage_tracker.record_anthropic_response(response, ai_model, "legacy_enrichment")
        raw_text = extract_anthropic_text(response)
    except Exception as exc:
        return LegacyEnrichmentResult(
            model=ai_model, queries_used=query_strings,
            call_attempted=True, call_success=False,
            error=f"legacy_enrichment_call_failed: {str(exc)[:200]}",
        )

    data = _parse_response(raw_text)
    if not data:
        return LegacyEnrichmentResult(
            model=ai_model, queries_used=query_strings,
            call_attempted=True, call_success=False,
            error="legacy_enrichment_parse_failed",
        )

    lead_score = str(data.get("lead_score") or "").strip()
    return LegacyEnrichmentResult(
        icp_lead_score=lead_score,
        icp_buying_signals=str(data.get("buying_signals") or "").strip(),
        icp_likely_training_interest=str(data.get("likely_training_interest") or "").strip(),
        icp_potential_buyer_function=str(data.get("potential_buyer_function") or "").strip(),
        icp_why_relevant=str(data.get("why_relevant") or "").strip(),
        icp_evidence=str(data.get("evidence") or "").strip(),
        legacy_score=_legacy_score_for(lead_score),
        legacy_tier=lead_score,
        queries_used=query_strings,
        model=ai_model,
        call_attempted=True,
        call_success=True,
        error="",
    )
