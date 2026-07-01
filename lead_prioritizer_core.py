"""Lead Prioritizer v2 — orchestration core.

Implements AI-first HQ detection for a single lead:
  1. Build query via ``build_simple_hq_query()``
  2. Fetch Serper results via ``call_serper_for_hq()``
  3. Interpret HQ with Anthropic Haiku via ``interpret_hq_with_ai()``
  4. Map ``HQDetectionResult`` → ``LeadPrioritizationResult``
"""

from __future__ import annotations

from lead_output_schema import LeadInput, LeadPrioritizationResult, HQDetectionResult
from hq_simple_detector import build_simple_hq_query
from lead_hq_ai_interpreter import call_serper_for_hq, interpret_hq_with_ai
from lead_non_hq_enrichment import collect_non_hq_enrichment_evidence
from lead_non_hq_signal_extractor import (
    extract_non_hq_signals,
    summarize_non_hq_signals_for_result,
)
from lead_app_summary_builder import build_app_summary_fields
from lead_v2_scoring_adapter import score_lead_v2_result

_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"


def prioritize_single_lead(
    input_row: LeadInput,
    *,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    ai_model: str = _DEFAULT_AI_MODEL,
    default_input_country: str = "Italy",
    collect_non_hq_evidence: bool = False,
    extract_non_hq_signals_flag: bool = False,
    build_app_summary_fields_flag: bool = False,
    calculate_commercial_score_flag: bool = False,
) -> LeadPrioritizationResult:
    """Orchestrate HQ detection and scoring for a single lead.

    Requires ``serper_api_key`` and ``anthropic_api_key`` for live operation.
    In tests these can be mocked at the ``call_serper_for_hq`` /
    ``interpret_hq_with_ai`` level.

    ``default_input_country`` supplies the run-context local/entity country when
    ``input_row.input_country`` is blank/None; the effective country is what the
    interpreter compares against and what the result records.

    ``collect_non_hq_evidence`` (default ``False``) enables Step-2 non-HQ
    evidence collection.  It only fills ``evidence_items`` — no non-HQ scores are
    produced.  HQ detection always runs first and is unaffected by this flag.

    ``extract_non_hq_signals_flag`` (default ``False``) enables Step-3
    deterministic signal extraction from whatever evidence is present.  It never
    triggers a Serper call itself: if evidence was not collected, extraction runs
    over an empty list and yields empty signals.  Intermediate signal scores are
    filled; the final commercial score and ranking are NOT touched here.

    ``build_app_summary_fields_flag`` (default ``False``) enables Step-4
    deterministic app/evidence summary building from the signals and evidence
    already present.  It never collects evidence or extracts signals implicitly;
    it only fills ``evidence_summary_app`` / ``key_source_links_app`` /
    ``advanced_notes_app``.  Final scoring and ranking are unchanged.

    ``calculate_commercial_score_flag`` (default ``False``) enables Step-5
    commercial scoring for this single lead via
    ``commercial_fit_scoring.score_company`` (profile
    ``italy_register_icp_only``).  It maps the v2 HQ/non-HQ signals already on
    the result and fills only the scoring output fields; if no non-HQ signals
    were extracted, scoring still runs from the HQ signal plus zeros.  It never
    collects evidence, extracts signals, or builds summaries implicitly, and does
    NOT change batch ranking or legacy scoring behavior.
    """
    effective_country = (input_row.input_country or "").strip() or default_input_country

    domain_root, query = build_simple_hq_query(input_row.company_name, input_row.domain)

    serper_payload = call_serper_for_hq(
        domain_root=domain_root,
        query=query,
        serper_api_key=serper_api_key,
    )

    # Use the effective (defaulted) country for interpretation without mutating
    # the caller's input row.
    interp_input = LeadInput(
        company_name=input_row.company_name,
        domain=input_row.domain,
        input_country=effective_country,
    )

    hq: HQDetectionResult = interpret_hq_with_ai(
        lead_input=interp_input,
        domain_root=domain_root,
        query=query,
        serper_payload=serper_payload,
        anthropic_api_key=anthropic_api_key,
        model=ai_model,
    )

    # ── Step 2: non-HQ evidence collection (evidence only, no scores) ─────────
    # Runs strictly after HQ detection and only when explicitly enabled.
    evidence_items = []
    if collect_non_hq_evidence:
        evidence_items = collect_non_hq_enrichment_evidence(
            company_name=input_row.company_name,
            domain=input_row.domain,
            serper_api_key=serper_api_key,
        )

    # ── Step 3: deterministic non-HQ signal extraction (no live calls) ────────
    # Extracts only from evidence already present — never triggers Serper here.
    signals = []
    if extract_non_hq_signals_flag:
        signals = extract_non_hq_signals(evidence_items)
    non_hq_summary = summarize_non_hq_signals_for_result(signals)

    # ── Step 4: deterministic app/evidence summary fields (no live calls) ─────
    # Built only from signals/evidence already present; never collects or
    # extracts implicitly.
    app_summary = {
        "evidence_summary_app": None,
        "key_source_links_app": None,
        "advanced_notes_app": None,
    }
    if build_app_summary_fields_flag:
        app_summary = build_app_summary_fields(signals, evidence_items)

    result = LeadPrioritizationResult(
        company_name=input_row.company_name,
        domain=input_row.domain,
        input_country=effective_country,
        # HQ location
        hq_detected_country=hq.hq_detected_country,
        hq_detected_city=hq.hq_detected_city,
        hq_confidence=hq.hq_confidence,
        foreign_hq_simple=hq.foreign_hq_simple,
        needs_manual_review=hq.needs_manual_review,
        hq_reason=hq.hq_reason,
        hq_evidence_url=hq.hq_evidence_url,
        hq_evidence_quote=hq.hq_evidence_quote,
        hq_structure_type=hq.hq_structure_type,
        # Scoring
        sig_foreign_hq_score_for_next_scoring=hq.sig_foreign_hq_score_for_next_scoring,
        # Competitor evidence is audit-only and never enters HQ scoring.
        competitor_signal_excluded_from_next_scoring=(
            "Competitor evidence is audit-only and excluded from HQ scoring."
        ),
        # Query / parser provenance
        domain_root=hq.domain_root or domain_root,
        query_used=hq.query_used or query,
        parser_source=hq.parser_source,
        # AI audit
        ai_hq_model=hq.ai_hq_model,
        ai_hq_classification=hq.ai_hq_classification,
        ai_hq_confidence=hq.ai_hq_confidence,
        ai_parent_company=hq.ai_parent_company,
        ai_parent_hq_country=hq.ai_parent_hq_country,
        ai_parent_hq_city=hq.ai_parent_hq_city,
        ai_call_attempted=hq.ai_call_attempted,
        ai_call_success=hq.ai_call_success,
        ai_hq_error=hq.ai_hq_error,
        ai_hq_raw_json=hq.ai_hq_raw_json,
        # ── Non-HQ signal scores (Step 3 — intermediate, not final fit score) ──
        # Filled from deterministic extraction when the flag is on; otherwise the
        # summary is all-None, matching the previous placeholder behavior.
        sig_international_profile_score=non_hq_summary["sig_international_profile_score"],
        sig_onboarding_training_need_score=non_hq_summary["sig_onboarding_training_need_score"],
        sig_company_size_complexity_score=non_hq_summary["sig_company_size_complexity_score"],
        sig_icp_keyword_match_score=non_hq_summary["sig_icp_keyword_match_score"],
        international_profile_reason=non_hq_summary["international_profile_reason"],
        onboarding_training_need_reason=non_hq_summary["onboarding_training_need_reason"],
        company_size_complexity_reason=non_hq_summary["company_size_complexity_reason"],
        icp_keyword_match_reason=non_hq_summary["icp_keyword_match_reason"],
        international_profile_evidence_url=non_hq_summary["international_profile_evidence_url"],
        onboarding_training_need_evidence_url=non_hq_summary["onboarding_training_need_evidence_url"],
        company_size_complexity_evidence_url=non_hq_summary["company_size_complexity_evidence_url"],
        icp_keyword_match_evidence_url=non_hq_summary["icp_keyword_match_evidence_url"],
        international_profile_evidence_quote=non_hq_summary["international_profile_evidence_quote"],
        onboarding_training_need_evidence_quote=non_hq_summary["onboarding_training_need_evidence_quote"],
        company_size_complexity_evidence_quote=non_hq_summary["company_size_complexity_evidence_quote"],
        icp_keyword_match_evidence_quote=non_hq_summary["icp_keyword_match_evidence_quote"],
        evidence_summary_app=app_summary["evidence_summary_app"],
        key_source_links_app=app_summary["key_source_links_app"],
        advanced_notes_app=app_summary["advanced_notes_app"],
        evidence_items=evidence_items,
        signals=signals,
    )

    # ── Step 5: commercial scoring (opt-in, single-lead flow only) ────────────
    # Maps the v2 signals already on `result` into commercial_fit_scoring and
    # fills only the scoring output fields. Runs from HQ + zeros for missing
    # non-HQ signals. Does not change batch ranking or legacy scoring.
    if calculate_commercial_score_flag:
        score_out = score_lead_v2_result(result, scoring_profile="italy_register_icp_only")
        result.final_commercial_fit_score = score_out.get("final_commercial_fit_score")
        result.commercial_tier = score_out.get("commercial_tier")
        result.icp_similarity_score = score_out.get("icp_similarity_score")
        result.lean_model_prob = score_out.get("lean_model_prob")
        result.lr_z_score = score_out.get("lr_z_score")
        result.scoring_profile = score_out.get("scoring_profile") or score_out.get("v2_scoring_profile_used")
        result.scoring_notes = score_out.get("scoring_notes")
        result.missing_scoring_fields = score_out.get("missing_scoring_fields")
        result.top_score_drivers = score_out.get("top_score_drivers")
        result.weak_score_drivers = score_out.get("weak_score_drivers")
        result.v2_score_input_mapping_note = score_out.get("v2_score_input_mapping_note")
        result.score_input_foreign_hq = score_out.get("score_input_foreign_hq")
        result.score_input_intl_footprint = score_out.get("score_input_intl_footprint")
        result.score_input_explicit_lnd = score_out.get("score_input_explicit_lnd")
        result.score_input_lnd_onboarding = score_out.get("score_input_lnd_onboarding")
        result.score_input_rapid_growth = score_out.get("score_input_rapid_growth")

    return result
