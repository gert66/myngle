"""Deterministic caller/app field builder for Lead Prioritizer v2 (Step 6).

Builds app-facing text fields (Lovable / Company Hub payload) from fields
already present on a ``LeadPrioritizationResult`` — HQ fields, non-HQ signal
scores, evidence, and optional commercial-score fields.

Fully deterministic: no AI, no live search, no implicit scoring, no competitor
logic, and rapid growth is never presented as a positive driver.  Nothing is
invented — only existing field values are reused.
"""

from __future__ import annotations

from lead_output_schema import LeadPrioritizationResult


def _pos(value) -> bool:
    """True when a numeric score is present and strictly positive."""
    return value is not None and isinstance(value, (int, float)) and value > 0


def _is_zero(value) -> bool:
    """True when a numeric score is present and exactly zero."""
    return value is not None and isinstance(value, (int, float)) and value == 0.0


def _join(parts: list[str]) -> "str | None":
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


def build_caller_app_fields(result: LeadPrioritizationResult) -> dict:
    """Build the 11 deterministic caller/app fields from an existing result."""
    hq_score = result.sig_foreign_hq_score_for_next_scoring
    intl = result.sig_international_profile_score
    onb = result.sig_onboarding_training_need_score
    size = result.sig_company_size_complexity_score
    icp = result.sig_icp_keyword_match_score

    foreign_hq = _pos(hq_score)

    # ── B. Foreign HQ app fields ──────────────────────────────────────────────
    if foreign_hq:
        foreign_hq_signal_used_in_app = "Yes"
        foreign_hq_country_app = result.hq_detected_country
        foreign_hq_city_app = result.hq_detected_city
    else:
        foreign_hq_signal_used_in_app = "No"
        foreign_hq_country_app = None
        foreign_hq_city_app = None

    # ── C. what_is_hot_app ────────────────────────────────────────────────────
    hot: list[str] = []
    if foreign_hq:
        country = result.hq_detected_country
        city = result.hq_detected_city
        if country and city:
            hot.append(f"Foreign HQ signal: {country} / {city}")
        elif country:
            hot.append(f"Foreign HQ signal: {country}")
        else:
            hot.append("Foreign HQ signal confirmed")
    if _pos(intl):
        hot.append("International profile evidence found")
    if _pos(onb):
        hot.append("Onboarding/training need evidence found")
    if _pos(size):
        hot.append("Company complexity evidence found")
    if _pos(icp):
        hot.append("ICP keyword evidence found")
    what_is_hot_app = _join(hot)

    # ── D. what_is_not_app (factual gaps, not framed as failure) ──────────────
    not_hot: list[str] = []
    if not result.evidence_items:
        not_hot.append("No non-HQ evidence collected yet")
    if not result.signals:
        not_hot.append("No non-HQ signals extracted yet")
    if _is_zero(intl):
        not_hot.append("No positive international profile signal found")
    if _is_zero(onb):
        not_hot.append("No positive onboarding/training signal found")
    if _is_zero(size):
        not_hot.append("No positive company complexity signal found")
    if _is_zero(icp):
        not_hot.append("No positive ICP keyword signal found")
    if result.final_commercial_fit_score is None:
        not_hot.append("Commercial score not calculated")
    what_is_not_app = _join(not_hot)

    # ── E. why_relevant_app (priority-ordered) ────────────────────────────────
    if foreign_hq and _pos(intl):
        why_relevant_app = (
            "Relevant because the lead shows a foreign HQ signal and "
            "international profile evidence."
        )
    elif foreign_hq:
        why_relevant_app = (
            "Relevant because the lead has a foreign parent/HQ signal outside "
            "the input country."
        )
    elif _pos(intl) and _pos(onb):
        why_relevant_app = (
            "Relevant because the lead shows international and "
            "people-development evidence."
        )
    elif _pos(icp):
        why_relevant_app = (
            "Relevant because the lead shows ICP-relevant keyword evidence "
            "(international teams, training, or language support)."
        )
    elif result.final_commercial_fit_score is not None:
        why_relevant_app = "Relevant based on the calculated commercial fit score."
    else:
        why_relevant_app = None

    # ── F. caller_angle_app (always has a light-discovery fallback) ───────────
    if foreign_hq:
        caller_angle_app = (
            "Position mYngle around supporting local teams connected to an "
            "international HQ."
        )
    elif _pos(onb):
        caller_angle_app = (
            "Explore whether onboarding, training, or learning needs are handled "
            "centrally or locally."
        )
    elif _pos(icp):
        caller_angle_app = (
            "Ask how they currently support international teams, sales, service, "
            "or language-related learning."
        )
    else:
        caller_angle_app = (
            "Use a light discovery angle and validate whether international "
            "training needs exist."
        )

    # ── G. call_starter_app (deterministic, uses company name) ────────────────
    company = (result.company_name or "").strip() or "your company"
    if foreign_hq:
        call_starter_app = (
            f"I saw that {company} appears to be connected to an international "
            "group, and I wanted to understand how training and language support "
            "are handled locally."
        )
    elif _pos(intl) or _pos(onb):
        call_starter_app = (
            f"I saw some signals around international operations and people "
            f"development at {company}, and wanted to ask how you support teams "
            "across countries."
        )
    else:
        call_starter_app = (
            f"I am reaching out to understand whether {company} has international "
            "training or language support needs."
        )

    # ── H. caution_app ────────────────────────────────────────────────────────
    caution: list[str] = []
    if result.needs_manual_review:
        caution.append("Manual review recommended before outreach.")
    if result.ai_hq_error:
        caution.append("HQ interpretation reported an error.")
    if result.hq_confidence == "Low":
        caution.append("HQ confidence is low.")
    if foreign_hq and not result.hq_detected_country:
        caution.append("Foreign HQ signal without a detected HQ country.")
    if not result.evidence_items and not result.signals:
        caution.append("No non-HQ evidence collected yet.")
    if result.final_commercial_fit_score is not None and (result.missing_scoring_fields or ""):
        caution.append("Commercial score uses missing signal defaults.")
    caution_app = _join(caution)

    return {
        "commercial_fit_score_app": result.final_commercial_fit_score,
        "commercial_tier_app": result.commercial_tier,
        "what_is_hot_app": what_is_hot_app,
        "what_is_not_app": what_is_not_app,
        "why_relevant_app": why_relevant_app,
        "caller_angle_app": caller_angle_app,
        "call_starter_app": call_starter_app,
        "caution_app": caution_app,
        "foreign_hq_signal_used_in_app": foreign_hq_signal_used_in_app,
        "foreign_hq_country_app": foreign_hq_country_app,
        "foreign_hq_city_app": foreign_hq_city_app,
    }
