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
# ---------------------------------------------------------------------------

_HR_LD_KEYWORDS = {
    "hr", "human resources", "people", "talent", "l&d", "learning",
    "development", "training", "organisational", "organizational",
    "wellbeing", "employee experience", "culture", "onboarding",
    "workforce", "hrd", "hro", "chro", "chief people",
}

_OPS_KEYWORDS = {
    "operations", "operational", "site", "plant", "facility", "facilities",
    "safety", "hse", "logistics", "supply chain", "procurement", "warehouse",
    "warehousing", "terminal", "port", "maritime", "aviation", "transport",
    "transportation", "manufacturing", "production", "industrial",
}

_PROCUREMENT_KEYWORDS = {
    "procurement", "purchasing", "sourcing", "vendor", "category",
    "supply", "contracts",
}

_MGMT_KEYWORDS = {
    "ceo", "coo", "managing director", "general manager", "country manager",
    "regional director", "executive director", "president",
}

# Seniority boost — titles containing these words score higher
_SENIOR_KEYWORDS = {
    "chro", "chief people", "vp", "vice president", "svp", "evp",
    "director", "head of", "head,", "lead", "manager", "senior manager",
    "group", "global",
}

# Titles that signal irrelevance (junior, support, clearly non-buyer)
_PENALISE_KEYWORDS = {
    "intern", "trainee", "assistant", "coordinator", "associate",
    "junior", "jr.", "student", "graduate", "entry",
}

# ---------------------------------------------------------------------------
# Industry contexts where Ops/Safety contacts are high priority
# ---------------------------------------------------------------------------

_OPS_PRIORITY_INDUSTRIES = {
    "logistics", "transport", "transportation", "shipping", "maritime",
    "aviation", "manufacturing", "production", "industrial", "warehousing",
    "terminal", "port", "supply chain", "oil", "gas", "energy", "utilities",
    "construction", "chemical",
}


def _lower_set(text: str) -> set[str]:
    """Return a set of lowercase words/phrases from a string."""
    return {text.lower().strip()}


def _matches_any(text: str, keyword_set: set[str]) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in keyword_set)


def _classify_department(title: str, dept: str) -> str:
    """Return a human-readable department label."""
    combined = f"{title} {dept}".lower()
    # Check HR/L&D first — takes priority over operations keywords
    if _matches_any(combined, _HR_LD_KEYWORDS):
        return "HR / L&D"
    # Procurement before Operations to avoid "procurement" → "supply chain" overlap
    if _matches_any(combined, _PROCUREMENT_KEYWORDS):
        return "Procurement"
    if _matches_any(combined, _OPS_KEYWORDS):
        return "Operations"
    if _matches_any(combined, _MGMT_KEYWORDS):
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
    if department == "Operations":  # non-ops industry
        return 0.35
    return 0.20  # Other


def _seniority_boost(title: str) -> float:
    """Return additional score 0.0–0.25 for seniority signals."""
    title_lower = title.lower()
    if any(kw in title_lower for kw in ("chro", "chief people", "vp", "vice president", "svp", "evp")):
        return 0.25
    if any(kw in title_lower for kw in ("director", "head of", "head,")):
        return 0.18
    if any(kw in title_lower for kw in ("lead", "manager", "senior manager", "group", "global")):
        return 0.10
    return 0.0


def _penalty(title: str, dept: str) -> float:
    """Return a negative adjustment for clearly irrelevant profiles."""
    combined = f"{title} {dept}".lower()
    # Always penalise unless clearly HR/L&D
    if _matches_any(combined, _PENALISE_KEYWORDS):
        if not _matches_any(combined, _HR_LD_KEYWORDS):
            return -0.25
    return 0.0


def _match_reason(department: str, title: str, industry_is_ops: bool) -> str:
    if department == "HR / L&D":
        return "Likely HR or L&D decision maker"
    if department == "Operations" and industry_is_ops:
        return "Operations/site manager relevant for multilingual workforce training"
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

        department  = _classify_department(title, dept)
        base        = _base_score(department, industry_is_ops)
        boost       = _seniority_boost(title)
        pen         = _penalty(title, dept)
        score       = min(max(round(base + boost + pen, 2), 0.0), 1.0)
        reason      = _match_reason(department, title, industry_is_ops)

        ranked.append({
            **c,
            "department":  department,
            "matchReason": reason,
            "confidence":  score,
        })

    ranked.sort(key=lambda x: x["confidence"], reverse=True)
    return ranked[:max_results]
