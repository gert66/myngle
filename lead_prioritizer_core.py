"""Lead Prioritizer v2 — orchestration core.

Implements AI-first HQ detection for a single lead:
  1. Build query via ``build_simple_hq_query()``
  2. Fetch Serper results via ``call_serper_for_hq()``
  3. Interpret HQ with Anthropic Haiku via ``interpret_hq_with_ai()``
  4. Map ``HQDetectionResult`` → ``LeadPrioritizationResult``
"""

from __future__ import annotations

import json

from lead_output_schema import LeadInput, LeadPrioritizationResult, HQDetectionResult
from hq_simple_detector import build_simple_hq_query, is_hosted_careers_platform_domain
from lead_hq_ai_interpreter import call_serper_for_hq, interpret_hq_with_ai
from lead_non_hq_enrichment import collect_non_hq_enrichment_evidence
from lead_non_hq_signal_extractor import (
    extract_non_hq_signals,
    extract_sector_industry,
    summarize_non_hq_signals_for_result,
)
from lead_app_summary_builder import build_app_summary_fields
from lead_v2_scoring_adapter import score_lead_v2_result
from lead_caller_app_fields_builder import build_caller_app_fields
from lead_caller_content_composer import (
    DRIVER_SIGNAL_NAMES,
    build_curated_signals_from_result,
    compose_caller_content,
)
from lead_icp_context_composer import (
    collect_icp_context_evidence,
    compose_icp_context as run_icp_context_composition,
)

_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"


def _quality_flags_for_result(result: LeadPrioritizationResult) -> list[str]:
    """Short list of already-known caveats to pass as prompt context — never
    used to invent facts, only so the composed text does not contradict them."""
    flags = []
    if result.needs_manual_review:
        flags.append("needs_manual_review")
    if result.hq_evidence_domain_mismatch_warning == "Yes":
        flags.append("hq_evidence_domain_mismatch_warning")
    if result.hq_positive_score_suppressed_for_review == "Yes":
        flags.append("hq_positive_score_suppressed_for_review")
    if result.domain_is_hosted_platform:
        flags.append("domain_is_hosted_platform")
    return flags


def prioritize_single_lead(
    input_row: LeadInput,
    *,
    serper_api_key: str = "",
    anthropic_api_key: str = "",
    ai_model: str = _DEFAULT_AI_MODEL,
    ai_provider: str = "anthropic",
    openai_api_key: str = "",
    deepseek_api_key: str = "",
    default_input_country: str = "Italy",
    collect_non_hq_evidence: bool = False,
    extract_non_hq_signals_flag: bool = False,
    build_app_summary_fields_flag: bool = False,
    calculate_commercial_score_flag: bool = False,
    build_caller_app_fields_flag: bool = False,
    compose_caller_content_flag: bool = False,
    compose_icp_context: bool = False,
    run_full_v2_pipeline: bool = False,
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

    ``build_caller_app_fields_flag`` (default ``False``) enables Step-6
    deterministic caller/app field generation from whatever is already on the
    result (HQ, non-HQ signals, evidence, optional score).  It only fills the
    app-facing fields; it never collects evidence, extracts signals, builds
    summaries, or scores implicitly.

    ``compose_caller_content_flag`` (default ``False``) enables the Step-3 AI
    caller-content composition step: it calls the Anthropic Messages API with
    the curated (already quality-checked) non-HQ signals and the HQ/parent
    conclusion already on the result, and — on success — fills the
    ``composed_*`` fields (``composed_why_relevant``, ``composed_what_is_hot``,
    ``composed_cold_caller_summary``, ``composed_caller_angle``,
    ``composed_call_starter``, ``composed_driver_evidence_json``).  It never
    collects evidence, extracts signals, or scores implicitly; it only reads
    what Steps 2–6 already produced.  On any failure (no Anthropic key, call
    error, unparseable response) it leaves the ``composed_*`` fields blank and
    records why in ``composed_content_note`` — the exporter then falls back to
    its existing deterministic templates, exactly as if this flag were off.
    Deliberately **not** part of the ``run_full_v2_pipeline`` preset: it must
    be turned on explicitly.

    ``compose_icp_context`` (default ``False``) enables an explicit opt-in AI
    ICP-context composition step, fully **independent** of
    ``compose_caller_content_flag`` — either can be on without the other. It
    runs its own broader thematic Serper queries
    (``lead_icp_context_composer.collect_icp_context_evidence``) plus the
    curated non-HQ signals already on the result, and — on success — fills
    ``icp_buying_signals``, ``icp_likely_training_interest``,
    ``icp_potential_buyer_function``. That evidence is collected into a
    throwaway list, never written to ``evidence_items`` and never read by
    signal extraction or scoring — this step can never affect
    ``final_commercial_fit_score`` or any ``sig_*``/``signals`` field. On any
    failure (no Anthropic key, call error, unparseable response) it leaves
    the ``icp_*`` fields blank and records why in
    ``icp_context_content_note``. Deliberately **not** part of the
    ``run_full_v2_pipeline`` preset: it must be turned on explicitly.

    ``run_full_v2_pipeline`` (default ``False``) is an explicit opt-in preset
    that turns on all optional v2 steps (2–6) for a single-lead end-to-end run.
    It does not add batch processing, change legacy ranking, or alter the
    canonical HQ-first order.  It deliberately does NOT enable
    ``compose_caller_content_flag`` or ``compose_icp_context`` — those stay
    explicit, separate opt-ins.
    ``v2_pipeline_mode`` on the result records which mode ran: ``"hq_only"``,
    ``"partial_v2"``, or ``"full_v2_single_lead"``.
    """
    # Full-pipeline preset: enable every optional v2 step explicitly (order of
    # operations below is unchanged — HQ first, then 2→6). Deliberately does
    # NOT include compose_caller_content_flag / compose_icp_context — those
    # stay separate opt-ins.
    if run_full_v2_pipeline:
        collect_non_hq_evidence = True
        extract_non_hq_signals_flag = True
        build_app_summary_fields_flag = True
        calculate_commercial_score_flag = True
        build_caller_app_fields_flag = True

    if run_full_v2_pipeline:
        v2_pipeline_mode = "full_v2_single_lead"
    elif any((
        collect_non_hq_evidence, extract_non_hq_signals_flag,
        build_app_summary_fields_flag, calculate_commercial_score_flag,
        build_caller_app_fields_flag, compose_caller_content_flag,
        compose_icp_context,
    )):
        v2_pipeline_mode = "partial_v2"
    else:
        v2_pipeline_mode = "hq_only"

    effective_country = (input_row.input_country or "").strip() or default_input_country

    domain_root, query = build_simple_hq_query(input_row.company_name, input_row.domain)
    domain_is_hosted_platform = is_hosted_careers_platform_domain(input_row.domain)

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
        ai_provider=ai_provider,
        openai_api_key=openai_api_key,
        deepseek_api_key=deepseek_api_key,
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

    # Sector/industry metadata from sector_industry evidence (deterministic,
    # audit/app only — feeds no score, C4, C5, HQ, or foreign-HQ filtering).
    sector_summary = extract_sector_industry(evidence_items)

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
        hq_evidence_urls=hq.hq_evidence_urls,
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
        domain_is_hosted_platform=domain_is_hosted_platform,
        # C4 positive-score safety audit
        hq_query_risk_flag=hq.hq_query_risk_flag,
        hq_evidence_domain_match=hq.hq_evidence_domain_match,
        hq_evidence_domain_mismatch_warning=hq.hq_evidence_domain_mismatch_warning,
        hq_positive_score_suppressed_for_review=hq.hq_positive_score_suppressed_for_review,
        hq_review_reason=hq.hq_review_reason,
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
        # Provider/usage audit (in-memory only — not exported)
        ai_hq_provider=hq.ai_hq_provider,
        ai_hq_input_tokens=hq.ai_hq_input_tokens,
        ai_hq_output_tokens=hq.ai_hq_output_tokens,
        ai_hq_total_tokens=hq.ai_hq_total_tokens,
        ai_hq_estimated_cost_usd=hq.ai_hq_estimated_cost_usd,
        # ── Non-HQ signal scores (Step 3 — intermediate, not final fit score) ──
        # Filled from deterministic extraction when the flag is on; otherwise the
        # summary is all-None, matching the previous placeholder behavior.
        sig_international_profile_score=non_hq_summary["sig_international_profile_score"],
        sig_onboarding_training_need_score=non_hq_summary["sig_onboarding_training_need_score"],
        sig_company_size_complexity_score=non_hq_summary["sig_company_size_complexity_score"],
        sig_icp_keyword_match_score=non_hq_summary["sig_icp_keyword_match_score"],
        sig_employer_branding_score=non_hq_summary["sig_employer_branding_score"],
        international_profile_reason=non_hq_summary["international_profile_reason"],
        onboarding_training_need_reason=non_hq_summary["onboarding_training_need_reason"],
        company_size_complexity_reason=non_hq_summary["company_size_complexity_reason"],
        icp_keyword_match_reason=non_hq_summary["icp_keyword_match_reason"],
        employer_branding_reason=non_hq_summary["employer_branding_reason"],
        international_profile_evidence_url=non_hq_summary["international_profile_evidence_url"],
        onboarding_training_need_evidence_url=non_hq_summary["onboarding_training_need_evidence_url"],
        company_size_complexity_evidence_url=non_hq_summary["company_size_complexity_evidence_url"],
        icp_keyword_match_evidence_url=non_hq_summary["icp_keyword_match_evidence_url"],
        employer_branding_evidence_url=non_hq_summary["employer_branding_evidence_url"],
        international_profile_evidence_urls=non_hq_summary["international_profile_evidence_urls"],
        onboarding_training_need_evidence_urls=non_hq_summary["onboarding_training_need_evidence_urls"],
        company_size_complexity_evidence_urls=non_hq_summary["company_size_complexity_evidence_urls"],
        icp_keyword_match_evidence_urls=non_hq_summary["icp_keyword_match_evidence_urls"],
        employer_branding_evidence_urls=non_hq_summary["employer_branding_evidence_urls"],
        international_profile_evidence_quote=non_hq_summary["international_profile_evidence_quote"],
        onboarding_training_need_evidence_quote=non_hq_summary["onboarding_training_need_evidence_quote"],
        company_size_complexity_evidence_quote=non_hq_summary["company_size_complexity_evidence_quote"],
        icp_keyword_match_evidence_quote=non_hq_summary["icp_keyword_match_evidence_quote"],
        employer_branding_evidence_quote=non_hq_summary["employer_branding_evidence_quote"],
        # Sector / industry metadata (audit & app only — never scoring)
        detected_industry=sector_summary["detected_industry"],
        detected_sub_industry=sector_summary["detected_sub_industry"],
        detected_company_type=sector_summary["detected_company_type"],
        sector_confidence=sector_summary["sector_confidence"],
        sector_reason=sector_summary["sector_reason"],
        sector_evidence_url=sector_summary["sector_evidence_url"],
        sector_evidence_quote=sector_summary["sector_evidence_quote"],
        sector_source_title=sector_summary["sector_source_title"],
        evidence_summary_app=app_summary["evidence_summary_app"],
        key_source_links_app=app_summary["key_source_links_app"],
        advanced_notes_app=app_summary["advanced_notes_app"],
        evidence_items=evidence_items,
        signals=signals,
        v2_pipeline_mode=v2_pipeline_mode,
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

    # ── Step 6: deterministic caller/app fields (uses only existing result) ───
    # Builds the Lovable/Company Hub app payload from fields already present;
    # never collects, extracts, summarizes, or scores implicitly.
    if build_caller_app_fields_flag:
        caller_fields = build_caller_app_fields(result)
        result.commercial_fit_score_app = caller_fields["commercial_fit_score_app"]
        result.commercial_tier_app = caller_fields["commercial_tier_app"]
        result.what_is_hot_app = caller_fields["what_is_hot_app"]
        result.what_is_not_app = caller_fields["what_is_not_app"]
        result.why_relevant_app = caller_fields["why_relevant_app"]
        result.caller_angle_app = caller_fields["caller_angle_app"]
        result.call_starter_app = caller_fields["call_starter_app"]
        result.caution_app = caller_fields["caution_app"]
        result.foreign_hq_signal_used_in_app = caller_fields["foreign_hq_signal_used_in_app"]
        result.foreign_hq_country_app = caller_fields["foreign_hq_country_app"]
        result.foreign_hq_city_app = caller_fields["foreign_hq_city_app"]
        result.cold_caller_summary_app = caller_fields["cold_caller_summary_app"]
        result.parent_hq_summary_app = caller_fields["parent_hq_summary_app"]

    # ── Step 3 (opt-in): AI-composed caller content ───────────────────────────
    # Explicit opt-in only — never enabled by run_full_v2_pipeline. Falls back
    # silently to the deterministic *_app templates above on any failure (no
    # key, call error, unparseable response); composed_content_note records
    # why for audit purposes.
    if compose_caller_content_flag:
        composed = compose_caller_content(
            company_name=result.company_name,
            country=result.input_country,
            industry=result.detected_industry,
            foreign_hq_detected=bool(
                result.sig_foreign_hq_score_for_next_scoring
                and result.sig_foreign_hq_score_for_next_scoring > 0
            ),
            parent_company=result.ai_parent_company,
            parent_hq_country=result.ai_parent_hq_country,
            parent_hq_city=result.ai_parent_hq_city,
            hq_adjudication=result.hq_structure_type,
            curated_signals=build_curated_signals_from_result(result),
            driver_ids=list(DRIVER_SIGNAL_NAMES),
            quality_flags=_quality_flags_for_result(result),
            anthropic_api_key=anthropic_api_key,
            model=ai_model,
        )
        if composed.call_success:
            result.composed_why_relevant = composed.why_relevant
            result.composed_what_is_hot = (
                "\n".join(composed.what_is_hot) if composed.what_is_hot else None
            )
            result.composed_cold_caller_summary = composed.cold_caller_summary
            result.composed_caller_angle = composed.caller_angle
            result.composed_call_starter = composed.call_starter
            result.composed_driver_evidence_json = (
                json.dumps(composed.driver_evidence) if composed.driver_evidence else None
            )
            result.composed_by_ai = True
            result.composed_content_note = "AI-composed caller content used."
        else:
            result.composed_by_ai = False
            result.composed_content_note = (
                f"AI composition unavailable ({composed.error}); "
                "fell back to deterministic templates."
            )

    # ── Rich ICP context (opt-in): AI-composed ICP buying-context fields ──────
    # Explicit opt-in only, INDEPENDENT of compose_caller_content_flag — never
    # enabled by run_full_v2_pipeline. Its own broader Serper queries
    # (collect_icp_context_evidence) are collected into a throwaway list, never
    # written to evidence_items, and never read by signal extraction or
    # scoring; icp_context_content_note records why for audit purposes.
    if compose_icp_context:
        icp_extra_evidence = collect_icp_context_evidence(
            company_name=input_row.company_name,
            domain=input_row.domain,
            serper_api_key=serper_api_key,
        )
        icp_composed = run_icp_context_composition(
            company_name=result.company_name,
            country=result.input_country,
            curated_signals=build_curated_signals_from_result(result),
            extra_evidence=icp_extra_evidence,
            anthropic_api_key=anthropic_api_key,
            ai_model=ai_model,
        )
        if icp_composed.call_success:
            result.icp_buying_signals = icp_composed.buying_signals
            result.icp_likely_training_interest = icp_composed.likely_training_interest
            result.icp_potential_buyer_function = icp_composed.potential_buyer_function
            result.icp_context_by_ai = True
            result.icp_context_content_note = "AI-composed ICP context used."
        else:
            result.icp_context_by_ai = False
            result.icp_context_content_note = (
                f"AI ICP context unavailable ({icp_composed.error})."
            )

    return result
