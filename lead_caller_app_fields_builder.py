"""Deterministic caller/app field builder for Lead Prioritizer v2 (Step 6).

Builds app-facing text fields (Lovable / Company Hub payload) from fields
already present on a ``LeadPrioritizationResult`` — HQ fields, non-HQ signal
scores, evidence, and optional commercial-score fields.

Fully deterministic: no AI, no live search, no implicit scoring, no competitor
logic, and rapid growth is never presented as a positive driver.  Nothing is
invented — only existing field values are reused, phrased as practical,
concrete cold-caller prose rather than raw tag labels.  Internal technical
labels (score field names, adjudication/parser jargon) are never surfaced in
this user-facing text.
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


def _country_adj(input_country) -> str:
    """Adjective phrase for the local operation, e.g. 'Brazil-based' or 'local'."""
    country = (input_country or "").strip()
    return f"{country}-based" if country else "local"


def _team_phrase(input_country) -> str:
    """'the Brazil team' or 'the local team', for caller-angle prose."""
    country = (input_country or "").strip()
    return f"the {country} team" if country else "the local team"


def _foreign_hq_context(result: LeadPrioritizationResult) -> str:
    """'a foreign parent or HQ context in <country>' (or a generic fallback)."""
    country = (result.hq_detected_country or "").strip()
    if country:
        return f"a foreign parent or HQ context in {country}"
    city = (result.hq_detected_city or "").strip()
    if city:
        return f"a foreign parent or HQ context in {city}"
    return "a foreign parent or HQ context"


def _foreign_hq_sentence(result: LeadPrioritizationResult) -> str:
    """Narrative sentence explaining the foreign-HQ signal in caller terms."""
    country_adj = _country_adj(result.input_country)
    context = _foreign_hq_context(result)
    return (
        f"The company appears to be a {country_adj} operation connected to "
        f"{context}. This creates a concrete reason to explore cross-border "
        "communication, onboarding, and alignment with international group "
        "expectations."
    )


def _parent_hq_summary(result: LeadPrioritizationResult) -> "str | None":
    """One-line parent-company/HQ summary, hedged when data is partial."""
    parent = (result.ai_parent_company or "").strip()
    country = (result.ai_parent_hq_country or result.hq_detected_country or "").strip()
    city = (result.ai_parent_hq_city or result.hq_detected_city or "").strip()
    location = f"{country} / {city}" if country and city else (country or city or "")

    if parent and location:
        return (
            f"The enrichment data identifies {parent} as the parent company, "
            f"with HQ context in {location}."
        )
    if parent:
        return f"The enrichment data identifies {parent} as the parent company."
    if location:
        return f"The enrichment data indicates a foreign parent/HQ context in {location}."
    return None


def _hot_items(result: LeadPrioritizationResult, foreign_hq: bool) -> list[str]:
    """Prose phrases (not raw tags) for what_is_hot_app."""
    items: list[str] = []
    if foreign_hq:
        items.append(
            "Foreign-parent context gives a clear reason to discuss "
            "cross-border communication and team alignment."
        )

    intl = _pos(result.sig_international_profile_score)
    onb = _pos(result.sig_onboarding_training_need_score)
    if intl and onb:
        items.append(
            "Signals point to international operations and onboarding or "
            "training needs."
        )
    elif intl:
        items.append(
            "Signals suggest international operations that may need "
            "cross-border communication support."
        )
    elif onb:
        items.append(
            "The enrichment data indicates onboarding or training needs "
            "worth exploring."
        )

    if _pos(result.sig_company_size_complexity_score):
        items.append(
            "Company size or complexity suggests structured training "
            "coordination may be relevant."
        )
    if _pos(result.sig_icp_keyword_match_score):
        items.append(
            "Keyword evidence signals alignment with the target profile for "
            "language or training support."
        )
    return items


def _not_hot_items(result: LeadPrioritizationResult) -> list[str]:
    """Factual caveats (not framed as rejection) for what_is_not_app."""
    intl = result.sig_international_profile_score
    onb = result.sig_onboarding_training_need_score
    size = result.sig_company_size_complexity_score
    icp = result.sig_icp_keyword_match_score

    items: list[str] = []
    if not result.evidence_items:
        items.append(
            "The evidence does not yet show detailed supporting signals "
            "beyond the HQ check."
        )
    if not result.signals:
        items.append("No structured non-HQ signals have been extracted yet.")
    if _is_zero(intl):
        items.append("The evidence does not yet show clear signs of international operations.")
    if _is_zero(onb):
        items.append("No onboarding or training need signal was found in the available evidence.")
    if _is_zero(size):
        items.append("The evidence does not yet show company size or complexity signals.")
    if _is_zero(icp):
        items.append("No keyword evidence matching the target profile was found.")
    if result.final_commercial_fit_score is None:
        items.append("A commercial fit score has not yet been calculated for this lead.")
    # Evergreen practical reminder — always relevant before outreach.
    items.append("Source evidence should be checked before outreach.")
    return items


def _why_relevant_app(result: LeadPrioritizationResult, foreign_hq: bool) -> "str | None":
    """Short paragraph explaining relevance, or None if nothing to say."""
    company = (result.company_name or "").strip() or "This company"
    country_adj = _country_adj(result.input_country)
    intl = _pos(result.sig_international_profile_score)
    onb = _pos(result.sig_onboarding_training_need_score)
    icp = _pos(result.sig_icp_keyword_match_score)
    any_signal = intl or onb or _pos(result.sig_company_size_complexity_score) or icp

    if foreign_hq and any_signal:
        return (
            f"{company} is relevant because it combines a foreign-parent or "
            "international group signal with evidence of international "
            "operations, onboarding, training, or company complexity. That "
            "makes it a practical target for a first conversation about "
            f"language, communication, or training support for {country_adj} teams."
        )
    if foreign_hq:
        country = (result.input_country or "").strip() or "the input country"
        return (
            f"{company} is relevant because it shows a foreign-parent or HQ "
            f"context outside {country}. That alone is a practical reason to "
            "open a conversation about how the local team stays aligned with "
            "the wider group."
        )
    if intl and onb:
        return (
            f"{company} is relevant because it shows evidence of both "
            "international operations and onboarding or training needs, "
            "which together suggest a practical opening for a conversation "
            "about team support and communication."
        )
    if icp:
        return (
            f"{company} is relevant because the available evidence matches "
            "keywords associated with international teams, training, or "
            "language support needs."
        )
    if result.final_commercial_fit_score is not None:
        return (
            f"{company} is relevant based on the calculated commercial fit "
            "score, even though no single strong qualitative signal stands "
            "out yet."
        )
    return None


def _caller_angle_app(result: LeadPrioritizationResult, foreign_hq: bool) -> str:
    """Practical opening angle — always has a light-discovery fallback."""
    if foreign_hq:
        return (
            f"Open around how {_team_phrase(result.input_country)} stays "
            "aligned with international business expectations, especially "
            "in customer-facing, sales, service, onboarding, or internal "
            "communication roles."
        )
    if _pos(result.sig_onboarding_training_need_score):
        return (
            "Explore whether onboarding, training, or learning needs are "
            "handled centrally or locally, and who owns that decision today."
        )
    if _pos(result.sig_icp_keyword_match_score):
        return (
            "Ask how they currently support international teams, sales, "
            "service, or language-related learning."
        )
    return (
        "Use a light discovery angle: ask a few open questions to validate "
        "whether international training or communication needs exist before "
        "proposing anything specific."
    )


def _call_starter_app(result: LeadPrioritizationResult, foreign_hq: bool) -> str:
    """Natural first sentence for the call, deterministic and company-specific."""
    company = (result.company_name or "").strip() or "your company"
    if foreign_hq:
        country = (result.input_country or "").strip()
        where = f"in {country}" if country else "locally"
        return (
            f"I saw that {company} appears to operate {where} within a wider "
            "international group context. I was wondering how you currently "
            "support teams that need to work across local priorities and "
            "international expectations."
        )
    if _pos(result.sig_international_profile_score) or _pos(result.sig_onboarding_training_need_score):
        return (
            f"I saw some signals around international operations and people "
            f"development at {company}, and wanted to ask how you support "
            "teams across countries."
        )
    return (
        f"I am reaching out to understand whether {company} has "
        "international training or language support needs."
    )


def _caution_app(result: LeadPrioritizationResult, foreign_hq: bool) -> "str | None":
    """Manual-review, low-confidence, missing-evidence, domain-mismatch, and
    suppressed-HQ-positive warnings — kept separate from the positive fields."""
    caution: list[str] = []
    if result.needs_manual_review:
        caution.append("Manual review recommended before outreach.")
    if result.ai_hq_error:
        caution.append("HQ interpretation reported an error.")
    if result.hq_confidence == "Low":
        caution.append("HQ confidence is low.")
    if foreign_hq and not result.hq_detected_country:
        caution.append("Foreign HQ signal without a detected HQ country.")
    if result.hq_evidence_domain_mismatch_warning == "Yes":
        caution.append(
            "The HQ evidence source does not clearly match the lead's own "
            "domain; verify the HQ signal before relying on it."
        )
    if result.hq_positive_score_suppressed_for_review == "Yes":
        caution.append(
            "The foreign-HQ signal was flagged for manual review before "
            "being treated as confirmed."
        )
    if not result.evidence_items and not result.signals:
        caution.append("No non-HQ evidence collected yet.")
    if result.final_commercial_fit_score is not None and (result.missing_scoring_fields or ""):
        caution.append("Commercial score uses missing signal defaults.")
    return _join(caution)


def build_caller_app_fields(result: LeadPrioritizationResult) -> dict:
    """Build the deterministic caller/app fields from an existing result."""
    hq_score = result.sig_foreign_hq_score_for_next_scoring
    foreign_hq = _pos(hq_score)

    # ── Foreign HQ app fields (unchanged shape) ────────────────────────────────
    if foreign_hq:
        foreign_hq_signal_used_in_app = "Yes"
        foreign_hq_country_app = result.hq_detected_country
        foreign_hq_city_app = result.hq_detected_city
    else:
        foreign_hq_signal_used_in_app = "No"
        foreign_hq_country_app = None
        foreign_hq_city_app = None

    what_is_hot_app = _join(_hot_items(result, foreign_hq))
    what_is_not_app = _join(_not_hot_items(result))
    why_relevant_app = _why_relevant_app(result, foreign_hq)
    caller_angle_app = _caller_angle_app(result, foreign_hq)
    call_starter_app = _call_starter_app(result, foreign_hq)
    caution_app = _caution_app(result, foreign_hq)

    parent_hq_summary_app = _parent_hq_summary(result) if foreign_hq else None

    # The full narrative sentence (company/location/foreign-context/why-it-
    # matters) is the most useful single briefing block for a cold caller.
    if foreign_hq:
        cold_caller_summary_app = f"{_foreign_hq_sentence(result)} {caller_angle_app}"
    elif why_relevant_app:
        cold_caller_summary_app = f"{why_relevant_app} {caller_angle_app}"
    else:
        cold_caller_summary_app = None

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
        "cold_caller_summary_app": cold_caller_summary_app,
        "parent_hq_summary_app": parent_hq_summary_app,
    }
