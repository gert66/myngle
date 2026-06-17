"""
prepare_domains.py — Domain QA & repair for already-cleaned Excel files.

Repairs missing, PEC, generic, social, directory, profile, or hosted-subdomain
domains before enrichment, without rerunning the full input cleaner.

Usage:
    # Audit only (no file changes):
    python prepare_domains.py --input Italy_cleaned.xlsx --serper-key KEY

    # Apply replacements:
    python prepare_domains.py --input Italy_cleaned.xlsx --serper-key KEY --apply

    # Full folder:
    python prepare_domains.py --input ./cleaned/ --serper-key KEY --apply

    # Self-test (no API key needed):
    python prepare_domains.py --self-test-domain-quality
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

# =============================================================================
# CONSTANTS — mirrored from input_cleaner_register_edition.py
# =============================================================================

SERPER_URL = "https://google.serper.dev/search"

_GENERIC_DOMAINS: frozenset = frozenset({
    # Social networks
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "xing.com",
    # Global business directories / data providers
    "bloomberg.com", "crunchbase.com", "zoominfo.com", "dnb.com",
    "glassdoor.com", "indeed.com", "angel.co", "pitchbook.com",
    "opencorporates.com", "companieshouse.gov.uk",
    "rocketreach.co", "signalhire.com", "apollo.io", "hunter.io",
    "trustpilot.com", "yelp.com", "reuters.com", "ft.com",
    "github.com", "amazon.com", "app.lusha.com", "wikipedia.org",
    "google.com", "bing.com", "yahoo.com",
    # Job boards
    "jobrapido.it", "monster.it", "infojobs.it", "jobbydoo.it",
    "lavoro.corriere.it", "subito.it", "kijiji.it",
    # Italian company registers / directories
    "registroimprese.it", "infocamere.it", "imprese.it",
    "ufficiocamerale.it", "companywall.it", "reportaziende.it",
    "companyreports.it", "atoka.io",
    "paginegialle.it", "paginebianche.it",
    "europages.it", "europages.com",
    "kompass.com", "kompass.it",
    "cerved.com", "cervedgroup.it",
    "aziende.it", "icecat.it", "businessit.it",
    "italianmade.com", "viesus.com",
    "madeintaly.com", "italyexport.com",
    "nixonpowerseo.it", "dnbItaly.com",
    # Italian business-profile / financial-data sites
    "fatturatoaziende.com", "fatturatoitalia.it",
    "registroaziende.it", "registroaziende.com",
    "informazione-aziende.it", "aziendit.com",
    "dati-aziende.it", "ufficio-camerale.it",
    "companiesitaly.com", "italianbusinessregister.it",
    # Business-data lookup services
    "visura.pro", "abbrevia.it",
    # Free hosted-site platforms
    "altervista.org", "wordpress.com", "blogspot.com",
    "wixsite.com", "weebly.com", "sites.google.com",
    # News / price comparison / marketplaces
    "corriere.it", "repubblica.it", "ilsole24ore.com", "sole24ore.com",
    "trovaprezzi.it", "idealo.it", "amazon.it",
})

_GENERIC_DOMAIN_BASES: tuple = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "registroimprese.it", "infocamere.it", "atoka.io", "kompass.com", "kompass.it",
    "europages.com", "europages.it", "paginegialle.it", "paginebianche.it",
    "cerved.com", "cervedgroup.it", "dnb.com", "zoominfo.com",
    "bloomberg.com", "crunchbase.com", "glassdoor.com", "indeed.com",
    "fatturatoaziende.com", "fatturatoitalia.it",
    "registroaziende.it", "registroaziende.com",
    "informazione-aziende.it", "aziendit.com",
    "dati-aziende.it", "ufficio-camerale.it",
    "companiesitaly.com", "italianbusinessregister.it",
    # Hosted platforms — catches subdomains like rsuibmsegrate.altervista.org
    "altervista.org", "blogspot.com", "wordpress.com",
    "wixsite.com", "weebly.com", "sites.google.com",
    "visura.pro", "abbrevia.it",
    "reportaziende.it", "companyreports.it",
)

_HOSTED_PLATFORM_BASES: tuple = (
    "altervista.org", "blogspot.com", "wordpress.com",
    "wixsite.com", "weebly.com", "sites.google.com",
)

_DISCOVERY_BLOCKED_DOMAINS: frozenset = frozenset({
    "music.apple.com", "apps.apple.com", "podcasts.apple.com", "apple.com",
    "spotify.com", "youtube.com", "soundcloud.com", "deezer.com",
    "tidal.com", "bandcamp.com", "iheart.com", "iheartradio.com",
    "last.fm", "tunein.com", "pandora.com", "audiomack.com", "napster.com",
    "amazon.com", "amazon.de", "amazon.it", "amazon.co.uk", "amazon.fr",
    "amazon.es", "amazon.com.au",
    "ebay.com", "ebay.de", "ebay.it",
    "play.google.com", "store.google.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "xing.com", "reddit.com",
    "wikipedia.org", "wikidata.org", "wikimedia.org",
    "genius.com", "azlyrics.com", "lyrics.com", "musixmatch.com",
})

_URL_SHORTENER_DOMAINS: frozenset = frozenset({
    "t.co", "bit.ly", "bitly.com", "tinyurl.com", "ow.ly", "buff.ly",
    "shorturl.at", "rebrand.ly", "cutt.ly", "lnkd.in", "linktr.ee",
    "goo.gl", "is.gd", "s.id", "trib.al",
})

_PEC_DOMAIN_PATTERNS: tuple = (
    "pec.it", "pec.com", "pec.eu", "legalmail.it", "legalmail.com",
    "postecert.it", "arubapec.it", "arubapec.eu", "pecactually.it",
    "cert.it", "pecimprese.it", "certificata.it", "pecaziende.it",
    "pecmail.it", "pec.tiscali.it", "pecimprese.com",
    "ordineavvocati", "ordinedottori", "caf", "patronato",
    "libero.it", "yahoo.it", "gmail.com", "hotmail.it",
    "alice.it", "tin.it", "virgilio.it", "live.com", "outlook.com",
    "tiscali.it", "hotmail.com", "icloud.com", "protonmail.com",
)

_RISKY_MARKERS: list = [
    "forum", "foro", "fan", "fans", "club", "community",
    "archive", "archivio", "wiki", "museum",
    "dealer", "reseller", "shop", "store",
    "directory", "profile",
    "altervista", "blogspot", "wordpress", "wixsite", "weebly",
]

_TLDS: frozenset = frozenset({
    "com", "net", "org", "it", "eu", "nl", "de", "fr", "be", "uk", "co",
    "io", "biz", "info", "at", "ch", "es", "pl", "cz", "se", "no",
    "dk", "fi", "pt", "hu", "ro", "hr", "gr", "gov", "edu",
})

_NOISE_TOKENS: frozenset = frozenset({
    "the", "and", "for", "global", "international", "services", "solutions",
    "consulting", "management", "technology", "technologies", "systems",
    "software", "digital", "enterprise", "enterprises", "partners",
    "italia", "italy", "italian", "europe", "european",
    "snc", "srl", "spa", "sas", "del", "della", "degli", "dei",
    "di", "da", "in", "con", "su", "per", "tra", "fra",
    "group", "holding", "co", "ltd", "inc", "bv",
})

_OFFICIAL_SIGNALS: frozenset = frozenset({
    "official", "sito ufficiale", "home page", "homepage",
    "benvenuti", "welcome", "chi siamo", "about us",
    "sito web ufficiale", "official website", "official site",
})

_WEAK_BRAND_TOKENS: frozenset = frozenset({
    "global", "group", "services", "solutions", "italia", "italy",
    "industry", "industries", "holding", "holdings", "international",
    "management", "consulting", "digital", "technology", "technologies",
    "systems", "system", "enterprise", "enterprises", "partners",
})

_LEGAL_TOKENS = re.compile(
    r"\b(s\.?r\.?l\.?|s\.?p\.?a\.?|s\.?a\.?s?|snc|s\.?n\.?c\.?|"
    r"s\.?a\.?p\.?a?\.?|ltd|limited|b\.?v\.?|n\.?v\.?|gmbh|ag|"
    r"llc|inc|corp|plc|holding|holdings|group|co|company|pty|"
    r"se|pte|bhd|sarl|eurl|scs|cv|impresa|ditta|studio|"
    r"kg|k\.g\.|ohg|o\.h\.g\.|ug|kgaa|k\.g\.a\.a\.|gbr|g\.b\.r\.|ek|e\.k\.|partg)\b\.?",
    re.IGNORECASE,
)

_GOVT_PATTERNS = re.compile(
    r"\.gov\.it$|\.gov\b|agenziaentrate|"
    r"(?:^|\.)comune\.|(?:^|\.)regione\.|(?:^|\.)provincia\.|"
    r"prefettura|questura|tribunale|ministero|"
    r"inps\.it$|inail\.it$|agenziademanio|"
    r"camera\.it$|senato\.it$|governo\.it$|quirinale\.it$|mef\.gov",
    re.IGNORECASE,
)

_RELIGIOUS_PATTERNS = re.compile(
    r"basilica|diocesi|parrocchia|chiesa(?:cattolica)?|santuario|"
    r"abbazia|convento|vescovado|cattedrale|arcidiocesi|"
    r"seminario|oratorio|vaticano|pontific|caritas|"
    r"cappella|pieve|fraternita|confraternita",
    re.IGNORECASE,
)

_DIRECTORY_EXTRA_PATTERNS = re.compile(
    r"oraridiapertura|aperturenegozi|tuttopmi|impresaitalia|"
    r"businessfinder|b2bnetwork|catalogoimprese|trovimprese|"
    r"ioimpresa|businessregister|italiabusiness|infobel|"
    r"fatturato|bilanci|dati-aziend|scheda-aziend|scheda-impres|"
    r"visura-aziend|report-aziend|company-profile|business-profile",
    re.IGNORECASE,
)

_DIRECTORY_PROFILE_TITLE_SIGNALS = re.compile(
    r"\bfatturato\b|\bbilancio\b|\butili\b|\bricavi\b|\bpartita\s+iva\b|"
    r"\bp\.?\s*iva\b|\bscheda\s+azienda\b|\bscheda\s+impresa\b|"
    r"\bdati\s+aziendali\b|\breport\s+azienda\b|\bvisura\b|"
    r"\bregistro\s+aziende\b|\bcompany\s+profile\b|\bbusiness\s+profile\b|"
    r"\bcodice\s+ateco\b|\bforma\s+giuridica\b|\bcapitale\s+sociale\b",
    re.IGNORECASE,
)

_ACADEMIC_PATTERNS = re.compile(
    r"\.edu$|\.ac\.[a-z]{2,}$|universit[aà]|polimi|polito|"
    r"unimi|unibo|unitn|luiss|bocconi|sapienza|unipd|unifi|politecnico",
    re.IGNORECASE,
)

_EDU_IT_RE = re.compile(r"\.edu\.it$", re.IGNORECASE)

# Score thresholds (internal float score maps to 0-100 scale)
_MIN_BRAND_SIM    = 0.15
_WEAK_BRAND_SIM   = 0.25
_SCORE_ACCEPT     = 75   # auto-accept
_SCORE_SUGGEST    = 50   # suggest + needs_domain_review


# =============================================================================
# DOMAIN HELPERS
# =============================================================================

def normalize_domain(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].strip()
    if not d or " " in d or d in ("nan", "none", "n/a", "-", "—"):
        return ""
    if "." not in d:
        return ""
    return d


def is_generic(domain: str) -> bool:
    if not domain:
        return False
    dl = domain.lower()
    if dl in _GENERIC_DOMAINS:
        return True
    for base in _GENERIC_DOMAIN_BASES:
        if dl == base or dl.endswith("." + base):
            return True
    return False


def is_discovery_blocked(domain: str) -> bool:
    if not domain:
        return False
    dl = domain.lower()
    for blocked in _DISCOVERY_BLOCKED_DOMAINS:
        if dl == blocked or dl.endswith("." + blocked):
            return True
    return False


def is_url_shortener(domain: str) -> bool:
    if not domain:
        return False
    dl = domain.lower()
    return any(dl == b or dl.endswith("." + b) for b in _URL_SHORTENER_DOMAINS)


def is_hosted_platform(domain: str) -> bool:
    dl = (domain or "").lower()
    return any(dl == base or dl.endswith("." + base) for base in _HOSTED_PLATFORM_BASES)


def is_pec_or_personal_email(email_domain: str) -> bool:
    if not email_domain:
        return True
    dl = email_domain.lower()
    return any(dl == pat or dl.endswith("." + pat) for pat in _PEC_DOMAIN_PATTERNS)


def domain_has_risky_marker(domain: str) -> tuple[bool, str]:
    d = (domain or "").lower()
    base = d.split(".")[0]
    compact = re.sub(r"[^a-z0-9]", "", base)
    parts = re.split(r"[-._]", d)
    for m in _RISKY_MARKERS:
        if m in parts:
            return True, f"risky marker: {m}"
        if m in compact:
            return True, f"risky marker: {m}"
    return False, ""


def classify_domain_cat(domain: str, title: str = "", snippet: str = "") -> str | None:
    """Return rejection category or None if domain looks acceptable."""
    dl = domain.lower()
    if _GOVT_PATTERNS.search(dl):
        return "government"
    if _RELIGIOUS_PATTERNS.search(dl) or _RELIGIOUS_PATTERNS.search(title.lower()):
        return "religious"
    if _DIRECTORY_EXTRA_PATTERNS.search(dl):
        return "directory"
    if _ACADEMIC_PATTERNS.search(dl):
        return "academic"
    combined = (title + " " + snippet).lower()
    if _DIRECTORY_PROFILE_TITLE_SIGNALS.search(combined):
        return "directory"
    return None


def extract_email_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    parts = email.strip().split("@")
    if len(parts) < 2:
        return ""
    d = parts[-1].strip().lower()
    return d if "." in d else ""


def _strip_legal(name: str) -> str:
    cleaned = _LEGAL_TOKENS.sub(" ", name)
    return re.sub(r"\s+", " ", cleaned).strip(" .,/-")


def _extract_domain(url: str) -> str:
    try:
        p = urlparse(url if url.startswith("http") else f"https://{url}")
        host = p.hostname or ""
        return re.sub(r"^www\.", "", host.lower())
    except Exception:
        return ""


# =============================================================================
# DOMAIN CLASSIFICATION (current domain in Excel)
# =============================================================================

# Action labels
ACT_KEEP    = "KEEP"
ACT_REPLACE = "REPLACE"
ACT_SUGGEST = "SUGGEST"
ACT_BLANK   = "BLANK"
ACT_REVIEW  = "REVIEW"

# Current-domain status labels
STATUS_OK         = "ok"
STATUS_MISSING    = "missing"
STATUS_PEC        = "pec_email"
STATUS_SOCIAL     = "social_profile"
STATUS_DIRECTORY  = "directory_database"
STATUS_HOSTED     = "hosted_subdomain"
STATUS_WEAK       = "weak_suspicious"


def classify_current_domain(domain: str, email_domain: str = "") -> tuple[str, str]:
    """
    Return (status, reason) for the domain already in the Excel file.
    status: one of STATUS_* constants above.
    """
    if not domain:
        return STATUS_MISSING, "no domain"

    if is_hosted_platform(domain):
        return STATUS_HOSTED, f"free hosted platform: {domain}"

    if is_generic(domain) or is_discovery_blocked(domain):
        # Distinguish social/profile from directory
        dl = domain.lower()
        if any(s in dl for s in ("linkedin", "facebook", "twitter", "instagram", "xing", "wikipedia")):
            return STATUS_SOCIAL, f"social/profile site: {domain}"
        return STATUS_DIRECTORY, f"directory/database domain: {domain}"

    if is_url_shortener(domain):
        return STATUS_WEAK, f"URL shortener: {domain}"

    # PEC check: if domain matches email domain AND it's a PEC provider
    if email_domain and domain == email_domain and is_pec_or_personal_email(domain):
        return STATUS_PEC, f"PEC/personal email provider: {domain}"
    if is_pec_or_personal_email(domain) and not email_domain:
        return STATUS_PEC, f"PEC/personal email provider: {domain}"

    cat = classify_domain_cat(domain)
    if cat:
        return STATUS_DIRECTORY, f"rejected category: {cat}"

    risky, risky_reason = domain_has_risky_marker(domain)
    if risky:
        return STATUS_WEAK, risky_reason

    return STATUS_OK, "domain looks plausible"


# =============================================================================
# NAME VARIANTS (lightweight, for scoring only)
# =============================================================================

def _extract_brand(name: str) -> str:
    cleaned = _strip_legal(name)
    cleaned = re.sub(r"\b(societ[aà]|aziend[ae]|impres[ae]|industri[ae]|"
                     r"gruppo|holding|cooperativ[ae]|manifattur[ae]|"
                     r"costruzioni|distribuzione|produzione|prodotti|"
                     r"fratelli|f\.lli|flli|figli|eredi|successori|"
                     r"succ\.?|consorzio|consorzi|associazione|fondazione|istituto)\b\.?",
                     " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,/-")
    tokens = [t for t in re.split(r"[\s\-_/&,]+", cleaned)
              if len(t) >= 2 and t.lower() not in _NOISE_TOKENS and not t.isdigit()]
    if not tokens:
        return cleaned or name
    if len(tokens) == 1:
        return tokens[0]
    if len(tokens) == 2:
        return " ".join(tokens)
    long_toks = [t for t in tokens if len(t) >= 4]
    return " ".join(long_toks[-2:]) if len(long_toks) >= 2 else tokens[-1]


# =============================================================================
# SCORING
# =============================================================================

def _company_tokens(name: str) -> set:
    clean = _strip_legal(name)
    clean = re.sub(r"[^\w\s\-]", " ", clean)
    return {t.lower() for t in re.split(r"[\s\-_]+", clean)
            if len(t) >= 2} - _NOISE_TOKENS


def _domain_tokens(domain: str) -> set:
    if not domain:
        return set()
    parts = domain.split(".")
    while len(parts) > 1 and parts[-1].lower() in _TLDS:
        parts = parts[:-1]
    base = ".".join(parts)
    return {t for t in re.split(r"[-.]", base.lower()) if t and len(t) >= 2} - _TLDS


def _token_overlap(name: str, domain: str) -> float:
    ctok = _company_tokens(name)
    dtok = _domain_tokens(domain)
    if not ctok or not dtok:
        return 0.0
    overlap = ctok & dtok
    for c in ctok:
        for d in dtok:
            if c in d or d in c:
                overlap.add(c)
    return len(overlap) / min(len(ctok), len(dtok))


def _brand_overlap(brand: str, domain: str) -> float:
    if not brand or not domain:
        return 0.0
    b = re.sub(r"[^\w]", "", brand.lower())
    parts = domain.split(".")
    while len(parts) > 1 and parts[-1].lower() in _TLDS:
        parts = parts[:-1]
    base = re.sub(r"[^\w]", "", ".".join(parts).lower())
    if not b or not base:
        return 0.0
    if b == base:
        return 1.0
    if b in base or base in b:
        return 0.8
    b_toks = set(re.split(r"[-.]", b)) - _TLDS
    base_toks = set(re.split(r"[-.]", base)) - _TLDS
    if b_toks and base_toks:
        hit = b_toks & base_toks
        return len(hit) / min(len(b_toks), len(base_toks))
    return 0.0


def _score_candidate(
    domain: str,
    rank: int,
    title: str,
    snippet: str,
    company_name: str,
    brand: str,
    email_domain: str,
    city: str,
    province: str,
) -> int:
    """Score a domain candidate; return 0-100 integer."""
    if is_generic(domain) or is_discovery_blocked(domain) or is_hosted_platform(domain):
        return 0

    cat = classify_domain_cat(domain, title, snippet)
    if cat:
        return 0

    risky, _ = domain_has_risky_marker(domain)
    if risky:
        return 0

    if _EDU_IT_RE.search(domain):
        return 0

    # Internal float score (0–3+ scale), then map to 0-100
    position_w = 1.0 / (rank + 1)

    full_ov  = _token_overlap(company_name, domain)
    brand_ov = _brand_overlap(brand, domain)
    best_ov  = max(full_ov, brand_ov)

    score = position_w * (0.5 + best_ov * 1.5)

    if brand_ov >= 0.8:
        score += 0.4

    if best_ov < _MIN_BRAND_SIM:
        score *= 0.10
    elif best_ov < _WEAK_BRAND_SIM:
        score *= 0.35

    combined = (title + " " + snippet).lower()

    if any(sig in combined for sig in _OFFICIAL_SIGNALS):
        score += 0.25

    brand_lower = brand.lower()
    if brand_lower and brand_lower in combined:
        score += 0.2

    # Location signal
    t = combined
    if city and len(city) >= 3 and city.lower() in t:
        score += 0.3
    elif province and len(province) >= 2 and province.lower() in t:
        score += 0.3

    if email_domain and domain == email_domain:
        score += 0.5

    if domain.endswith(".it"):
        score += 0.15
    elif domain.endswith(".com") or domain.endswith(".eu"):
        score += 0.07

    # Penalties
    if re.search(r"\b(forum|foro|archive|archivio)\b", domain, re.I):
        score -= 0.3
    if re.search(r"\b(associazione|fondazione|onlus|odv|aps)\b", domain, re.I):
        score -= 0.3

    if score < 0:
        score = 0.0

    # Map 0–3 → 0–100
    raw_100 = min(100, int(round(score / 3.0 * 100)))

    # Email match boost
    if email_domain and domain == email_domain:
        raw_100 = min(100, raw_100 + 15)

    # Penalty: only weak brand tokens
    brand_meaningful_tokens = set(re.split(r"[\W_]+", brand_lower)) - _WEAK_BRAND_TOKENS - _TLDS
    if brand_meaningful_tokens and brand_ov < _MIN_BRAND_SIM:
        raw_100 = min(raw_100, 20)
    elif not brand_meaningful_tokens:
        if not (brand_lower and brand_lower in combined):
            raw_100 = min(raw_100, 30)

    return raw_100


# =============================================================================
# SERPER
# =============================================================================

_serper_calls: int = 0


def _call_serper(query: str, serper_key: str) -> tuple[list, str | None]:
    global _serper_calls
    _serper_calls += 1
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "it", "hl": "it", "num": 5},
            timeout=12,
        )
        resp.raise_for_status()
        return resp.json().get("organic", []), None
    except requests.Timeout:
        return [], "Serper timeout"
    except Exception as e:
        return [], str(e)


def _build_queries(company_name: str, brand: str, city: str, province: str) -> list[str]:
    clean = company_name.strip()
    loc = city or province
    queries = [
        f'"{clean}" "sito ufficiale"',
        f'"{clean}" "official website"',
        f'"{clean}" azienda contatti',
    ]
    if brand and brand.lower() != clean.lower() and len(brand) >= 3:
        if loc:
            queries.append(f'"{brand}" "{loc}" sito')
        queries.append(f'"{brand}" "{province or city}" azienda')
    else:
        if loc:
            queries.append(f'"{clean}" "{loc}" sito')
        if province:
            queries.append(f'"{clean}" "{province}" azienda')
    return queries


def search_best_domain(
    company_name: str,
    email_domain: str,
    city: str,
    province: str,
    serper_key: str,
    max_serper_calls: int = 3,
) -> dict:
    """
    Run Serper queries and return the best candidate domain.

    Returns dict with:
      domain, score, reason, candidates (list of top-2 dicts)
    """
    brand = _extract_brand(company_name)
    queries = _build_queries(company_name, brand, city, province)[:max_serper_calls]

    all_candidates: dict[str, int] = {}
    candidate_details: dict[str, dict] = {}

    for query in queries:
        results, err = _call_serper(query, serper_key)
        if err or not results:
            continue

        for rank, item in enumerate(results[:5]):
            url     = item.get("link", "")
            title   = item.get("title", "")
            snippet = item.get("snippet", "")
            domain  = _extract_domain(url)
            if not domain:
                continue

            sc = _score_candidate(
                domain, rank, title, snippet,
                company_name, brand, email_domain, city, province,
            )
            if sc > all_candidates.get(domain, 0):
                all_candidates[domain] = sc
                candidate_details[domain] = {
                    "domain": domain,
                    "score": sc,
                    "reason": f"rank {rank+1} — {title[:80]}",
                    "query": query,
                }

        time.sleep(0.25)

    if not all_candidates:
        return {"domain": "", "score": 0, "reason": "no candidates found", "candidates": []}

    sorted_cands = sorted(all_candidates.items(), key=lambda x: x[1], reverse=True)
    best_domain, best_score = sorted_cands[0]
    top2 = [candidate_details[d] for d, _ in sorted_cands[:2]]

    return {
        "domain":     best_domain,
        "score":      best_score,
        "reason":     candidate_details[best_domain]["reason"],
        "candidates": top2,
    }


# =============================================================================
# COLUMN DETECTION
# =============================================================================

_NAME_CANDIDATES = (
    "company name", "company_name", "ragione sociale", "denominazione",
    "nome", "company", "name",
)
_WEBSITE_CANDIDATES = (
    "website", "sito web", "sito", "url", "web", "homepage",
    "website_url", "domain", "validated_domain", "recommended_domain",
)
_EMAIL_CANDIDATES = (
    "email address", "email", "e-mail", "posta elettronica", "indirizzo email",
)
_CITY_CANDIDATES = (
    "city", "città", "comune", "citta",
)
_PROVINCE_CANDIDATES = (
    "national statistical institute province", "province", "provincia", "prov",
)


def detect_columns(df: pd.DataFrame) -> dict[str, str | None]:
    cols_lower = {c.lower().strip(): c for c in df.columns}
    result: dict[str, str | None] = {}

    for field, candidates in [
        ("company",  _NAME_CANDIDATES),
        ("website",  _WEBSITE_CANDIDATES),
        ("email",    _EMAIL_CANDIDATES),
        ("city",     _CITY_CANDIDATES),
        ("province", _PROVINCE_CANDIDATES),
    ]:
        found = None
        for c in candidates:
            if c in cols_lower:
                found = cols_lower[c]
                break
        result[field] = found

    return result


# =============================================================================
# ROW PROCESSING
# =============================================================================

_DIAG_COLS = [
    "domain_prepare_action",
    "domain_prepare_source",
    "domain_prepare_old_domain",
    "domain_prepare_new_domain",
    "domain_prepare_score",
    "domain_prepare_reason",
    "candidate_1_domain",
    "candidate_1_score",
    "candidate_1_reason",
    "candidate_2_domain",
    "candidate_2_score",
    "candidate_2_reason",
    "needs_domain_review",
]


def process_row(
    row: pd.Series,
    col_map: dict,
    serper_key: str,
    max_serper_calls: int,
    serper_budget: list,   # [used, max] — mutable list for budget tracking
) -> dict:
    """
    Evaluate and optionally repair the domain for one row.
    Returns a dict of diagnostic column values + 'new_website' key.
    """
    company  = str(row.get(col_map["company"], "") or "").strip()
    raw_web  = str(row.get(col_map["website"], "") or "").strip() if col_map.get("website") else ""
    raw_email = str(row.get(col_map["email"], "") or "").strip() if col_map.get("email") else ""
    city     = str(row.get(col_map["city"], "") or "").strip() if col_map.get("city") else ""
    province = str(row.get(col_map["province"], "") or "").strip() if col_map.get("province") else ""

    current_domain = normalize_domain(raw_web)
    email_domain   = extract_email_domain(raw_email)

    diag: dict = {c: "" for c in _DIAG_COLS}
    diag["domain_prepare_old_domain"] = current_domain
    diag["new_website"]               = raw_web  # default: no change

    status, status_reason = classify_current_domain(current_domain, email_domain)

    # --- KEEP good domains immediately ---
    if status == STATUS_OK:
        diag["domain_prepare_action"] = ACT_KEEP
        diag["domain_prepare_source"] = "original"
        diag["domain_prepare_new_domain"] = current_domain
        diag["domain_prepare_score"]  = 100
        diag["domain_prepare_reason"] = status_reason
        diag["needs_domain_review"]   = False
        return diag

    # --- Domain is bad/missing → try Serper if budget allows ---
    diag["domain_prepare_reason"] = status_reason

    needs_search = status in (STATUS_MISSING, STATUS_PEC, STATUS_SOCIAL,
                              STATUS_DIRECTORY, STATUS_HOSTED, STATUS_WEAK)

    if not needs_search or not serper_key or not company:
        # Cannot search: blank the domain if it was bad
        if status != STATUS_MISSING:
            diag["domain_prepare_action"] = ACT_BLANK
            diag["domain_prepare_new_domain"] = ""
            diag["new_website"] = ""
        else:
            diag["domain_prepare_action"] = ACT_REVIEW
        diag["domain_prepare_source"] = "none"
        diag["needs_domain_review"] = True
        return diag

    # Budget check
    if serper_budget[0] >= serper_budget[1]:
        diag["domain_prepare_action"] = ACT_REVIEW
        diag["domain_prepare_source"] = "none"
        diag["domain_prepare_reason"] += " [serper budget exhausted]"
        diag["needs_domain_review"] = True
        return diag

    # Try email domain as quick check first (free, no Serper call)
    if email_domain and not is_pec_or_personal_email(email_domain) and not is_generic(email_domain):
        em_score = _score_candidate(
            email_domain, 0, "", "",
            company, _extract_brand(company), email_domain, city, province,
        )
        if em_score >= _SCORE_ACCEPT:
            diag["domain_prepare_action"]     = ACT_REPLACE
            diag["domain_prepare_source"]     = "email_domain"
            diag["domain_prepare_new_domain"] = email_domain
            diag["domain_prepare_score"]      = em_score
            diag["domain_prepare_reason"]     = f"email domain accepted (score {em_score})"
            diag["candidate_1_domain"]        = email_domain
            diag["candidate_1_score"]         = em_score
            diag["candidate_1_reason"]        = "email domain"
            diag["needs_domain_review"]       = False
            diag["new_website"]               = email_domain
            return diag

    # Serper search
    serper_budget[0] += max_serper_calls
    result = search_best_domain(
        company, email_domain, city, province, serper_key, max_serper_calls,
    )

    cands = result.get("candidates", [])
    if len(cands) > 0:
        diag["candidate_1_domain"] = cands[0].get("domain", "")
        diag["candidate_1_score"]  = cands[0].get("score", "")
        diag["candidate_1_reason"] = cands[0].get("reason", "")[:120]
    if len(cands) > 1:
        diag["candidate_2_domain"] = cands[1].get("domain", "")
        diag["candidate_2_score"]  = cands[1].get("score", "")
        diag["candidate_2_reason"] = cands[1].get("reason", "")[:120]

    best_domain = result.get("domain", "")
    best_score  = result.get("score", 0)

    if best_score >= _SCORE_ACCEPT and best_domain:
        diag["domain_prepare_action"]     = ACT_REPLACE
        diag["domain_prepare_source"]     = "serper"
        diag["domain_prepare_new_domain"] = best_domain
        diag["domain_prepare_score"]      = best_score
        diag["domain_prepare_reason"]     += f" → replaced with {best_domain} (score {best_score})"
        diag["needs_domain_review"]       = False
        diag["new_website"]               = best_domain

    elif best_score >= _SCORE_SUGGEST and best_domain:
        diag["domain_prepare_action"]     = ACT_SUGGEST
        diag["domain_prepare_source"]     = "serper"
        diag["domain_prepare_new_domain"] = best_domain
        diag["domain_prepare_score"]      = best_score
        diag["domain_prepare_reason"]     += f" → suggested {best_domain} (score {best_score})"
        diag["needs_domain_review"]       = True
        # Do NOT update new_website — only a suggestion

    else:
        # No confident replacement
        diag["domain_prepare_action"]     = ACT_BLANK if status != STATUS_MISSING else ACT_REVIEW
        diag["domain_prepare_source"]     = "none"
        diag["domain_prepare_score"]      = best_score
        diag["domain_prepare_reason"]     += f" → no confident replacement (best score {best_score})"
        diag["needs_domain_review"]       = True
        if status != STATUS_MISSING:
            diag["new_website"] = ""

    return diag


# =============================================================================
# FILE PROCESSING
# =============================================================================

def process_file(
    input_path: Path,
    output_dir: Path,
    review_dir: Path,
    serper_key: str,
    max_serper_calls: int,
    apply: bool,
    limit: int,
    max_total_serper: int,
) -> dict:
    """Process one Excel file. Return summary dict."""
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    # Output naming: strip _cleaned_... suffix, replace with domain_prepared / domain_review
    stem = input_path.stem
    # Remove trailing _cleaned_YYYYMMDD_HHMM or similar timestamp suffixes
    stem_base = re.sub(r"_cleaned.*$", "", stem, flags=re.IGNORECASE)
    stem_base = re.sub(r"_domain_prepared.*$", "", stem_base, flags=re.IGNORECASE)

    prepared_name = f"{stem_base}_domain_prepared_{ts}.xlsx"
    review_name   = f"{stem_base}_domain_review_{ts}.xlsx"

    prepared_path = output_dir / prepared_name
    review_path   = review_dir / review_name

    # Read
    try:
        df = pd.read_excel(input_path, dtype=str)
    except Exception as e:
        return {"file": str(input_path), "error": str(e)}

    df = df.where(df.notna(), "")

    if limit and limit > 0:
        df = df.head(limit)

    col_map = detect_columns(df)
    if not col_map.get("company") or not col_map.get("website"):
        return {
            "file": str(input_path),
            "error": f"could not detect company or website column. Detected: {col_map}",
        }

    # Add diagnostic columns if missing
    for col in _DIAG_COLS:
        if col not in df.columns:
            df[col] = ""

    website_col = col_map["website"]

    # Budget: [used_calls, max_calls]
    serper_budget = [0, max_total_serper]

    changed_rows = []
    review_rows  = []

    for idx, row in df.iterrows():
        diag = process_row(row, col_map, serper_key, max_serper_calls, serper_budget)

        # Write diagnostic columns
        for col in _DIAG_COLS:
            df.at[idx, col] = diag.get(col, "")

        # Apply domain change
        action = diag.get("domain_prepare_action", "")
        new_web = diag.get("new_website", "")

        if apply and action == ACT_REPLACE and new_web:
            df.at[idx, website_col] = new_web
            changed_rows.append(idx)

        # Collect review rows
        if action in (ACT_SUGGEST, ACT_BLANK, ACT_REVIEW) or diag.get("needs_domain_review"):
            review_rows.append(idx)
        elif action == ACT_REPLACE:
            review_rows.append(idx)  # always include replacements in review for auditing

    # Write files
    output_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    if apply:
        df.to_excel(prepared_path, index=False)

    # Review sheet: only flagged rows
    if review_rows:
        review_df = df.loc[sorted(set(review_rows))].copy()
    else:
        review_df = df.head(0).copy()

    review_df.to_excel(review_path, index=False)

    return {
        "file":          str(input_path),
        "rows":          len(df),
        "changed":       len(changed_rows),
        "review_rows":   len(set(review_rows)),
        "prepared_path": str(prepared_path) if apply else "(not written — use --apply)",
        "review_path":   str(review_path),
        "serper_calls":  _serper_calls,
    }


# =============================================================================
# SELF-TEST
# =============================================================================

def run_self_test() -> None:
    """Lightweight domain quality self-test. No API keys required."""
    REJECT = [
        ("it.linkedin.com",              "LinkedIn country subdomain"),
        ("it.wikipedia.org",             "Wikipedia country subdomain"),
        ("it.kompass.com",               "Kompass country subdomain"),
        ("fatturatoitalia.it",           "Italian financial-data directory"),
        ("visura.pro",                   "Business data lookup service"),
        ("x.abbrevia.it",               "Abbrevia subdomain"),
        ("rsuibmsegrate.altervista.org", "Altervista hosted blog"),
        ("mybrand.blogspot.com",         "Blogspot hosted blog"),
        ("myfirm.wixsite.com",           "Wix hosted site"),
        ("myfirm.weebly.com",            "Weebly hosted site"),
        ("myfirm.wordpress.com",         "WordPress hosted blog"),
    ]
    ACCEPT = [
        ("ibm.com",         "IBM global domain"),
        ("zf.com",          "ZF global domain"),
        ("q8.it",           "Q8 Italy"),
        ("solutions30.com", "Solutions30 corporate"),
        ("pirelli.com",     "Pirelli corporate"),
    ]

    passed = 0
    failed = 0

    print("=" * 62)
    print("prepare_domains.py — Domain Quality Self-Test")
    print("=" * 62)

    print("\n── Should-REJECT cases ──")
    for domain, desc in REJECT:
        generic  = is_generic(domain)
        blocked  = is_discovery_blocked(domain)
        platform = is_hosted_platform(domain)
        cat      = classify_domain_cat(domain)
        status, _ = classify_current_domain(domain)
        rejected = generic or blocked or platform or bool(cat) or status != STATUS_OK
        reason = (
            "generic"         if generic  else
            "media_blocked"   if blocked  else
            "hosted_platform" if platform else
            cat               if cat      else
            status
        )
        ok = rejected
        print(f"  [{'PASS' if ok else 'FAIL'}] {domain:<42} ({desc}) → {reason}")
        passed += ok
        failed += (not ok)

    print("\n── Should-ACCEPT cases (must NOT be hard-rejected) ──")
    for domain, desc in ACCEPT:
        generic  = is_generic(domain)
        blocked  = is_discovery_blocked(domain)
        platform = is_hosted_platform(domain)
        cat      = classify_domain_cat(domain)
        hard_rejected = generic or blocked or platform or bool(cat)
        ok = not hard_rejected
        reason = (
            "generic"         if generic  else
            "media_blocked"   if blocked  else
            "hosted_platform" if platform else
            cat               if cat      else
            "ok"
        )
        print(f"  [{'PASS' if ok else 'FAIL'}] {domain:<42} ({desc}) → {reason}")
        passed += ok
        failed += (not ok)

    print(f"\n{'='*62}")
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 62)

    if failed:
        sys.exit(1)


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="prepare_domains.py — Domain QA & repair for cleaned Excel files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input",
        help="Input .xlsx file or folder of .xlsx files")
    parser.add_argument("--output-dir", default=".",
        help="Directory for prepared output files (default: current dir)")
    parser.add_argument("--review-dir", default=".",
        help="Directory for review/audit files (default: current dir)")
    parser.add_argument("--apply", action="store_true",
        help="Write the prepared Excel file with replacements applied")
    parser.add_argument("--serper-key", default=None,
        help="Serper API key (falls back to SERPER_API_KEY env var)")
    parser.add_argument("--max-serper-calls", type=int, default=3,
        help="Max Serper queries per company (default 3)")
    parser.add_argument("--max-total-serper", type=int, default=500,
        help="Hard cap on total Serper calls across all rows (default 500)")
    parser.add_argument("--limit", type=int, default=0,
        help="Process only the first N rows per file (0 = all)")
    parser.add_argument("--self-test-domain-quality", action="store_true",
        help="Run lightweight domain quality self-tests (no API key required) and exit")

    args = parser.parse_args()

    if args.self_test_domain_quality:
        run_self_test()
        sys.exit(0)

    if not args.input:
        parser.error("--input is required (unless using --self-test-domain-quality)")

    serper_key = (
        args.serper_key
        or os.environ.get("SERPER_API_KEY", "")
        or ""
    )
    if not serper_key:
        print("WARNING: no Serper key provided — Serper search disabled", file=sys.stderr)

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    review_dir = Path(args.review_dir).resolve()

    if input_path.is_dir():
        files = sorted(input_path.glob("*.xlsx"))
        if not files:
            print(f"No .xlsx files found in {input_path}", file=sys.stderr)
            sys.exit(1)
    elif input_path.is_file():
        files = [input_path]
    else:
        print(f"ERROR: input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    for f in files:
        print(f"\nProcessing: {f.name}")
        summary = process_file(
            input_path=f,
            output_dir=output_dir,
            review_dir=review_dir,
            serper_key=serper_key,
            max_serper_calls=args.max_serper_calls,
            apply=args.apply,
            limit=args.limit,
            max_total_serper=args.max_total_serper,
        )
        if "error" in summary:
            print(f"  ERROR: {summary['error']}", file=sys.stderr)
        else:
            print(f"  rows:        {summary['rows']}")
            print(f"  changed:     {summary['changed']}")
            print(f"  review rows: {summary['review_rows']}")
            print(f"  serper:      {summary['serper_calls']} calls")
            print(f"  review:      {summary['review_path']}")
            if args.apply:
                print(f"  prepared:    {summary['prepared_path']}")
            else:
                print("  (use --apply to write prepared file with replacements)")


if __name__ == "__main__":
    if "--self-test-domain-quality" in sys.argv:
        run_self_test()
        sys.exit(0)
    main()
