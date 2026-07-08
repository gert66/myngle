"""AI composition of caller-facing content for Lead Prioritizer v2 (Step 3).

Explicit opt-in enrichment step, off by default. It writes ``why_relevant`` /
``what_is_hot`` / ``cold_caller_summary`` / ``caller_angle`` / ``call_starter``
and a short evidence sentence per fixed commercial-fit driver via the
Anthropic Messages API, using ONLY the curated evidence already computed
upstream (Step 2/3 signal extraction, HQ detection) -- aiming to match the
quality of the frozen, hand-tuned Italy output for non-Italy leads, without
ever touching the Italy path itself.

Same pattern as ``lead_hq_ai_interpreter.py`` / ``lead_hq_sonnet_adjudicator.py``:
an explicit ``anthropic_api_key`` parameter, a JSON-only prompt, tolerant
parsing (markdown fences / prose around the JSON object are stripped), and a
function that never raises. Any failure (no key, call error, unparseable
response) yields ``call_success=False`` so the caller can fall back to the
existing deterministic templates.
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

# Default model — same family/tier as the other v2 AI steps (HQ interpreter).
DEFAULT_CALLER_CONTENT_MODEL = "claude-haiku-4-5-20251001"

# The six fixed commercial-fit dimensions, keyed by signal_name ("foreign_hq"
# for the HQ/parent driver). Callers build driver_evidence lookups by these
# keys -- kept independent of the exporter's own id/label strings so this
# module has no dependency on export_lead_prioritizer_to_lovable_json.py.
DRIVER_SIGNAL_NAMES: tuple[str, ...] = (
    "foreign_hq",
    "international_profile",
    "icp_keyword_match",
    "onboarding_training_need",
    "company_size_complexity",
    "employer_branding",
)

# Human-readable labels for the non-HQ curated signals passed into the prompt.
_SIGNAL_LABELS: dict[str, str] = {
    "international_profile": "International business context",
    "icp_keyword_match": "Explicit learning and development",
    "onboarding_training_need": "Learning and development or onboarding needs",
    "company_size_complexity": "Possible onboarding need",
    "employer_branding": "Employer branding or employee satisfaction",
}


@dataclass
class ComposedCallerContent:
    """Result of one AI caller-content composition attempt."""
    why_relevant: Optional[str] = None
    what_is_hot: list = field(default_factory=list)
    cold_caller_summary: Optional[str] = None
    caller_angle: Optional[str] = None
    call_starter: Optional[str] = None
    driver_evidence: dict = field(default_factory=dict)  # signal_name -> sentence
    model: str = DEFAULT_CALLER_CONTENT_MODEL
    call_attempted: bool = False
    call_success: bool = False
    error: str = ""
    raw_json: Optional[str] = None


# ---------------------------------------------------------------------------
# Curated-signal input builder (from an existing LeadPrioritizationResult)
# ---------------------------------------------------------------------------

def build_curated_signals_from_result(result) -> list:
    """Curated ``compose_caller_content(curated_signals=...)`` input built from
    an existing ``LeadPrioritizationResult``.

    Only positively-scored signals with a non-blank evidence quote are
    included. No quality guard is re-derived here: Step 2/3
    (``extract_non_hq_signals``) has already ensured that a positively-scored
    signal's evidence is neither a hosted careers-platform hit nor (for the
    L&D-family signals) external installer/product/partner training -- so a
    positive score here is always backed by genuinely usable evidence.
    """
    curated = []
    for sig in (getattr(result, "signals", None) or []):
        score = getattr(sig, "signal_score", None)
        if not (score and score > 0):
            continue
        evidence = (getattr(sig, "evidence_quote", None)
                    or getattr(sig, "signal_reason", None) or "").strip()
        if not evidence:
            continue
        name = getattr(sig, "signal_name", None)
        curated.append({
            "signal_name": name,
            "label": _SIGNAL_LABELS.get(name, name or "signal"),
            "evidence": evidence,
            "source_url": getattr(sig, "evidence_url", None),
        })
    return curated


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a B2B sales research assistant preparing caller-facing briefing "
    "notes for a cold caller. Use ONLY the evidence supplied in the user "
    "message -- never invent facts, company details, or evidence that is not "
    "present in the input. External installer, product, partner, or reseller "
    "training must NEVER be presented as internal employee learning & "
    "development. Rapid growth must NEVER be presented as a positive reason "
    "to prioritize a lead, not even as neutral context. When the supplied "
    "evidence is thin, do not force a confident claim -- instead write a "
    "useful light-discovery angle: concrete, checkable hypotheses grounded in "
    "the company's sector, parent company, or country. Each driver_evidence "
    "sentence must be grounded ONLY in that same signal's own evidence line "
    "from the Curated signals list -- never combine, borrow, or blend a "
    "specific fact (a country count, employee count, or other detail) from a "
    "DIFFERENT signal's evidence line into it, even when both are true and "
    "doing so would make the sentence sound more complete. "
    "Reply ONLY with a valid JSON object -- no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Company: {company_name}
Country: {country}
Industry: {industry}
Employee range: {employee_range}

HQ / parent-company conclusion:
- foreign_hq_detected: {foreign_hq_detected}
- parent_company: {parent_company}
- parent_hq_country: {parent_hq_country}
- parent_hq_city: {parent_hq_city}
- hq_adjudication: {hq_adjudication}

Curated signals (already quality-checked -- use verbatim, do not invent
additional signals or evidence beyond what is listed):
{signals_text}

Quality flags (caveats already known -- do not contradict them):
{quality_flags_text}

Return JSON with exactly these keys:
{{
  "why_relevant": "2-4 company-specific sentences explaining why this lead is worth calling",
  "what_is_hot": ["...", "..."],
  "cold_caller_summary": "a short practical briefing paragraph for the caller",
  "caller_angle": "a practical opening angle for the first conversation",
  "call_starter": "a natural first sentence the caller could say",
  "driver_evidence": {{"<driver_signal_name>": "one short evidence sentence, or empty string if not evidenced"}}
}}

Rules:
- "what_is_hot" has at most 5 concrete bullets, each grounded in the supplied evidence.
- "driver_evidence" must use exactly these keys, one entry per key: {driver_keys}
- Each "driver_evidence" sentence must use ONLY the evidence listed under that
  same signal above -- never blend in a fact (e.g. a country count, employee
  count, or other specific detail) that appears only under a different
  signal's evidence line, even if it would make the sentence read better.
- Never present external installer/product/partner/reseller training as internal L&D.
- Never present rapid growth as a positive driver.
- If curated signals are thin or empty, "why_relevant" and "cold_caller_summary" must
  offer a light-discovery angle (concrete next-step hypotheses based on sector, parent
  company, or country) rather than an unsupported confident claim.
"""


def _format_signals(curated_signals: list) -> str:
    if not curated_signals:
        return "  (none)"
    lines = []
    for sig in curated_signals:
        label = sig.get("label") or sig.get("signal_name") or "signal"
        evidence = sig.get("evidence") or ""
        url = sig.get("source_url") or sig.get("evidence_url") or ""
        line = f"  - {label}: {evidence}"
        if url:
            line += f" (source: {url})"
        lines.append(line)
    return "\n".join(lines)


def _format_quality_flags(quality_flags: list) -> str:
    if not quality_flags:
        return "  (none)"
    return "\n".join(f"  - {flag}" for flag in quality_flags)


def build_caller_content_prompt(
    *,
    company_name: str,
    country: Optional[str],
    industry: Optional[str],
    employee_range: Optional[str],
    foreign_hq_detected: bool,
    parent_company: Optional[str],
    parent_hq_country: Optional[str],
    parent_hq_city: Optional[str],
    hq_adjudication: Optional[str],
    curated_signals: list,
    driver_ids: list,
    quality_flags: list,
) -> str:
    """Build the user message (no secrets)."""
    return _USER_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        country=country or "(unknown)",
        industry=industry or "(unknown)",
        employee_range=employee_range or "(unknown)",
        foreign_hq_detected=bool(foreign_hq_detected),
        parent_company=parent_company or "(unknown)",
        parent_hq_country=parent_hq_country or "(unknown)",
        parent_hq_city=parent_hq_city or "(unknown)",
        hq_adjudication=hq_adjudication or "(unknown)",
        signals_text=_format_signals(curated_signals),
        quality_flags_text=_format_quality_flags(quality_flags),
        driver_keys=", ".join(driver_ids or DRIVER_SIGNAL_NAMES),
    )


# ---------------------------------------------------------------------------
# Robust parsing (self-contained, no cross-module import — mirrors the same
# tolerant-JSON-extraction pattern used by lead_hq_ai_interpreter.py /
# lead_hq_sonnet_adjudicator.py)
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
    """Return a dict, or ``{}`` when nothing usable could be parsed."""
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


def _clean_str_list(value, limit: int) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out[:limit]


def _clean_driver_evidence(value, driver_ids: list) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in driver_ids:
        val = value.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


# ---------------------------------------------------------------------------
# Composition entry point
# ---------------------------------------------------------------------------

def compose_caller_content(
    *,
    company_name: str,
    country: Optional[str] = None,
    industry: Optional[str] = None,
    employee_range: Optional[str] = None,
    foreign_hq_detected: bool = False,
    parent_company: Optional[str] = None,
    parent_hq_country: Optional[str] = None,
    parent_hq_city: Optional[str] = None,
    hq_adjudication: Optional[str] = None,
    curated_signals: Optional[list] = None,
    driver_ids: Optional[list] = None,
    quality_flags: Optional[list] = None,
    anthropic_api_key: str = "",
    model: str = DEFAULT_CALLER_CONTENT_MODEL,
) -> ComposedCallerContent:
    """Compose caller-facing content via the Anthropic Messages API.

    Never raises: any failure (no key, call error, unparseable response)
    yields ``call_success=False`` with a short ``error`` string so the caller
    can fall back to the existing deterministic templates.
    """
    driver_ids = list(driver_ids or DRIVER_SIGNAL_NAMES)

    if not anthropic_api_key:
        return ComposedCallerContent(
            model=model, call_attempted=False, call_success=False,
            error="no_anthropic_api_key",
        )

    prompt = build_caller_content_prompt(
        company_name=company_name, country=country, industry=industry,
        employee_range=employee_range, foreign_hq_detected=foreign_hq_detected,
        parent_company=parent_company, parent_hq_country=parent_hq_country,
        parent_hq_city=parent_hq_city, hq_adjudication=hq_adjudication,
        curated_signals=curated_signals or [], driver_ids=driver_ids,
        quality_flags=quality_flags or [],
    )

    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        import usage_tracker
        usage_tracker.record_anthropic_response(response, model, "caller_content")
        raw_text = extract_anthropic_text(response)
    except Exception as exc:
        return ComposedCallerContent(
            model=model, call_attempted=True, call_success=False,
            error=f"caller_content_call_failed: {str(exc)[:200]}",
        )

    data = _parse_response(raw_text)
    if not data:
        return ComposedCallerContent(
            model=model, call_attempted=True, call_success=False,
            error="caller_content_parse_failed",
            raw_json=(raw_text or "")[:2000],
        )

    return ComposedCallerContent(
        why_relevant=(str(data.get("why_relevant") or "").strip() or None),
        what_is_hot=_clean_str_list(data.get("what_is_hot"), limit=5),
        cold_caller_summary=(str(data.get("cold_caller_summary") or "").strip() or None),
        caller_angle=(str(data.get("caller_angle") or "").strip() or None),
        call_starter=(str(data.get("call_starter") or "").strip() or None),
        driver_evidence=_clean_driver_evidence(data.get("driver_evidence"), driver_ids),
        model=model,
        call_attempted=True,
        call_success=True,
        error="",
        raw_json=(raw_text or "")[:2000],
    )
