"""AI composition of rich ICP context for Lead Prioritizer v2.

Explicit opt-in enrichment step, off by default and fully independent of
Step 3 caller-content composition (``lead_caller_content_composer.py``). It
recreates three fields the older ``enrich_clients_claude.py`` used to
produce via ``ICP_FIELDS`` (see that module, ``ICP_FIELDS`` around line 494
and ``_build_serper_queries`` around line 2854) — ``icp_buying_signals``,
``icp_likely_training_interest``, ``icp_potential_buyer_function`` — using
broader thematic Serper queries plus the curated non-HQ signals already on
the result, distilled via one Anthropic Messages API call.

Deliberately excluded, matching the legacy module's four competitor/
alternative-provider fields (``icp_competitor_signal``,
``icp_direct_language_competitor_signal``, ``icp_online_language_learning_signal``,
``icp_broader_lnd_platform_signal``): this module never asks for, and its
prompt explicitly forbids, competitor names or alternative-provider claims.

Same pattern as ``lead_caller_content_composer.py``: an explicit
``anthropic_api_key`` parameter, a JSON-only prompt, tolerant parsing
(markdown fences / prose around the JSON object are stripped, self-contained
— no cross-module import, mirroring the other AI-step modules), and a
function that never raises. Any failure (no key, call error, unparseable
response) yields ``call_success=False`` so the caller can leave the
``icp_*`` fields blank.

Evidence collection here is entirely separate from Step 2
(``lead_non_hq_enrichment.collect_non_hq_enrichment_evidence``): the broader
queries built by ``build_icp_context_queries`` are never written to
``LeadPrioritizationResult.evidence_items`` and are never read by
``extract_non_hq_signals`` or ``commercial_fit_scoring`` — this feature can
never influence scoring.
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

from hq_simple_detector import build_simple_hq_query, is_hosted_careers_platform_domain
from lead_non_hq_enrichment import call_serper_for_enrichment, extract_evidence_from_serper_payload

# Default model — same family/tier as the caller-content composer.
DEFAULT_ICP_CONTEXT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class IcpContextResult:
    """Result of one AI ICP-context composition attempt."""
    buying_signals: Optional[str] = None
    likely_training_interest: Optional[str] = None
    potential_buyer_function: Optional[str] = None
    model: str = DEFAULT_ICP_CONTEXT_MODEL
    call_attempted: bool = False
    call_success: bool = False
    error: str = ""
    raw_json: Optional[str] = None


# ---------------------------------------------------------------------------
# Broader thematic queries (in the spirit of enrich_clients_claude.py's
# _build_serper_queries Q1/Q3/Q4 — general context, L&D/training, language/
# global teams — deliberately WITHOUT its Q2 international-footprint query
# (already covered by Step 2's own international_profile signal) and WITHOUT
# its Q5 competitor/online-learning-tool query.)
# ---------------------------------------------------------------------------

def build_icp_context_queries(root: str) -> list[dict]:
    """Return up to 3 broader thematic Serper query specs for ICP context.

    Each spec is ``{"label": str, "query": str}``. Returns ``[]`` for a blank
    root. No competitor, alternative-provider, or online-learning-platform
    query is ever produced.
    """
    r = (root or "").strip()
    if not r:
        return []
    return [
        {
            "label": "general_company_context",
            "query": f"{r} company overview about products services headquarters",
        },
        {
            "label": "lnd_employee_training",
            "query": f"{r} learning and development training academy onboarding "
                     "talent development",
        },
        {
            "label": "language_global_teams",
            "query": f"{r} global teams international employees multilingual "
                     "language training",
        },
    ]


def collect_icp_context_evidence(
    company_name: str,
    domain: Optional[str],
    serper_api_key: str,
    max_evidence_per_query: int = 3,
) -> list[dict]:
    """Collect broader ICP-context evidence for the 3 thematic queries above.

    Fully independent of Step 2 non-HQ evidence collection: the returned list
    is never written to ``LeadPrioritizationResult.evidence_items`` and is
    never read by signal extraction or scoring — it only ever feeds the
    ``compose_icp_context`` prompt.

    Hosted careers-platform evidence (Workday, Greenhouse, Lever, ...) is
    excluded here, before it can reach the prompt — a display/quality guard
    only, mirroring the scoring-side guard in
    ``lead_non_hq_signal_extractor.py``, not a scoring decision itself.
    """
    root, _ = build_simple_hq_query(company_name, domain)
    specs = build_icp_context_queries(root)
    out: list[dict] = []
    for spec in specs:
        payload = call_serper_for_enrichment(
            spec["query"], serper_api_key, usage_kind="icp_context")
        items = extract_evidence_from_serper_payload(
            payload, signal_name=spec["label"], query_used=spec["query"],
            max_items=max_evidence_per_query,
        )
        for item in items:
            if is_hosted_careers_platform_domain(item.source_url):
                continue
            evidence_text = (item.source_snippet or item.source_title or "").strip()
            if not evidence_text:
                continue
            out.append({
                "label": spec["label"],
                "evidence": evidence_text,
                "source_url": item.source_url,
            })
    return out


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a B2B sales research assistant identifying ICP (ideal customer "
    "profile) buying context for a lead. Use ONLY the evidence supplied in "
    "the user message -- never invent facts, company details, or evidence "
    "that is not present in the input. Never mention competitor names, "
    "alternative training providers, or online-learning-platform "
    "comparisons -- that is out of scope for this task. When the supplied "
    "evidence is thin, keep the text short and hedge appropriately rather "
    "than inventing confidence. "
    "Reply ONLY with a valid JSON object -- no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Company: {company_name}
Country: {country}

Curated non-HQ signals (already quality-checked -- use verbatim, do not
invent additional signals or evidence beyond what is listed):
{curated_signals_text}

Additional broader context evidence (general company / L&D / global-teams
research -- use verbatim, do not invent additional evidence):
{extra_evidence_text}

Return JSON with exactly these keys:
{{
  "icp_buying_signals": "1-3 sentences on concrete buying signals for this lead",
  "icp_likely_training_interest": "1-2 sentences on the likely training/L&D interest area",
  "icp_potential_buyer_function": "the most likely buyer function/department (e.g. HR, L&D, Talent)"
}}

Rules:
- Base every claim only on the evidence above; never invent facts.
- Never mention competitor names, alternative training providers, or
  online-learning-platform comparisons.
- If evidence is thin, offer a cautious, checkable hypothesis rather than an
  unsupported confident claim.
"""


def _format_evidence_list(items: list) -> str:
    if not items:
        return "  (none)"
    lines = []
    for item in items:
        label = item.get("label") or item.get("signal_name") or "signal"
        evidence = item.get("evidence") or ""
        url = item.get("source_url") or item.get("evidence_url") or ""
        line = f"  - {label}: {evidence}"
        if url:
            line += f" (source: {url})"
        lines.append(line)
    return "\n".join(lines)


def build_icp_context_prompt(
    *,
    company_name: str,
    country: Optional[str],
    curated_signals: list,
    extra_evidence: list,
) -> str:
    """Build the user message (no secrets)."""
    return _USER_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        country=country or "(unknown)",
        curated_signals_text=_format_evidence_list(curated_signals),
        extra_evidence_text=_format_evidence_list(extra_evidence),
    )


# ---------------------------------------------------------------------------
# Robust parsing (self-contained, no cross-module import — mirrors the same
# tolerant-JSON-extraction pattern used by lead_caller_content_composer.py /
# lead_hq_ai_interpreter.py / lead_hq_sonnet_adjudicator.py)
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


# ---------------------------------------------------------------------------
# Composition entry point
# ---------------------------------------------------------------------------

def compose_icp_context(
    *,
    company_name: str,
    country: Optional[str] = None,
    curated_signals: Optional[list] = None,
    extra_evidence: Optional[list] = None,
    anthropic_api_key: str = "",
    ai_model: str = DEFAULT_ICP_CONTEXT_MODEL,
) -> IcpContextResult:
    """Compose rich ICP context via the Anthropic Messages API.

    Never raises: any failure (no key, call error, unparseable response)
    yields ``call_success=False`` with a short ``error`` string.
    """
    if not anthropic_api_key:
        return IcpContextResult(
            model=ai_model, call_attempted=False, call_success=False,
            error="no_anthropic_api_key",
        )

    prompt = build_icp_context_prompt(
        company_name=company_name, country=country,
        curated_signals=curated_signals or [], extra_evidence=extra_evidence or [],
    )

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
        usage_tracker.record_anthropic_response(response, ai_model, "icp_context")
        raw_text = extract_anthropic_text(response)
    except Exception as exc:
        return IcpContextResult(
            model=ai_model, call_attempted=True, call_success=False,
            error=f"icp_context_call_failed: {str(exc)[:200]}",
        )

    data = _parse_response(raw_text)
    if not data:
        return IcpContextResult(
            model=ai_model, call_attempted=True, call_success=False,
            error="icp_context_parse_failed",
            raw_json=(raw_text or "")[:2000],
        )

    return IcpContextResult(
        buying_signals=(str(data.get("icp_buying_signals") or "").strip() or None),
        likely_training_interest=(
            str(data.get("icp_likely_training_interest") or "").strip() or None),
        potential_buyer_function=(
            str(data.get("icp_potential_buyer_function") or "").strip() or None),
        model=ai_model,
        call_attempted=True,
        call_success=True,
        error="",
        raw_json=(raw_text or "")[:2000],
    )
