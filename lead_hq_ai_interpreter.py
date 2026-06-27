"""AI-first HQ interpreter for Lead Prioritizer v2.

Provides a Serper helper and an Anthropic (Haiku) interpreter that together
replace the deterministic parser for HQ detection.

Flow
----
1. ``call_serper_for_hq()``  — one Serper call: "{domain_root} headquarters"
2. ``interpret_hq_with_ai()`` — sends KG + answerBox + top-5 organic to Haiku,
   then applies the post-AI country-comparison scoring rules.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

try:
    import anthropic as _anthropic_lib
except ImportError:
    _anthropic_lib = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from lead_output_schema import HQDetectionResult, LeadInput

# Default model for AI HQ interpretation
_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Serper helper
# ---------------------------------------------------------------------------

def call_serper_for_hq(
    *,
    domain_root: str,
    query: str,
    serper_api_key: str,
) -> dict:
    """Fire a single Serper search and return the raw JSON payload.

    The query must already be built by ``build_simple_hq_query()``; this
    function does not construct queries itself.

    Returns an empty dict on any error so callers can treat it defensively.
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
# AI interpreter
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a corporate structure analyst. "
    "Given search-engine results about a company, determine where its ultimate parent "
    "group headquarters is located. "
    "Reply ONLY with a valid JSON object — no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Company domain root: {domain_root}
Input country (where the local entity operates): {input_country}

Search results for query: "{query}"

Knowledge Graph:
{kg_text}

Answer Box:
{ab_text}

Top organic results (title + snippet):
{organic_text}

Classify and return JSON with these exact keys:
- "classification": one of "foreign_parent", "domestic", "regional_branch_only", "unclear"
- "confidence": one of "High", "Medium", "Low"
- "parent_company": name of the ultimate parent group (empty string if unknown)
- "parent_hq_country": country of the ultimate parent HQ (empty string if unknown)
- "parent_hq_city": city of the ultimate parent HQ (empty string if unknown)
- "evidence_url": best source URL from the results (empty string if none)
- "evidence_quote": short verbatim quote supporting your answer (max 200 chars, empty if none)
- "reason": one short sentence explaining your classification

Rules:
- Use "foreign_parent" when the ultimate controlling group HQ is in a DIFFERENT country than input_country.
- Use "domestic" when the ultimate HQ is in the SAME country as input_country.
- Use "regional_branch_only" when the result clearly describes a regional/local branch, not the global HQ.
- Use "unclear" only when evidence is contradictory or absent.
- Never invent information not present in the search results.
"""


def _build_user_message(
    *,
    domain_root: str,
    input_country: str,
    query: str,
    serper_payload: dict,
) -> str:
    kg: dict = serper_payload.get("knowledgeGraph") or {}
    kg_parts = []
    for key in ("title", "type", "address", "headquarters", "location", "description"):
        val = kg.get(key, "")
        if val:
            kg_parts.append(f"  {key}: {val}")
    kg_text = "\n".join(kg_parts) if kg_parts else "  (none)"

    ab: dict = serper_payload.get("answerBox") or {}
    ab_text = (ab.get("answer") or ab.get("snippet") or "(none)")[:400]

    organic: list[dict] = (serper_payload.get("organic") or [])[:5]
    organic_lines = []
    for i, item in enumerate(organic, 1):
        t = (item.get("title") or "")[:120]
        s = (item.get("snippet") or "")[:200]
        url = item.get("link", "")
        organic_lines.append(f"  [{i}] {t}\n      {s}\n      URL: {url}")
    organic_text = "\n".join(organic_lines) if organic_lines else "  (none)"

    return _USER_TEMPLATE.format(
        domain_root=domain_root,
        input_country=input_country or "(unknown)",
        query=query,
        kg_text=kg_text,
        ab_text=ab_text,
        organic_text=organic_text,
    )


def _parse_ai_response(raw: str) -> dict:
    """Extract the JSON object from the AI response text."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
    try:
        return json.loads(text)
    except Exception:
        # Try to find first {...} block
        m = re.search(r"\{[\s\S]+\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _normalize_country_for_hq(value: object) -> str:
    """Lowercase canonical country — handles ISO-2, ISO-3, full names."""
    _MAP = {
        "it": "italy", "ita": "italy", "italia": "italy", "italy": "italy", "italian": "italy",
        "de": "germany", "deu": "germany", "germany": "germany",
        "deutschland": "germany", "german": "germany",
        "fr": "france", "fra": "france", "france": "france", "french": "france",
        "uk": "united kingdom", "gb": "united kingdom", "gbr": "united kingdom",
        "united kingdom": "united kingdom", "great britain": "united kingdom",
        "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
        "us": "united states", "usa": "united states", "united states": "united states",
        "america": "united states",
        "ch": "switzerland", "switzerland": "switzerland", "swiss": "switzerland",
        "nl": "netherlands", "netherlands": "netherlands", "holland": "netherlands",
        "be": "belgium", "belgium": "belgium",
        "at": "austria", "austria": "austria",
        "es": "spain", "spain": "spain",
        "se": "sweden", "sweden": "sweden",
        "no": "norway", "norway": "norway",
        "dk": "denmark", "denmark": "denmark",
        "fi": "finland", "finland": "finland",
        "jp": "japan", "japan": "japan",
        "cn": "china", "china": "china",
        "pl": "poland", "poland": "poland",
        "pt": "portugal", "portugal": "portugal",
        "ie": "ireland", "ireland": "ireland",
        "lu": "luxembourg", "luxembourg": "luxembourg",
        "sg": "singapore", "singapore": "singapore",
    }
    text = re.sub(r"\s+", " ", re.sub(r"\.", "", str(value or "").strip().lower()))
    return _MAP.get(text, text)


def interpret_hq_with_ai(
    *,
    lead_input: "LeadInput",
    domain_root: str,
    query: str,
    serper_payload: dict,
    anthropic_api_key: str,
    model: str = _DEFAULT_AI_MODEL,
) -> "HQDetectionResult":
    """Interpret HQ from a Serper payload using Anthropic (Haiku by default).

    Returns an ``HQDetectionResult`` with AI audit fields populated.
    The only deterministic post-AI step is a normalised country comparison.
    """
    from lead_output_schema import HQDetectionResult

    _base = dict(
        domain_root=domain_root,
        query_used=query,
        ai_hq_model=model,
        ai_call_attempted="Yes",
    )

    if not anthropic_api_key:
        return HQDetectionResult(
            **_base,
            ai_call_attempted="No",
            ai_hq_error="no_anthropic_api_key",
            needs_manual_review=True,
            hq_reason="ai_hq_not_eligible: no API key",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    # ── Call Anthropic ────────────────────────────────────────────────────────
    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        user_msg = _build_user_message(
            domain_root=domain_root,
            input_country=lead_input.input_country or "",
            query=query,
            serper_payload=serper_payload,
        )
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = response.content[0].text if response.content else ""
    except Exception as exc:
        return HQDetectionResult(
            **_base,
            ai_hq_error=f"anthropic_call_failed: {exc}",
            needs_manual_review=True,
            hq_reason=f"ai_hq_error: {exc}",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    ai_data = _parse_ai_response(raw_text)

    clf        = (ai_data.get("classification") or "").strip().lower()
    confidence = (ai_data.get("confidence") or "").strip()
    parent_co  = (ai_data.get("parent_company") or "").strip()
    ai_country = (ai_data.get("parent_hq_country") or "").strip()
    ai_city    = (ai_data.get("parent_hq_city") or "").strip()
    ev_url     = (ai_data.get("evidence_url") or "").strip()
    ev_quote   = (ai_data.get("evidence_quote") or "").strip()[:200]
    ai_reason  = (ai_data.get("reason") or "").strip()

    ai_fields = dict(
        ai_hq_classification=clf,
        ai_hq_confidence=confidence,
        ai_parent_company=parent_co,
        ai_parent_hq_country=ai_country,
        ai_parent_hq_city=ai_city,
        hq_detected_country=ai_country or None,
        hq_detected_city=ai_city or None,
        hq_confidence=confidence or None,
        hq_evidence_url=ev_url or None,
        hq_evidence_quote=ev_quote or None,
    )

    # ── Post-AI scoring (only deterministic step) ────────────────────────────
    _inp_norm = _normalize_country_for_hq(lead_input.input_country or "")
    _ai_norm  = _normalize_country_for_hq(ai_country)

    if not ai_data or clf == "unclear" or (not clf):
        return HQDetectionResult(
            **_base, **ai_fields,
            ai_call_success="No",
            ai_hq_error="ai_hq_unclear_or_empty",
            needs_manual_review=True,
            hq_reason=f"ai_hq_unclear: {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    if not ai_country:
        return HQDetectionResult(
            **_base, **ai_fields,
            ai_call_success="No",
            ai_hq_error="ai_hq_blank_country",
            needs_manual_review=True,
            hq_reason=f"ai_hq_blank_country: {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    if _ai_norm and _inp_norm and _ai_norm == _inp_norm:
        # Domestic
        low_conf = confidence == "Low"
        return HQDetectionResult(
            **_base, **ai_fields,
            ai_call_success="Yes",
            foreign_hq_simple=False,
            hq_structure_type="domestic",
            needs_manual_review=low_conf,
            hq_reason=f"domestic_hq: {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=0.0,
        )

    # Countries differ
    if clf == "foreign_parent" and confidence in ("High", "Medium"):
        return HQDetectionResult(
            **_base, **ai_fields,
            ai_call_success="Yes",
            foreign_hq_simple=True,
            hq_structure_type="foreign_parent",
            needs_manual_review=False,
            hq_reason=f"foreign_parent ({confidence}): {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=3.0,
        )

    if clf == "foreign_parent" and confidence == "Low":
        return HQDetectionResult(
            **_base, **ai_fields,
            ai_call_success="Yes",
            foreign_hq_simple=True,
            hq_structure_type="foreign_parent",
            needs_manual_review=True,
            hq_reason=f"foreign_parent_low_confidence: {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=0.0,
        )

    # regional_branch_only or other classification
    return HQDetectionResult(
        **_base, **ai_fields,
        ai_call_success="Yes",
        foreign_hq_simple=False,
        hq_structure_type=clf if clf else "other",
        needs_manual_review=False,
        hq_reason=f"{clf}: {ai_reason}",
        sig_foreign_hq_score_for_next_scoring=0.0,
    )
