"""Lead Prioritizer v2 → commercial_fit_scoring adapter (Step 5).

Maps the v2 intermediate signals on a ``LeadPrioritizationResult`` into the row
dict format expected by ``commercial_fit_scoring.score_company`` and runs it.

Conservative, explicit mapping:
- HQ, international footprint, onboarding/training, ICP-keyword and employer
  branding signals map to their existing scoring fields.
- TI-onboarding is left at 0.0 (not inferred yet).
- Rapid growth is 0.0 and never presented as a positive driver.
- Company size complexity (the 0-3 AI/audit signal) is NOT used as an
  employee range -- but the raw Lusha employee count/range on the result
  (``lusha_employees``) is a genuinely different field and IS passed
  through as ``lusha_employee_range``, so ``score_company``'s real
  employee-size lookup (25% of the final blend) reflects the company's
  actual size instead of always falling back to its "unknown" default.
- Competitor is never mapped or scored.

No AI, no live Serper, no mutation of the input result.  This does not change
batch ranking or legacy scoring behavior.
"""

from __future__ import annotations

from lead_output_schema import LeadPrioritizationResult

_MAPPING_NOTE = (
    "v2→score_company mapping: sig_foreign_hq_score<-foreign HQ, "
    "sig_intl_footprint_score<-international_profile, "
    "sig_lnd_onboarding_score<-onboarding_training_need, "
    "sig_explicit_lnd_score<-icp_keyword_match, "
    "sig_employer_branding_score<-employer_branding, "
    "lusha_employee_range<-lusha_employees; "
    "ti_onboarding/rapid_growth=0.0; "
    "company_size_complexity (the 0-3 audit signal) is not used as the "
    "employee range -- lusha_employees is; "
    "competitor not mapped."
)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_score_company_input_from_v2_result(result: LeadPrioritizationResult) -> dict:
    """Build a ``score_company`` row dict from a v2 result (no mutation)."""
    return {
        # ── Metadata ──────────────────────────────────────────────────────────
        "company_name": result.company_name,
        "domain": result.domain,
        "input_country": result.input_country,
        # A. HQ (canonical v2 HQ signal)
        "sig_foreign_hq_score": _num(result.sig_foreign_hq_score_for_next_scoring),
        # B. International footprint
        "sig_intl_footprint_score": _num(result.sig_international_profile_score),
        # C. L&D / onboarding
        "sig_lnd_onboarding_score": _num(result.sig_onboarding_training_need_score),
        # D. Explicit L&D / ICP fit
        "sig_explicit_lnd_score": _num(result.sig_icp_keyword_match_score),
        # E. Employer branding
        "sig_employer_branding_score": _num(result.sig_employer_branding_score),
        # F. TI onboarding — not inferred from current v2 evidence yet.
        "ti_onboarding_score": 0.0,
        # G. Rapid growth — intentionally 0.0. Rapid growth is deliberately not
        #    collected or presented as a positive v2 driver (and carries a
        #    negative coefficient in the model), so v2 never feeds it.
        "sig_rapid_growth_score": 0.0,
        # H. Size — the 0-3 company_size_complexity AI/audit signal is NOT an
        #    employee-range replacement, but result.lusha_employees (the raw
        #    Lusha employee count/range, always populated verbatim from the
        #    input row when present) genuinely is one, so it's passed through
        #    here -- otherwise company_size_score (25% of the final blend)
        #    silently falls back to its "unknown" default for every v2-scored
        #    company regardless of the company's actual size.
        "employee_range": "",
        "company_size": "",
        "lusha_employee_range": result.lusha_employees or "",
        "lusha_api_employee_range": "",
    }


def score_lead_v2_result(
    result: LeadPrioritizationResult,
    scoring_profile: str = "italy_register_icp_only",
) -> dict:
    """Score a v2 result via ``commercial_fit_scoring.score_company``.

    Returns the full score_company output plus ``v2_scoring_profile_used`` and
    ``v2_score_input_mapping_note``.  Does not mutate ``result``.
    """
    from commercial_fit_scoring import score_company

    row = build_score_company_input_from_v2_result(result)
    out = dict(score_company(row, params={"scoring_profile": scoring_profile}))
    out["v2_scoring_profile_used"] = scoring_profile
    out["v2_score_input_mapping_note"] = _MAPPING_NOTE
    return out
