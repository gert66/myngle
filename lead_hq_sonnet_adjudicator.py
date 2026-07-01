"""C5 — Claude Sonnet HQ adjudication probe (optional, removable layer).

A SECOND OPINION for risky / manual-review HQ cases from the Serper + Haiku
flow.  It asks Sonnet one explicit target-identity question:

    "Is this specific company/domain, operating in this input country,
     ultimately controlled by a parent company headquartered OUTSIDE the
     input country?"

This module is fully standalone: it does not import or modify the existing HQ
flow, C4, ``enrich_clients_claude.py``, scoring, or the 4-class production
taxonomy.  Deleting this file (and its probe/test) removes C5 entirely.

No Serper calls.  No competitor evidence.  No network beyond the single Sonnet
call.  API keys are never logged or returned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

try:
    import anthropic as _anthropic_lib
except ImportError:  # pragma: no cover
    _anthropic_lib = None  # type: ignore[assignment]

# Central, configurable Sonnet model ID for C5 adjudication. Override at the
# call site or via the probe's --model flag if Anthropic rejects this ID.
DEFAULT_SONNET_ADJUDICATION_MODEL = "claude-sonnet-5"

# C5 model tiers. Sonnet is the default tier and has a baked model ID.
# Opus is an OPTIONAL, expensive tier and deliberately has NO baked default:
# the exact Opus API model ID must be supplied explicitly via --model so we
# never guess an ID the account may not have access to. (For reference, the
# Opus 4.8 ID is "claude-opus-4-8" — but it is intentionally not auto-applied.)
C5_MODEL_TIERS: dict[str, "str | None"] = {
    "sonnet": DEFAULT_SONNET_ADJUDICATION_MODEL,
    "opus": None,
}
C5_MODEL_TIER_CHOICES = ("sonnet", "opus")

# Allowed enum values.
_ADJUDICATIONS = ("foreign_parent_confirmed", "domestic_confirmed", "unclear")
_CONFIDENCES = ("High", "Medium", "Low")
_MATCHES = ("yes", "no", "unclear")


@dataclass
class SonnetHQAdjudicationResult:
    adjudication: str = "unclear"           # foreign_parent_confirmed | domestic_confirmed | unclear
    confidence: str = "Low"                 # High | Medium | Low
    target_company_match: str = "unclear"   # yes | no | unclear
    parent_company: str = ""
    parent_hq_country: str = ""
    parent_hq_city: str = ""
    reason: str = ""
    model: str = DEFAULT_SONNET_ADJUDICATION_MODEL
    call_attempted: bool = False
    call_success: bool = False
    error: str = ""
    raw_json: Optional[str] = None          # gated; only populated when include_raw=True


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a corporate ownership analyst. You determine whether one SPECIFIC "
    "company (identified by its exact name and domain) is ultimately controlled "
    "by a parent company headquartered outside a given input country. "
    "Reply ONLY with a valid JSON object — no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Target company to adjudicate (judge THIS entity only, identified by its domain):
- company_name: {company_name}
- domain: {domain}
- input_country: {input_country}

Existing HQ detection (from a prior automated pass — may be wrong):
- hq_detected_country: {hq_detected_country}
- hq_detected_city: {hq_detected_city}
- ai_parent_company: {ai_parent_company}
- ai_parent_hq_country: {ai_parent_hq_country}
- ai_parent_hq_city: {ai_parent_hq_city}
- hq_evidence_url: {hq_evidence_url}
- hq_evidence_quote: {hq_evidence_quote}
- hq_reason: {hq_reason}

Question:
Is THIS specific company/domain, operating in {input_country}, ultimately
controlled by a parent company headquartered OUTSIDE {input_country}?

Rules:
- Judge only the company identified by the supplied domain and name.
- Do NOT answer based on a different, same-name company. If the evidence appears
  to describe a different company than the supplied domain/name, return
  target_company_match = "no" and adjudication = "unclear".
- If you are uncertain, return "unclear". Do not guess.
- "foreign_parent_confirmed" only when the ultimate parent HQ is clearly in a
  DIFFERENT country than input_country, for THIS company.
- "domestic_confirmed" when the ultimate HQ is in the SAME country as
  input_country.
- Return only valid JSON with exactly these keys:
{{
  "adjudication": "foreign_parent_confirmed | domestic_confirmed | unclear",
  "confidence": "High | Medium | Low",
  "target_company_match": "yes | no | unclear",
  "parent_company": "",
  "parent_hq_country": "",
  "parent_hq_city": "",
  "reason": ""
}}
"""


def build_adjudication_prompt(
    *,
    company_name: str,
    domain: str,
    input_country: str,
    hq_detected_country: str = "",
    hq_detected_city: str = "",
    ai_parent_company: str = "",
    ai_parent_hq_country: str = "",
    ai_parent_hq_city: str = "",
    hq_evidence_url: str = "",
    hq_evidence_quote: str = "",
    hq_reason: str = "",
) -> str:
    """Build the target-identity user prompt (no secrets)."""
    return _USER_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        domain=domain or "(unknown)",
        input_country=input_country or "(unknown)",
        hq_detected_country=hq_detected_country or "",
        hq_detected_city=hq_detected_city or "",
        ai_parent_company=ai_parent_company or "",
        ai_parent_hq_country=ai_parent_hq_country or "",
        ai_parent_hq_city=ai_parent_hq_city or "",
        hq_evidence_url=hq_evidence_url or "",
        hq_evidence_quote=hq_evidence_quote or "",
        hq_reason=hq_reason or "",
    )


# ---------------------------------------------------------------------------
# Robust parsing (mirrors C1 style — self-contained, no cross-module import)
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


def _regex_extract_core_fields(raw: str) -> dict:
    text = str(raw or "")

    def _str_field(name: str) -> str:
        m = re.search(
            rf'"{re.escape(name)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            text, flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return ""
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    return {
        "adjudication": _str_field("adjudication"),
        "confidence": _str_field("confidence"),
        "target_company_match": _str_field("target_company_match"),
        "parent_company": _str_field("parent_company"),
        "parent_hq_country": _str_field("parent_hq_country"),
        "parent_hq_city": _str_field("parent_hq_city"),
        "reason": _str_field("reason"),
    }


def _parse_adjudication_response(raw: str) -> dict:
    """Return a dict; ``{}`` only when not even an adjudication is recoverable."""
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
    fields = _regex_extract_core_fields(raw)
    if fields.get("adjudication"):
        return fields
    return {}


def _norm_enum(value: str, allowed: tuple, default: str) -> str:
    v = str(value or "").strip()
    if v in allowed:
        return v
    low = v.lower()
    for a in allowed:
        if a.lower() == low:
            return a
    return default


# ---------------------------------------------------------------------------
# Adjudication
# ---------------------------------------------------------------------------

def adjudicate_hq_with_sonnet(
    company_name: str,
    domain: str,
    input_country: str,
    hq_detected_country: str = "",
    hq_detected_city: str = "",
    ai_parent_company: str = "",
    ai_parent_hq_country: str = "",
    ai_parent_hq_city: str = "",
    hq_evidence_url: str = "",
    hq_evidence_quote: str = "",
    hq_reason: str = "",
    anthropic_api_key: str = "",
    model: str = DEFAULT_SONNET_ADJUDICATION_MODEL,
    include_raw: bool = False,
) -> SonnetHQAdjudicationResult:
    """Ask Sonnet the target-identity question.  Never raises."""
    if not anthropic_api_key:
        return SonnetHQAdjudicationResult(
            model=model, call_attempted=False, call_success=False,
            error="no_anthropic_api_key",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="ai_not_eligible: no API key",
        )

    prompt = build_adjudication_prompt(
        company_name=company_name, domain=domain, input_country=input_country,
        hq_detected_country=hq_detected_country, hq_detected_city=hq_detected_city,
        ai_parent_company=ai_parent_company, ai_parent_hq_country=ai_parent_hq_country,
        ai_parent_hq_city=ai_parent_hq_city, hq_evidence_url=hq_evidence_url,
        hq_evidence_quote=hq_evidence_quote, hq_reason=hq_reason,
    )

    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = resp.content[0].text if resp.content else ""
    except Exception as exc:
        return SonnetHQAdjudicationResult(
            model=model, call_attempted=True, call_success=False,
            error=f"sonnet_call_failed: {str(exc)[:200]}",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="ai_error",
        )

    data = _parse_adjudication_response(raw_text)
    if not data:
        return SonnetHQAdjudicationResult(
            model=model, call_attempted=True, call_success=False,
            error="sonnet_parse_failed",
            adjudication="unclear", confidence="Low", target_company_match="unclear",
            reason="unparseable_response",
            raw_json=(raw_text or "")[:2000] if include_raw else None,
        )

    return SonnetHQAdjudicationResult(
        model=model,
        call_attempted=True,
        call_success=True,
        error="",
        adjudication=_norm_enum(data.get("adjudication"), _ADJUDICATIONS, "unclear"),
        confidence=_norm_enum(data.get("confidence"), _CONFIDENCES, "Low"),
        target_company_match=_norm_enum(data.get("target_company_match"), _MATCHES, "unclear"),
        parent_company=str(data.get("parent_company") or "").strip(),
        parent_hq_country=str(data.get("parent_hq_country") or "").strip(),
        parent_hq_city=str(data.get("parent_hq_city") or "").strip(),
        reason=str(data.get("reason") or "").strip(),
        raw_json=(raw_text or "")[:2000] if include_raw else None,
    )


# ---------------------------------------------------------------------------
# C5 recommendation (proposal only — never changes production score here)
# ---------------------------------------------------------------------------

def build_c5_recommendation(result: SonnetHQAdjudicationResult) -> dict:
    """Map an adjudication to a *proposed* HQ score / review flag.

    This is a recommendation for review only; it does not alter any production
    score. Returns ``c5_recommended_hq_score`` / ``c5_recommended_manual_review``
    / ``c5_recommendation_reason``.
    """
    if (result.adjudication == "foreign_parent_confirmed"
            and result.confidence in ("High", "Medium")
            and result.target_company_match == "yes"):
        return {
            "c5_recommended_hq_score": 3.0,
            "c5_recommended_manual_review": False,
            "c5_recommendation_reason": (
                "C5 confirmed foreign parent for the target company "
                f"({result.confidence} confidence, target match yes)."
            ),
        }
    if result.adjudication == "domestic_confirmed":
        return {
            "c5_recommended_hq_score": 0.0,
            "c5_recommended_manual_review": False,
            "c5_recommendation_reason": "C5 confirmed domestic HQ for the target company.",
        }
    # unclear, target no/unclear, Low confidence, or parse error → review-safe.
    return {
        "c5_recommended_hq_score": 0.0,
        "c5_recommended_manual_review": True,
        "c5_recommendation_reason": (
            "C5 could not confirm a foreign parent for the target company "
            f"(adjudication={result.adjudication}, confidence={result.confidence}, "
            f"target_company_match={result.target_company_match})."
        ),
    }
