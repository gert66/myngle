"""Opt-in AI signal scoring for Lead Prioritizer v2 (Onderdeel 2).

Explicit, separate opt-in that REPLACES the deterministic keyword-count
verdicts in ``lead_non_hq_signal_extractor.extract_non_hq_signals`` with
semantic judgments from one Anthropic call -- for the exact five supported
non-HQ signals in ``SUPPORTED_SIGNALS``, no more, no fewer. It never touches
HQ detection, never adds new evidence, and never changes the downstream
score mapping (``lead_v2_scoring_adapter.py`` / ``commercial_fit_scoring.py``
stay unchanged) -- only which ``LeadSignal`` objects are fed into it.

Guard reuse: the evidence handed to the AI is filtered with the exact same
``_usable_evidence_for_signal`` guard the deterministic extractor uses
(hosted-careers-platform domains and external installer/partner/product
training excluded) -- reused, not duplicated.

Never trust an invented reference: every ``supporting_evidence_ids`` entry
the AI returns is mechanically checked against the evidence actually
supplied for that signal. Ids that don't exist are dropped; a positive
verdict left with zero valid ids is downgraded to ``no_positive_match``.
The AI never gets the final say on which sources exist -- only which of the
real ones support its judgment.

Mirrors the same pattern as ``lead_icp_context_composer.py``: explicit
``anthropic_api_key`` parameter, JSON-only prompt, tolerant parsing, and a
function that never raises -- any failure (no key, call error, unparseable
response) yields ``call_success=False`` so the caller falls back to the
deterministic extractor rather than ever shipping a row with no signals.
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

from lead_non_hq_signal_extractor import SUPPORTED_SIGNALS, _usable_evidence_for_signal
from lead_output_schema import LeadEvidence, LeadSignal

DEFAULT_AI_SIGNAL_SCORING_MODEL = "claude-haiku-4-5-20251001"

_VALID_VERDICTS = ("positive_evidence", "weak_evidence", "no_positive_match")
_VERDICT_TO_SCORE = {
    "positive_evidence": 2.0,
    "weak_evidence": 1.0,
    "no_positive_match": 0.0,
}


@dataclass
class AiSignalScoringResult:
    """Result of one AI signal-scoring attempt."""
    signals: list = field(default_factory=list)  # list[LeadSignal]
    model: str = DEFAULT_AI_SIGNAL_SCORING_MODEL
    call_attempted: bool = False
    call_success: bool = False
    error: str = ""
    raw_json: Optional[str] = None


# ---------------------------------------------------------------------------
# Evidence filtering (reuses the deterministic extractor's guard, never
# duplicates it) and prompt construction
# ---------------------------------------------------------------------------

def _filter_usable_evidence_by_signal(
    evidence_items: list[LeadEvidence],
) -> dict[str, list[LeadEvidence]]:
    """Group evidence by supported signal name, keeping only evidence that
    passes the exact same guard the deterministic extractor uses. Signal
    names with no usable evidence are omitted entirely (mirrors the
    deterministic extractor never producing a signal for an empty group)."""
    by_signal: dict[str, list[LeadEvidence]] = {}
    for ev in evidence_items or []:
        name = ev.signal_name
        if name not in SUPPORTED_SIGNALS:
            continue
        if not _usable_evidence_for_signal(ev, name):
            continue
        by_signal.setdefault(name, []).append(ev)
    return by_signal


_SYSTEM_PROMPT = (
    "You are a B2B sales research analyst judging whether pieces of search "
    "evidence support specific commercial-fit signals for a lead. Use ONLY "
    "the evidence items supplied in the user message -- never invent facts, "
    "evidence, or evidence ids that are not present in the input. Evidence "
    "may be in any language; judge meaning and synonyms semantically, not "
    "literal English keyword matches (e.g. \"11 countries\" or \"filiali\" "
    "both count as international-profile evidence). A parent-company or "
    "ownership claim only counts as evidence when the evidence text "
    "recognizably names the company itself, not just an unrelated group. "
    "When you are unsure between two verdicts, always choose the lower one. "
    "Reply ONLY with a valid JSON object -- no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Company: {company_name}
Country: {country}

For each signal below, judge the supplied evidence items and return a
verdict. Evidence items already passed basic quality filters -- judge only
what they say, do not invent additional facts or evidence.

{evidence_block}

Return JSON with exactly these top-level keys (one entry per signal name
listed above, using the exact signal name as the key):
{{
  "<signal_name>": {{
    "verdict": "positive_evidence" | "weak_evidence" | "no_positive_match",
    "reason": "short reason (1 sentence)",
    "supporting_evidence_ids": ["<evidence_id>", "..."]
  }},
  ...
}}

Rules:
- "positive_evidence": strong, clear, semantically unambiguous support.
- "weak_evidence": some support but thin, indirect, or hedged.
- "no_positive_match": no real support in the supplied evidence.
- supporting_evidence_ids must only contain evidence_id values copied
  verbatim from the list above for that same signal -- never invent one.
- A verdict other than "no_positive_match" must be backed by at least one
  supporting_evidence_id.
- If in doubt between two verdicts, pick the lower one.
"""


def _format_signal_evidence_block(by_signal: dict[str, list[LeadEvidence]]) -> str:
    lines = []
    for signal_name, items in by_signal.items():
        lines.append(f"Signal: {signal_name}")
        for ev in items:
            title = (ev.source_title or "").strip()
            snippet = (ev.source_snippet or "").strip()
            text = " -- ".join(t for t in (title, snippet) if t) or "(no text)"
            lines.append(f"  - evidence_id={ev.evidence_id}: {text}")
        lines.append("")
    return "\n".join(lines).strip()


def build_ai_signal_scoring_prompt(
    *,
    company_name: str,
    country: Optional[str],
    by_signal: dict[str, list[LeadEvidence]],
) -> str:
    """Build the user message (no secrets)."""
    return _USER_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        country=country or "(unknown)",
        evidence_block=_format_signal_evidence_block(by_signal),
    )


# ---------------------------------------------------------------------------
# Robust parsing (self-contained, mirrors lead_icp_context_composer.py /
# lead_hq_ai_interpreter.py's tolerant-JSON-extraction pattern)
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


# ---------------------------------------------------------------------------
# Mechanical validation -- the AI never gets the final say on which sources
# exist, only which of the real ones support its verdict.
# ---------------------------------------------------------------------------

def _build_signal_from_verdict(
    signal_name: str,
    items: list[LeadEvidence],
    raw_entry: dict,
) -> LeadSignal:
    by_id = {ev.evidence_id: ev for ev in items if ev.evidence_id}

    raw_verdict = str((raw_entry or {}).get("verdict") or "").strip()
    verdict = raw_verdict if raw_verdict in _VALID_VERDICTS else "no_positive_match"

    raw_ids = (raw_entry or {}).get("supporting_evidence_ids")
    validated_ids: list[str] = []
    if isinstance(raw_ids, list):
        for raw_id in raw_ids:
            eid = str(raw_id or "").strip()
            if eid and eid in by_id and eid not in validated_ids:
                validated_ids.append(eid)

    # A positive verdict with no real supporting evidence is not trustworthy
    # -- the AI never gets the final say on which sources exist.
    if verdict != "no_positive_match" and not validated_ids:
        verdict = "no_positive_match"

    validated_evidence = [by_id[eid] for eid in validated_ids]

    evidence_urls: list[str] = []
    for ev in validated_evidence:
        url = (ev.source_url or "").strip()
        if url and url not in evidence_urls:
            evidence_urls.append(url)
    evidence_url = evidence_urls[0] if evidence_urls else None

    score = _VERDICT_TO_SCORE[verdict]
    has_url = evidence_url is not None
    if score == 2.0 and has_url:
        confidence = "High"
    elif score == 1.0 and has_url:
        confidence = "Medium"
    else:
        confidence = "Low"

    reason = str((raw_entry or {}).get("reason") or "").strip()
    if not reason:
        reason = (
            "AI verdict with no supporting evidence."
            if verdict == "no_positive_match"
            else f"AI verdict: {verdict}."
        )

    quote_source = validated_evidence or items
    evidence_quote = next(
        (e.source_snippet for e in quote_source if (e.source_snippet or "").strip()), None)
    evidence_title = next(
        (e.source_title for e in quote_source if (e.source_title or "").strip()), None)
    query_used = next((e.query_used for e in quote_source if e.query_used), None)

    return LeadSignal(
        signal_name=signal_name,
        signal_value=verdict,
        signal_score=score,
        signal_confidence=confidence,
        signal_reason=reason,
        evidence_url=evidence_url,
        evidence_urls=evidence_urls,
        evidence_quote=evidence_quote,
        evidence_title=evidence_title,
        query_used=query_used,
        parser_source="ai_signal_scorer",
        needs_manual_review=False,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def score_signals_with_ai(
    company_name: str,
    country: Optional[str],
    evidence_items: list[LeadEvidence],
    anthropic_api_key: str,
    ai_model: str = DEFAULT_AI_SIGNAL_SCORING_MODEL,
) -> AiSignalScoringResult:
    """Score the five supported non-HQ signals via one Anthropic call.

    Never raises: any failure (no key, no usable evidence, call error,
    unparseable response) yields ``call_success=False`` with an ``error``
    string and an empty ``signals`` list, so the caller can fall back to the
    deterministic extractor.
    """
    if not anthropic_api_key:
        return AiSignalScoringResult(
            model=ai_model, call_attempted=False, call_success=False,
            error="no_anthropic_api_key",
        )

    by_signal = _filter_usable_evidence_by_signal(evidence_items)
    if not by_signal:
        return AiSignalScoringResult(
            model=ai_model, call_attempted=False, call_success=False,
            error="no_usable_evidence",
        )

    prompt = build_ai_signal_scoring_prompt(
        company_name=company_name, country=country, by_signal=by_signal,
    )

    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=ai_model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        import usage_tracker
        usage_tracker.record_anthropic_response(response, ai_model, "ai_signal_scoring")
        raw_text = extract_anthropic_text(response)
    except Exception as exc:
        return AiSignalScoringResult(
            model=ai_model, call_attempted=True, call_success=False,
            error=f"ai_signal_scoring_call_failed: {str(exc)[:200]}",
        )

    data = _parse_response(raw_text)
    if not data:
        return AiSignalScoringResult(
            model=ai_model, call_attempted=True, call_success=False,
            error="ai_signal_scoring_parse_failed",
            raw_json=(raw_text or "")[:2000],
        )

    signals = [
        _build_signal_from_verdict(signal_name, items, data.get(signal_name) or {})
        for signal_name, items in by_signal.items()
    ]

    return AiSignalScoringResult(
        signals=signals,
        model=ai_model,
        call_attempted=True,
        call_success=True,
        error="",
        raw_json=(raw_text or "")[:2000],
    )
