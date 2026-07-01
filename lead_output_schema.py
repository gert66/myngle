"""Shared output schema for Lead Prioritizer v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LeadInput:
    company_name: str
    domain: Optional[str] = None
    input_country: Optional[str] = None


@dataclass
class HQDetectionResult:
    hq_detected_country: Optional[str] = None
    hq_detected_city: Optional[str] = None
    hq_confidence: Optional[str] = None          # "High" | "Medium" | "Low"
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    hq_evidence_quote: Optional[str] = None
    # Parser provenance
    domain_root: Optional[str] = None
    query_used: Optional[str] = None
    parser_source: Optional[str] = None          # e.g. "knowledge_graph", "answer_box", "organic_1"
    # HQ structure type (set by AI path)
    hq_structure_type: Optional[str] = None      # "domestic", "foreign_parent", "regional_branch_only", …
    # Scoring signal
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # AI audit fields (populated only when AI-first path is used)
    ai_hq_model: Optional[str] = None
    ai_hq_classification: Optional[str] = None  # raw AI classification
    ai_hq_confidence: Optional[str] = None      # raw AI confidence
    ai_parent_company: Optional[str] = None
    ai_parent_hq_country: Optional[str] = None
    ai_parent_hq_city: Optional[str] = None
    ai_call_attempted: Optional[str] = None      # "Yes" | "No"
    ai_call_success: Optional[str] = None        # "Yes" | "No"
    ai_hq_error: Optional[str] = None
    ai_hq_raw_json: Optional[str] = None         # raw model text (truncated), for debug


@dataclass
class LeadEvidence:
    """A single piece of source evidence backing a signal.

    Evidence must flow downstream with every signal so any score can be traced
    back to the search result / parser output it came from.
    """
    evidence_id: Optional[str] = None
    signal_name: Optional[str] = None
    query_used: Optional[str] = None
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    source_snippet: Optional[str] = None
    source_type: Optional[str] = None            # e.g. "knowledge_graph", "answer_box", "organic"
    parser_source: Optional[str] = None
    retrieved_at: Optional[str] = None           # ISO-8601 timestamp string
    confidence: Optional[str] = None             # "High" | "Medium" | "Low"
    notes: Optional[str] = None


@dataclass
class LeadSignal:
    """A single extracted signal with its score and backing evidence."""
    signal_name: str
    signal_value: Optional[str] = None
    signal_score: Optional[float] = None
    signal_confidence: Optional[str] = None      # "High" | "Medium" | "Low"
    signal_reason: Optional[str] = None
    evidence_url: Optional[str] = None
    evidence_quote: Optional[str] = None
    evidence_title: Optional[str] = None
    query_used: Optional[str] = None
    parser_source: Optional[str] = None
    needs_manual_review: bool = False


@dataclass
class LeadEnrichmentResult:
    """Optional grouped container for the raw enrichment output of one lead.

    Groups the HQ detection result with the collected signals and evidence so a
    single enrichment pass can be passed around before it is flattened into a
    ``LeadPrioritizationResult``.  Placeholder for now — non-HQ enrichment is
    not implemented yet.
    """
    hq: Optional[HQDetectionResult] = None
    signals: list[LeadSignal] = field(default_factory=list)
    evidence_items: list[LeadEvidence] = field(default_factory=list)


@dataclass
class LeadPrioritizationResult:
    company_name: str
    domain: Optional[str] = None
    input_country: Optional[str] = None

    # HQ detection fields
    hq_detected_country: Optional[str] = None
    hq_detected_city: Optional[str] = None
    hq_confidence: Optional[str] = None
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    hq_evidence_quote: Optional[str] = None

    # HQ structure type
    hq_structure_type: Optional[str] = None
    # Scoring input signals (not scores themselves)
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # Competitor evidence is audit-only; excluded from scoring
    competitor_signal_excluded_from_next_scoring: Optional[str] = None
    # Query / parser provenance (for audit & debug)
    domain_root: Optional[str] = None
    query_used: Optional[str] = None
    parser_source: Optional[str] = None
    # AI audit fields
    ai_hq_model: Optional[str] = None
    ai_hq_classification: Optional[str] = None
    ai_hq_confidence: Optional[str] = None
    ai_parent_company: Optional[str] = None
    ai_parent_hq_country: Optional[str] = None
    ai_parent_hq_city: Optional[str] = None
    ai_call_attempted: Optional[str] = None
    ai_call_success: Optional[str] = None
    ai_hq_error: Optional[str] = None
    ai_hq_raw_json: Optional[str] = None

    # ── Non-HQ v2 signal scores (placeholders — no live enrichment yet) ────────
    sig_international_profile_score: Optional[float] = None
    sig_onboarding_training_need_score: Optional[float] = None
    sig_company_size_complexity_score: Optional[float] = None
    sig_icp_keyword_match_score: Optional[float] = None
    # Reasons
    international_profile_reason: Optional[str] = None
    onboarding_training_need_reason: Optional[str] = None
    company_size_complexity_reason: Optional[str] = None
    icp_keyword_match_reason: Optional[str] = None
    # Evidence URLs
    international_profile_evidence_url: Optional[str] = None
    onboarding_training_need_evidence_url: Optional[str] = None
    company_size_complexity_evidence_url: Optional[str] = None
    icp_keyword_match_evidence_url: Optional[str] = None
    # Evidence quotes
    international_profile_evidence_quote: Optional[str] = None
    onboarding_training_need_evidence_quote: Optional[str] = None
    company_size_complexity_evidence_quote: Optional[str] = None
    icp_keyword_match_evidence_quote: Optional[str] = None
    # App-facing text (placeholders)
    evidence_summary_app: Optional[str] = None
    key_source_links_app: Optional[str] = None
    advanced_notes_app: Optional[str] = None

    # ── Structured evidence / signals (flow downstream with every signal) ──────
    evidence_items: list[LeadEvidence] = field(default_factory=list)
    signals: list[LeadSignal] = field(default_factory=list)

    # ── Commercial scoring output (Step 5 — opt-in, single-lead flow only) ─────
    final_commercial_fit_score: Optional[float] = None
    commercial_tier: Optional[str] = None
    icp_similarity_score: Optional[float] = None
    lean_model_prob: Optional[float] = None
    lr_z_score: Optional[float] = None
    scoring_profile: Optional[str] = None
    scoring_notes: Optional[str] = None
    missing_scoring_fields: Optional[str] = None
    top_score_drivers: Optional[str] = None
    weak_score_drivers: Optional[str] = None
    # v2 scoring audit — how v2 signals were mapped into score_company inputs
    v2_score_input_mapping_note: Optional[str] = None
    score_input_foreign_hq: Optional[float] = None
    score_input_intl_footprint: Optional[float] = None
    score_input_explicit_lnd: Optional[float] = None
    score_input_lnd_onboarding: Optional[float] = None
    score_input_rapid_growth: Optional[float] = None

    # ── Caller / app-facing fields (Step 6 — opt-in, deterministic) ───────────
    commercial_fit_score_app: Optional[float] = None
    commercial_tier_app: Optional[str] = None
    what_is_hot_app: Optional[str] = None
    what_is_not_app: Optional[str] = None
    why_relevant_app: Optional[str] = None
    caller_angle_app: Optional[str] = None
    call_starter_app: Optional[str] = None
    caution_app: Optional[str] = None
    foreign_hq_signal_used_in_app: Optional[str] = None
    foreign_hq_country_app: Optional[str] = None
    foreign_hq_city_app: Optional[str] = None
