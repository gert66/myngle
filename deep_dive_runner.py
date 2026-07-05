"""Deep Dive collection + AI distillation for Lead Prioritizer v2 (Step B).

Standalone, callable function — no Streamlit and no batch-core dependency,
so it can be reused unchanged behind a future FastAPI on-demand endpoint
for Lovable. ``run_deep_dive()`` never raises: any failure (collection
error, no Anthropic key, AI call error, unparseable response) yields a
``DeepDiveResult`` with a short ``error`` string, never an exception that
could break a batch run.

Two collection modes, same output schema either way:
  - **With a Firecrawl key**: targeted scrape of the own-domain and (if
    known) parent-domain about/careers/locations/newsroom-style pages via
    the Firecrawl v1 scrape REST API (same request/response shape as
    ``input_cleaner_register_edition.py``'s Firecrawl verifier, without its
    multi-key failover/health-tracking machinery — that file is not
    imported or modified).
  - **Without a key, or on a Firecrawl outage** (network error / key or
    quota failure): 3–5 localized Serper queries (``gl``/``hl`` derived from
    the lead's country — never a hardcoded ``gl=us``) plus a bare
    ``urllib`` fetch of the own/parent homepage.

Both modes feed one Anthropic Messages call that distills discrete,
source-backed claims (a fixed category, a one-sentence statement, a short
literal quote, and the exact source URL) — self-contained JSON-only prompt
and tolerant parsing, mirroring the pattern used by
``lead_caller_content_composer.py`` / ``lead_icp_context_composer.py``. A
claim whose ``source_url`` is not one of the URLs actually supplied is
dropped rather than trusted, since the model could otherwise invent a URL.

Hosted careers/job-platform URLs (Workday, Greenhouse, Lever, ...) are
never presented as a deep-dive source — the same guard used everywhere
else in the v2 pipeline (``hq_simple_detector.is_hosted_careers_platform_domain``).
``domain_verified`` / ``source_kind`` reuse the existing host-match logic
from ``lead_hq_ai_interpreter.py`` rather than re-implementing it.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from typing import Optional

try:
    import anthropic as _anthropic_lib
except ImportError:  # pragma: no cover
    _anthropic_lib = None  # type: ignore[assignment]

import requests

from deep_dive_schema import DeepDiveClaim, DeepDiveResult, DEEP_DIVE_CATEGORIES
from hq_simple_detector import is_hosted_careers_platform_domain
from lead_hq_ai_interpreter import _host_from, _hosts_match
from lead_non_hq_enrichment import extract_evidence_from_serper_payload

DEFAULT_DEEP_DIVE_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Firecrawl collection (same request/response shape as
# input_cleaner_register_edition.py's Firecrawl verifier — single key only,
# no failover/health-tracking, since this module is a standalone unit).
# ---------------------------------------------------------------------------

_FC_API_URL = "https://api.firecrawl.dev/v1/scrape"
_FC_MAX_CHARS = 5000
_FC_KEY_FAILURE_CODES = frozenset({401, 402, 403, 429})

# Candidate page paths tried per domain, in order. Cheap and best-effort —
# a 404 just skips that one page; it is never treated as a Firecrawl outage.
_CANDIDATE_PAGE_PATHS: tuple[str, ...] = (
    "", "/about", "/about-us", "/careers", "/locations", "/newsroom", "/news",
)


def _firecrawl_scrape_page(url: str, firecrawl_api_key: str, timeout: int = 15) -> dict:
    """POST one Firecrawl scrape request. Never raises.

    Returns ``{"ok": bool, "text": str, "status": str, "hard_failure": bool}``.
    ``hard_failure=True`` means Firecrawl itself is unusable right now
    (network error, or a key/quota problem) — the caller should abandon
    Firecrawl entirely and fall back, rather than just skipping this page.
    """
    try:
        resp = requests.post(
            _FC_API_URL,
            headers={"Authorization": f"Bearer {firecrawl_api_key}",
                     "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"]},
            timeout=timeout,
        )
    except Exception as exc:
        return {"ok": False, "text": "", "status": f"error:{str(exc)[:80]}", "hard_failure": True}

    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:
            return {"ok": False, "text": "", "status": "invalid_json", "hard_failure": False}
        page_data = body.get("data") or {}
        text = (page_data.get("markdown") or "")[:_FC_MAX_CHARS]
        return {"ok": bool(text.strip()), "text": text, "status": "ok", "hard_failure": False}
    if resp.status_code == 404:
        return {"ok": False, "text": "", "status": "404", "hard_failure": False}
    if resp.status_code in _FC_KEY_FAILURE_CODES:
        return {"ok": False, "text": "", "status": f"http_{resp.status_code}", "hard_failure": True}
    return {"ok": False, "text": "", "status": f"http_{resp.status_code}", "hard_failure": False}


def _collect_pages_via_firecrawl(
    domain: Optional[str],
    parent_domain: Optional[str],
    firecrawl_api_key: str,
    max_pages: int,
) -> dict:
    """Returns ``{"pages": [...], "pages_crawled": [...], "used": bool}``.

    ``used=False`` means Firecrawl could not be used at all (no domain to
    crawl, or a hard failure) — the caller must fall back for everything.
    A hard failure discards any pages already collected in this call, since
    a bad/exhausted key fails consistently and partial results would be
    misleading about what Firecrawl actually delivered.
    """
    targets = []
    if domain:
        targets.append((domain, "own_domain"))
    if parent_domain:
        targets.append((parent_domain, "parent_domain"))
    if not targets:
        return {"pages": [], "pages_crawled": [], "used": False}

    pages: list[dict] = []
    pages_crawled: list[dict] = []

    for root_domain, kind in targets:
        base = root_domain if "://" in root_domain else f"https://{root_domain}"
        base = base.rstrip("/")
        for path in _CANDIDATE_PAGE_PATHS:
            if len(pages) >= max_pages:
                break
            url = base + path
            result = _firecrawl_scrape_page(url, firecrawl_api_key)
            pages_crawled.append({"url": url, "status": result["status"]})
            if result["hard_failure"]:
                return {"pages": pages, "pages_crawled": pages_crawled, "used": False}
            if result["ok"]:
                pages.append({
                    "url": url, "title": None, "text": result["text"],
                    "source_kind": kind, "retrieval_method": "firecrawl",
                })
        if len(pages) >= max_pages:
            break

    if not pages:
        return {"pages": pages, "pages_crawled": pages_crawled, "used": False}
    return {"pages": pages, "pages_crawled": pages_crawled, "used": True}


# ---------------------------------------------------------------------------
# Fallback collection: localized Serper queries + bare urllib fetches.
# ---------------------------------------------------------------------------

# gl/hl derived from the lead's country — deliberately NOT a hardcoded
# "gl=us"/"hl=en" default like enrich_clients_claude.py's _call_serper.
# Unknown/blank countries omit gl/hl entirely (Serper's own default) rather
# than guessing a market.
_COUNTRY_GL_HL: dict[str, tuple[str, str]] = {
    "italy": ("it", "it"), "netherlands": ("nl", "nl"), "germany": ("de", "de"),
    "france": ("fr", "fr"), "spain": ("es", "es"), "brazil": ("br", "pt"),
    "united states": ("us", "en"), "usa": ("us", "en"), "us": ("us", "en"),
    "united kingdom": ("gb", "en"), "uk": ("gb", "en"),
    "australia": ("au", "en"), "belgium": ("be", "nl"), "switzerland": ("ch", "de"),
    "austria": ("at", "de"), "sweden": ("se", "sv"), "denmark": ("dk", "da"),
    "norway": ("no", "no"), "poland": ("pl", "pl"), "portugal": ("pt", "pt"),
    "ireland": ("ie", "en"), "new zealand": ("nz", "en"), "mexico": ("mx", "es"),
    "japan": ("jp", "ja"), "china": ("cn", "zh"), "canada": ("ca", "en"),
    "india": ("in", "en"), "turkey": ("tr", "tr"),
}


def gl_hl_for_country(country: Optional[str]) -> tuple[str, str]:
    """Return ``(gl, hl)`` for a country, or ``("", "")`` when unrecognised."""
    return _COUNTRY_GL_HL.get((country or "").strip().lower(), ("", ""))


def _call_serper_localized(query: str, serper_api_key: str, gl: str = "", hl: str = "") -> dict:
    """Fire one localized Serper search. Never raises; ``{}`` on any error.

    A separate, small function from ``lead_non_hq_enrichment.call_serper_for_enrichment``
    on purpose: that helper has no gl/hl support at all, and this module
    needs country-localized results for the deep-dive fallback path.
    """
    if not serper_api_key or not query:
        return {}
    payload: dict = {"q": query, "num": 10}
    if gl:
        payload["gl"] = gl
    if hl:
        payload["hl"] = hl
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps(payload).encode(),
        headers={"X-API-KEY": serper_api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def _plain_fetch(url: str, timeout: int = 10, max_chars: int = 4000) -> str:
    """Bare GET + crude HTML-tag stripping. Never raises; ``""`` on error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _classify_source_kind(host: str, domain: Optional[str], parent_domain: Optional[str]) -> str:
    own_host = _host_from(domain) if domain else ""
    parent_host = _host_from(parent_domain) if parent_domain else ""
    if own_host and _hosts_match(own_host, host):
        return "own_domain"
    if parent_host and _hosts_match(parent_host, host):
        return "parent_domain"
    return "external"


def _collect_pages_via_fallback(
    company_name: str,
    domain: Optional[str],
    parent_domain: Optional[str],
    parent_company: Optional[str],
    country: Optional[str],
    serper_api_key: str,
    max_pages: int,
) -> dict:
    """Returns ``{"pages": [...], "localized_queries_used": [...]}``."""
    gl, hl = gl_hl_for_country(country)
    name = (company_name or domain or "").strip()

    queries: list[str] = []
    if name:
        queries.append(f"{name} company overview headquarters")
        queries.append(f"{name} careers locations offices")
        queries.append(f"{name} learning development training academy")
        if parent_company:
            queries.append(f"{parent_company} {name} subsidiary parent company")
    queries = queries[:5]

    pages: list[dict] = []
    localized_queries_used: list[str] = []
    for query in queries:
        if len(pages) >= max_pages:
            break
        localized_queries_used.append(query)
        payload = _call_serper_localized(query, serper_api_key, gl, hl)
        items = extract_evidence_from_serper_payload(
            payload, signal_name="deep_dive", query_used=query, max_items=2)
        for ev in items:
            if len(pages) >= max_pages:
                break
            text = (ev.source_snippet or "").strip()
            if not text or not ev.source_url:
                continue
            kind = _classify_source_kind(_host_from(ev.source_url), domain, parent_domain)
            pages.append({
                "url": ev.source_url, "title": ev.source_title, "text": text,
                "source_kind": kind, "retrieval_method": "serper_localized",
            })

    # Guaranteed own/parent-context pages: bare fetch of each homepage.
    for target_domain, kind in ((domain, "own_domain"), (parent_domain, "parent_domain")):
        if not target_domain or len(pages) >= max_pages:
            continue
        url = target_domain if "://" in target_domain else f"https://{target_domain}"
        text = _plain_fetch(url)
        if text:
            pages.append({
                "url": url, "title": None, "text": text,
                "source_kind": kind, "retrieval_method": "plain_fetch",
            })

    return {"pages": pages[:max_pages], "localized_queries_used": localized_queries_used}


# ---------------------------------------------------------------------------
# AI distillation prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a B2B sales research assistant distilling verifiable company "
    "facts from supplied web page material for a 'deep dive' briefing. Use "
    "ONLY the material supplied in the user message -- never invent facts, "
    "quotes, or source URLs. Every claim MUST include a short literal quote "
    "copied from the supplied material and the exact source_url it came "
    "from -- never paraphrase the quote and never invent a URL. Assign each "
    "claim to exactly one of the five fixed categories. If the material "
    "does not clearly support a category, omit it rather than inventing a "
    "claim. "
    "Reply ONLY with a valid JSON object -- no prose, no markdown fences."
)

_USER_TEMPLATE = """\
Company: {company_name}
Country: {country}
Own domain: {domain}
Parent company: {parent_company}
Parent domain: {parent_domain}

Source material (use verbatim -- do not invent facts, quotes, or URLs
beyond what is listed below):
{pages_text}

Return JSON with exactly this shape:
{{
  "claims": [
    {{"category": "hq_structure|locations|training_infrastructure|workforce|recent_developments",
      "statement": "one factual sentence",
      "quote": "short literal quote copied from the source material",
      "source_url": "must be exactly one of the URLs listed above"}}
  ]
}}

Rules:
- Only use the five category values listed above.
- "source_url" must be copied exactly from one of the URLs above -- never invent a URL.
- "quote" must be a short literal excerpt actually present in that source's text.
- If the material is thin, return fewer claims (or an empty "claims" list)
  rather than inventing content.
"""


def _format_pages(pages: list) -> str:
    if not pages:
        return "  (none)"
    lines = []
    for p in pages:
        title = p.get("title") or ""
        text = (p.get("text") or "")[:1200]
        header = f"  URL: {p.get('url')}" + (f" ({title})" if title else "")
        lines.append(f"{header}\n    {text}")
    return "\n".join(lines)


def build_deep_dive_prompt(
    *,
    company_name: str,
    country: Optional[str],
    domain: Optional[str],
    parent_company: Optional[str],
    parent_domain: Optional[str],
    pages: list,
) -> str:
    """Build the user message (no secrets)."""
    return _USER_TEMPLATE.format(
        company_name=company_name or "(unknown)",
        country=country or "(unknown)",
        domain=domain or "(unknown)",
        parent_company=parent_company or "(unknown)",
        parent_domain=parent_domain or "(unknown)",
        pages_text=_format_pages(pages),
    )


# ---------------------------------------------------------------------------
# Robust parsing (self-contained, no cross-module import — mirrors the same
# tolerant-JSON-extraction pattern used by lead_caller_content_composer.py /
# lead_icp_context_composer.py / lead_hq_ai_interpreter.py).
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


def _validate_and_build_claims(
    raw_claims: list,
    retrieval_method_by_url: dict,
    title_by_url: dict,
    domain: Optional[str],
    parent_domain: Optional[str],
) -> list:
    """Build ``DeepDiveClaim`` objects, dropping anything unverifiable.

    A claim is dropped (never trusted) when: the category is not one of the
    five fixed values, a required field is blank, the ``source_url`` is not
    one of the URLs actually supplied to the prompt (the model could
    otherwise invent one), or the URL resolves to a hosted careers/job
    platform.
    """
    claims: list[DeepDiveClaim] = []
    counts: dict[str, int] = {}
    for item in raw_claims or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        if category not in DEEP_DIVE_CATEGORIES:
            continue
        statement = str(item.get("statement") or "").strip()
        quote = str(item.get("quote") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        if not (statement and quote and source_url):
            continue
        if source_url not in retrieval_method_by_url:
            continue  # not one of the supplied URLs — never trust an invented one
        if is_hosted_careers_platform_domain(source_url):
            continue

        host = _host_from(source_url)
        source_kind = _classify_source_kind(host, domain, parent_domain)
        domain_verified = source_kind in ("own_domain", "parent_domain")

        counts[category] = counts.get(category, 0) + 1
        claims.append(DeepDiveClaim(
            claim_id=f"{category}:{counts[category]}",
            category=category,
            statement=statement,
            quote=quote,
            source_url=source_url,
            source_title=title_by_url.get(source_url),
            source_kind=source_kind,
            domain_verified=domain_verified,
            retrieval_method=retrieval_method_by_url[source_url],
        ))
    return claims


def _distill_claims(
    *,
    company_name: str,
    country: Optional[str],
    domain: Optional[str],
    parent_company: Optional[str],
    parent_domain: Optional[str],
    pages: list,
    anthropic_api_key: str,
    ai_model: str,
) -> tuple[list, str]:
    """Returns ``(claims, error)``. Never raises."""
    if not anthropic_api_key:
        return [], "no_anthropic_api_key"

    prompt = build_deep_dive_prompt(
        company_name=company_name, country=country, domain=domain,
        parent_company=parent_company, parent_domain=parent_domain, pages=pages,
    )

    try:
        if _anthropic_lib is None:
            raise ImportError("anthropic package not installed")
        client = _anthropic_lib.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=ai_model,
            max_tokens=1536,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = extract_anthropic_text(response)
    except Exception as exc:
        return [], f"deep_dive_call_failed: {str(exc)[:200]}"

    data = _parse_response(raw_text)
    raw_claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(raw_claims, list):
        return [], "deep_dive_parse_failed"

    retrieval_method_by_url = {p["url"]: p["retrieval_method"] for p in pages}
    title_by_url = {p["url"]: p.get("title") for p in pages}
    claims = _validate_and_build_claims(
        raw_claims, retrieval_method_by_url, title_by_url, domain, parent_domain)
    return claims, ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_deep_dive(
    *,
    company_name: str,
    domain: Optional[str] = None,
    country: Optional[str] = None,
    parent_company: Optional[str] = None,
    parent_domain: Optional[str] = None,
    trigger_reason: str = "manual",
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    firecrawl_api_key: str = "",
    max_pages: int = 6,
    ai_model: str = DEFAULT_DEEP_DIVE_MODEL,
) -> DeepDiveResult:
    """Run one deep dive for a single company. Never raises.

    Collection: Firecrawl (own + parent domain) when ``firecrawl_api_key``
    is set and reachable; otherwise (or on a Firecrawl outage) localized
    Serper queries + bare homepage fetches. Distillation: one Anthropic
    call that extracts source-backed claims from whatever material was
    collected. Any failure — collection error, missing key, AI call error,
    unparseable response — yields ``error`` set on the result rather than
    an exception, so a batch run can never break on one company's deep dive.
    """
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = DeepDiveResult(
        company_name=company_name, domain=domain, parent_domain=parent_domain,
        trigger_reason=trigger_reason, generated_at=generated_at,
    )
    try:
        pages: list[dict] = []
        pages_crawled: list[dict] = []
        localized_queries_used: list[str] = []
        firecrawl_used = False

        if firecrawl_api_key:
            fc = _collect_pages_via_firecrawl(domain, parent_domain, firecrawl_api_key, max_pages)
            pages_crawled = fc["pages_crawled"]
            if fc["used"]:
                pages = fc["pages"]
                firecrawl_used = True

        if not firecrawl_used:
            fb = _collect_pages_via_fallback(
                company_name=company_name, domain=domain, parent_domain=parent_domain,
                parent_company=parent_company, country=country,
                serper_api_key=serper_api_key, max_pages=max_pages,
            )
            pages = fb["pages"]
            localized_queries_used = fb["localized_queries_used"]

        # Hosted-platform guard, applied to whichever collection mode ran.
        pages = [p for p in pages if not is_hosted_careers_platform_domain(p.get("url"))]

        result.pages_crawled = pages_crawled
        result.firecrawl_used = firecrawl_used
        result.localized_queries_used = localized_queries_used

        if not pages:
            return result

        claims, distill_error = _distill_claims(
            company_name=company_name, country=country, domain=domain,
            parent_company=parent_company, parent_domain=parent_domain,
            pages=pages, anthropic_api_key=anthropic_api_key, ai_model=ai_model,
        )
        result.claims = claims
        if distill_error:
            result.error = distill_error
        return result
    except Exception as exc:
        result.error = f"deep_dive_failed: {str(exc)[:200]}"
        return result
