"""Shared output schema for Lead Prioritizer v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LeadInput:
    company_name: str
    domain: Optional[str] = None
    input_country: Optional[str] = None
    # Optional Lusha-export fields (Lusha enrichment plan, Stap 2). Blank for
    # any non-Lusha caller -- every downstream consumer treats these as
    # "no Lusha data available" and falls back to its existing behavior.
    lusha_main_industry: Optional[str] = None
    lusha_sub_industry: Optional[str] = None
    lusha_description: Optional[str] = None
    lusha_specialties: Optional[str] = None


@dataclass
class HQDetectionResult:
    hq_detected_country: Optional[str] = None
    hq_detected_city: Optional[str] = None
    hq_confidence: Optional[str] = None          # "High" | "Medium" | "Low"
    foreign_hq_simple: Optional[bool] = None
    needs_manual_review: bool = False
    hq_reason: Optional[str] = None
    hq_evidence_url: Optional[str] = None
    # ALL usable HQ evidence URLs (deduplicated, ordered, mechanically
    # validated against the Serper payload actually supplied to the AI —
    # never an invented URL); hq_evidence_url is unchanged and always
    # equals hq_evidence_urls[0] when non-empty.
    hq_evidence_urls: list = field(default_factory=list)
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
    # Industry/sector the HQ interpreter derived from the same material
    # (primarily the company's own crawled-domain content) — a free side
    # product of the HQ call, at no extra API cost. Audit/app metadata only;
    # never feeds scoring. See lead_prioritizer_core.py for how this backs up
    # the deterministic keyword-based sector detector when it finds nothing.
    ai_hq_industry: Optional[str] = None
    ai_hq_sub_industry: Optional[str] = None
    ai_call_attempted: Optional[str] = None      # "Yes" | "No"
    ai_call_success: Optional[str] = None        # "Yes" | "No"
    ai_hq_error: Optional[str] = None
    ai_hq_raw_json: Optional[str] = None         # raw model text (truncated), for debug
    # Provider/usage audit (experimental multi-provider comparison; in-memory
    # only — deliberately NOT flattened into Excel / Lovable exports)
    ai_hq_provider: Optional[str] = None         # "anthropic" | "openai"
    ai_hq_input_tokens: Optional[int] = None
    ai_hq_output_tokens: Optional[int] = None
    ai_hq_total_tokens: Optional[int] = None
    ai_hq_estimated_cost_usd: Optional[float] = None
    # Anthropic prompt-caching audit (Anthropic path only; always None for
    # OpenAI/DeepSeek). See lead_hq_ai_interpreter._call_anthropic_hq.
    ai_hq_cache_creation_tokens: Optional[int] = None
    ai_hq_cache_read_tokens: Optional[int] = None
    # Cache-aware cost estimate, kept alongside (not replacing)
    # ai_hq_estimated_cost_usd above so the two can be compared during
    # validation. See lead_hq_ai_interpreter.estimate_ai_cost_usd_with_cache.
    ai_hq_estimated_cost_usd_with_cache: Optional[float] = None
    # C4 positive-score safety audit (optional, backwards compatible)
    hq_query_risk_flag: Optional[str] = None                    # "Yes" | "No"
    hq_evidence_domain_match: Optional[str] = None              # "Yes" | "No" | ""
    hq_evidence_domain_mismatch_warning: Optional[str] = None   # "Yes" | "No"
    hq_positive_score_suppressed_for_review: Optional[str] = None  # "Yes" | "No"
    hq_review_reason: Optional[str] = None


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
    # ALL usable evidence URLs backing this signal (deduplicated, ordered);
    # evidence_url above is unchanged and always equals evidence_urls[0]
    # when non-empty. See lead_non_hq_signal_extractor.py.
    evidence_urls: list = field(default_factory=list)


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
    hq_evidence_urls: list = field(default_factory=list)
    hq_evidence_quote: Optional[str] = None

    # HQ structure type
    hq_structure_type: Optional[str] = None
    # Always-shown structured HQ location line for the app (ADDITIONAL to, and
    # independent of, the foreign_ownership_or_group_structure driver badge).
    # "Parent company headquarters: {city}, {country}" for a foreign parent,
    # "Headquarters: {city}, {country}" for a domestic HQ, else None. Localized
    # for NL/IT in the export. See lead_hq_location_summary.py.
    hq_location_summary: Optional[str] = None
    # Scoring input signals (not scores themselves)
    sig_foreign_hq_score_for_next_scoring: Optional[float] = None
    # Competitor evidence is audit-only; excluded from scoring
    competitor_signal_excluded_from_next_scoring: Optional[str] = None
    # Query / parser provenance (for audit & debug)
    domain_root: Optional[str] = None
    query_used: Optional[str] = None
    parser_source: Optional[str] = None
    # True when the input `domain` resolves to a hosted careers/job platform
    # (Workday, Greenhouse, Lever, ...) rather than the company's own site.
    # The original `domain` value is never overwritten; this only flags that
    # it was not treated as the lead's own website for HQ/query purposes.
    domain_is_hosted_platform: Optional[bool] = None
    # C4 positive-score safety audit (optional, backwards compatible)
    hq_query_risk_flag: Optional[str] = None
    hq_evidence_domain_match: Optional[str] = None
    hq_evidence_domain_mismatch_warning: Optional[str] = None
    hq_positive_score_suppressed_for_review: Optional[str] = None
    hq_review_reason: Optional[str] = None
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
    # Industry/sector the HQ interpreter derived from the same material as
    # its HQ classification (see HQDetectionResult.ai_hq_industry) — feeds the
    # sector fallback below, never scoring.
    ai_hq_industry: Optional[str] = None
    ai_hq_sub_industry: Optional[str] = None
    # Provider/usage audit (experimental multi-provider comparison; in-memory
    # only — deliberately NOT in _RESULT_FLAT_FIELDS, so Excel / Lovable
    # exports are unchanged)
    ai_hq_provider: Optional[str] = None
    ai_hq_input_tokens: Optional[int] = None
    ai_hq_output_tokens: Optional[int] = None
    ai_hq_total_tokens: Optional[int] = None
    ai_hq_estimated_cost_usd: Optional[float] = None
    # Anthropic prompt-caching audit (Anthropic path only; always None for
    # OpenAI/DeepSeek). See lead_hq_ai_interpreter._call_anthropic_hq.
    ai_hq_cache_creation_tokens: Optional[int] = None
    ai_hq_cache_read_tokens: Optional[int] = None
    # Cache-aware cost estimate, kept alongside (not replacing)
    # ai_hq_estimated_cost_usd above so the two can be compared during
    # validation. See lead_hq_ai_interpreter.estimate_ai_cost_usd_with_cache.
    ai_hq_estimated_cost_usd_with_cache: Optional[float] = None

    # ── Non-HQ v2 signal scores (placeholders — no live enrichment yet) ────────
    sig_international_profile_score: Optional[float] = None
    sig_onboarding_training_need_score: Optional[float] = None
    sig_company_size_complexity_score: Optional[float] = None
    sig_icp_keyword_match_score: Optional[float] = None
    sig_employer_branding_score: Optional[float] = None
    # Reasons
    international_profile_reason: Optional[str] = None
    onboarding_training_need_reason: Optional[str] = None
    company_size_complexity_reason: Optional[str] = None
    icp_keyword_match_reason: Optional[str] = None
    employer_branding_reason: Optional[str] = None
    # Evidence URLs
    international_profile_evidence_url: Optional[str] = None
    onboarding_training_need_evidence_url: Optional[str] = None
    company_size_complexity_evidence_url: Optional[str] = None
    icp_keyword_match_evidence_url: Optional[str] = None
    employer_branding_evidence_url: Optional[str] = None
    # ALL usable evidence URLs per signal, semicolon-joined (same guards as
    # the single evidence_url above; that field is unchanged and always
    # equals the first entry here).
    international_profile_evidence_urls: Optional[str] = None
    onboarding_training_need_evidence_urls: Optional[str] = None
    company_size_complexity_evidence_urls: Optional[str] = None
    icp_keyword_match_evidence_urls: Optional[str] = None
    employer_branding_evidence_urls: Optional[str] = None
    # Evidence quotes
    international_profile_evidence_quote: Optional[str] = None
    onboarding_training_need_evidence_quote: Optional[str] = None
    company_size_complexity_evidence_quote: Optional[str] = None
    icp_keyword_match_evidence_quote: Optional[str] = None
    employer_branding_evidence_quote: Optional[str] = None
    # Which deterministic signal-extractor keyword set produced these signals
    # (e.g. "v2-multilingual" once localized keywords/gl/hl are in use), so
    # old and new datasets are never silently mixed.
    signal_extractor_version: Optional[str] = None
    # "deterministic" (default) or "ai" (Onderdeel 2 opt-in) -- which path
    # actually produced `signals`, so AI- and keyword-scored datasets are
    # never silently mixed.
    signal_scoring_mode: Optional[str] = "deterministic"
    # AI signal-scoring usage/cost audit (populated only when ai_signal_scoring
    # was actually attempted -- see lead_ai_signal_scorer.py). Blank when the
    # call was never attempted, or when MODEL_PRICING_USD_PER_MTOK does not
    # know the model -- never a guessed cost, mirrors ai_hq_estimated_cost_usd.
    non_hq_ai_model: Optional[str] = None
    non_hq_ai_input_tokens: Optional[int] = None
    non_hq_ai_output_tokens: Optional[int] = None
    non_hq_ai_total_tokens: Optional[int] = None
    non_hq_ai_estimated_cost_usd: Optional[float] = None
    # ── Sector / industry detection (audit & app metadata — NEVER scoring) ─────
    detected_industry: Optional[str] = None
    detected_sub_industry: Optional[str] = None
    detected_company_type: Optional[str] = None
    sector_confidence: Optional[str] = None
    sector_reason: Optional[str] = None
    sector_evidence_url: Optional[str] = None
    sector_evidence_quote: Optional[str] = None
    sector_source_title: Optional[str] = None
    # Which path produced detected_industry/detected_sub_industry:
    # "lusha_mapped" (Lusha Sub/Main Industry mapped onto our internal
    # categories, highest priority — see lead_lusha_sector_mapping.py),
    # "keyword_match" (deterministic Serper-snippet keyword match),
    # "own_domain_ai" (the HQ interpreter's AI-derived industry from
    # genuinely crawled own-domain content), or "lusha_text_fallback" (last
    # resort: the same keyword matcher applied to Lusha Company
    # Description/Specialties text). None when nothing found anything.
    sector_source: Optional[str] = None
    # Raw Lusha industry values (audit only) — always populated verbatim
    # from the input row when present, REGARDLESS of whether the mapping in
    # lead_lusha_sector_mapping.py produced a hit, so the original label is
    # never lost even when detected_industry came from a different tier.
    lusha_main_industry: Optional[str] = None
    lusha_sub_industry: Optional[str] = None
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
    cold_caller_summary_app: Optional[str] = None
    parent_hq_summary_app: Optional[str] = None

    # ── AI-composed caller content (Step 3 — opt-in, off by default; never
    # auto-enabled by run_full_v2_pipeline). Falls back silently to the
    # deterministic *_app templates above when unavailable;
    # composed_content_note records why (missing key, call/parse failure, or
    # success) for audit purposes. ────────────────────────────────────────────
    composed_why_relevant: Optional[str] = None
    composed_what_is_hot: Optional[str] = None
    composed_cold_caller_summary: Optional[str] = None
    composed_caller_angle: Optional[str] = None
    composed_call_starter: Optional[str] = None
    composed_driver_evidence_json: Optional[str] = None
    composed_by_ai: Optional[bool] = None
    composed_content_note: Optional[str] = None

    # ── AI-composed rich ICP context (opt-in, off by default; independent of
    # compose_caller_content_flag above — see lead_icp_context_composer.py).
    # Never read by scoring, signal extraction, or the *_app templates;
    # icp_context_content_note records why (missing key, call/parse failure,
    # or success) for audit purposes. ───────────────────────────────────────
    icp_buying_signals: Optional[str] = None
    icp_likely_training_interest: Optional[str] = None
    icp_potential_buyer_function: Optional[str] = None
    icp_context_by_ai: Optional[bool] = None
    icp_context_content_note: Optional[str] = None

    # ── Legacy enrichment mode (opt-in, off by default; comparison feature —
    # see lead_legacy_enrichment.py). Reproduces the enrich_clients_claude.py
    # Step-2 evaluation style for direct side-by-side comparison against the
    # v2 pipeline. Runs NEXT TO the normal v2 flow, never replacing it —
    # final_commercial_fit_score and signals are completely untouched.
    # NOTE: the icp_* names here are deliberately prefixed with "legacy_"
    # (legacy_icp_*) even though LegacyEnrichmentResult itself uses the bare
    # enrich_clients_claude.py field names (icp_buying_signals, etc.) --
    # bare names would collide with the rich-ICP-context fields directly
    # above, and the two features must stay independently usable (including
    # both at once) without overwriting each other. ───────────────────────
    legacy_score: Optional[float] = None
    legacy_tier: Optional[str] = None
    legacy_icp_lead_score: Optional[str] = None
    legacy_icp_buying_signals: Optional[str] = None
    legacy_icp_likely_training_interest: Optional[str] = None
    legacy_icp_potential_buyer_function: Optional[str] = None
    legacy_icp_why_relevant: Optional[str] = None
    legacy_icp_evidence: Optional[str] = None
    legacy_enrichment_error: Optional[str] = None

    # ── Run metadata ──────────────────────────────────────────────────────────
    # "hq_only" | "partial_v2" | "full_v2_single_lead"
    v2_pipeline_mode: Optional[str] = None
