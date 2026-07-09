"""
Commercial Fit Scoring Engine — Results(8).xlsx-compatible formula
==================================================================
Implements the logistic regression + sigmoid-stretch + size-blend formula
validated against Results(8).xlsx.

Formula summary
---------------
1.  Normalise each input signal:  norm = clamp(v, 0, 3) / 3
2.  LR score:   lr_z_score  = INTERCEPT + Σ(coeff_i × norm_i)
3.  Prob:        lean_model_prob = 1 / (1 + exp(−lr_z_score))
4.  Sigmoid stretch:
      sigmoid_raw_s       = 1 / (1 + exp(−k × (prob − 0.5)))
      icp_similarity_score = clamp(1 + 9×(s − s_min)/(s_max − s_min), 1, 10)
5.  Size score: exact band lookup (9 bands, 1–10 float scale)
6.  Blend:
      final_commercial_fit_score =
          clamp(0.75 × icp_similarity_score + 0.25 × company_size_score, 1, 10)

      (final_commercial_fit_score_75_25_legacy is still emitted for audit —
       under the default profile it is now identical to final_commercial_fit_score
       since the default blend itself is 75/25; it stays meaningful for profiles
       that override model_weight/size_weight, e.g. italy_register_icp_only.)

Backward-compatible aliases (calculated from canonical fields, not independently)
-----------------------------------------------------------------------------------
  lean_model_logit  → alias of lr_z_score
  model_probability → alias of lean_model_prob

Reference validation (Capgemini, all 7 signals supplied)
---------------------------------------------------------
  lr_z_score          ≈ 0.9870
  lean_model_prob     ≈ 0.7285
  icp_similarity_score ≈ 9.69
  company_size_score  = 10.0
  final_commercial_fit_score ≈ 9.77  (75/25 blend)
  commercial_tier     = 🥇 Hot
"""

from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd
import warnings as _warnings
from pandas.errors import PerformanceWarning as _PerformanceWarning
_warnings.simplefilter("ignore", _PerformanceWarning)

# =============================================================================
# CONSTANTS — Results(8).xlsx-compatible
# =============================================================================

#: LR intercept.
INTERCEPT: float = -0.35

#: Coefficients applied to normalised signals (clamp(v, 0, 3) / 3).
#: IMPORTANT: uses sig_employer_branding_score — NOT sig_merger_acq_score.
LEAN_COEFFICIENTS: dict[str, float] = {
    "sig_foreign_hq_score":        0.7465,
    "sig_explicit_lnd_score":      0.2185,
    "sig_intl_footprint_score":    0.1795,
    "sig_employer_branding_score": 0.1602,   # NOT sig_merger_acq_score
    "sig_lnd_onboarding_score":    0.1250,
    "ti_onboarding_score":         0.1488,
    "sig_rapid_growth_score":     -0.2905,   # negative coefficient
}

#: Exact employee-range string → company_size_score (1–10 float).
#: Keys are the normalised canonical form (lower - upper).
SIZE_BAND_LOOKUP: dict[str, float] = {
    "100001 - 10000000": 10.0,
    "10001 - 100000":     8.88,
    "5001 - 10000":       7.75,
    "1001 - 5000":        6.63,
    "501 - 1000":          5.5,
    "201 - 500":           4.38,
    "51 - 200":            3.25,
    "11 - 50":             2.13,
    "1 - 10":              1.0,
}
#: Numeric midpoint thresholds for fallback band lookup (used when exact
#: string match fails).  Checked ascending; first band whose upper bound
#: exceeds the midpoint wins.
_SIZE_MIDPOINT_BANDS: list[tuple[float, float]] = [
    (10.5,      1.0),
    (50.5,      2.13),
    (200.5,     3.25),
    (500.5,     4.38),
    (1_000.5,   5.5),
    (5_000.5,   6.63),
    (10_000.5,  7.75),
    (100_000.5, 8.88),
    (math.inf,  10.0),
]
SIZE_SCORE_MISSING: float = 5.5   # default when range is unknown

#: Sigmoid steepness.  Controls how strongly probabilities are spread around
#: the midpoint.  Higher k → sharper separation; does NOT change prob ranking.
SIGMOID_K: float = 10.0

#: Reference probability boundaries for the normalisation range.
#: These are the practical min/max LR probabilities from model calibration.
#: Changing SIGMOID_K does NOT require changing these — S_MIN/S_MAX are
#: recomputed automatically below.
_SIGMOID_P_LO: float = 0.35734
_SIGMOID_P_HI: float = 0.76427

#: Sigmoid values at the reference probabilities.  Auto-derived from SIGMOID_K
#: so they are always consistent — never hardcode these independently.
SIGMOID_S_MIN: float = 1.0 / (1.0 + math.exp(-SIGMOID_K * (_SIGMOID_P_LO - 0.5)))
SIGMOID_S_MAX: float = 1.0 / (1.0 + math.exp(-SIGMOID_K * (_SIGMOID_P_HI - 0.5)))

#: Blend weights — ICP similarity dominates; size is a secondary commercial factor.
ICP_SIMILARITY_WEIGHT: float = 0.75
COMPANY_SIZE_WEIGHT:   float = 0.25
assert abs((ICP_SIMILARITY_WEIGHT + COMPANY_SIZE_WEIGHT) - 1.0) < 1e-9, \
    "Blend weights must sum to 1.0"

# Legacy aliases kept for backward compatibility with callers that import MODEL_WEIGHT / SIZE_WEIGHT.
MODEL_WEIGHT: float = ICP_SIMILARITY_WEIGHT
SIZE_WEIGHT:  float = COMPANY_SIZE_WEIGHT

# Legacy 75/25 weights — used only to compute the comparison column.
_LEGACY_MODEL_WEIGHT: float = 0.75
_LEGACY_SIZE_WEIGHT:  float = 0.25

#: Tier thresholds — 75/25 blend, percentile cutoffs from Results1.xlsx
#: ("Score Methodology" / "Validation" tabs): top 10% / next 20% / next 30%.
TIER_THRESHOLDS: list[tuple[float, str]] = [
    (8.86, "🥇 Hot"),
    (7.32, "🥈 Warm"),
    (5.04, "🥉 Cool"),
    (0.0,  "❄️ Pass"),
]

# Tier thresholds for italy_register_icp_only (K=1, size_weight=0).
# With K=1 the sigmoid barely moves so icp_sim ≈ 3.0–7.5; final = icp_sim × 1.0.
# Recalibrated from empirical distribution of Italian register batch scores.
_TIER_THRESHOLDS_ITALY: list[tuple[float, str]] = [
    (6.50, "🥇 Hot"),
    (5.00, "🥈 Warm"),
    (3.00, "🥉 Cool"),
    (0.0,  "❄️ Pass"),
]

# ── Scoring profiles ──────────────────────────────────────────────────────────
# Each profile overrides the module-level defaults inside score_company().
# Pass via params["scoring_profile"] or params["_profile_override"].
SCORING_PROFILES: dict = {
    "default": {
        "model_weight": ICP_SIMILARITY_WEIGHT,  # 0.75
        "size_weight":  COMPANY_SIZE_WEIGHT,     # 0.25
        "sigmoid_k":    SIGMOID_K,               # 10.0
        "tier_thresholds": TIER_THRESHOLDS,
        "label": "Default (ICP 75% + size 25%)",
    },
    "italy_register_icp_only": {
        "model_weight": 1.0,
        "size_weight":  0.0,
        "sigmoid_k":    1.0,
        "tier_thresholds": _TIER_THRESHOLDS_ITALY,
        "label": "Italy register, ICP only (K=1, size excluded)",
    },
}

# Composite profile score groupings (display-only — not part of LR)
GLOBAL_COMPLEXITY_FIELDS: list[str] = [
    "sig_intl_footprint_score",
    "sig_foreign_hq_score",
    "sig_multicultural_score",
    "ti_intercultural_score",
]
PEOPLE_DEVELOPMENT_FIELDS: list[str] = [
    "sig_explicit_lnd_score",
    "sig_lnd_onboarding_score",
    "sig_employer_branding_score",
    "ti_leadership_score",
    "ti_onboarding_score",
]
COMMERCIAL_COMPLEXITY_FIELDS: list[str] = [
    "sig_intl_footprint_score",
    "ti_leadership_score",
    "ti_negotiation_sales_score",
    "language_competitor_strength_score",
    "competitor_signal_strength_score",
]

HIGH_VALUE_MIN_SCORE: float = 7.32   # Hot or Warm (75/25 blend)
WEAK_MAX_SCORE:       float = 5.04   # below Cool  (75/25 blend)
DATA_QUALITY_MEDIUM_MISSING: int = 2
DATA_QUALITY_LOW_MISSING:    int = 5
TOP_DRIVER_THRESHOLD:   float = 0.15
WEAK_DRIVER_MIN_COEFF:  float = 0.10   # positive coefficients only

#: All columns appended by score_dataframe().
SCORE_OUTPUT_COLS: list[str] = [
    # ── Canonical result fields ──────────────────────────────────────────────
    "lr_z_score",
    "lean_model_prob",
    "icp_similarity_score",
    "company_size_score",
    "company_size_missing",
    "final_commercial_fit_score",
    "final_commercial_fit_score_75_25_legacy",   # audit comparison — temporary
    "commercial_tier",
    # ── Backward-compat aliases ──────────────────────────────────────────────
    "lean_model_logit",       # = lr_z_score
    "model_probability",      # = lean_model_prob
    # ── Audit: LR raw inputs ─────────────────────────────────────────────────
    "score_input_foreign_hq",
    "score_input_explicit_lnd",
    "score_input_intl_footprint",
    "score_input_employer_branding",
    "score_input_lnd_onboarding",
    "score_input_ti_onboarding",
    "score_input_rapid_growth",
    "score_input_employee_range",
    # ── Audit: normalised inputs ─────────────────────────────────────────────
    "norm_foreign_hq",
    "norm_explicit_lnd",
    "norm_intl_footprint",
    "norm_employer_branding",
    "norm_lnd_onboarding",
    "norm_ti_onboarding",
    "norm_rapid_growth",
    # ── Audit: LR components ─────────────────────────────────────────────────
    "lr_intercept_component",
    "lr_foreign_hq_component",
    "lr_explicit_lnd_component",
    "lr_intl_footprint_component",
    "lr_employer_branding_component",
    "lr_lnd_onboarding_component",
    "lr_ti_onboarding_component",
    "lr_rapid_growth_component",
    # ── Audit: size ──────────────────────────────────────────────────────────
    "employee_range_normalized",
    "score_employee_range_source",
    "score_employee_range_confidence",
    "size_needs_manual_review",
    "size_scoring_note",
    # ── Audit: sigmoid ───────────────────────────────────────────────────────
    "sigmoid_k",
    "sigmoid_s_min",
    "sigmoid_s_max",
    "sigmoid_input_value",
    "sigmoid_raw_s",
    # ── Audit: blend ─────────────────────────────────────────────────────────
    "model_weight",
    "size_weight",
    "weighted_model_component",
    "weighted_size_component",
    # ── Composite profile scores (display-only) ───────────────────────────────
    "global_complexity_score",
    "people_development_score",
    "commercial_complexity_score",
    # ── Quality flags ────────────────────────────────────────────────────────
    "high_value_flag",
    "weak_flag",
    "data_quality_flag",
    "top_score_drivers",
    "weak_score_drivers",
    "scoring_notes",
    "missing_scoring_fields",
    "scoring_profile",
]

# =============================================================================
# Internal helpers
# =============================================================================


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in ("", "nan", "none", "n/a", "unknown", "null")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _normalize_range_str(raw: str) -> str:
    """Canonicalise 'lower-upper' range string to 'lo - hi' form."""
    s = raw.strip().lower().replace(",", "").replace("–", "-").replace(" to ", "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    return s


def _parse_range_midpoint(raw: Any) -> float | None:
    """Return numeric midpoint of an employee-range string, or None."""
    s = str(raw or "").strip().lower().replace(",", "").replace("–", "-")
    if s in ("", "nan", "none", "n/a", "unknown", "null"):
        return None
    m = re.search(r"(\d+)\s*[-to]+\s*(\d+)", s)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    m = re.search(r"(\d+)\+", s)
    if m:
        return float(m.group(1)) * 1.5
    m = re.search(r"(\d+)", s)
    if m:
        return float(m.group(1))
    return None


def _band_lookup_midpoint(midpoint: float) -> float:
    for upper, score in _SIZE_MIDPOINT_BANDS:
        if midpoint < upper:
            return score
    return 10.0


def _resolve_size_score(row: dict) -> tuple[float, bool, str]:
    """Return (size_score, size_missing, range_key).

    Tries lusha_api_employee_range then lusha_employee_range.
    Falls back to SIZE_SCORE_MISSING when nothing is parseable.
    """
    for field in ("lusha_api_employee_range", "lusha_employee_range",
                  "employee_range", "company_size"):
        raw = row.get(field)
        if _is_missing(raw):
            continue
        # Try exact normalised-key lookup first
        norm_key = _normalize_range_str(str(raw))
        if norm_key in SIZE_BAND_LOOKUP:
            return SIZE_BAND_LOOKUP[norm_key], False, norm_key
        # Try direct key match (handles already-canonical strings)
        direct = str(raw).strip()
        if direct in SIZE_BAND_LOOKUP:
            return SIZE_BAND_LOOKUP[direct], False, direct
        # Fall back to midpoint-based band
        mid = _parse_range_midpoint(raw)
        if mid is not None:
            return _band_lookup_midpoint(mid), False, norm_key
    return SIZE_SCORE_MISSING, True, ""


# Priority order for employee_range source attribution.
# lusha_api_employee_range is always "" in Layer 1 (live API disabled),
# so it falls through to lusha_employee_range (uploaded static data),
# then employee_range (input file), then company_size (input file).
_EMPLOYEE_RANGE_SOURCE_PRIORITY: list[tuple[str, str, str]] = [
    # (field_name, source_label, confidence)  — confidence title-cased to match resolver
    ("employee_range",             "input_file",                 "High"),
    ("lusha_employee_range",       "uploaded_lusha_company_data","Medium"),
    ("lusha_api_employee_range",   "uploaded_lusha_company_data","Medium"),
    ("company_size",               "input_file",                 "High"),
]


def _resolve_employee_range_provenance(row: dict) -> tuple[str, str, bool]:
    """Return (employee_range_source, employee_range_confidence, size_needs_manual_review).

    Priority:
    1. employee_range in input file        → input_file / high
    2. lusha_employee_range (uploaded)     → uploaded_lusha_company_data / medium
    3. lusha_api_employee_range (uploaded) → uploaded_lusha_company_data / medium
       (live API is disabled in Layer 1; this field is always "" at runtime)
    4. company_size in input file          → input_file / high
    5. missing                             → missing / missing / True
    """
    for field, source, confidence in _EMPLOYEE_RANGE_SOURCE_PRIORITY:
        raw = row.get(field)
        if not _is_missing(raw):
            # Only credit the field if the value is parseable (not garbage)
            norm_key = _normalize_range_str(str(raw))
            if norm_key in SIZE_BAND_LOOKUP:
                return source, confidence, False
            if str(raw).strip() in SIZE_BAND_LOOKUP:
                return source, confidence, False
            if _parse_range_midpoint(raw) is not None:
                return source, confidence, False
    return "missing", "None", True


def _composite_score(row: dict, fields: list[str], max_per_field: float = 3.0) -> float:
    total   = sum(_to_float(row.get(f, 0)) for f in fields)
    ceiling = max_per_field * len(fields)
    if ceiling <= 0:
        return 0.0
    return round(min(total / ceiling * 10.0, 10.0), 1)


# =============================================================================
# Public API
# =============================================================================

_FIELD_TO_AUDIT_INPUT: dict[str, str] = {
    "sig_foreign_hq_score":        "score_input_foreign_hq",
    "sig_explicit_lnd_score":      "score_input_explicit_lnd",
    "sig_intl_footprint_score":    "score_input_intl_footprint",
    "sig_employer_branding_score": "score_input_employer_branding",
    "sig_lnd_onboarding_score":    "score_input_lnd_onboarding",
    "ti_onboarding_score":         "score_input_ti_onboarding",
    "sig_rapid_growth_score":      "score_input_rapid_growth",
}
_FIELD_TO_NORM: dict[str, str] = {
    "sig_foreign_hq_score":        "norm_foreign_hq",
    "sig_explicit_lnd_score":      "norm_explicit_lnd",
    "sig_intl_footprint_score":    "norm_intl_footprint",
    "sig_employer_branding_score": "norm_employer_branding",
    "sig_lnd_onboarding_score":    "norm_lnd_onboarding",
    "ti_onboarding_score":         "norm_ti_onboarding",
    "sig_rapid_growth_score":      "norm_rapid_growth",
}
_FIELD_TO_COMPONENT: dict[str, str] = {
    "sig_foreign_hq_score":        "lr_foreign_hq_component",
    "sig_explicit_lnd_score":      "lr_explicit_lnd_component",
    "sig_intl_footprint_score":    "lr_intl_footprint_component",
    "sig_employer_branding_score": "lr_employer_branding_component",
    "sig_lnd_onboarding_score":    "lr_lnd_onboarding_component",
    "ti_onboarding_score":         "lr_ti_onboarding_component",
    "sig_rapid_growth_score":      "lr_rapid_growth_component",
}


def score_company(
    row: "dict | pd.Series",
    params: dict | None = None,
) -> dict:
    """Score a single enriched company row using the Results(8)-compatible formula.

    Returns a dict whose keys are a superset of SCORE_OUTPUT_COLS, including
    all audit fields so callers can write them to a model_features sheet.
    """
    p = params or {}

    # Resolve scoring profile — profile name → profile dict → override individual params
    _profile_name = p.get("scoring_profile", "default")
    _profile = SCORING_PROFILES.get(_profile_name, SCORING_PROFILES["default"])

    intercept    = float(p.get("intercept", INTERCEPT))
    coeffs       = {**LEAN_COEFFICIENTS, **p.get("coefficients", {})}
    tiers        = p.get("tier_thresholds", _profile["tier_thresholds"])
    _model_w     = float(p.get("model_weight", _profile["model_weight"]))
    _size_w      = float(p.get("size_weight",  _profile["size_weight"]))
    _sig_k       = float(p.get("sigmoid_k",    _profile["sigmoid_k"]))

    if isinstance(row, pd.Series):
        row = row.to_dict()

    notes: list[str]   = []
    missing: list[str] = []
    out: dict = {}

    # ── 1. Read raw signal values, normalise, compute LR components ──────────
    lr_z  = intercept
    out["lr_intercept_component"] = intercept

    for field, coeff in coeffs.items():
        raw = row.get(field)
        if _is_missing(raw):
            missing.append(field)
            raw_val = 0.0
        else:
            raw_val = _clamp(_to_float(raw), 0.0, 3.0)

        norm = raw_val / 3.0
        comp = coeff * norm
        lr_z += comp

        out[_FIELD_TO_AUDIT_INPUT[field]] = raw_val
        out[_FIELD_TO_NORM[field]]        = round(norm, 6)
        out[_FIELD_TO_COMPONENT[field]]   = round(comp, 6)

    if missing:
        notes.append(
            f"Score based on incomplete data — {len(missing)} of {len(coeffs)} "
            f"signal field(s) missing (defaulted to 0): {', '.join(missing)}."
        )

    lr_z = _clamp(lr_z, -500.0, 500.0)

    # ── 2. Probability ────────────────────────────────────────────────────────
    lean_model_prob = 1.0 / (1.0 + math.exp(-lr_z))

    # ── 3. Sigmoid stretch → ICP Similarity Score ────────────────────────────
    # Use profile-specific K; recompute S_MIN/S_MAX so normalisation stays valid.
    _s_min = 1.0 / (1.0 + math.exp(-_sig_k * (_SIGMOID_P_LO - 0.5)))
    _s_max = 1.0 / (1.0 + math.exp(-_sig_k * (_SIGMOID_P_HI - 0.5)))
    sigmoid_raw_s = 1.0 / (1.0 + math.exp(-_sig_k * (lean_model_prob - 0.5)))
    _denom = _s_max - _s_min if abs(_s_max - _s_min) > 1e-9 else 1.0
    icp_sim = _clamp(1.0 + 9.0 * (sigmoid_raw_s - _s_min) / _denom, 1.0, 10.0)

    # ── 4. Size score ─────────────────────────────────────────────────────────
    size_score, size_missing, range_key = _resolve_size_score(row)
    er_source, er_confidence, er_needs_review = _resolve_employee_range_provenance(row)
    out["score_input_employee_range"] = str(
        next((row.get(f) for f in ("lusha_api_employee_range", "lusha_employee_range",
                                   "employee_range", "company_size")
              if not _is_missing(row.get(f))), "")
    )
    out["employee_range_normalized"] = range_key
    if _profile_name == "italy_register_icp_only":
        # Size is excluded from scoring for Italy register inputs; note is deferred to
        # the profile summary note added below — do not emit a per-row size note here.
        notes.append(
            "Company size excluded from Layer 1 scoring. Input list is register-filtered "
            "for 100+ employees. Employee range fields are audit-only."
        )
    elif size_missing:
        notes.append(
            "No employee range data found; company_size_score defaulted to "
            f"{SIZE_SCORE_MISSING}."
        )
    else:
        _src_label = {
            "input_file":                       "Employee range from input/Lucia data",
            "uploaded_lusha_company_data":      "Employee range from uploaded Lusha data",
            "explicit_text_employee_evidence":  "Employee range extracted from text evidence",
            "heuristic_size_estimate":          "Employee range estimated heuristically (Low confidence)",
            "existing_lucia_or_input_employee_range": "Employee range from Lucia/input data",
            "serper_employee_search":           "Employee range from Serper web search",
            "default_commercial_minimum_assumption": "Employee range: scoring default (no data found)",
        }.get(er_source, f"Employee range (source: {er_source})")
        notes.append(
            f"{_src_label}: {range_key} → company_size_score {round(size_score, 2)}/10."
        )

    # ── 5. Blend — profile-controlled weights ────────────────────────────────
    w_model = _model_w * icp_sim
    w_size  = _size_w  * size_score
    final   = _clamp(w_model + w_size, 1.0, 10.0)
    # Legacy 75/25 formula — kept for ranking-impact audit (default profile only).
    _legacy_final = _clamp(
        _LEGACY_MODEL_WEIGHT * icp_sim + _LEGACY_SIZE_WEIGHT * size_score,
        1.0, 10.0,
    )

    # ── 6. Tier ───────────────────────────────────────────────────────────────
    tier = TIER_THRESHOLDS[-1][1]
    for threshold, label in tiers:
        if final >= threshold:
            tier = label
            break

    # ── 7. Composite profile scores ───────────────────────────────────────────
    global_complexity     = _composite_score(row, GLOBAL_COMPLEXITY_FIELDS)
    people_development    = _composite_score(row, PEOPLE_DEVELOPMENT_FIELDS)
    commercial_complexity = _composite_score(row, COMMERCIAL_COMPLEXITY_FIELDS)

    # ── 8. Driver analysis ────────────────────────────────────────────────────
    pos_contributions = {
        f: out[_FIELD_TO_COMPONENT[f]]
        for f in coeffs
        if out[_FIELD_TO_COMPONENT[f]] > 0
    }
    total_pos = sum(pos_contributions.values()) or 1.0

    top_drivers = [
        f"{f}={out[_FIELD_TO_AUDIT_INPUT[f]]:.0f} (+{c:.2f})"
        for f, c in sorted(pos_contributions.items(), key=lambda x: -x[1])
        if c / total_pos >= TOP_DRIVER_THRESHOLD
    ]
    weak_drivers = [
        f"{f} (coeff={coeffs[f]:.4f}, current={out[_FIELD_TO_AUDIT_INPUT[f]]:.0f})"
        for f in sorted(coeffs, key=lambda x: -coeffs[x])
        if coeffs[f] >= WEAK_DRIVER_MIN_COEFF
        and out[_FIELD_TO_AUDIT_INPUT[f]] == 0.0
    ]

    # ── 9. Data quality ───────────────────────────────────────────────────────
    n_missing  = len(missing)
    manual_rev = _to_float(row.get("model_signal_needs_manual_review", 0)) > 0
    if manual_rev or n_missing >= DATA_QUALITY_LOW_MISSING:
        dqf = "low"
    elif n_missing >= DATA_QUALITY_MEDIUM_MISSING:
        dqf = "medium"
    else:
        dqf = "high"
    if dqf == "low" and not manual_rev:
        notes.append(f"Data quality is low — {n_missing} of {len(coeffs)} signal fields missing.")
    elif manual_rev:
        notes.append("Flagged for manual review; score reliability is reduced.")

    if _profile_name == "italy_register_icp_only":
        notes.append(
            f"Final score = ICP signal only (Italy register profile, size excluded). "
            f"icp_similarity={round(icp_sim, 2)}/10, K={_sig_k} → final={round(final, 2)}/10. "
            "Company size is audit/context data only; input list is register-filtered for 100+ employees."
        )
    else:
        notes.append(
            f"Final score = {round(_model_w*100):.0f}% ICP signal similarity + "
            f"{round(_size_w*100):.0f}% company size "
            f"({round(icp_sim, 2)} × {_model_w} + {round(size_score, 2)} × {_size_w}"
            f" = {round(final, 2)}/10)."
        )

    # ── Assemble result ───────────────────────────────────────────────────────
    out.update({
        # Canonical fields
        "lr_z_score":                  round(lr_z, 6),
        "lean_model_prob":             round(lean_model_prob, 7),
        "icp_similarity_score":        round(icp_sim, 2),
        "company_size_score":          round(size_score, 2),
        "company_size_missing":        size_missing,
        "final_commercial_fit_score":              round(final, 2),
        "final_commercial_fit_score_75_25_legacy": round(_legacy_final, 2),
        "commercial_tier":                         tier,
        # Backward-compat aliases (derived from canonical — not a separate calculation)
        "lean_model_logit":            round(lr_z, 6),
        "model_probability":           round(lean_model_prob, 7),
        # Sigmoid audit
        "sigmoid_k":           _sig_k,
        "sigmoid_s_min":       _s_min,
        "sigmoid_s_max":       _s_max,
        "sigmoid_input_value": round(lean_model_prob, 7),
        "sigmoid_raw_s":       round(sigmoid_raw_s, 7),
        # Blend audit
        "model_weight":              _model_w,
        "size_weight":               _size_w,
        "weighted_model_component":  round(w_model, 4),
        "weighted_size_component":   round(w_size, 4),
        "scoring_profile":           _profile_name,
        # Composite
        "global_complexity_score":     global_complexity,
        "people_development_score":    people_development,
        "commercial_complexity_score": commercial_complexity,
        # Flags
        "high_value_flag":        final >= HIGH_VALUE_MIN_SCORE,
        "weak_flag":              final < WEAK_MAX_SCORE,
        "data_quality_flag":      dqf,
        "top_score_drivers":      "; ".join(top_drivers) if top_drivers else "none",
        "weak_score_drivers":     "; ".join(weak_drivers) if weak_drivers else "none",
        "scoring_notes":          " | ".join(notes),
        "missing_scoring_fields": ", ".join(missing) if missing else "",
        # ── Employee range provenance as seen by scoring (separate from resolver fields) ───
        # For Italy register profile, size is excluded — override source label accordingly.
        "score_employee_range_source":      (
            "excluded_from_scoring_italy_register_profile"
            if _profile_name == "italy_register_icp_only" else er_source
        ),
        "score_employee_range_confidence":  (
            "N/A — size excluded from scoring"
            if _profile_name == "italy_register_icp_only" else er_confidence
        ),
        "size_needs_manual_review":         er_needs_review,
        "size_scoring_note":                (
            "Company size excluded from Layer 1 scoring. "
            "Italian register input is prefiltered for 100+ employees. "
            "Employee estimates are audit-only."
            if _profile_name == "italy_register_icp_only" else ""
        ),
    })
    return out


def score_dataframe(
    df: pd.DataFrame,
    params: dict | None = None,
    scoring_profile: str = "default",
) -> pd.DataFrame:
    """Apply score_company to every row and append SCORE_OUTPUT_COLS.

    scoring_profile: "default" or "italy_register_icp_only"
    """
    _params = dict(params or {})
    if "scoring_profile" not in _params:
        _params["scoring_profile"] = scoring_profile
    records = df.to_dict("records")
    scored  = [score_company(r, _params) for r in records]
    for col in SCORE_OUTPUT_COLS:
        df[col] = [r.get(col) for r in scored]
    return df


# =============================================================================
# Smoke tests — run with:  python commercial_fit_scoring.py
# =============================================================================

if __name__ == "__main__":
    import json

    PASS  = "\033[92m✓\033[0m"
    FAIL  = "\033[91m✗\033[0m"
    _failures: list[str] = []

    def _chk(label: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"  {PASS}  {label}")
        else:
            _failures.append(label)
            print(f"  {FAIL}  {label}" + (f"  [{detail}]" if detail else ""))

    def _section(title: str) -> None:
        print(f"\n{'─'*60}\n  {title}\n{'─'*60}")

    def _tier_for(score: float) -> str:
        t = TIER_THRESHOLDS[-1][1]
        for thresh, lbl in TIER_THRESHOLDS:
            if score >= thresh:
                t = lbl
                break
        return t

    # ── Smoke Test 1: Capgemini reference ─────────────────────────────────────
    _section("Smoke Test 1: Capgemini reference values (Results(8).xlsx)")

    capgemini = {
        "sig_foreign_hq_score":        3,
        "sig_explicit_lnd_score":      3,
        "sig_intl_footprint_score":    3,
        "sig_employer_branding_score": 2,
        "sig_lnd_onboarding_score":    2,
        "ti_onboarding_score":         2,
        "sig_rapid_growth_score":      1,
        "lusha_api_employee_range":    "100001 - 10000000",
    }
    r1 = score_company(capgemini)
    print(json.dumps({k: v for k, v in r1.items()
                      if k in ("lr_z_score", "lean_model_prob", "icp_similarity_score",
                               "company_size_score", "final_commercial_fit_score",
                               "commercial_tier", "model_probability")},
                     indent=2))

    _chk("lr_z_score ≈ 0.9870",
         abs(r1["lr_z_score"] - 0.9870) < 0.001,
         str(round(r1["lr_z_score"], 4)))
    _chk("lean_model_prob ≈ 0.7285",
         abs(r1["lean_model_prob"] - 0.7285) < 0.001,
         str(round(r1["lean_model_prob"], 4)))
    _chk("model_probability == lean_model_prob (alias, not separate calc)",
         r1["model_probability"] == r1["lean_model_prob"],
         str(r1["model_probability"]))
    _chk("model_probability ≠ 0.9992  (old wrong value)",
         abs(r1["model_probability"] - 0.9992) > 0.01,
         str(round(r1["model_probability"], 4)))
    _chk("icp_similarity_score ≈ 9.69  (k=10)",
         abs(r1["icp_similarity_score"] - 9.69) < 0.05,
         str(r1["icp_similarity_score"]))
    _chk("company_size_score = 10",
         r1["company_size_score"] == 10.0,
         str(r1["company_size_score"]))
    _chk("final_commercial_fit_score ≈ 9.77  (75/25 blend, k=10)",
         abs(r1["final_commercial_fit_score"] - 9.77) < 0.05,
         str(r1["final_commercial_fit_score"]))
    _chk("final_commercial_fit_score_75_25_legacy == final_commercial_fit_score "
         "(default profile blend IS 75/25 now)",
         r1["final_commercial_fit_score_75_25_legacy"] == r1["final_commercial_fit_score"],
         f"{r1['final_commercial_fit_score_75_25_legacy']} vs {r1['final_commercial_fit_score']}")
    _chk("commercial_tier = 🥇 Hot",
         r1["commercial_tier"] == "🥇 Hot",
         r1["commercial_tier"])
    _chk("commercial_tier ≠ 'Tier 1'  (old wrong value)",
         r1["commercial_tier"] != "Tier 1",
         r1["commercial_tier"])
    _chk("company_size_missing is False",
         r1["company_size_missing"] is False)
    _chk("lean_model_logit == lr_z_score (alias)",
         r1["lean_model_logit"] == r1["lr_z_score"])

    # ── Smoke Test 2: All signals = 3, largest employee range ─────────────────
    _section("Smoke Test 2: Maximum signals, largest range")

    strong = {f: 3 for f in LEAN_COEFFICIENTS}
    strong["lusha_api_employee_range"] = "100001 - 10000000"
    r2 = score_company(strong)
    _chk("lean_model_prob > 0.65",  r2["lean_model_prob"] > 0.65, str(round(r2["lean_model_prob"], 4)))
    _chk("company_size_score = 10", r2["company_size_score"] == 10.0)
    _chk("commercial_tier = 🥇 Hot", r2["commercial_tier"] == "🥇 Hot", r2["commercial_tier"])
    _chk("high_value_flag is True",  r2["high_value_flag"] is True)

    # ── Smoke Test 3: All signals = 0, smallest range ─────────────────────────
    _section("Smoke Test 3: Minimum signals, smallest range")

    weak = {f: 0 for f in LEAN_COEFFICIENTS}
    weak["lusha_api_employee_range"] = "1 - 10"
    r3 = score_company(weak)
    _chk("lean_model_prob < 0.5",   r3["lean_model_prob"] < 0.5, str(round(r3["lean_model_prob"], 4)))
    _chk("company_size_score = 1.0", r3["company_size_score"] == 1.0)
    _chk("commercial_tier = ❄️ Pass", r3["commercial_tier"] == "❄️ Pass", r3["commercial_tier"])
    _chk("weak_flag is True",        r3["weak_flag"] is True)

    # ── Smoke Test 4: Missing employee range ──────────────────────────────────
    _section("Smoke Test 4: Missing employee range → defaults to 5.5")

    no_size = {f: 2 for f in LEAN_COEFFICIENTS}
    r4 = score_company(no_size)
    _chk("company_size_score = 5.5",    r4["company_size_score"] == SIZE_SCORE_MISSING,
         str(r4["company_size_score"]))
    _chk("company_size_missing is True", r4["company_size_missing"] is True)

    # ── Smoke Test 5: size band exact-string variants ─────────────────────────
    _section("Smoke Test 5: Employee range string variants")

    for raw, expected in [
        ("100001 - 10000000", 10.0),
        ("10001 - 100000",    8.88),
        ("5001 - 10000",      7.75),
        ("1001 - 5000",       6.63),
        ("501 - 1000",         5.5),
        ("201 - 500",          4.38),
        ("51 - 200",           3.25),
        ("11 - 50",            2.13),
        ("1 - 10",             1.0),
    ]:
        rz = score_company({"lusha_api_employee_range": raw})
        _chk(f"range '{raw}' → {expected}",
             rz["company_size_score"] == expected,
             str(rz["company_size_score"]))

    # ── Smoke Test 6: lusha_employee_range fallback ───────────────────────────
    _section("Smoke Test 6: lusha_employee_range fallback key")

    r6 = score_company({"lusha_employee_range": "1001 - 5000",
                         "sig_foreign_hq_score": 2})
    _chk("lusha_employee_range fallback → 6.63",
         r6["company_size_score"] == 6.63,
         str(r6["company_size_score"]))

    # ── Smoke Test 7: audit columns present ──────────────────────────────────
    _section("Smoke Test 7: All SCORE_OUTPUT_COLS produced by score_dataframe")

    test_df = pd.DataFrame([capgemini, strong, weak])
    out_df  = score_dataframe(test_df.copy())
    missing_cols = [c for c in SCORE_OUTPUT_COLS if c not in out_df.columns]
    _chk("all SCORE_OUTPUT_COLS present",
         len(missing_cols) == 0,
         str(missing_cols))
    _chk("row count unchanged", len(out_df) == 3)

    # ── Smoke Test 8: sig_merger_acq_score has NO effect ─────────────────────
    _section("Smoke Test 8: sig_merger_acq_score is NOT a coefficient input")

    row_with_merger  = {**capgemini, "sig_merger_acq_score": 3}
    row_no_merger    = {**capgemini, "sig_merger_acq_score": 0}
    r8a = score_company(row_with_merger)
    r8b = score_company(row_no_merger)
    _chk("sig_merger_acq_score=3 and =0 produce same lr_z_score",
         r8a["lr_z_score"] == r8b["lr_z_score"],
         f"{r8a['lr_z_score']} vs {r8b['lr_z_score']}")

    # ── Smoke Test 9: params override ────────────────────────────────────────
    _section("Smoke Test 9: intercept override drives prob to ~0")

    r9 = score_company(strong, params={"intercept": -99.0})
    _chk("intercept=-99 → lean_model_prob < 0.01",
         r9["lean_model_prob"] < 0.01, str(r9["lean_model_prob"]))
    _chk("intercept=-99 → ❄️ Pass",
         r9["commercial_tier"] == "❄️ Pass", r9["commercial_tier"])

    # ── Smoke Test 10: tier boundaries ───────────────────────────────────────
    _section("Smoke Test 10: Tier boundary values")

    for score_val, expected_tier in [
        (9.5,  "🥇 Hot"),
        (8.86, "🥇 Hot"),
        (8.5,  "🥈 Warm"),
        (7.32, "🥈 Warm"),
        (6.0,  "🥉 Cool"),
        (5.04, "🥉 Cool"),
        (2.0,  "❄️ Pass"),
    ]:
        row_t = {f: 0 for f in LEAN_COEFFICIENTS}
        row_t["lusha_api_employee_range"] = "1 - 10"
        r_t = score_company(row_t)
        # Directly test tier threshold logic
        computed_tier = TIER_THRESHOLDS[-1][1]
        for thresh, lbl in TIER_THRESHOLDS:
            if score_val >= thresh:
                computed_tier = lbl
                break
        _chk(f"score {score_val} → {expected_tier}",
             computed_tier == expected_tier,
             computed_tier)

    # ── Smoke Test 11: 75/25 weight verification ─────────────────────────────
    _section("Smoke Test 11: 75/25 blend — weights, legacy, tier")

    # Sanity: weights sum to 1
    _chk("ICP_SIMILARITY_WEIGHT + COMPANY_SIZE_WEIGHT == 1.0",
         abs((ICP_SIMILARITY_WEIGHT + COMPANY_SIZE_WEIGHT) - 1.0) < 1e-9)
    _chk("ICP_SIMILARITY_WEIGHT == 0.75", ICP_SIMILARITY_WEIGHT == 0.75)
    _chk("COMPANY_SIZE_WEIGHT   == 0.25", COMPANY_SIZE_WEIGHT   == 0.25)

    # Case A: High ICP, low size — under 75/25 the small-company size score
    # (1.0) pulls the blend down enough to land in Warm, not Hot.
    case_a = {
        "sig_foreign_hq_score": 3, "sig_explicit_lnd_score": 3,
        "sig_intl_footprint_score": 3, "sig_employer_branding_score": 3,
        "sig_lnd_onboarding_score": 3, "ti_onboarding_score": 3,
        "sig_rapid_growth_score": 3,
        "lusha_api_employee_range": "1 - 10",   # size_score = 1.0
    }
    ra = score_company(case_a)
    _chk("Case A: final ≈ 7.44  (high ICP, low size, 75/25 blend)",
         abs(ra["final_commercial_fit_score"] - 7.44) < 0.05,
         str(ra["final_commercial_fit_score"]))
    _chk("Case A: tier = 🥈 Warm  (size drag keeps it out of Hot)",
         ra["commercial_tier"] == "🥈 Warm", ra["commercial_tier"])
    _chk("Case A: final == final_75_25_legacy  (default profile blend IS 75/25)",
         ra["final_commercial_fit_score"] == ra["final_commercial_fit_score_75_25_legacy"],
         f"{ra['final_commercial_fit_score']} vs {ra['final_commercial_fit_score_75_25_legacy']}")
    _chk("Case A: legacy column present",
         "final_commercial_fit_score_75_25_legacy" in ra)

    # Case B: Medium ICP, very high size — size contributes a full quarter now.
    case_b_signals = {f: 1 for f in LEAN_COEFFICIENTS}
    case_b_signals["lusha_api_employee_range"] = "100001 - 10000000"  # size_score = 10.0
    rb = score_company(case_b_signals)
    _chk("Case B: final ≈ 6.5  (medium ICP, max size, 75/25 blend)",
         abs(rb["final_commercial_fit_score"] - 6.5) < 0.05,
         str(rb["final_commercial_fit_score"]))
    _chk("Case B: tier = 🥉 Cool",
         rb["commercial_tier"] == "🥉 Cool", rb["commercial_tier"])

    # Case C: High ICP, high size — should be among the highest
    case_c = {
        "sig_foreign_hq_score": 3, "sig_explicit_lnd_score": 3,
        "sig_intl_footprint_score": 3, "sig_employer_branding_score": 3,
        "sig_lnd_onboarding_score": 3, "ti_onboarding_score": 3,
        "sig_rapid_growth_score": 3,
        "lusha_api_employee_range": "100001 - 10000000",  # size_score = 10.0
    }
    rc = score_company(case_c)
    _chk("Case C: final > 9.5  (high ICP + high size = top score)",
         rc["final_commercial_fit_score"] > 9.5,
         str(rc["final_commercial_fit_score"]))
    _chk("Case C: final >= Case A final  (size still adds a small boost)",
         rc["final_commercial_fit_score"] >= ra["final_commercial_fit_score"],
         f"{rc['final_commercial_fit_score']} vs {ra['final_commercial_fit_score']}")
    _chk("Case C: tier = 🥇 Hot", rc["commercial_tier"] == "🥇 Hot", rc["commercial_tier"])

    # Confirm tier uses the NEW score, not legacy
    _chk("Case A: tier computed from final_commercial_fit_score (not legacy)",
         ra["commercial_tier"] == _tier_for(ra["final_commercial_fit_score"]))

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if _failures:
        print(f"  FAILURES ({len(_failures)}):")
        for f in _failures:
            print(f"    • {f}")
    else:
        print("  All smoke tests passed.")
    print("═"*60)
