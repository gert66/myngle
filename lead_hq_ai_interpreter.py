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

try:
    import openai as _openai_lib
except ImportError:
    _openai_lib = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from lead_output_schema import HQDetectionResult, LeadInput

# Default model for AI HQ interpretation
_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"

# Experimental OpenAI provider (opt-in only; production default stays
# Anthropic). Same prompt, parser, and post-AI scoring — only the API call
# differs.
SUPPORTED_AI_PROVIDERS = ("anthropic", "openai")
DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"
SUPPORTED_OPENAI_MODELS = ("gpt-5.4-nano", "gpt-5.4-mini")

# ---------------------------------------------------------------------------
# Model pricing for cost comparison (audit only — never affects scoring)
# ---------------------------------------------------------------------------
# USD per MILLION tokens as ``(input_price, output_price)``. Used only to fill
# the ``ai_hq_estimated_cost_usd`` audit field. A model mapped to ``None`` (or
# absent from the table) gets a BLANK estimated cost — never a guessed one;
# token counts are still recorded so costs can be computed later.
#
# IMPORTANT: these are provisional prices for cost-comparison purposes only —
# verify against the provider pricing pages before any production cost
# analysis, and update the values here (not elsewhere) when they change:
#   - Anthropic: https://www.anthropic.com/pricing
#   - OpenAI:    https://openai.com/api/pricing/
MODEL_PRICING_USD_PER_MTOK: dict[str, "tuple[float, float] | None"] = {
    # Anthropic — Claude Haiku 4.5
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    # OpenAI (experimental, provisional pricing — verify before production use)
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
}


def estimate_ai_cost_usd(model, input_tokens, output_tokens):
    """Estimated USD cost for one call, or None when pricing/tokens unknown."""
    pricing = MODEL_PRICING_USD_PER_MTOK.get(str(model or ""))
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    try:
        in_price, out_price = pricing
        return round(
            (float(input_tokens) * in_price + float(output_tokens) * out_price)
            / 1_000_000, 6)
    except (TypeError, ValueError):
        return None


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


def _extract_json_object(text: str) -> str:
    """Best-effort isolation of the JSON object in an AI response string.

    Drops markdown fences (```json … ```) and any prose before/after the object
    by keeping the span from the first '{' to the last '}'.
    """
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
    """Recover core HQ fields with conservative regex when json.loads fails.

    A malformed/truncated free-text field (typically ``reason``) must not make
    the whole row fail when classification / confidence / country are still
    recoverable.  Only simple JSON string fields are matched, so a broken later
    field does not corrupt the earlier ones.
    """
    text = str(raw or "")

    def _str_field(name: str) -> str:
        m = re.search(
            rf'"{re.escape(name)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return ""
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    return {
        "classification":    _str_field("classification"),
        "confidence":        _str_field("confidence"),
        "parent_company":    _str_field("parent_company"),
        "parent_hq_country": _str_field("parent_hq_country"),
        "parent_hq_city":    _str_field("parent_hq_city"),
        "evidence_url":      _str_field("evidence_url"),
        "evidence_quote":    _str_field("evidence_quote"),
        "reason":            _str_field("reason"),
    }


def _parse_ai_response(raw: str) -> dict:
    """Extract the JSON object from the AI response text.

    Tolerates markdown fences and prose around the JSON, then — if strict
    parsing fails — recovers the core fields by regex so a truncated/malformed
    ``reason`` does not discard an otherwise usable classification.

    Returns ``{}`` only when nothing usable (not even a classification) could
    be recovered, so the caller can route to manual review.
    """
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

    # Strict parse failed — recover what we can.
    fields = _regex_extract_core_fields(raw)
    if fields.get("classification"):
        return fields
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
        "the netherlands": "netherlands", "nederland": "netherlands",
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
        "br": "brazil", "bra": "brazil", "brasil": "brazil",
        "brazil": "brazil", "brazilian": "brazil",
        "uy": "uruguay", "ury": "uruguay", "uruguay": "uruguay",
        "uruguayan": "uruguay", "república oriental del uruguay": "uruguay",
    }
    text = re.sub(r"\s+", " ", re.sub(r"\.", "", str(value or "").strip().lower()))
    return _MAP.get(text, text)


# ---------------------------------------------------------------------------
# C4 — positive-score safety layer
# ---------------------------------------------------------------------------

# Small generic acronym / short-root set that is prone to matching a DIFFERENT
# international entity than the target company (not company-specific exceptions).
_RISKY_ROOTS = frozenset({
    "fiap", "fia", "sh", "vr", "rnp", "uol", "pbh", "rj", "sp", "bild",
})

# Multi-label public suffixes used to guard the "lead is a subdomain of
# evidence" match against public-suffix artifacts (e.g. matching on "com.br").
_PUBLIC_SUFFIXES = frozenset({
    "com.br", "com.au", "co.uk", "co.jp", "co.in", "com.mx", "com.ar",
    "co.za", "com.tr", "com.cn",
})


def _host_from(url_or_domain) -> str:
    """Return a bare lowercase host (no scheme / www / path) or ""."""
    s = str(url_or_domain or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)   # drop scheme
    s = s.split("/")[0].split("?")[0].split("#")[0]  # drop path/query/fragment
    s = s.split(":")[0]                              # drop port
    s = re.sub(r"^www\.", "", s)
    return s.strip().strip(".")


def _hosts_match(lead_host: str, ev_host: str) -> bool:
    """True when the evidence host plausibly belongs to the lead domain.

    - exact host match, or
    - evidence host is a subdomain of the lead host, or
    - lead host is a subdomain of the evidence host, but only when the evidence
      host is a real registrable domain and not a bare public-suffix artifact.
    """
    if not lead_host or not ev_host:
        return False
    if lead_host == ev_host:
        return True
    if ev_host.endswith("." + lead_host):
        return True
    if lead_host.endswith("." + ev_host):
        return ev_host not in _PUBLIC_SUFFIXES and ev_host.count(".") >= 1
    return False


def evaluate_hq_positive_score_safety(
    *,
    lead_domain,
    domain_root: str,
    evidence_url,
) -> dict:
    """Decide whether a provisional positive foreign-HQ score is safe to keep.

    Deterministic and evidence-only (no competitor evidence, no network). Returns
    an audit dict; ``suppress`` is True only for a risky short/generic domain
    root whose supporting evidence URL is blank or does not match the lead
    domain — i.e. the evidence likely belongs to a different company.
    """
    root = (domain_root or "").strip().lower()
    risky = len(root) <= 4 or root in _RISKY_ROOTS

    lead_host = _host_from(lead_domain)
    ev_host = _host_from(evidence_url)
    has_evidence_url = bool(ev_host)
    evidence_match = _hosts_match(lead_host, ev_host) if has_evidence_url else False
    mismatch_warning = has_evidence_url and not evidence_match

    suppress = risky and ((not has_evidence_url) or (not evidence_match))

    return {
        "risky": risky,
        "has_evidence_url": has_evidence_url,
        "evidence_match": evidence_match,
        "mismatch_warning": mismatch_warning,
        "suppress": suppress,
        "lead_host": lead_host,
        "evidence_host": ev_host,
    }


def _usage_field(usage_obj, *names):
    """First present integer attribute from a provider usage object, or None."""
    for name in names:
        value = getattr(usage_obj, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _call_anthropic_hq(api_key: str, model: str, user_msg: str) -> tuple[str, dict]:
    """One Anthropic HQ call. Returns ``(raw_text, usage)``; raises on failure."""
    if _anthropic_lib is None:
        raise ImportError("anthropic package not installed")
    client = _anthropic_lib.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw_text = response.content[0].text if response.content else ""
    usage_obj = getattr(response, "usage", None)
    input_tokens = _usage_field(usage_obj, "input_tokens")
    output_tokens = _usage_field(usage_obj, "output_tokens")
    total = (input_tokens + output_tokens
             if input_tokens is not None and output_tokens is not None else None)
    return raw_text, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }


def _call_openai_hq(api_key: str, model: str, user_msg: str) -> tuple[str, dict]:
    """One OpenAI HQ call — same system prompt and user message as the
    Anthropic path. Returns ``(raw_text, usage)``; raises on failure."""
    if _openai_lib is None:
        raise ImportError("openai package not installed")
    client = _openai_lib.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw_text = (
        response.choices[0].message.content if getattr(response, "choices", None)
        else ""
    ) or ""
    usage_obj = getattr(response, "usage", None)
    input_tokens = _usage_field(usage_obj, "prompt_tokens", "input_tokens")
    output_tokens = _usage_field(usage_obj, "completion_tokens", "output_tokens")
    total = _usage_field(usage_obj, "total_tokens")
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    return raw_text, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }


def interpret_hq_with_ai(
    *,
    lead_input: "LeadInput",
    domain_root: str,
    query: str,
    serper_payload: dict,
    anthropic_api_key: str,
    model: str = _DEFAULT_AI_MODEL,
    ai_provider: str = "anthropic",
    openai_api_key: str = "",
) -> "HQDetectionResult":
    """Interpret HQ from a Serper payload using Anthropic (Haiku by default).

    ``ai_provider`` selects the experimental OpenAI path ("openai") — same
    system prompt, user message, JSON parser, and post-AI scoring; only the
    API call differs. The default ("anthropic") is byte-for-byte the existing
    behavior. Usage/cost audit fields (``ai_hq_provider`` /
    ``ai_hq_*_tokens`` / ``ai_hq_estimated_cost_usd``) are populated when the
    provider reports usage; they stay blank rather than guessed.

    Returns an ``HQDetectionResult`` with AI audit fields populated.
    The only deterministic post-AI step is a normalised country comparison.
    """
    from lead_output_schema import HQDetectionResult

    provider = (ai_provider or "anthropic").strip().lower()

    _base = dict(
        domain_root=domain_root,
        query_used=query,
        ai_hq_model=model,
        ai_hq_provider=provider,
        ai_call_attempted="Yes",
    )

    if provider not in SUPPORTED_AI_PROVIDERS:
        return HQDetectionResult(
            **{**_base, "ai_call_attempted": "No"},
            ai_hq_error=f"unknown_ai_provider: {ai_provider}",
            needs_manual_review=True,
            hq_reason=f"ai_hq_not_eligible: unknown provider {ai_provider}",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    provider_api_key = openai_api_key if provider == "openai" else anthropic_api_key
    if not provider_api_key:
        return HQDetectionResult(
            **{**_base, "ai_call_attempted": "No"},
            ai_hq_error=f"no_{provider}_api_key",
            needs_manual_review=True,
            hq_reason="ai_hq_not_eligible: no API key",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    # ── Call the selected provider ───────────────────────────────────────────
    try:
        user_msg = _build_user_message(
            domain_root=domain_root,
            input_country=lead_input.input_country or "",
            query=query,
            serper_payload=serper_payload,
        )
        if provider == "openai":
            raw_text, usage = _call_openai_hq(provider_api_key, model, user_msg)
        else:
            raw_text, usage = _call_anthropic_hq(provider_api_key, model, user_msg)
    except Exception as exc:
        return HQDetectionResult(
            **_base,
            ai_hq_error=f"{provider}_call_failed: {exc}",
            needs_manual_review=True,
            hq_reason=f"ai_hq_error: {exc}",
            sig_foreign_hq_score_for_next_scoring=None,
        )

    _base.update(
        ai_hq_input_tokens=usage.get("input_tokens"),
        ai_hq_output_tokens=usage.get("output_tokens"),
        ai_hq_total_tokens=usage.get("total_tokens"),
        ai_hq_estimated_cost_usd=estimate_ai_cost_usd(
            model, usage.get("input_tokens"), usage.get("output_tokens")),
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
        parser_source="ai_first",
        ai_hq_raw_json=(raw_text or "")[:2000],
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
        # C4 positive-score safety: keep the foreign_parent classification, but
        # only keep score 3.0 when the evidence appears to belong to this
        # company/domain. Risky short/generic roots with blank/mismatched
        # evidence are routed to manual review at score 0.0.
        _safety = evaluate_hq_positive_score_safety(
            lead_domain=lead_input.domain,
            domain_root=domain_root,
            evidence_url=ev_url,
        )
        _audit = dict(
            hq_query_risk_flag="Yes" if _safety["risky"] else "No",
            hq_evidence_domain_match=(
                "Yes" if _safety["evidence_match"]
                else ("No" if _safety["has_evidence_url"] else "")
            ),
            hq_evidence_domain_mismatch_warning="Yes" if _safety["mismatch_warning"] else "No",
        )
        if _safety["suppress"]:
            return HQDetectionResult(
                **_base, **ai_fields, **_audit,
                ai_call_success="Yes",
                foreign_hq_simple=True,
                hq_structure_type="foreign_parent",
                needs_manual_review=True,
                hq_reason=(
                    "foreign_parent_score_suppressed_for_review: risky domain root "
                    "and evidence URL does not match lead domain"
                    + (f" | {ai_reason}" if ai_reason else "")
                ),
                sig_foreign_hq_score_for_next_scoring=0.0,
                hq_positive_score_suppressed_for_review="Yes",
                hq_review_reason=(
                    "risky domain root and evidence URL does not match lead domain"
                ),
            )
        return HQDetectionResult(
            **_base, **ai_fields, **_audit,
            ai_call_success="Yes",
            foreign_hq_simple=True,
            hq_structure_type="foreign_parent",
            needs_manual_review=False,
            hq_reason=f"foreign_parent ({confidence}): {ai_reason}",
            sig_foreign_hq_score_for_next_scoring=3.0,
            hq_positive_score_suppressed_for_review="No",
            hq_review_reason="",
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
