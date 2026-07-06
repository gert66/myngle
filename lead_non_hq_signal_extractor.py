"""Deterministic non-HQ signal extraction for Lead Prioritizer v2 (Step 3).

Converts collected ``LeadEvidence`` into ``LeadSignal`` objects using simple,
deterministic keyword rules — no AI, no live Serper calls, no competitor logic,
and no final commercial scoring.  The produced ``signal_score`` values are
intermediate signal scores, NOT the final commercial fit score.
"""

from __future__ import annotations

import re
from typing import Optional

from hq_simple_detector import is_hosted_careers_platform_domain, derive_domain_root
from lead_output_schema import LeadEvidence, LeadSignal


# Exactly the five supported non-HQ signals and their positive keyword groups.
# (No competitor / alternative-provider / rapid-growth keywords anywhere.)
#
# Each list also carries a short, unambiguous set of NL/IT/DE/FR/ES
# equivalents for the core concept the signal is looking for (employees,
# training, international, locations/sites, ...). Kept deliberately short —
# only words that cannot reasonably appear as a false positive in unrelated
# evidence text. English-only synonyms that are spelled identically in
# another language (e.g. "international" in DE/FR) are not duplicated.
_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "international_profile": [
        "international", "global", "worldwide", "countries", "offices",
        "subsidiaries", "locations", "export", "markets", "presence", "group",
        # NL, IT, ES equivalents of "international"; DE/FR share the EN spelling.
        "internationaal", "internazionale", "internacional",
        # NL, IT, DE, ES equivalents of "locations/offices/sites".
        "vestigingen", "sedi", "standorte", "sedes",
    ],
    "onboarding_training_need": [
        "careers", "training", "onboarding", "academy", "learning",
        "development", "employees", "talent", "people", "team", "hiring",
        # NL, IT, DE, FR, ES equivalents of "employees".
        "medewerkers", "dipendenti", "mitarbeiter", "employés", "empleados",
        # NL, IT, DE, FR, ES equivalents of "training".
        "opleiding", "formazione", "schulung", "formation", "formación",
    ],
    "company_size_complexity": [
        "employees", "revenue", "locations", "offices", "subsidiaries", "group",
        "company profile", "annual report", "global", "production sites", "plants",
        # NL, IT, DE, FR, ES equivalents of "employees".
        "medewerkers", "dipendenti", "mitarbeiter", "employés", "empleados",
        # NL, IT, DE, ES equivalents of "locations/offices/sites".
        "vestigingen", "sedi", "standorte", "sedes",
    ],
    "icp_keyword_match": [
        "corporate training", "sales", "customer service", "support",
        "global teams", "multilingual", "language", "learning", "academy",
        "employees", "international teams",
        # NL, IT, DE, FR, ES equivalents of "customer service".
        "klantenservice", "servizio clienti", "kundenservice",
        "service client", "servicio al cliente",
        # NL, IT, DE, FR, ES equivalents of "multilingual".
        "meertalig", "multilingue", "mehrsprachig", "multilingüe",
    ],
    "employer_branding": [
        "employer brand", "employer branding", "employee satisfaction",
        "employee experience", "workplace culture", "company culture",
        "great place to work", "best places to work", "employee engagement",
        "employee wellbeing", "people culture", "career development",
        "learning culture",
        # NL, IT, DE, FR, ES equivalents of "employer brand(ing)".
        "werkgeversmerk", "marchio del datore di lavoro", "arbeitgebermarke",
        "marque employeur", "marca empleadora",
    ],
}

SUPPORTED_SIGNALS: tuple[str, ...] = tuple(_SIGNAL_KEYWORDS.keys())

# Bumped whenever _SIGNAL_KEYWORDS or the matching rules change in a way that
# can alter signal_score/signal_value output, so old and new datasets (e.g.
# pre- and post-multilingual-keywords Excel/Lovable exports) can be told
# apart. v1 = English-only keywords; v2 = adds NL/IT/DE/FR/ES equivalents.
# v3 = per-signal positive threshold with an own-domain gate (see below).
SIGNAL_EXTRACTOR_VERSION = "v3-own-domain-threshold"


# ---------------------------------------------------------------------------
# Per-signal positive-tier threshold (ICP_COMPARE_REPORT.md, proposals #1 + #2).
#
# Default: a signal needs >= _DEFAULT_POSITIVE_THRESHOLD (2) distinct keyword
# hits to reach the positive tier (score 2.0). Unchanged for every signal not
# listed in _SIGNAL_POSITIVE_THRESHOLD.
#
# The two signals the report found chronically under-coloured
# (icp_keyword_match = explicit L&D, international_profile = international
# business context) get a lowered threshold of 1 hit, but ONLY when the signal
# is also in _OWN_DOMAIN_GATED_SIGNALS and at least one usable evidence URL sits
# on the company's OWN registrable domain (own-domain gate, proposal #2). That
# gate simultaneously blocks entity-contaminated evidence (Villa Silvana ->
# Korian, SIDEL -> sidel.com) and any hosted platform / aggregator (Glassdoor,
# Indeed, LinkedIn company pages, ...), because their registrable root never
# equals the company's own. company_size_complexity and onboarding_training_need
# already perform well and are left at the default; employer_branding is
# deliberately NOT lowered — its evidence leans almost entirely on hosted review
# platforms, so a lowered threshold there would manufacture false positives.
#
# Everything here is data, not logic scattered through the function: to revert
# to the old uniform >= 2 behaviour, clear both collections below (or set the
# thresholds back to 2) and nothing else changes.
_DEFAULT_POSITIVE_THRESHOLD = 2

_SIGNAL_POSITIVE_THRESHOLD: dict[str, int] = {
    "icp_keyword_match": 1,
    "international_profile": 1,
}

# Signals whose lowered threshold only applies when own-domain evidence backs it.
_OWN_DOMAIN_GATED_SIGNALS = frozenset({"icp_keyword_match", "international_profile"})


def _evidence_on_company_domain(ev: LeadEvidence, company_root: str) -> bool:
    """True when this evidence URL sits on the company's own registrable domain.

    ``company_root`` is the registrable label of the company's input domain
    (``derive_domain_root``). A hosted careers platform, aggregator, review
    site, or entity-contaminated URL yields a different root (or ``""``) and is
    therefore rejected — exactly the guard the lowered per-signal threshold
    needs so it can never promote off-company evidence.
    """
    if not company_root:
        return False
    ev_root = derive_domain_root(ev.source_url or "")
    return bool(ev_root) and ev_root == company_root


def _evidence_text(ev: LeadEvidence) -> str:
    """Human-readable evidence content used for deterministic keyword matching.

    Only the Serper-provided title and snippet are inspected (never the URL) so
    matches are defensible and free of URL-substring noise.
    """
    return " ".join(filter(None, [ev.source_title or "", ev.source_snippet or ""])).lower()


def _first(values: list[Optional[str]]) -> Optional[str]:
    for v in values:
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


# ---------------------------------------------------------------------------
# External-training / hosted-platform guards.
#
# Shared with export_lead_prioritizer_to_lovable_json.py (imported from here)
# so the signal *score* and the exporter's *displayed* rationale can never
# disagree — before this, a Samsung-style external-installer-training snippet
# scored a full 2.0 onboarding_training_need signal here while the export
# layer's independent (display-only) guard rejected the very same evidence,
# producing a high commercial score with every driver shown as "Rejected".
# ---------------------------------------------------------------------------

def _topic_pattern(words: tuple[str, ...]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)


# "training" alone is not enough — external installer/product/partner/
# reseller training must not be counted as internal employee learning &
# development just because the word "training" appears.
_EXTERNAL_TRAINING_RE = _topic_pattern((
    "installer", "installers", "reseller", "resellers", "distributor",
    "distributors", "partner training", "customer training",
    "product training", "certification program", "become a",
    "channel partner", "climate solutions partner",
))
_INTERNAL_LD_MARKERS_RE = _topic_pattern((
    "employee", "employees", "internal", "hr team", "people team",
    "people development", "leadership development", "talent development",
    "lms", "academy", "career development", "upskilling", "workforce",
    "staff training", "new hire", "new-hire", "onboarding",
))


def is_external_training_evidence(text: str) -> bool:
    """True when L&D/onboarding evidence looks like external product,
    partner, reseller, or installer training rather than internal employee
    development (e.g. "become a ... installer... climate solutions
    partner") — checked only for the L&D-family signals."""
    return bool(_EXTERNAL_TRAINING_RE.search(text or "")) and not bool(
        _INTERNAL_LD_MARKERS_RE.search(text or ""))


# signal_names treated as the "learning & development" family for the
# external-training check.
_LD_FAMILY_SIGNAL_NAMES = frozenset({"onboarding_training_need", "icp_keyword_match"})


def _usable_evidence_for_signal(ev: LeadEvidence, signal_name: str) -> bool:
    """False when evidence must not count toward a positive score for this
    signal: hosted careers-platform evidence never counts for any signal (it
    is about the platform vendor, not the company), and L&D-family evidence
    describing external installer/product/partner training never counts as
    an internal L&D/onboarding keyword hit."""
    if is_hosted_careers_platform_domain(ev.source_url):
        return False
    if signal_name in _LD_FAMILY_SIGNAL_NAMES and is_external_training_evidence(_evidence_text(ev)):
        return False
    return True


def extract_non_hq_signals(
    evidence_items: list[LeadEvidence],
    company_domain: Optional[str] = None,
) -> list[LeadSignal]:
    """Extract deterministic non-HQ signals from collected evidence.

    Produces at most one ``LeadSignal`` per supported signal name, and only for
    signals that actually have evidence.  No signal is created for a name with
    no evidence.  Evidence that is a hosted careers-platform hit, or (for the
    L&D-family signals) external installer/product/partner training, never
    counts toward a positive keyword match — it stays in ``evidence_items``
    for audit purposes, but cannot be the sole basis for a positive score.

    ``company_domain`` is the lead's input domain. It only matters for the
    own-domain-gated signals (``_OWN_DOMAIN_GATED_SIGNALS``): their lowered
    per-signal threshold (1 hit) is applied solely when at least one usable
    evidence URL sits on the company's own registrable domain. When
    ``company_domain`` is omitted/blank the gate can never open, so those
    signals fall back to the default >= 2 threshold — i.e. behaviour is
    identical to the pre-v3 uniform rule for any caller that does not pass it.
    """
    signals: list[LeadSignal] = []
    company_root = derive_domain_root(company_domain or "")

    for signal_name in SUPPORTED_SIGNALS:
        group = [e for e in (evidence_items or []) if e.signal_name == signal_name]
        if not group:
            continue  # no evidence → no signal

        usable: list[LeadEvidence] = []
        excluded: list[LeadEvidence] = []
        for e in group:
            (usable if _usable_evidence_for_signal(e, signal_name) else excluded).append(e)

        keywords = _SIGNAL_KEYWORDS[signal_name]
        combined = " ".join(_evidence_text(e) for e in usable)
        matched = [kw for kw in keywords if kw in combined]
        n_hits = len(matched)

        # Per-signal positive threshold. A lowered threshold only takes effect
        # for own-domain-gated signals when at least one usable evidence URL is
        # on the company's own registrable domain; otherwise the signal falls
        # back to the default so off-company / hosted / contaminated evidence
        # can never be promoted at the lowered bar.
        threshold = _SIGNAL_POSITIVE_THRESHOLD.get(signal_name, _DEFAULT_POSITIVE_THRESHOLD)
        own_domain_ok = (
            signal_name in _OWN_DOMAIN_GATED_SIGNALS
            and any(_evidence_on_company_domain(e, company_root) for e in usable)
        )
        if signal_name in _OWN_DOMAIN_GATED_SIGNALS and not own_domain_ok:
            threshold = _DEFAULT_POSITIVE_THRESHOLD
        lowered_promotion = (
            threshold < _DEFAULT_POSITIVE_THRESHOLD and n_hits >= threshold
        )

        if n_hits >= threshold and n_hits >= 1:
            score, value = 2.0, "positive_evidence"
        elif n_hits == 1:
            score, value = 1.0, "weak_evidence"
        else:
            score, value = 0.0, "no_positive_match"

        first_url = _first([e.source_url for e in usable]) or _first([e.source_url for e in group])
        has_url = first_url is not None

        # ALL usable evidence URLs (deduplicated, ordered) -- never a URL
        # from excluded evidence (hosted-platform / external-training),
        # even when evidence_url above falls back to one for audit purposes
        # because usable is entirely empty. evidence_urls[0] == evidence_url
        # whenever usable has at least one URL.
        evidence_urls: list[str] = []
        for e in usable:
            url = (e.source_url or "").strip()
            if url and url not in evidence_urls:
                evidence_urls.append(url)

        if score == 2.0 and has_url:
            confidence = "High"
        elif score == 1.0 and has_url:
            confidence = "Medium"
        else:
            confidence = "Low"

        if matched:
            reason = (
                f"{n_hits} distinct keyword match(es) in evidence: "
                + ", ".join(matched)
            )
            if lowered_promotion:
                reason += (
                    " (promoted to positive at lowered per-signal threshold "
                    f"of {threshold}: backed by own-domain evidence)"
                )
        elif excluded:
            excluded_notes = []
            if any(is_hosted_careers_platform_domain(e.source_url) for e in excluded):
                excluded_notes.append("hosted careers-platform evidence excluded")
            if signal_name in _LD_FAMILY_SIGNAL_NAMES and any(
                is_external_training_evidence(_evidence_text(e)) for e in excluded
            ):
                excluded_notes.append("external installer/partner/product training evidence excluded")
            reason = (
                "No positive keywords matched in available evidence ("
                + "; ".join(excluded_notes) + ")."
                if excluded_notes
                else "No positive keywords matched in available evidence."
            )
        else:
            reason = "No positive keywords matched in available evidence."

        signals.append(LeadSignal(
            signal_name=signal_name,
            signal_value=value,
            signal_score=score,
            signal_confidence=confidence,
            signal_reason=reason,
            evidence_url=first_url,
            evidence_urls=evidence_urls,
            evidence_quote=(_first([e.source_snippet for e in usable])
                            or _first([e.source_snippet for e in group])),
            evidence_title=(_first([e.source_title for e in usable])
                            or _first([e.source_title for e in group])),
            query_used=_first([e.query_used for e in group]),
            parser_source=_first([e.parser_source for e in group]),
            needs_manual_review=False,
        ))

    return signals


# Result-field name templates per signal.
_RESULT_FIELD_MAP: dict[str, dict[str, str]] = {
    "international_profile": {
        "score": "sig_international_profile_score",
        "reason": "international_profile_reason",
        "evidence_url": "international_profile_evidence_url",
        "evidence_urls": "international_profile_evidence_urls",
        "evidence_quote": "international_profile_evidence_quote",
    },
    "onboarding_training_need": {
        "score": "sig_onboarding_training_need_score",
        "reason": "onboarding_training_need_reason",
        "evidence_url": "onboarding_training_need_evidence_url",
        "evidence_urls": "onboarding_training_need_evidence_urls",
        "evidence_quote": "onboarding_training_need_evidence_quote",
    },
    "company_size_complexity": {
        "score": "sig_company_size_complexity_score",
        "reason": "company_size_complexity_reason",
        "evidence_url": "company_size_complexity_evidence_url",
        "evidence_urls": "company_size_complexity_evidence_urls",
        "evidence_quote": "company_size_complexity_evidence_quote",
    },
    "icp_keyword_match": {
        "score": "sig_icp_keyword_match_score",
        "reason": "icp_keyword_match_reason",
        "evidence_url": "icp_keyword_match_evidence_url",
        "evidence_urls": "icp_keyword_match_evidence_urls",
        "evidence_quote": "icp_keyword_match_evidence_quote",
    },
    "employer_branding": {
        "score": "sig_employer_branding_score",
        "reason": "employer_branding_reason",
        "evidence_url": "employer_branding_evidence_url",
        "evidence_urls": "employer_branding_evidence_urls",
        "evidence_quote": "employer_branding_evidence_quote",
    },
}


def summarize_non_hq_signals_for_result(signals: list[LeadSignal]) -> dict:
    """Map extracted signals onto the flat ``LeadPrioritizationResult`` fields.

    Missing signals map to ``None`` for every field.
    """
    out: dict = {"signal_extractor_version": SIGNAL_EXTRACTOR_VERSION}
    for fields in _RESULT_FIELD_MAP.values():
        for key in fields.values():
            out[key] = None

    by_name = {s.signal_name: s for s in (signals or [])}
    for signal_name, fields in _RESULT_FIELD_MAP.items():
        sig = by_name.get(signal_name)
        if sig is None:
            continue
        out[fields["score"]] = sig.signal_score
        out[fields["reason"]] = sig.signal_reason
        out[fields["evidence_url"]] = sig.evidence_url
        out[fields["evidence_urls"]] = "; ".join(sig.evidence_urls) if sig.evidence_urls else None
        out[fields["evidence_quote"]] = sig.evidence_quote

    return out


# ---------------------------------------------------------------------------
# Sector / industry detection (audit & app metadata only — NEVER scoring)
# ---------------------------------------------------------------------------
#
# ``sector_industry`` evidence is deliberately absent from ``_SIGNAL_KEYWORDS``
# and ``_RESULT_FIELD_MAP`` above, so ``extract_non_hq_signals`` ignores it and
# it can never become a commercial scoring signal.  The helper below turns that
# evidence into descriptive metadata fields instead.

# Sector keyword -> (industry category, optional sub-industry).  Matching is
# word-boundary based on Serper title + snippet text only; nothing is invented.
_SECTOR_KEYWORD_MAP: dict[str, tuple[str, Optional[str]]] = {
    "financial services": ("Financial services", None),
    "banking": ("Financial services", None),
    "asset management": ("Financial services", None),
    "fintech": ("Financial services", "Fintech"),
    "insurance": ("Insurance", None),
    "insurer": ("Insurance", None),
    "healthcare": ("Healthcare", None),
    "health care": ("Healthcare", None),
    "hospital": ("Healthcare", None),
    "medical devices": ("Healthcare", "Medical devices"),
    "pharmaceutical": ("Pharmaceuticals", None),
    "biotech": ("Pharmaceuticals", "Biotechnology"),
    "technology company": ("Technology", None),
    "tech company": ("Technology", None),
    "information technology": ("Technology", None),
    "it services": ("Technology", "IT services"),
    "software": ("Software", None),
    "telecommunications": ("Telecommunications", None),
    "telecom": ("Telecommunications", None),
    "retail": ("Retail", None),
    "retailer": ("Retail", None),
    "e-commerce": ("Retail", "E-commerce"),
    "ecommerce": ("Retail", "E-commerce"),
    "manufacturing": ("Manufacturing", None),
    "manufacturer": ("Manufacturing", None),
    "oil and gas": ("Energy", "Oil and gas"),
    "renewable energy": ("Energy", "Renewable energy"),
    "energy company": ("Energy", None),
    "utility company": ("Energy", None),
    "logistics": ("Logistics", None),
    "freight": ("Logistics", None),
    "transportation": ("Transportation", None),
    "airline": ("Transportation", "Airline"),
    "shipping company": ("Transportation", "Shipping"),
    "education company": ("Education", None),
    "university": ("Education", None),
    "edtech": ("Education", "Edtech"),
    "e-learning": ("Education", "E-learning"),
    "consulting": ("Consulting", None),
    "consultancy": ("Consulting", None),
    "professional services": ("Consulting", None),
    "advertising agency": ("Marketing and advertising", "Advertising agency"),
    "marketing agency": ("Marketing and advertising", "Marketing agency"),
    "advertising": ("Marketing and advertising", None),
    "public relations": ("Marketing and advertising", "Public relations"),
    "food and beverage": ("Food and beverage", None),
    "food company": ("Food and beverage", None),
    "beverage": ("Food and beverage", None),
    "brewery": ("Food and beverage", "Brewery"),
    "agriculture": ("Agriculture", None),
    "agribusiness": ("Agriculture", None),
    "chemical company": ("Chemicals", None),
    "chemicals": ("Chemicals", None),
    "construction": ("Construction", None),
    "real estate": ("Real estate", None),
    "hospitality": ("Hospitality", None),
    "hotel": ("Hospitality", "Hotels"),
    "media company": ("Media", None),
    "broadcasting": ("Media", "Broadcasting"),
    "publishing": ("Media", "Publishing"),
    "consumer goods": ("Consumer goods", None),
    "consumer electronics": ("Consumer goods", "Consumer electronics"),
    "fmcg": ("Consumer goods", "FMCG"),
    "automotive": ("Automotive", None),
    "car manufacturer": ("Automotive", "Car manufacturer"),
    # Public sector only on clearly governmental terms (per policy).
    "government agency": ("Public sector / government", None),
    "ministry": ("Public sector / government", None),
    "municipality": ("Public sector / government", None),
    "public agency": ("Public sector / government", None),
    "public administration": ("Public sector / government", None),
    # Specialty/industrial chemicals (e.g. resins, coatings, inks manufacturers).
    "specialty chemicals": ("Chemicals", "Specialty chemicals"),
    "resins": ("Chemicals", "Resins"),
    "resin": ("Chemicals", "Resins"),
    "coatings": ("Chemicals", "Coatings"),
    "adhesives": ("Chemicals", "Adhesives"),
    "inks": ("Chemicals", "Inks"),
    "polymers": ("Chemicals", "Polymers"),
    "raw materials": ("Chemicals", "Raw materials"),
    "uv curing": ("Chemicals", "UV curing"),
    "photoinitiators": ("Chemicals", "Photoinitiators"),
    # Industrial equipment, machinery, packaging, building materials.
    "industrial equipment": ("Industrial equipment and machinery", None),
    "machinery": ("Industrial equipment and machinery", None),
    "packaging": ("Packaging", None),
    "building materials": ("Building materials", None),
    # Security / site monitoring services.
    "security services": ("Security services", None),
    "site monitoring": ("Security services", "Site monitoring"),
    # Medical technology / ophthalmic devices — more specific sub-industry
    # terms alongside the existing Healthcare / "medical devices" entry.
    "medical technology": ("Healthcare", "Medical technology"),
    "ophthalmic devices": ("Healthcare", "Ophthalmic devices"),
    "ophthalmic": ("Healthcare", "Ophthalmic devices"),
    # Logistics / manufacturing services — sub-industry of the existing
    # "logistics" and "manufacturing" entries above.
    "logistics services": ("Logistics", "Logistics services"),
    "manufacturing services": ("Manufacturing", "Manufacturing services"),
}

# Company-type phrase -> label; first phrase found in evidence text wins.
_COMPANY_TYPE_MAP: dict[str, str] = {
    "state-owned": "State-owned company",
    "government agency": "Government agency",
    "subsidiary": "Subsidiary",
    "publicly traded": "Public company",
    "listed company": "Public company",
    "public company": "Public company",
    "privately held": "Private company",
    "private company": "Private company",
    "family-owned": "Family-owned company",
    "joint venture": "Joint venture",
    "multinational": "Multinational",
    "nonprofit": "Nonprofit",
    "non-profit": "Nonprofit",
}

_SECTOR_RESULT_KEYS = (
    "detected_industry", "detected_sub_industry", "detected_company_type",
    "sector_confidence", "sector_reason", "sector_evidence_url",
    "sector_evidence_quote", "sector_source_title",
)


def _sector_evidence_priority(ev: LeadEvidence) -> int:
    """Prefer official/profile-style sources over generic directory hits."""
    if (ev.source_type or "") == "knowledge_graph":
        return 0
    url = (ev.source_url or "").lower()
    if "linkedin.com" in url:
        return 1
    if (ev.source_type or "") == "answer_box":
        return 2
    return 3


def _kw_match(keyword: str, text: str):
    return re.search(rf"\b{re.escape(keyword)}\b", text)


def extract_sector_industry(evidence_items: list[LeadEvidence]) -> dict:
    """Derive descriptive sector/industry metadata from ``sector_industry``
    evidence rows.

    Conservative and deterministic: inspects Serper titles/snippets only, never
    invents facts, and returns all-``None`` fields when nothing defensible was
    found.  The output is audit/app metadata only — it feeds no commercial
    score, C4, C5, HQ, or foreign-HQ filtering.
    """
    out: dict = {key: None for key in _SECTOR_RESULT_KEYS}

    raw_group = [e for e in (evidence_items or []) if e.signal_name == "sector_industry"]
    if not raw_group:
        return out

    # Hosted careers-platform evidence (Workday, Greenhouse, ...) describes the
    # platform vendor, not the company's own sector — it must never count
    # toward sector detection.
    group = [e for e in raw_group if not is_hosted_careers_platform_domain(e.source_url)]
    if not group:
        out["sector_confidence"] = "Low"
        out["sector_reason"] = (
            "Sector evidence came only from a hosted careers/job platform "
            "and was not used for sector detection."
        )
        return out

    ordered = sorted(group, key=_sector_evidence_priority)

    # Collect keyword hits per industry across all sector evidence, remembering
    # the first (highest-priority, earliest-position) hit per industry.
    hits: dict[str, dict] = {}
    for rank, ev in enumerate(ordered):
        text = _evidence_text(ev)
        if not text:
            continue
        for keyword, (industry, sub_industry) in _SECTOR_KEYWORD_MAP.items():
            m = _kw_match(keyword, text)
            if not m:
                continue
            entry = hits.setdefault(industry, {
                "keywords": [], "first_pos": (rank, m.start()),
                "evidence": ev, "sub_industry": None,
            })
            if keyword not in entry["keywords"]:
                entry["keywords"].append(keyword)
            if (rank, m.start()) < entry["first_pos"]:
                entry["first_pos"] = (rank, m.start())
                entry["evidence"] = ev
            if entry["sub_industry"] is None and sub_industry:
                entry["sub_industry"] = sub_industry

    if not hits:
        out["sector_confidence"] = "Low"
        out["sector_reason"] = (
            "No clear sector keywords matched in available evidence."
        )
        return out

    # Most distinct keyword hits wins; ties go to the earliest hit in the
    # highest-priority evidence (most specific defensible mention first).
    chosen_industry = min(
        hits, key=lambda ind: (-len(hits[ind]["keywords"]), hits[ind]["first_pos"]),
    )
    chosen = hits[chosen_industry]
    ev = chosen["evidence"]

    n_hits = len(chosen["keywords"])
    if n_hits >= 2 and ev.source_url:
        confidence = "High"
    elif n_hits == 1 and ev.source_url:
        confidence = "Medium"
    else:
        confidence = "Low"

    reason = "Matched sector keyword(s): " + ", ".join(chosen["keywords"]) + "."
    others = sorted(ind for ind in hits if ind != chosen_industry)
    if others:
        reason += (
            " Other sector mentions in evidence: " + ", ".join(others)
            + "; chose the most specific defensible match."
        )

    combined_text = " ".join(_evidence_text(e) for e in ordered)
    company_type = None
    for phrase, label in _COMPANY_TYPE_MAP.items():
        if _kw_match(phrase, combined_text):
            company_type = label
            break

    out["detected_industry"] = chosen_industry
    out["detected_sub_industry"] = chosen["sub_industry"]
    out["detected_company_type"] = company_type
    out["sector_confidence"] = confidence
    out["sector_reason"] = reason
    out["sector_evidence_url"] = ev.source_url
    out["sector_evidence_quote"] = ev.source_snippet
    out["sector_source_title"] = ev.source_title
    return out
