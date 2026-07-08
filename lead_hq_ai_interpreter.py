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
import time
from typing import TYPE_CHECKING, Optional

import api_retry

try:
    import anthropic as _anthropic_lib
except ImportError:
    _anthropic_lib = None  # type: ignore[assignment]

try:
    import openai as _openai_lib
except ImportError:
    _openai_lib = None  # type: ignore[assignment]

from lead_country_config import normalize_country_for_hq as _normalize_country_for_hq

if TYPE_CHECKING:
    from lead_output_schema import HQDetectionResult, LeadInput

# Default model for AI HQ interpretation
_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"

# Experimental OpenAI / DeepSeek providers (opt-in only; production default
# stays Anthropic). Same prompt, parser, and post-AI scoring — only the API
# call differs.
SUPPORTED_AI_PROVIDERS = ("anthropic", "openai", "deepseek")
DEFAULT_OPENAI_MODEL = "gpt-5.4-nano"
SUPPORTED_OPENAI_MODELS = ("gpt-5.4-nano", "gpt-5.4-mini")

# DeepSeek uses the OpenAI-compatible chat completions API at a different
# base_url (see _call_deepseek_hq) — same openai client package, no separate
# SDK dependency.
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
SUPPORTED_DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

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
    # DeepSeek (experimental, provisional pricing, cache-miss input price —
    # verify at https://api-docs.deepseek.com/quick_start/pricing before
    # production use)
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
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


# Anthropic prompt-caching price multipliers (applied to the model's normal
# input price) for the default 5-minute-TTL ephemeral cache used by
# _call_anthropic_hq — NOT the 1-hour cache, which is priced differently and
# is not used here. See https://www.anthropic.com/pricing.
_CACHE_WRITE_PRICE_MULTIPLIER = 1.25
_CACHE_READ_PRICE_MULTIPLIER = 0.1


def estimate_ai_cost_usd_with_cache(
    model, input_tokens, output_tokens,
    cache_creation_input_tokens=None, cache_read_input_tokens=None,
):
    """Cache-aware estimated USD cost for one call, or None when pricing/base
    tokens are unknown.

    Kept as a separate function (rather than changing ``estimate_ai_cost_usd``'s
    signature) so existing callers of the plain per-token estimate are
    unaffected. ``input_tokens``/``output_tokens`` are billed at the normal
    per-model rate exactly like ``estimate_ai_cost_usd``; on top of that,
    ``cache_creation_input_tokens`` (a cache write) is billed at
    ``_CACHE_WRITE_PRICE_MULTIPLIER`` times the input price and
    ``cache_read_input_tokens`` (a cache hit) at
    ``_CACHE_READ_PRICE_MULTIPLIER`` times the input price. Missing cache
    fields (older SDK / no caching used) are treated as zero, so this
    reduces to ``estimate_ai_cost_usd``'s result when no caching occurred.
    """
    pricing = MODEL_PRICING_USD_PER_MTOK.get(str(model or ""))
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    try:
        in_price, out_price = pricing
        cache_creation = float(cache_creation_input_tokens or 0)
        cache_read = float(cache_read_input_tokens or 0)
        total = (
            float(input_tokens) * in_price
            + float(output_tokens) * out_price
            + cache_creation * in_price * _CACHE_WRITE_PRICE_MULTIPLIER
            + cache_read * in_price * _CACHE_READ_PRICE_MULTIPLIER
        )
        return round(total / 1_000_000, 6)
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
    gl: Optional[str] = None,
    hl: Optional[str] = None,
    cache_index: Optional[dict] = None,
    force_refresh: bool = False,
    max_429_retries: int = api_retry.DEFAULT_MAX_RETRIES,
) -> dict:
    """Fire a single Serper search and return the raw JSON payload.

    The query must already be built by ``build_simple_hq_query()``; this
    function does not construct queries itself.

    Returns an empty dict on any error so callers can treat it defensively.
    A 429 (rate limited) is retried up to ``max_429_retries`` times with
    backoff before giving up and returning ``{}``, same as any other error.

    ``gl``/``hl`` (Serper's country/language params) are only included in
    the request when explicitly given — mirrors
    ``lead_non_hq_enrichment.call_serper_for_enrichment`` exactly, so a
    caller that doesn't pass them keeps the exact request shape used today.
    The caller (``prioritize_single_lead``) resolves them from the lead's
    effective country via ``lead_country_config.gl_hl_for_hq_country`` —
    this function itself does no country lookup.

    ``cache_index`` (default ``None``) is an optional in-memory, GCS-backed
    shared cache index (see ``enrichment_cache.py``), keyed on
    ``domain_root`` + signal type ``"hq"`` (not on ``gl``/``hl`` — an HQ fact
    doesn't change with search locale). When ``None`` — the default —
    behavior is completely unchanged from before this parameter existed:
    every call hits Serper live. When provided, a fresh-enough cached
    response is returned without a network call; a miss still calls Serper
    live and stores the raw JSON payload back into ``cache_index`` (the
    caller is responsible for eventually persisting ``cache_index`` to GCS
    via ``enrichment_cache.save_cache_index``).
    """
    import urllib.error
    import urllib.request
    import usage_tracker

    if not serper_api_key or not query:
        return {}

    if cache_index is not None:
        import enrichment_cache
        cached = enrichment_cache.get_cached(
            cache_index, "serper", domain_root, "hq",
            ttl_days=enrichment_cache.serper_ttl_days("hq"),
            force_refresh=force_refresh,
        )
        if cached is not None:
            usage_tracker.record_cache_hit("serper")
            return cached
        usage_tracker.record_cache_miss("serper")

    usage_tracker.record_serper_call("hq")
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

    if cache_index is not None and result:
        enrichment_cache.put_cached(
            cache_index, "serper", domain_root, "hq", response=result)
    return result


# ---------------------------------------------------------------------------
# AI interpreter
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a corporate structure analyst. "
    "Given the company's own website content plus search-engine results, "
    "determine where its ultimate parent group headquarters is located, and "
    "identify the company's industry/sector from the same material. "
    "Treat the company's own website content as the primary, most-authoritative "
    "source; use the search-engine results only as secondary corroboration. "
    "Reply ONLY with a valid JSON object — no prose, no markdown fences."
)


def _known_industry_categories() -> str:
    """Comma-separated industry categories the deterministic sector detector
    already knows (``lead_non_hq_signal_extractor._SECTOR_KEYWORD_MAP``),
    shown to the AI purely as style guidance for consistent labeling — never
    a hard enum. Function-level import avoids a module-level dependency
    between the two files; returns "" (silently) if that module is
    unavailable for any reason, so prompt-building can never fail on this."""
    try:
        from lead_non_hq_signal_extractor import _SECTOR_KEYWORD_MAP
        seen: list[str] = []
        for industry, _sub in _SECTOR_KEYWORD_MAP.values():
            if industry not in seen:
                seen.append(industry)
        return ", ".join(seen)
    except Exception:
        return ""


# Per-lead part of the user message: domain_root, input_country, query, and
# the Serper/own-site text all differ between leads, so this is formatted
# fresh for every call (see _build_user_message). Never cached.
_USER_TEMPLATE_PER_LEAD = """\
Company domain root: {domain_root}
Input country (where the local entity operates): {input_country}

PRIMARY SOURCE — the company's own website content (most authoritative;
redirects followed). Base your answer on this first when it is present:
{own_site_text}

SECONDARY SOURCE — search results for query: "{query}"

Knowledge Graph:
{kg_text}

Answer Box:
{ab_text}

Top organic results (title + snippet):
{organic_text}
"""

# ADDITIONAL CONTEXT block — appended (never baked into the template above)
# ONLY when the own-domain Firecrawl content is thin/absent (Lusha
# enrichment plan, Stap 5 scope correction). Rich own-domain content
# already gives the classifier enough to work with; adding generic Lusha
# marketing text on top of that was observed to occasionally tip a
# genuinely ambiguous case's classification (e.g. a dual-HQ multinational)
# without ever changing the actual score (C4 already catches that), so it
# is now withheld entirely for well-evidenced companies -- not even a
# "(none)" placeholder, to keep the prompt (and cost) identical to before
# Stap 5 for every row where it isn't needed.
_LUSHA_CONTEXT_BLOCK_TEMPLATE = """
ADDITIONAL CONTEXT — company's own self-description (structured company
data, not the live website; lower authority than the primary and
secondary sources above — use this only to fill gaps when those are thin
or silent, never to override them):
{lusha_context_text}
"""

# A company's own-domain Firecrawl content counts as "thin" when the
# combined stripped text across all crawled pages is under this many
# characters (or no pages were crawled at all). Validated against real
# data: a company with genuinely no usable own-site content measures 0
# chars (e.g. "5265 studio"/5265.it), while every well-evidenced company
# checked measured 5,400+ chars (Sika, TE Connectivity) up to 15,000
# (dsm.com, Nestle) -- a wide gap with no borderline cases near 400, so
# this threshold cleanly separates "nothing useful" from "already enough".
_OWN_SITE_CONTENT_THIN_THRESHOLD_CHARS = 400

# IMPORTANT — prompt-cache boundary. Together with ``_SYSTEM_PROMPT``, this
# text forms the single ``cache_control``-marked system block sent on every
# Anthropic HQ call (see ``_call_anthropic_hq``). For Anthropic's prompt
# cache to ever hit, this string MUST be byte-for-byte identical across every
# call: never insert a date, run ID, lead-specific value, or per-country text
# here — even one differing character anywhere in this block turns what
# should be a cheap cache read into a fresh (more expensive) cache write for
# that call. ``known_industries`` is resolved exactly once, below, precisely
# because it is a fixed, process-wide constant derived from a static keyword
# map — never a per-lead or per-country value.
_USER_TEMPLATE_STATIC_INSTRUCTIONS = """\
Classify and return JSON with these exact keys:
- "classification": one of "foreign_parent", "domestic", "regional_branch_only", "unclear"
- "confidence": one of "High", "Medium", "Low"
- "parent_company": name of the ultimate parent group (empty string if unknown)
- "parent_hq_country": country of the ultimate parent HQ (empty string if unknown)
- "parent_hq_city": city of the ultimate parent HQ (empty string if unknown)
- "evidence_url": best source URL from the material above (empty string if none)
- "evidence_urls": ALL URLs (from the primary or secondary material above) that support your answer, best first (empty list if none)
- "evidence_quote": short verbatim quote supporting your answer (max 200 chars, empty if none)
- "reason": one short sentence explaining your classification
- "industry": the company's industry/sector, as a short, concise category
  label (e.g. "Power electronics", "Industrial software") derived from what
  the material actually says the company makes/does. Reuse one of these
  existing categories when it genuinely fits, for labeling consistency:
  {known_industries}
  Otherwise, use your own concise, standard category label. Empty string
  only when the material gives no basis at all to judge this.
- "sub_industry": a more specific sub-category when the material supports one
  (e.g. "Resins" under "Chemicals"), empty string otherwise.

Rules:
- Use "foreign_parent" when the ultimate controlling group/parent HQ is in a
  DIFFERENT country than input_country. This applies EVEN IF this specific
  entity is described as a regional/local branch, subsidiary, or division:
  being a local branch of a foreign-headquartered group is still foreign_parent.
- Use "domestic" when the ultimate HQ is in the SAME country as input_country.
- Use "regional_branch_only" ONLY when the entity is a regional/local branch
  AND its ultimate/global parent HQ is in the SAME country as input_country, or
  the parent's country genuinely cannot be determined from the material.
- Use "unclear" only when evidence is contradictory or absent.
- Never invent information not present in the supplied material (neither the
  own-website content nor the search results). Prefer the own-website content
  when the two sources disagree.
- "industry"/"sub_industry" are independent of the HQ classification above:
  give your best answer for them even when the HQ classification itself is
  "unclear", as long as the material describes what the company does.
""".format(known_industries=_known_industry_categories() or "(none available)")


def _format_own_site_pages(crawled_pages) -> str:
    """Render the crawled own-domain pages for the primary-source section.

    Each page is a ``{"url", "text", ...}`` dict as produced by
    ``lead_hq_firecrawl_source.collect_own_domain_hq_pages``. Returns
    ``"  (none — no own-website content was retrieved)"`` when there is
    nothing, so the classifier knows the primary source was absent.
    """
    pages = [p for p in (crawled_pages or []) if (p.get("text") or "").strip()]
    if not pages:
        return "  (none — no own-website content was retrieved)"
    lines = []
    for page in pages:
        text = (page.get("text") or "").strip()[:1500]
        lines.append(f"  URL: {page.get('url')}\n    {text}")
    return "\n".join(lines)


def _own_site_content_is_thin(crawled_pages) -> bool:
    """True when the company's own-domain Firecrawl content gives the
    classifier little/nothing to work with — no pages at all, or a
    combined (stripped, pre-truncation) text length under
    ``_OWN_SITE_CONTENT_THIN_THRESHOLD_CHARS`` across every crawled page.
    Used to gate whether the Lusha ADDITIONAL CONTEXT section is even
    considered (see ``_build_user_message``) — a well-evidenced company
    never needs it.
    """
    pages = [p for p in (crawled_pages or []) if (p.get("text") or "").strip()]
    if not pages:
        return True
    combined_len = sum(len((p.get("text") or "").strip()) for p in pages)
    return combined_len < _OWN_SITE_CONTENT_THIN_THRESHOLD_CHARS


# Lusha enrichment plan, Stap 5 — truncation lengths for the free,
# already-in-hand Lusha Company Description / Company Specialties text
# added to the per-lead ADDITIONAL CONTEXT section. Sized relative to the
# other per-lead budgets already in this file (own-site pages: 1500
# chars/page; KG/answer-box text: ~400 chars; an organic snippet: 200
# chars): a description is normally a few sentences of free-form prose
# (needs more room than a single search snippet, but far less than a full
# crawled page), a specialties field is just a short comma-separated
# keyword list (a few words are enough to be useful).
_LUSHA_DESCRIPTION_MAX_CHARS = 800
_LUSHA_SPECIALTIES_MAX_CHARS = 300


def _format_lusha_context(description, specialties) -> str:
    """Render the Lusha Description/Specialties ADDITIONAL CONTEXT section.

    Returns ``"  (none)"`` when both are blank — same convention as the
    other per-lead sections (``kg_text``/``ab_text``/``organic_text``) —
    so a non-Lusha caller (or a Lusha row with both fields blank) produces
    an identical section to what a caller passing nothing at all would.
    """
    desc = (description or "").strip()[:_LUSHA_DESCRIPTION_MAX_CHARS]
    spec = (specialties or "").strip()[:_LUSHA_SPECIALTIES_MAX_CHARS]
    lines = []
    if desc:
        lines.append(f"  Description: {desc}")
    if spec:
        lines.append(f"  Specialties: {spec}")
    return "\n".join(lines) if lines else "  (none)"


def _build_user_message(
    *,
    domain_root: str,
    input_country: str,
    query: str,
    serper_payload: dict,
    crawled_pages=None,
    lusha_description=None,
    lusha_specialties=None,
) -> str:
    own_site_text = _format_own_site_pages(crawled_pages)

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

    message = _USER_TEMPLATE_PER_LEAD.format(
        domain_root=domain_root,
        input_country=input_country or "(unknown)",
        query=query,
        own_site_text=own_site_text,
        kg_text=kg_text,
        ab_text=ab_text,
        organic_text=organic_text,
    )

    # ADDITIONAL CONTEXT (Lusha Description/Specialties) is appended ONLY
    # when the own-domain content above is thin/absent (Stap 5 scope
    # correction) — a well-evidenced company's prompt is byte-for-byte
    # identical to before this feature existed, no extra tokens, no extra
    # risk of a generic marketing blurb tipping an already-clear case.
    if _own_site_content_is_thin(crawled_pages):
        lusha_context_text = _format_lusha_context(lusha_description, lusha_specialties)
        message += _LUSHA_CONTEXT_BLOCK_TEMPLATE.format(lusha_context_text=lusha_context_text)

    return message


def _known_urls_from_serper_payload(serper_payload: dict) -> list:
    """URLs actually present in the supplied Serper payload, in the same
    order they were shown to the model (KG, answer box, then organic) — used
    to validate ``evidence_urls`` so the AI can never invent a source URL
    that was never in the material it was given."""
    urls: list[str] = []

    def _add(url) -> None:
        url = (url or "").strip()
        if url and url not in urls:
            urls.append(url)

    kg: dict = serper_payload.get("knowledgeGraph") or {}
    _add(kg.get("website"))
    _add(kg.get("descriptionLink"))

    ab: dict = serper_payload.get("answerBox") or {}
    _add(ab.get("link"))

    for item in (serper_payload.get("organic") or [])[:5]:
        if isinstance(item, dict):
            _add(item.get("link"))

    return urls


def _known_urls_for_hq(serper_payload: dict, crawled_pages=None) -> list:
    """All URLs the model was actually shown for HQ classification — the Serper
    payload URLs PLUS the crawled own-domain page URLs. Used to validate
    ``evidence_url(s)`` so the AI can cite a Firecrawl-sourced own-domain page
    (e.g. ``https://www.fujifilm.com/nl``) as evidence, but still can never
    invent a URL that was in neither source.
    """
    # Crawled own-domain pages first so a genuinely own-domain-grounded answer
    # surfaces its real crawled URL; membership is what actually validates a
    # cited URL, so exact ordering is not load-bearing.
    urls: list[str] = []

    def _add(url) -> None:
        url = str(url or "").strip()
        if url and url not in urls:
            urls.append(url)

    for page in (crawled_pages or []):
        _add((page or {}).get("url"))
    for url in _known_urls_from_serper_payload(serper_payload):
        _add(url)
    return urls


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
        "industry":          _str_field("industry"),
        "sub_industry":      _str_field("sub_industry"),
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
    """One Anthropic HQ call. Returns ``(raw_text, usage)``; raises on failure.

    ``system`` is sent as a single ``cache_control``-marked block
    (``_SYSTEM_PROMPT`` + ``_USER_TEMPLATE_STATIC_INSTRUCTIONS``, which never
    vary between calls) so Anthropic's prompt cache can serve this fixed
    instruction block on every subsequent lead instead of reprocessing (and
    re-billing at the full input price) it every time. ``messages`` carries
    only the per-lead part built by ``_build_user_message``.
    """
    if _anthropic_lib is None:
        raise ImportError("anthropic package not installed")
    client = _anthropic_lib.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT + "\n\n" + _USER_TEMPLATE_STATIC_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    raw_text = response.content[0].text if response.content else ""
    import usage_tracker
    usage_tracker.record_anthropic_response(response, model, "hq")
    usage_obj = getattr(response, "usage", None)
    input_tokens = _usage_field(usage_obj, "input_tokens")
    output_tokens = _usage_field(usage_obj, "output_tokens")
    # Only populated when the response actually used prompt caching (older
    # SDKs / non-caching responses simply lack these attributes) — the same
    # defensive multi-name lookup as the fields above, never guessed.
    cache_creation_input_tokens = _usage_field(usage_obj, "cache_creation_input_tokens")
    cache_read_input_tokens = _usage_field(usage_obj, "cache_read_input_tokens")
    total = (input_tokens + output_tokens
             if input_tokens is not None and output_tokens is not None else None)
    return raw_text, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
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


def _call_deepseek_hq(api_key: str, model: str, user_msg: str) -> tuple[str, dict]:
    """One DeepSeek HQ call via the OpenAI-compatible chat completions API —
    same system prompt and user message as the Anthropic/OpenAI paths, same
    ``openai`` client package pointed at DeepSeek's ``base_url``. Returns
    ``(raw_text, usage)``; raises on failure."""
    if _openai_lib is None:
        raise ImportError("openai package not installed")
    client = _openai_lib.OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
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
    deepseek_api_key: str = "",
    crawled_pages=None,
    lusha_description=None,
    lusha_specialties=None,
) -> "HQDetectionResult":
    """Interpret HQ from a Serper payload using Anthropic (Haiku by default).

    ``ai_provider`` selects an experimental path ("openai" or "deepseek") —
    same system prompt, user message, JSON parser, and post-AI scoring; only
    the API call differs. The default ("anthropic") is byte-for-byte the
    existing behavior. Usage/cost audit fields (``ai_hq_provider`` /
    ``ai_hq_*_tokens`` / ``ai_hq_estimated_cost_usd``) are populated when the
    provider reports usage; they stay blank rather than guessed.

    ``lusha_description``/``lusha_specialties`` (default ``None`` — Lusha
    enrichment plan, Stap 5) are optional, already-in-hand Lusha Company
    Description/Specialties text added to the per-lead user message as a
    lower-authority ADDITIONAL CONTEXT section (see
    ``_format_lusha_context``) — free, no extra API call. Omitting them
    (every existing caller) produces byte-for-byte the same prompt as
    before this parameter existed. Never touches the cached system prompt
    or the JSON parser/post-AI scoring rules.

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

    _PROVIDER_API_KEYS = {"openai": openai_api_key, "deepseek": deepseek_api_key}
    provider_api_key = _PROVIDER_API_KEYS.get(provider, anthropic_api_key)
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
            crawled_pages=crawled_pages,
            lusha_description=lusha_description,
            lusha_specialties=lusha_specialties,
        )
        if provider == "openai":
            raw_text, usage = _call_openai_hq(provider_api_key, model, user_msg)
        elif provider == "deepseek":
            raw_text, usage = _call_deepseek_hq(provider_api_key, model, user_msg)
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
        # Only ever populated for the Anthropic path (see _call_anthropic_hq);
        # None for OpenAI/DeepSeek, whose usage dicts don't carry these keys.
        ai_hq_cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        ai_hq_cache_read_tokens=usage.get("cache_read_input_tokens"),
        # Cache-aware estimate, kept alongside (not replacing) the plain
        # ai_hq_estimated_cost_usd above so the two can be compared during
        # validation. Reduces to the same value as the plain estimate when no
        # caching occurred (cache fields absent/None).
        ai_hq_estimated_cost_usd_with_cache=estimate_ai_cost_usd_with_cache(
            model, usage.get("input_tokens"), usage.get("output_tokens"),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
            cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        ),
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
    ai_industry     = (ai_data.get("industry") or "").strip()
    ai_sub_industry = (ai_data.get("sub_industry") or "").strip()

    # Mechanical validation: only URLs actually present in the supplied
    # material — the Serper payload OR the crawled own-domain pages — are ever
    # trusted (never an AI-invented URL). ev_url (the existing single-URL
    # field) is kept first when it is itself valid.
    _known_urls = _known_urls_for_hq(serper_payload, crawled_pages)
    _raw_ev_urls = ai_data.get("evidence_urls")
    ev_urls: list[str] = []
    if ev_url and ev_url in _known_urls:
        ev_urls.append(ev_url)
    if isinstance(_raw_ev_urls, list):
        for u in _raw_ev_urls:
            u = str(u or "").strip()
            if u and u in _known_urls and u not in ev_urls:
                ev_urls.append(u)

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
        hq_evidence_urls=ev_urls,
        hq_evidence_quote=ev_quote or None,
        parser_source="ai_first",
        ai_hq_raw_json=(raw_text or "")[:2000],
        ai_hq_industry=ai_industry or None,
        ai_hq_sub_industry=ai_sub_industry or None,
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
