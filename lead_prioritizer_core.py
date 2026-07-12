"""Lead Prioritizer v2 — orchestration core.

Implements AI-first HQ detection for a single lead:
  1. Build query via ``build_simple_hq_query()``
  2. Fetch Serper results via ``call_serper_for_hq()``
  3. Interpret HQ with Anthropic Haiku via ``interpret_hq_with_ai()``
  4. Map ``HQDetectionResult`` → ``LeadPrioritizationResult``
"""

from __future__ import annotations

import json
from typing import Optional

from lead_output_schema import LeadInput, LeadPrioritizationResult, HQDetectionResult
from hq_simple_detector import build_simple_hq_query, is_hosted_careers_platform_domain
from lead_country_config import gl_hl_for_hq_country, std_country
from lead_hq_ai_interpreter import call_serper_for_hq, interpret_hq_with_ai
from lead_hq_firecrawl_source import collect_own_domain_hq_pages
from lead_hq_location_summary import build_hq_location_summary
from lead_non_hq_enrichment import collect_non_hq_enrichment_evidence
from lead_non_hq_signal_extractor import (
    extract_non_hq_signals,
    extract_sector_industry,
    summarize_non_hq_signals_for_result,
)
from lead_lusha_sector_mapping import sector_from_lusha_industry, sector_from_lusha_text
from lead_lusha_size_signal import lusha_size_signal
from lead_ai_signal_scorer import score_signals_with_ai
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
from lead_legacy_enrichment import run_legacy_enrichment
from lead_public_source_signal_enrichment import collect_public_source_signal_evidence

_DEFAULT_AI_MODEL = "claude-haiku-4-5-20251001"

#: commercial_fit_scoring profile is per-country: "italy_register_icp_only"
#: was calibrated specifically for the Italian company-register source (size
#: excluded because that input list is pre-filtered for 100+ employees, and
#: a flat sigmoid_k=1 fitted to Italy's own probability distribution). Every
#: other country must use the "default" profile (size included, sigmoid_k=10)
#: -- see commercial_fit_scoring.SCORING_PROFILES.
_ITALY_ONLY_SCORING_PROFILE = "italy_register_icp_only"
_DEFAULT_SCORING_PROFILE = "default"


def _scoring_profile_for_country(country: "str | None") -> str:
    """Return the commercial_fit_scoring profile for the lead's effective
    input country: Italy keeps its register-calibrated profile, every other
    country gets the default (size-inclusive, sigmoid_k=10) profile."""
    if std_country(country or "") == "Italy":
        return _ITALY_ONLY_SCORING_PROFILE
    return _DEFAULT_SCORING_PROFILE


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
    firecrawl_api_key: str = "",
    ai_model: str = _DEFAULT_AI_MODEL,
    ai_provider: str = "anthropic",
    openai_api_key: str = "",
    deepseek_api_key: str = "",
    default_input_country: str = "Italy",
    collect_non_hq_evidence: bool = False,
    extract_non_hq_signals_flag: bool = False,
    ai_signal_scoring: bool = False,
    build_app_summary_fields_flag: bool = False,
    calculate_commercial_score_flag: bool = False,
    build_caller_app_fields_flag: bool = False,
    compose_caller_content_flag: bool = False,
    compose_icp_context: bool = False,
    legacy_enrichment_mode: bool = False,
    public_source_signal_enrichment: bool = False,
    public_source_signal_query: str = "vacancies",
    public_source_base_url: str = "",
    public_source_label: str = "",
    public_source_max_pages: int = 3,
    cache_index: Optional[dict] = None,
    force_refresh: bool = False,
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

    ``ai_signal_scoring`` (default ``False``) is a separate, explicit opt-in
    that, when combined with ``extract_non_hq_signals_flag=True``, replaces the
    deterministic Step-3 verdicts with one Anthropic call judging the same
    (guard-filtered) evidence semantically instead of by keyword count. This is
    the ONE part of Onderdeel 2 that can change ``final_commercial_fit_score``
    versus the default — the score mapping itself is unchanged, only its
    signal input is. Any AI call/parse failure falls back to the deterministic
    extractor. ``result.signal_scoring_mode`` records which path actually ran
    (``"deterministic"`` or ``"ai"``) so datasets are never silently mixed.

    ``build_app_summary_fields_flag`` (default ``False``) enables Step-4
    deterministic app/evidence summary building from the signals and evidence
    already present.  It never collects evidence or extracts signals implicitly;
    it only fills ``evidence_summary_app`` / ``key_source_links_app`` /
    ``advanced_notes_app``.  Final scoring and ranking are unchanged.

    ``calculate_commercial_score_flag`` (default ``False``) enables Step-5
    commercial scoring for this single lead via
    ``commercial_fit_scoring.score_company``. The profile is chosen per the
    lead's effective ``input_country`` (see ``_scoring_profile_for_country``):
    ``italy_register_icp_only`` for Italy (register-calibrated: company size
    excluded, flat sigmoid), ``default`` (size-inclusive, sigmoid_k=10) for
    every other country. It maps the v2 HQ/non-HQ signals already on
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

    ``legacy_enrichment_mode`` (default ``False``) runs a separate, parallel
    scoring path (``lead_legacy_enrichment.run_legacy_enrichment``) that
    reproduces the old ``enrich_clients_claude.py`` Step-2 Serper+Claude
    evaluation style (same holistic 9-signal judgment, minus the competitor
    signal and its query; no Jina full-page scraping) purely so the two
    systems can be compared on the same lead. It runs NEXT TO the normal v2
    flow — ``final_commercial_fit_score`` and ``signals`` are completely
    untouched either way. On success it fills ``legacy_score`` (a numeric
    High/Medium/Low → 9.0/6.0/3.0 mapping), ``legacy_tier`` (the raw
    High/Medium/Low label, deliberately NOT renamed to the v2 A/B/C/D tier
    scale), and the ``legacy_icp_*`` fields. On any failure it leaves those
    fields blank and records why in ``legacy_enrichment_error``. Deliberately
    **not** part of the ``run_full_v2_pipeline`` preset: it must be turned on
    explicitly.

    ``public_source_signal_enrichment`` (default ``False``) is an explicit
    opt-in, evidence-only step: it retrieves public company-level evidence
    for ``public_source_signal_query`` (default ``"vacancies"``) from a
    single user-configured public source (``public_source_base_url``) via
    Firecrawl (``lead_public_source_signal_enrichment.
    collect_public_source_signal_evidence``), and appends the result to
    ``evidence_items``. Runs strictly AFTER regular non-HQ evidence collection
    and BEFORE signal extraction, so the existing deterministic extractor and
    app-summary logic see it naturally. It can never directly move
    ``final_commercial_fit_score`` or create a score: its
    ``signal_name`` (``"public_source_signal"``) is deliberately not one of
    the five scored non-HQ signal names, so ``extract_non_hq_signals`` and
    ``extract_sector_industry`` both ignore it. A missing ``firecrawl_api_key``
    or ``public_source_base_url`` yields no evidence, never an error.
    Deliberately **not** part of the ``run_full_v2_pipeline`` preset: it must
    be turned on explicitly.

    ``cache_index`` (default ``None``) is an optional in-memory, GCS-backed
    shared enrichment cache (see ``enrichment_cache.py``) for ONE country —
    the caller (batch orchestration) downloads it once at the start of a run
    and is responsible for persisting it back to GCS afterward. When
    ``None`` — the default — every Serper/Firecrawl call this function makes
    (HQ, own-domain Firecrawl crawl, non-HQ evidence collection, Public
    Source Signal Enrichment) hits the network live, exactly as before this
    parameter existed. ``force_refresh`` (default ``False``) bypasses any
    cached entry for this lead's calls without needing to clear the whole
    index. Rich ICP context's own Serper queries are NOT wired into this
    cache in this iteration (out of scope; see enrichment-cache delivery
    notes) — only the four call families listed above are.

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
        compose_icp_context, legacy_enrichment_mode,
        public_source_signal_enrichment,
    )):
        v2_pipeline_mode = "partial_v2"
    else:
        v2_pipeline_mode = "hq_only"

    effective_country = (input_row.input_country or "").strip() or default_input_country

    domain_root, query = build_simple_hq_query(input_row.company_name, input_row.domain)
    domain_is_hosted_platform = is_hosted_careers_platform_domain(input_row.domain)

    # gl always set when the effective country is recognised; hl additionally
    # omitted for known multilingual countries (e.g. Switzerland) rather than
    # guessing a language. Unrecognised/blank country -> (None, None), so the
    # request is byte-identical to before gl/hl existed.
    _hq_gl, _hq_hl = gl_hl_for_hq_country(effective_country)
    serper_payload = call_serper_for_hq(
        domain_root=domain_root,
        query=query,
        serper_api_key=serper_api_key,
        gl=_hq_gl,
        hl=_hq_hl,
        cache_index=cache_index,
        force_refresh=force_refresh,
    )

    # ── PRIMARY HQ source: crawl the company's own domain via Firecrawl ────────
    # The single Serper query above is non-deterministic run-to-run, which used
    # to flip the HQ classification of the exact same company (see
    # HQ_FIRECRAWL_STABILITY_NOTES.md). The company's own website content is
    # stable, so we feed it to the classifier as the first, most-trusted source
    # with the Serper snippets as secondary corroboration. A missing Firecrawl
    # key, a hosted-platform "domain" (not the company's own site), or any hard
    # Firecrawl failure falls back cleanly to exactly today's Serper-only
    # behavior — never an error, mirroring the Deep Dive precedent.
    crawled_pages: list = []
    if firecrawl_api_key and input_row.domain and not domain_is_hosted_platform:
        fc = collect_own_domain_hq_pages(
            input_row.domain, firecrawl_api_key,
            country=effective_country,
            cache_index=cache_index, force_refresh=force_refresh,
        )
        if fc["used"]:
            crawled_pages = fc["pages"]

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
        crawled_pages=crawled_pages,
        lusha_description=input_row.lusha_description,
        lusha_specialties=input_row.lusha_specialties,
    )

    # ── Step 2: non-HQ evidence collection (evidence only, no scores) ─────────
    # Runs strictly after HQ detection and only when explicitly enabled.
    evidence_items = []
    if collect_non_hq_evidence:
        evidence_items = collect_non_hq_enrichment_evidence(
            company_name=input_row.company_name,
            domain=input_row.domain,
            serper_api_key=serper_api_key,
            country=effective_country,
            cache_index=cache_index,
            force_refresh=force_refresh,
        )

    # ── Public Source Signal Enrichment (opt-in, evidence-only) ───────────────
    # Adds LeadEvidence items from a single user-configured public source for
    # a user-configured signal query, via Firecrawl. Off by default. Runs
    # strictly AFTER regular non-HQ evidence collection and BEFORE signal
    # extraction, so the existing deterministic extractor / app-summary logic
    # picks it up naturally. Its signal_name ("public_source_signal") is not
    # one of the five scored non-HQ signal names, so extract_non_hq_signals /
    # extract_sector_industry both ignore it — this can never directly move
    # final_commercial_fit_score or create a score.
    if public_source_signal_enrichment:
        evidence_items.extend(collect_public_source_signal_evidence(
            company_name=input_row.company_name,
            domain=input_row.domain,
            signal_query=public_source_signal_query,
            source_base_url=public_source_base_url,
            firecrawl_api_key=firecrawl_api_key,
            source_label=public_source_label,
            max_pages=public_source_max_pages,
            cache_index=cache_index,
            force_refresh=force_refresh,
        ))

    # ── Step 3: non-HQ signal extraction (no live Serper calls) ───────────────
    # Deterministic by default. ``ai_signal_scoring`` is a separate, explicit
    # opt-in that replaces the deterministic verdicts with one Anthropic call
    # judging the same (already guard-filtered) evidence -- the score mapping
    # further downstream (lead_v2_scoring_adapter.py / commercial_fit_scoring.py)
    # is completely unchanged either way. Any AI failure falls back to the
    # deterministic extractor so a row is never left without signals.
    signals = []
    signal_scoring_mode = "deterministic"
    # Usage/cost audit for the AI signal-scoring call -- populated only when
    # ai_signal_scoring was actually attempted, whether or not it succeeded
    # (tokens are spent either way). Blank fields when never attempted.
    non_hq_ai_audit: dict = {}
    if extract_non_hq_signals_flag:
        if ai_signal_scoring:
            ai_scoring_result = score_signals_with_ai(
                company_name=input_row.company_name,
                country=effective_country,
                evidence_items=evidence_items,
                anthropic_api_key=anthropic_api_key,
                ai_model=ai_model,
            )
            if ai_scoring_result.call_attempted:
                non_hq_ai_audit = dict(
                    non_hq_ai_model=ai_scoring_result.model,
                    non_hq_ai_input_tokens=ai_scoring_result.input_tokens,
                    non_hq_ai_output_tokens=ai_scoring_result.output_tokens,
                    non_hq_ai_total_tokens=ai_scoring_result.total_tokens,
                    non_hq_ai_estimated_cost_usd=ai_scoring_result.estimated_cost_usd,
                )
            if ai_scoring_result.call_success:
                signals = ai_scoring_result.signals
                signal_scoring_mode = "ai"
            else:
                signals = extract_non_hq_signals(evidence_items, company_domain=input_row.domain)
        else:
            signals = extract_non_hq_signals(evidence_items, company_domain=input_row.domain)

        # ── company_size_complexity: Lusha employee/revenue data is the
        # ONLY source for this signal (Lusha enrichment plan, Stap 4 —
        # supersedes the earlier Stap-3 "Serper stays a permanent
        # fallback" design). There is no live Serper query for this
        # signal anymore (removed from build_non_hq_enrichment_queries),
        # so ``company_size_complexity_source`` is either ``"lusha"`` (a
        # usable Lusha value was found) or ``None`` (missing/unparseable
        # Lusha data — the score/reason/evidence fields simply stay
        # ``None``, exactly as the schema always allowed).
        company_size_complexity_source = None
        _lusha_size = lusha_size_signal(input_row.lusha_employees, input_row.lusha_revenue)
        if _lusha_size is not None:
            signals = [s for s in signals if s.signal_name != "company_size_complexity"]
            signals.append(_lusha_size)
            company_size_complexity_source = "lusha"
    else:
        company_size_complexity_source = None
    non_hq_summary = summarize_non_hq_signals_for_result(signals)

    # Sector/industry metadata (deterministic, audit/app only — feeds no
    # score, C4, C5, HQ, or foreign-HQ filtering). Priority chain (Lusha
    # enrichment plan, Stap 2, revised Stap 4 — the live Serper
    # sector_industry query/evidence tier is gone; there is no Serper
    # fallback for sector anymore):
    #   1. Lusha Sub/Main Industry mapped onto our internal categories
    #      (free, no API call, highest priority when present).
    #   2. Own-domain Firecrawl+AI-derived industry (existing fallback).
    #   3. Lusha Company Description/Specialties keyword match (last resort,
    #      free, no API call, reuses extract_sector_industry's own matcher).
    #   4. Empty, exactly as before any of this existed.
    sector_summary = sector_from_lusha_industry(
        input_row.lusha_main_industry, input_row.lusha_sub_industry)

    if sector_summary is None:
        # No live Serper sector_industry query/evidence anymore (Stap 4) —
        # start from the empty shape and try the remaining free tiers.
        sector_summary = extract_sector_industry([])

        # Fallback: reuse the industry the HQ interpreter already derived
        # from the SAME material at no extra API cost — but ONLY when that
        # material genuinely included the company's own crawled-domain
        # content (`crawled_pages` non-empty here means Firecrawl actually
        # fetched the company's own site; see collect_own_domain_hq_pages
        # above). A Serper-only AI guess (no own-domain crawl) is
        # deliberately NOT used as a sector source — the point of this
        # fallback is to lean on the same primary, most-authoritative
        # source the HQ classification already trusts, not to add a
        # second, weaker AI guess on top of thin secondary snippets.
        if not sector_summary["detected_industry"] and hq.ai_hq_industry and crawled_pages:
            sector_summary = dict(sector_summary)
            sector_summary["detected_industry"] = hq.ai_hq_industry
            sector_summary["detected_sub_industry"] = hq.ai_hq_sub_industry or None
            sector_summary["sector_confidence"] = "Medium"
            sector_summary["sector_reason"] = (
                "Derived by AI from the company's own crawled website content "
                "during HQ interpretation (own-domain source); no sector keyword "
                "matched in the separate Serper sector-search evidence."
            )
            sector_summary["sector_evidence_url"] = crawled_pages[0].get("url")
            sector_summary["sector_source_title"] = "Company website (AI-derived)"
            sector_summary["sector_source"] = "own_domain_ai"

        # Last resort: the same keyword matcher applied to Lusha Company
        # Description/Specialties text — free, no API call. Never
        # overwrites a hit from any tier above.
        if not sector_summary["detected_industry"]:
            text_sector = sector_from_lusha_text(
                input_row.lusha_description, input_row.lusha_specialties)
            if text_sector["detected_industry"]:
                sector_summary = text_sector

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
        # Always-shown structured HQ location line (independent of the
        # foreign-ownership driver badge). Built here from the AI/detected HQ
        # fields; the batch C5 layer recomputes it once richer c5_parent_*
        # fields exist so C5 takes priority — see lead_hq_location_summary.py.
        hq_location_summary=build_hq_location_summary(
            foreign_hq_simple=hq.foreign_hq_simple,
            hq_structure_type=hq.hq_structure_type,
            ai_parent_hq_country=hq.ai_parent_hq_country,
            ai_parent_hq_city=hq.ai_parent_hq_city,
            hq_detected_country=hq.hq_detected_country,
            hq_detected_city=hq.hq_detected_city,
        ),
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
        ai_hq_industry=hq.ai_hq_industry,
        ai_hq_sub_industry=hq.ai_hq_sub_industry,
        # Provider/usage audit (in-memory only — not exported)
        ai_hq_provider=hq.ai_hq_provider,
        ai_hq_input_tokens=hq.ai_hq_input_tokens,
        ai_hq_output_tokens=hq.ai_hq_output_tokens,
        ai_hq_total_tokens=hq.ai_hq_total_tokens,
        ai_hq_estimated_cost_usd=hq.ai_hq_estimated_cost_usd,
        ai_hq_cache_creation_tokens=hq.ai_hq_cache_creation_tokens,
        ai_hq_cache_read_tokens=hq.ai_hq_cache_read_tokens,
        ai_hq_estimated_cost_usd_with_cache=hq.ai_hq_estimated_cost_usd_with_cache,
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
        signal_extractor_version=non_hq_summary["signal_extractor_version"],
        signal_scoring_mode=signal_scoring_mode,
        company_size_complexity_source=company_size_complexity_source,
        lusha_employees=input_row.lusha_employees,
        lusha_revenue=input_row.lusha_revenue,
        **non_hq_ai_audit,
        # Sector / industry metadata (audit & app only — never scoring)
        detected_industry=sector_summary["detected_industry"],
        detected_sub_industry=sector_summary["detected_sub_industry"],
        detected_company_type=sector_summary["detected_company_type"],
        sector_confidence=sector_summary["sector_confidence"],
        sector_reason=sector_summary["sector_reason"],
        sector_evidence_url=sector_summary["sector_evidence_url"],
        sector_evidence_quote=sector_summary["sector_evidence_quote"],
        sector_source_title=sector_summary["sector_source_title"],
        sector_source=sector_summary["sector_source"],
        lusha_main_industry=input_row.lusha_main_industry,
        lusha_sub_industry=input_row.lusha_sub_industry,
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
    # Profile is per-country (see _scoring_profile_for_country): only Italy
    # keeps the register-calibrated "italy_register_icp_only" profile; every
    # other country scores with "default" (size-inclusive, sigmoid_k=10).
    if calculate_commercial_score_flag:
        score_out = score_lead_v2_result(
            result, scoring_profile=_scoring_profile_for_country(result.input_country))
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

    # ── Legacy enrichment mode (opt-in): comparison-only parallel scoring ─────
    # Runs NEXT TO the normal v2 flow above, never replacing any of it.
    # final_commercial_fit_score / signals / evidence_items are untouched
    # regardless of what this produces.
    if legacy_enrichment_mode:
        legacy = run_legacy_enrichment(
            company_name=input_row.company_name,
            domain=input_row.domain,
            country=effective_country,
            serper_api_key=serper_api_key,
            anthropic_api_key=anthropic_api_key,
            ai_model=ai_model,
        )
        if legacy.call_success:
            result.legacy_score = legacy.legacy_score
            result.legacy_tier = legacy.legacy_tier
            result.legacy_icp_lead_score = legacy.icp_lead_score
            result.legacy_icp_buying_signals = legacy.icp_buying_signals
            result.legacy_icp_likely_training_interest = legacy.icp_likely_training_interest
            result.legacy_icp_potential_buyer_function = legacy.icp_potential_buyer_function
            result.legacy_icp_why_relevant = legacy.icp_why_relevant
            result.legacy_icp_evidence = legacy.icp_evidence
        else:
            result.legacy_enrichment_error = legacy.error

    return result
