"""
lusha_ranker.py
---------------
Pure-Python ranking logic for Lusha contacts.
No API calls. No external dependencies beyond the standard library.

Takes normalised contact dicts (from lusha_client) and returns them
sorted by relevance for mYngle's language-training sales motion.
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Keyword sets for department / title matching
#
# Two types of entries:
#   - Short acronyms / abbreviations (2-5 chars, all-caps intent):
#     matched with word-boundary regex so "COO" does NOT hit "coordinator".
#   - Multi-word phrases or plain words:
#     matched as substrings after lowercasing (safe because they are long
#     enough to avoid false positives).
#
# Convention: put acronym-only tokens in _*_ACRONYMS sets;
#             put phrase/word tokens in _*_PHRASES sets.
# ---------------------------------------------------------------------------

# HR / L&D
# "development" alone is intentionally excluded — "business development",
# "software development", "product development" must NOT match HR/L&D.
# Only compound phrases that clearly mean Learning & Development qualify.
_HR_ACRONYMS  = {"hr", "hrd", "hro", "chro", "l&d"}
_HR_PHRASES   = {
    "human resources", "people", "talent",
    "learning and development", "learning & development",
    "learning development",   # covers "Head of Learning Development"
    "training", "organisational", "organizational",
    "wellbeing", "employee experience", "culture", "onboarding",
    "workforce", "chief people",
}

# Explicit exclusions — these must NOT trigger HR/L&D even if substrings match
_HR_EXCLUSIONS = {"business development", "sales development", "product development",
                  "software development", "commercial development", "web development",
                  "market development", "app development", "application development"}

# Operations
_OPS_PHRASES  = {
    "operations", "operational", "site manager", "site director",
    "plant", "facility", "facilities", "safety", "hse",
    "logistics", "supply chain", "warehouse", "warehousing",
    "terminal", "port", "maritime", "aviation", "transport",
    "transportation", "manufacturing", "production", "industrial",
}

# Procurement
_PROC_PHRASES = {
    "procurement", "purchasing", "sourcing", "vendor", "category",
    "supply", "contracts",
}

# General Management — acronyms matched with word boundaries
_MGMT_ACRONYMS = {"ceo", "coo", "cfo", "cto", "cmo"}
_MGMT_PHRASES  = {
    "managing director", "general manager", "country manager",
    "regional director", "executive director", "president",
    "chief executive", "chief operating",
}

# Seniority boost
_SENIOR_ACRONYMS = {"vp", "svp", "evp", "chro"}
_SENIOR_PHRASES  = {
    "chief people", "vice president", "director", "head of",
    "lead", "manager", "senior manager", "group manager",
    "global manager",
}

# Penalise — plain words; long enough for safe substring match
_PENALISE_PHRASES = {
    "intern", "trainee", "assistant", "junior", "jr.", "student",
    "graduate", "entry level", "entry-level",
}
# "coordinator" is penalised separately via exact word match to avoid
# hitting e.g. "coordinating director" — checked in _penalty().

# ---------------------------------------------------------------------------
# Industry contexts where Ops/Safety contacts are high priority
# ---------------------------------------------------------------------------

_OPS_PRIORITY_INDUSTRIES = {
    "logistics", "transport", "transportation", "shipping", "maritime",
    "aviation", "manufacturing", "production", "industrial", "warehousing",
    "terminal", "port", "supply chain", "oil", "gas", "energy", "utilities",
    "construction", "chemical",
}


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _wb(token: str) -> re.Pattern:
    """Compile a case-insensitive whole-word regex for a short token."""
    return re.compile(r"(?<![a-z])" + re.escape(token) + r"(?![a-z])", re.IGNORECASE)


# Pre-compile word-boundary patterns for all acronym sets
_HR_ACRONYM_RE   = [_wb(a) for a in _HR_ACRONYMS]
_MGMT_ACRONYM_RE = [_wb(a) for a in _MGMT_ACRONYMS]
_SENIOR_ACRO_RE  = [_wb(a) for a in _SENIOR_ACRONYMS]


def _matches_acronyms(text: str, patterns: list) -> bool:
    return any(p.search(text) for p in patterns)


def _matches_phrases(text: str, phrase_set: set) -> bool:
    tl = text.lower()
    return any(ph in tl for ph in phrase_set)


def _matches_hr(text: str) -> bool:
    # Reject explicit non-HR "development" compound phrases first
    if _matches_phrases(text, _HR_EXCLUSIONS):
        return False
    return _matches_acronyms(text, _HR_ACRONYM_RE) or _matches_phrases(text, _HR_PHRASES)


def _matches_ops(text: str) -> bool:
    return _matches_phrases(text, _OPS_PHRASES)


def _matches_proc(text: str) -> bool:
    return _matches_phrases(text, _PROC_PHRASES)


def _matches_mgmt(text: str) -> bool:
    return _matches_acronyms(text, _MGMT_ACRONYM_RE) or _matches_phrases(text, _MGMT_PHRASES)


def _matches_senior(text: str) -> bool:
    return _matches_acronyms(text, _SENIOR_ACRO_RE) or _matches_phrases(text, _SENIOR_PHRASES)


def _is_coordinator(text: str) -> bool:
    """True only when 'coordinator' appears as a distinct word."""
    return bool(re.search(r"\bcoordinator\b", text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Classification and scoring
# ---------------------------------------------------------------------------

def _classify_department(title: str, dept: str) -> str:
    """Return a human-readable department label."""
    combined = f"{title} {dept}"
    # Priority order: HR/L&D > Procurement > Operations > Management > Other
    # (Procurement before Ops to avoid supply-chain overlap.)
    if _matches_hr(combined):
        return "HR / L&D"
    if _matches_proc(combined):
        return "Procurement"
    if _matches_ops(combined):
        return "Operations"
    if _matches_mgmt(combined):
        return "General Management"
    return "Other"


def _base_score(department: str, industry_is_ops: bool) -> float:
    """Return base score 0.0–0.7 by department priority."""
    if department == "HR / L&D":
        return 0.70
    if department == "Operations" and industry_is_ops:
        return 0.60
    if department == "Procurement":
        return 0.50
    if department == "General Management":
        return 0.45
    if department == "Operations":
        return 0.35
    return 0.20  # Other


def _seniority_boost(title: str) -> float:
    """Return additional score 0.0–0.25 for seniority signals."""
    tl = title.lower()
    if _matches_acronyms(title, _SENIOR_ACRO_RE) or any(
        ph in tl for ph in ("chief people", "vice president")
    ):
        return 0.25
    if any(ph in tl for ph in ("director", "head of")):
        return 0.18
    if any(ph in tl for ph in ("lead", "manager", "senior manager", "group", "global")):
        return 0.10
    return 0.0


def _penalty(title: str, dept: str) -> float:
    """Return a negative adjustment for clearly irrelevant profiles."""
    combined = f"{title} {dept}"
    is_hr = _matches_hr(combined)

    # Plain junior/intern/trainee words
    if _matches_phrases(combined, _PENALISE_PHRASES) and not is_hr:
        return -0.25

    # "coordinator" as a whole word — penalise unless HR/L&D
    if _is_coordinator(combined) and not is_hr:
        return -0.25

    return 0.0


def _match_reason(department: str, title: str, industry_is_ops: bool) -> str:
    if department == "HR / L&D":
        return "Likely HR or L&D decision maker"
    if department == "Operations" and industry_is_ops:
        return "Operations/site manager relevant for multilingual workforce training"
    if department == "Operations":
        return "Operations contact may be relevant if teams work internationally or across sites"
    if department == "Procurement":
        return "Procurement contact may influence training vendor decisions"
    if department == "General Management":
        return "Senior management may sponsor language training initiatives"
    return "Contact included for completeness; lower relevance to mYngle offering"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_contacts_for_myngle(
    contacts: list[dict],
    industry: Optional[str] = None,
    max_results: int = 10,
) -> list[dict]:
    """
    Rank and annotate contacts for mYngle sales relevance.

    Args:
        contacts:    Normalised contact dicts from lusha_client.
        industry:    Company industry string (optional) — used to boost
                     Operations contacts in logistics/manufacturing contexts.
        max_results: Maximum number of contacts to return.

    Returns:
        Sorted list (highest confidence first), max max_results items.
        Each contact dict is augmented with: department, matchReason, confidence.
    """
    industry_str    = (industry or "").lower()
    industry_is_ops = any(kw in industry_str for kw in _OPS_PRIORITY_INDUSTRIES)

    ranked = []
    for c in contacts:
        title = str(c.get("jobTitle") or "")
        dept  = str(c.get("department") or "")

        department = _classify_department(title, dept)
        base       = _base_score(department, industry_is_ops)
        boost      = _seniority_boost(title)
        pen        = _penalty(title, dept)
        score      = min(max(round(base + boost + pen, 2), 0.0), 1.0)
        reason     = _match_reason(department, title, industry_is_ops)

        ranked.append({
            **c,
            "department":  department,
            "matchReason": reason,
            "confidence":  score,
        })

    ranked.sort(key=lambda x: x["confidence"], reverse=True)
    return ranked[:max_results]
