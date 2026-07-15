"""Shared batch core for Lead Prioritizer v2.

Reusable, UI-agnostic batch engine that runs ``prioritize_single_lead`` over a
pandas DataFrame and returns DataFrames ready for an output workbook.  It is
imported by both the (future) Streamlit upload/download app and the (future)
CLI runner — so this module deliberately has:

- no Streamlit imports,
- no command-line parsing,
- no new enrichment logic (it only orchestrates ``prioritize_single_lead``).

Secret hygiene: API keys are passed straight through to the pipeline and are
never printed, never stored on the result, and never written to any returned
DataFrame.  Raw Serper payloads are never surfaced; raw AI JSON is excluded
unless ``include_raw_ai_json=True``.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional

import io
import re
import threading
import time

import pandas as pd

from lead_output_schema import LeadInput
from lead_prioritizer_core import prioritize_single_lead
from lead_hq_location_summary import build_hq_location_summary_from_row
from deep_dive_runner import run_deep_dive


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUPPORTED_RUN_MODES = (
    "full",
    "hq_only",
    "evidence_only",
    "signals_no_score",
    "full_no_score",
)

# "Full enrichment, confirmed foreign-HQ only" — a separate, opt-in batch mode
# handled by ``run_batch_foreign_hq_only`` (not ``run_batch_dataframe`` /
# ``resolve_pipeline_flags``), since it needs a two-phase per-row decision
# (HQ+C4+optional-C5 screening, then a conditional full-enrichment pass).
# Deliberately excluded from SUPPORTED_RUN_MODES / the CLI so existing run
# modes stay untouched; only the Streamlit app offers it today.
FOREIGN_HQ_ONLY_MODE = "full_foreign_hq_only"

# Progress-phase labels for the foreign-HQ-only mode. Every progress payload
# emitted by ``run_batch_foreign_hq_only`` carries ``phase`` / ``phase_count`` /
# ``phase_label`` / ``phase_processed`` / ``phase_total`` so a UI can render
# per-phase progress; phase 2 is skipped when C5 is disabled.
FOREIGN_HQ_ONLY_PHASE_LABELS = {
    1: "HQ screening",
    2: "C5 adjudication",
    3: "Full enrichment for confirmed foreign-HQ leads",
}

# "Full enrichment, confirmed non-English foreign-HQ only" — a country-agnostic
# companion to FOREIGN_HQ_ONLY_MODE. Same two-phase HQ+C4+optional-C5 screening
# (shared via ``_run_hq_and_c5_screening``), but the full-enrichment gate
# additionally requires: input_country present, foreign HQ confirmed, a parent
# HQ country present and different from input_country, and that parent country
# is in a non-English-speaking market. Works for any input_country (Australia,
# New Zealand, ...) — the language/market classification itself
# (``classify_parent_hq_language_market``) has never been country-specific;
# only the export-eligibility gate now is, and it is generic. Deliberately
# excluded from SUPPORTED_RUN_MODES / the CLI, same as FOREIGN_HQ_ONLY_MODE.
NON_ENGLISH_FOREIGN_HQ_ONLY_MODE = "full_non_english_foreign_hq_only"

NON_ENGLISH_FOREIGN_HQ_ONLY_PHASE_LABELS = {
    1: "HQ screening",
    2: "C5 adjudication",
    3: "Full enrichment for confirmed non-English foreign-HQ leads",
}

# Phase labels for the opt-in foreign-HQ gate INSIDE the regular
# run_batch_dataframe (BatchRunConfig.gate_full_enrichment_on_foreign_hq).
# Phase 2 never actually fires on this path (C5 is not wired into it yet —
# see _run_batch_dataframe_gated); kept as a 3-entry dict purely for a
# consistent shape with the other phase-based progress payloads above.
GATED_FULL_ENRICHMENT_PHASE_LABELS = {
    1: "HQ screening",
    2: "C5 adjudication (not used in this path yet)",
    3: "Full enrichment for confirmed foreign-HQ leads",
}


@dataclass
class BatchRunConfig:
    company_name_column: str
    domain_column: str
    input_country_column: Optional[str] = None
    default_input_country: str = "Italy"
    run_mode: str = "full"
    start_row: int = 0
    row_limit: int = 10
    continue_on_error: bool = True
    max_evidence_urls: int = 6
    include_raw_ai_json: bool = False
    # Experimental AI-provider selection (audit-safe; never carries API keys).
    # Defaults preserve the existing Anthropic behavior exactly: ai_model ""
    # means "use the pipeline's own default model for the provider".
    ai_provider: str = "anthropic"
    ai_model: str = ""
    # Step 3 — explicit opt-in AI caller-content composition. Off by default
    # and independent of run_mode; falls back silently to the deterministic
    # templates per-row on any failure (see lead_caller_content_composer.py).
    compose_caller_content: bool = False
    # Rich ICP context — explicit opt-in, off by default, and INDEPENDENT of
    # compose_caller_content above (either can be on without the other). See
    # lead_icp_context_composer.py; never affects evidence_items, signals, or
    # scoring.
    rich_icp_context: bool = False
    # Onderdeel 2 — opt-in AI signal scoring. Off by default and INDEPENDENT
    # of every flag above; unlike them, this ONE flag changes
    # final_commercial_fit_score (same formula/weights, AI-judged signal
    # input instead of keyword-counted). See lead_ai_signal_scorer.py.
    ai_signal_scoring: bool = False
    # Legacy enrichment mode — explicit opt-in comparison feature, off by
    # default and INDEPENDENT of every flag above. Reproduces the old
    # enrich_clients_claude.py Step-2 evaluation style side by side with the
    # v2 pipeline; never touches final_commercial_fit_score or signals. See
    # lead_legacy_enrichment.py.
    legacy_enrichment_mode: bool = False
    # Deep Dive (Step B) — explicit opt-in, off by default, and INDEPENDENT
    # of both flags above. Runs AFTER scoring, only for rows that clear the
    # trigger gate (score threshold and/or confirmed foreign HQ); the result
    # is a separate DeepDiveResult per row, never fed back into scoring. See
    # deep_dive_runner.py.
    deep_dive: bool = False
    deep_dive_min_score: float = 8.0
    deep_dive_on_foreign_hq: bool = True
    deep_dive_max_pages: int = 6
    # Mechanical quote verification is the core of Deep Dive's value (it is
    # what catches an AI hallucinated/paraphrased quote), so it defaults to
    # on; auto_correct_quotes only takes effect when verify_quotes is also
    # on. Both are independent of deep_dive_on_foreign_hq / min_score.
    verify_quotes: bool = True
    auto_correct_quotes: bool = True
    # Public Source Signal Enrichment — explicit opt-in, off by default, and
    # INDEPENDENT of every flag above. Adds LeadEvidence items retrieved via
    # Firecrawl from a single user-configured public source for a user-
    # configured signal query; never touches final_commercial_fit_score or
    # creates a score directly. See lead_public_source_signal_enrichment.py.
    public_source_signal_enrichment: bool = False
    public_source_signal_query: str = "vacancies"
    public_source_base_url: str = ""
    public_source_label: str = ""
    public_source_max_pages: int = 3
    # Opt-in foreign-HQ gating inside the REGULAR run_batch_dataframe path —
    # explicit opt-in, off by default, and INDEPENDENT of every flag above.
    # Reuses the exact two-phase pattern already proven in
    # run_batch_foreign_hq_only (cheap HQ-only screening for every row, full
    # v2 enrichment only for rows confirmed foreign-HQ; skipped rows are kept
    # with enrichment_skipped=True + foreign_hq_skip_reason) via the shared
    # _run_gated_full_enrichment helper, but without switching run_mode to a
    # separate dedicated mode. Default False means run_batch_dataframe's
    # existing behavior is completely unchanged for every current caller. C5
    # is not supported in this gated path yet (run_batch_dataframe has never
    # supported C5) — see _run_batch_dataframe_gated.
    gate_full_enrichment_on_foreign_hq: bool = False
    # Shared, GCS-backed Serper/Firecrawl cache — explicit opt-in, off by
    # default, and INDEPENDENT of every flag above. When enabled, a batch run
    # downloads one cache index per country present in the dataset ONCE at
    # the start (see enrichment_cache.py), consults it in-memory for every
    # Serper/Firecrawl call made by prioritize_single_lead (HQ, own-domain
    # Firecrawl crawl, non-HQ evidence collection, Public Source Signal
    # Enrichment), and uploads the updated index back at the end (and
    # periodically during a long run). Lets runs from different machines
    # share results and avoid redundant lookups/scrapes. Default False means
    # every existing caller's behavior — and its exact Serper/Firecrawl call
    # volume — is completely unchanged.
    use_enrichment_cache: bool = False
    enrichment_cache_bucket: str = ""


# ---------------------------------------------------------------------------
# Pipeline flag resolution
# ---------------------------------------------------------------------------

_ALL_FLAGS = (
    "collect_non_hq_evidence",
    "extract_non_hq_signals_flag",
    "build_app_summary_fields_flag",
    "calculate_commercial_score_flag",
    "build_caller_app_fields_flag",
    "compose_caller_content_flag",
    "compose_icp_context",
    "run_full_v2_pipeline",
)


# Alternate raw-domain columns to fall back to when ``config.domain_column``
# is blank for a specific row — not just when the column is entirely absent
# from the input. Guards against input-prep steps that populate a raw
# domain column (e.g. Lusha's "Company Domain") but never back-fill the
# normalized "domain" column for every row, which otherwise silently skips
# the own-domain Firecrawl HQ crawl in lead_prioritizer_core.py (the most
# trusted HQ-detection source) for exactly those rows. First observed on
# Chamber-of-Commerce-sourced Italy rows, whose merge into the combined
# Lusha+CCIAA input never ran input_cleaner_lusha_edition.py's
# add_batch_app_compatible_columns() domain backfill.
_DOMAIN_FALLBACK_COLUMNS = ("Company Domain", "company_domain", "Website", "website")


def _normalize_fallback_domain(raw) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].strip()
    return "" if not d or " " in d or d in ("nan", "none") else d


def resolve_row_domain(row: dict, config: "BatchRunConfig") -> "str | None":
    """Domain for one row: ``config.domain_column``, falling back per-row to
    ``_DOMAIN_FALLBACK_COLUMNS`` when that field is blank for this row (see
    ``_DOMAIN_FALLBACK_COLUMNS`` docstring)."""
    primary = str(row.get(config.domain_column, "") or "").strip()
    if primary:
        return primary
    for col in _DOMAIN_FALLBACK_COLUMNS:
        if col == config.domain_column:
            continue
        fallback = _normalize_fallback_domain(row.get(col))
        if fallback:
            return fallback
    return None


def resolve_pipeline_flags(run_mode: str) -> dict:
    """Map a run mode to the ``prioritize_single_lead`` flag kwargs."""
    flags = {k: False for k in _ALL_FLAGS}

    if run_mode == "full":
        flags["run_full_v2_pipeline"] = True
    elif run_mode == "hq_only":
        pass  # every optional flag stays False
    elif run_mode == "evidence_only":
        flags["collect_non_hq_evidence"] = True
    elif run_mode == "signals_no_score":
        flags["collect_non_hq_evidence"] = True
        flags["extract_non_hq_signals_flag"] = True
        flags["build_app_summary_fields_flag"] = True
    elif run_mode == "full_no_score":
        flags["collect_non_hq_evidence"] = True
        flags["extract_non_hq_signals_flag"] = True
        flags["build_app_summary_fields_flag"] = True
        flags["build_caller_app_fields_flag"] = True
        # commercial score intentionally False
    else:
        raise ValueError(f"Unknown run_mode: {run_mode!r}")

    return flags


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------

def select_batch_rows(df: pd.DataFrame, config: BatchRunConfig) -> pd.DataFrame:
    """Apply start_row and row_limit, preserving the original DataFrame index.

    ``row_limit == 0`` means all remaining rows from ``start_row``.
    """
    start = max(0, int(config.start_row))
    sub = df.iloc[start:]
    if config.row_limit and int(config.row_limit) > 0:
        sub = sub.iloc[: int(config.row_limit)]
    return sub


# ---------------------------------------------------------------------------
# Flatten helpers
# ---------------------------------------------------------------------------

# Result fields flattened onto the Enriched Leads sheet (curated, ordered).
# NOTE: excludes list fields (evidence_items / signals — own sheets),
# ai_hq_raw_json (gated), and any competitor field (never displayed).
_RESULT_FLAT_FIELDS = [
    "company_name", "domain", "input_country", "v2_pipeline_mode",
    # HQ
    "hq_detected_country", "hq_detected_city", "hq_confidence",
    "foreign_hq_simple", "needs_manual_review", "hq_reason",
    "hq_evidence_url", "hq_evidence_urls", "hq_evidence_quote", "hq_structure_type",
    "hq_location_summary",
    "sig_foreign_hq_score_for_next_scoring",
    "domain_root", "query_used", "parser_source", "domain_is_hosted_platform",
    # C4 positive-score safety audit
    "hq_query_risk_flag", "hq_evidence_domain_match",
    "hq_evidence_domain_mismatch_warning",
    "hq_positive_score_suppressed_for_review", "hq_review_reason",
    "ai_hq_model", "ai_hq_classification", "ai_hq_confidence",
    "ai_parent_company", "ai_parent_hq_country", "ai_parent_hq_city",
    "ai_call_attempted", "ai_call_success", "ai_hq_error",
    # HQ-call usage/cost audit (pre-existing on LeadPrioritizationResult;
    # was missing from this export list -- found while comparing
    # ai_hq_input_tokens before/after the Lusha enrichment plan's Stap 5).
    "ai_hq_input_tokens", "ai_hq_output_tokens", "ai_hq_total_tokens",
    "ai_hq_estimated_cost_usd",
    # non-HQ signal scores / reasons / evidence
    "sig_international_profile_score", "sig_onboarding_training_need_score",
    "sig_company_size_complexity_score", "sig_icp_keyword_match_score",
    "sig_employer_branding_score",
    "international_profile_reason", "onboarding_training_need_reason",
    "company_size_complexity_reason", "icp_keyword_match_reason",
    "employer_branding_reason",
    "international_profile_evidence_url", "onboarding_training_need_evidence_url",
    "company_size_complexity_evidence_url", "icp_keyword_match_evidence_url",
    "employer_branding_evidence_url",
    "international_profile_evidence_urls", "onboarding_training_need_evidence_urls",
    "company_size_complexity_evidence_urls", "icp_keyword_match_evidence_urls",
    "employer_branding_evidence_urls",
    "international_profile_evidence_quote", "onboarding_training_need_evidence_quote",
    "company_size_complexity_evidence_quote", "icp_keyword_match_evidence_quote",
    "employer_branding_evidence_quote",
    "signal_extractor_version", "signal_scoring_mode",
    # Which source produced the company_size_complexity signal above —
    # "lusha" (Lusha employee/revenue data, highest priority), "serper_
    # keyword_match" (existing deterministic fallback, unchanged), or
    # None. See lead_lusha_size_signal.py (Lusha enrichment plan, Stap 3).
    "company_size_complexity_source",
    # sector / industry detection (audit & app metadata — never scoring)
    "detected_industry", "detected_sub_industry", "detected_company_type",
    "sector_confidence", "sector_reason", "sector_evidence_url",
    "sector_evidence_quote", "sector_source_title", "sector_source",
    # Raw Lusha values (audit only), always populated verbatim from the
    # input row when present — see lead_lusha_sector_mapping.py (Stap 2)
    # and lead_lusha_size_signal.py (Stap 3).
    "lusha_main_industry", "lusha_sub_industry",
    "lusha_employees", "lusha_revenue",
    # Raw AI-derived industry from the HQ interpretation step (own-domain
    # content) — the source of the "own_domain_ai" sector fallback above.
    "ai_hq_industry", "ai_hq_sub_industry",
    # score / tier
    "final_commercial_fit_score", "commercial_tier", "icp_similarity_score",
    "lean_model_prob", "lr_z_score", "scoring_profile", "scoring_notes",
    "missing_scoring_fields", "top_score_drivers", "weak_score_drivers",
    "v2_score_input_mapping_note",
    "score_input_foreign_hq", "score_input_intl_footprint",
    "score_input_explicit_lnd", "score_input_lnd_onboarding",
    "score_input_rapid_growth",
    # app / evidence summary
    "evidence_summary_app", "key_source_links_app", "advanced_notes_app",
    # caller / app
    "commercial_fit_score_app", "commercial_tier_app",
    "what_is_hot_app", "what_is_not_app", "why_relevant_app",
    "caller_angle_app", "call_starter_app", "caution_app",
    "foreign_hq_signal_used_in_app", "foreign_hq_country_app", "foreign_hq_city_app",
    "cold_caller_summary_app", "parent_hq_summary_app",
    # AI-composed caller content (Step 3, opt-in)
    "composed_why_relevant", "composed_what_is_hot", "composed_cold_caller_summary",
    "composed_caller_angle", "composed_call_starter", "composed_driver_evidence_json",
    "composed_by_ai", "composed_content_note",
    # AI-composed rich ICP context (opt-in, independent of the above)
    "icp_buying_signals", "icp_likely_training_interest",
    "icp_potential_buyer_function", "icp_context_by_ai", "icp_context_content_note",
    # Legacy enrichment mode (opt-in comparison feature, independent of the
    # above) — see lead_legacy_enrichment.py.
    "legacy_score", "legacy_tier", "legacy_icp_lead_score",
    "legacy_icp_buying_signals", "legacy_icp_likely_training_interest",
    "legacy_icp_potential_buyer_function", "legacy_icp_why_relevant",
    "legacy_icp_evidence", "legacy_enrichment_error",
]


def flatten_result_for_excel(
    result,
    original_row: dict,
    source_index,
    run_success: bool,
    run_error: str,
    include_raw_ai_json: bool = False,
) -> dict:
    """Flatten one result into a single Enriched Leads row.

    Preserves the original input columns, then adds run metadata and the curated
    result fields.  On error (``result is None``) only input columns + run
    metadata are present.
    """
    out: dict = dict(original_row)  # original input columns first
    out["source_index"] = source_index
    out["run_success"] = run_success
    out["run_error"] = run_error or ""

    if result is None:
        out["evidence_count"] = 0
        out["signal_count"] = 0
        return out

    for field in _RESULT_FLAT_FIELDS:
        out[field] = getattr(result, field, None)
    # hq_evidence_urls is the one _RESULT_FLAT_FIELDS entry holding a raw
    # list (mirrors LeadSignal.evidence_urls) -- join it for a clean Excel
    # cell, same semicolon-joined convention as the per-signal *_evidence_urls
    # columns above.
    out["hq_evidence_urls"] = (
        "; ".join(out["hq_evidence_urls"]) if out.get("hq_evidence_urls") else None
    )

    out["evidence_count"] = len(result.evidence_items or [])
    out["signal_count"] = len(result.signals or [])

    if include_raw_ai_json:
        out["ai_hq_raw_json"] = result.ai_hq_raw_json

    return out


def flatten_evidence_for_excel(result, source_index) -> list[dict]:
    """One row per LeadEvidence item on the result."""
    rows: list[dict] = []
    for ev in (getattr(result, "evidence_items", None) or []):
        rows.append({
            "source_index": source_index,
            "evidence_id": ev.evidence_id,
            "signal_name": ev.signal_name,
            "query_used": ev.query_used,
            "source_url": ev.source_url,
            "source_title": ev.source_title,
            "source_snippet": ev.source_snippet,
            "source_type": ev.source_type,
            "parser_source": ev.parser_source,
            "retrieved_at": ev.retrieved_at,
            "confidence": ev.confidence,
            "notes": ev.notes,
        })
    return rows


def should_run_deep_dive(result, config: "BatchRunConfig") -> tuple:
    """Deep-dive trigger gate, evaluated AFTER scoring for one row.

    Returns ``(should_run, trigger_reason)``. ``trigger_reason`` is
    ``"score_threshold"`` when ``final_commercial_fit_score`` clears
    ``config.deep_dive_min_score``, ``"foreign_hq"`` when
    ``config.deep_dive_on_foreign_hq`` is set and the row has a confirmed
    foreign-HQ signal, or ``("", False)``-equivalent when neither applies or
    ``config.deep_dive`` is off. Score takes priority when both conditions
    are true. Never mutates ``result`` or any scoring field.
    """
    if not config.deep_dive:
        return False, ""
    score = getattr(result, "final_commercial_fit_score", None)
    if score is not None and score >= config.deep_dive_min_score:
        return True, "score_threshold"
    foreign_hq = bool(
        getattr(result, "sig_foreign_hq_score_for_next_scoring", None)
        and result.sig_foreign_hq_score_for_next_scoring > 0
    )
    if config.deep_dive_on_foreign_hq and foreign_hq:
        return True, "foreign_hq"
    return False, ""


def flatten_deep_dive_for_excel(result, source_index) -> list[dict]:
    """One row per claim on a ``DeepDiveResult`` (per the Deep Dive sheet
    contract). Company-level context columns repeat on every claim row."""
    rows: list[dict] = []
    for claim in (getattr(result, "claims", None) or []):
        rows.append({
            "source_index": source_index,
            "company_name": result.company_name,
            "trigger_reason": result.trigger_reason,
            "category": claim.category,
            "statement": claim.statement,
            "quote": claim.quote,
            "source_url": claim.source_url,
            "source_kind": claim.source_kind,
            "domain_verified": claim.domain_verified,
            "retrieval_method": claim.retrieval_method,
            "quote_verified": claim.quote_verified,
            "quote_verification_status": claim.quote_verification_status,
            "quote_match_score": claim.quote_match_score,
            "original_quote": claim.original_quote,
            "error": result.error,
        })
    return rows


def flatten_signals_for_excel(result, source_index) -> list[dict]:
    """One row per LeadSignal on the result."""
    rows: list[dict] = []
    for sig in (getattr(result, "signals", None) or []):
        rows.append({
            "source_index": source_index,
            "signal_name": sig.signal_name,
            "signal_value": sig.signal_value,
            "signal_score": sig.signal_score,
            "signal_confidence": sig.signal_confidence,
            "signal_reason": sig.signal_reason,
            "evidence_url": sig.evidence_url,
            "evidence_urls": "; ".join(sig.evidence_urls) if sig.evidence_urls else None,
            "evidence_quote": sig.evidence_quote,
            "evidence_title": sig.evidence_title,
            "query_used": sig.query_used,
            "parser_source": sig.parser_source,
            "needs_manual_review": sig.needs_manual_review,
        })
    return rows


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

def build_run_summary_dataframe(
    config: BatchRunConfig,
    total_input_rows: int,
    selected_rows: int,
    processed_rows: int,
    success_count: int,
    error_count: int,
) -> pd.DataFrame:
    """Single-row summary DataFrame.  Contains no API keys."""
    return pd.DataFrame([{
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_mode": config.run_mode,
        "ai_provider": config.ai_provider,
        "ai_model": config.ai_model or "",
        "default_input_country": config.default_input_country,
        "total_input_rows": total_input_rows,
        "selected_rows": selected_rows,
        "processed_rows": processed_rows,
        "success_count": success_count,
        "error_count": error_count,
        "start_row": config.start_row,
        "row_limit": config.row_limit,
        "company_name_column": config.company_name_column,
        "domain_column": config.domain_column,
        "input_country_column": config.input_country_column,
    }])


# ---------------------------------------------------------------------------
# Shared enrichment cache orchestration (opt-in, GCS-backed) — see
# enrichment_cache.py for the actual index format / download-upload
# mechanics. Everything here is pure glue: resolve which country a row
# belongs to, load/save one index per distinct country actually present in
# the batch (never assuming a batch is single-country), and read the
# before/after usage_tracker delta for the run_summary hit/miss columns.
# ---------------------------------------------------------------------------

def _effective_country_for_row(original: dict, config: "BatchRunConfig") -> str:
    """Mirrors ``prioritize_single_lead``'s own effective-country resolution
    (the row's own ``input_country_column`` value, else
    ``config.default_input_country``) — used ONLY to pick which per-country
    cache index a row should use, so cache-index selection can never
    disagree with what ``prioritize_single_lead`` itself computes for that
    same row."""
    country = None
    if config.input_country_column:
        country = str(original.get(config.input_country_column, "") or "").strip() or None
    return (country or "").strip() or config.default_input_country


def _load_country_cache_indexes(rows: list, config: "BatchRunConfig") -> dict:
    """One cache index per DISTINCT effective country actually present among
    ``rows`` (a list of row dicts) — one GCS download per country, never per
    row, and never assuming the whole batch is a single country just because
    ``config.default_input_country`` names one: a dataset whose
    ``input_country`` column varies per row gets one index per country group.

    Callers are expected to only invoke this when
    ``config.use_enrichment_cache`` is True (checked at each call site, not
    guarded here), so a caller iterating a DataFrame never pays a wasted
    row-to-dict pass when caching is off.
    """
    import enrichment_cache
    from lovable_gcs_upload import country_folder_slug
    slugs = {country_folder_slug(_effective_country_for_row(r, config)) for r in rows}
    return {
        slug: enrichment_cache.load_cache_index(config.enrichment_cache_bucket, slug)
        for slug in slugs
    }


def _cache_index_for_row(
    original: dict, config: "BatchRunConfig", country_indexes: dict,
) -> Optional[dict]:
    """The correct per-country cache index for one row, or ``None`` when
    caching is off / that country's index was not loaded for some reason
    (defensive — a lookup miss here must never be able to break a row; it
    just means that one row runs live instead of cache-checked)."""
    if not country_indexes:
        return None
    from lovable_gcs_upload import country_folder_slug
    slug = country_folder_slug(_effective_country_for_row(original, config))
    return country_indexes.get(slug)


def _save_country_cache_indexes(config: "BatchRunConfig", country_indexes: dict) -> None:
    """Upload every loaded country's index back to GCS. No-op when caching is
    off or nothing was loaded. Never raises — ``enrichment_cache.
    save_cache_index`` already returns a result dict rather than raising; a
    failed upload only means this run's cache updates are lost, never that
    the batch run itself fails."""
    if not config.use_enrichment_cache or not country_indexes:
        return
    import enrichment_cache
    for slug, idx in country_indexes.items():
        enrichment_cache.save_cache_index(config.enrichment_cache_bucket, slug, idx)


def _cache_usage_counts_snapshot() -> dict:
    """Current cumulative cache hit/miss counts from ``usage_tracker``, or an
    all-zero dict if ``usage_tracker`` is unavailable for any reason
    (defensive — this must never break a batch run)."""
    try:
        import usage_tracker
        snap = usage_tracker.snapshot()
        return {
            "serper_hits": snap["cache_hits"].get("serper", 0),
            "serper_misses": snap["cache_misses"].get("serper", 0),
            "firecrawl_hits": snap["cache_hits"].get("firecrawl", 0),
            "firecrawl_misses": snap["cache_misses"].get("firecrawl", 0),
        }
    except Exception:
        return {"serper_hits": 0, "serper_misses": 0, "firecrawl_hits": 0, "firecrawl_misses": 0}


def _apply_cache_run_summary_counts(
    run_summary: pd.DataFrame, cache_usage_before: Optional[dict],
) -> pd.DataFrame:
    """Add ``serper_cache_hits``/``serper_cache_misses``/
    ``firecrawl_cache_hits``/``firecrawl_cache_misses`` columns, computed as
    the ``usage_tracker`` delta since ``cache_usage_before`` was snapshotted
    at the start of this run. A no-op — ``run_summary`` returned completely
    unchanged — when ``cache_usage_before`` is ``None`` (caching was off for
    this run), so the default run_summary shape never gains these columns.
    """
    if cache_usage_before is None:
        return run_summary
    after = _cache_usage_counts_snapshot()
    run_summary["serper_cache_hits"] = after["serper_hits"] - cache_usage_before["serper_hits"]
    run_summary["serper_cache_misses"] = after["serper_misses"] - cache_usage_before["serper_misses"]
    run_summary["firecrawl_cache_hits"] = after["firecrawl_hits"] - cache_usage_before["firecrawl_hits"]
    run_summary["firecrawl_cache_misses"] = (
        after["firecrawl_misses"] - cache_usage_before["firecrawl_misses"])
    return run_summary


# ---------------------------------------------------------------------------
# Lusha row-field auto-detection (Lusha enrichment plan, Stap 2) — no new
# BatchRunConfig column-name setting: Lusha export column names are a
# well-known, fixed vocabulary, so these are detected directly from each
# row dict's own keys, case-insensitively, mirroring the same approach
# input_cleaner_lusha_edition.py uses (kept self-contained here rather than
# imported from that Streamlit app, so the core pipeline never depends on
# a UI module).
# ---------------------------------------------------------------------------

_LUSHA_ROW_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "lusha_main_industry": ("company main industry", "main industry"),
    "lusha_sub_industry": ("company sub industry", "sub industry"),
    "lusha_description": ("company description", "description"),
    "lusha_specialties": ("company specialties", "specialties"),
    # Stap 3 — company size/complexity priority source.
    "lusha_employees": (
        "company number of employees", "number of employees",
        "lusha employee range", "lusha api employee range", "employee range",
    ),
    "lusha_revenue": (
        "company revenue", "revenue",
        "lusha revenue range", "lusha api revenue range", "revenue range",
    ),
}


def _normalize_row_col_key(col) -> str:
    return re.sub(r"[\s_\-]+", " ", str(col).strip().lower())


def _lusha_fields_from_row(row: dict) -> dict:
    """Best-effort detection of Lusha Main/Sub Industry, Description,
    Specialties, Number of Employees, and Revenue directly from ``row``'s
    own keys. A row from a non-Lusha dataset simply has none of these
    column names, so every value defaults to ``None`` — exactly "no Lusha
    data available", the existing fallback behavior for every downstream
    consumer of ``LeadInput.lusha_*``.
    """
    cols_norm = {_normalize_row_col_key(k): k for k in row.keys()}
    out: dict = {}
    for field_name, candidates in _LUSHA_ROW_FIELD_CANDIDATES.items():
        value = None
        for cand in candidates:
            norm = _normalize_row_col_key(cand)
            if norm in cols_norm:
                raw = row.get(cols_norm[norm])
                s = str(raw or "").strip()
                if s and s.lower() != "nan":
                    value = s
                break
        out[field_name] = value
    return out


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch_dataframe(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
    openai_api_key: str = "",
    firecrawl_api_key: str = "",
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    checkpoint_callback: Optional[Callable[[list, list, list], None]] = None,
    checkpoint_every_rows: int = 0,
) -> dict:
    """Run Lead Prioritizer v2 over selected rows.

    Returns a dict of DataFrames: ``enriched_leads``, ``evidence``, ``signals``,
    ``deep_dive``, ``run_summary``.  API keys are passed through only; they are
    never printed or written into any output.

    ``config.ai_provider`` / ``config.ai_model`` select the experimental AI
    provider for HQ interpretation; defaults ("anthropic", "") preserve the
    existing behavior exactly. ``openai_api_key`` is only used when the
    provider is "openai". ``firecrawl_api_key`` is optional — an empty value
    is not an error, it only means Deep Dive (below) uses its Serper/urllib
    fallback path instead of Firecrawl.

    ``config.deep_dive`` (default ``False``) runs an opt-in Deep Dive
    (``deep_dive_runner.run_deep_dive``) AFTER scoring for each row that
    clears ``should_run_deep_dive`` (score >= ``config.deep_dive_min_score``
    and/or, when ``config.deep_dive_on_foreign_hq``, a confirmed foreign-HQ
    signal). The result is a separate ``DeepDiveResult`` per row — it is
    never fed back into ``evidence_items``, ``signals``, or any scoring
    field, and a per-row Deep Dive failure never stops the batch. Fully
    independent of ``config.compose_caller_content`` / ``config.rich_icp_context``.

    ``progress_callback`` (optional) is invoked once after each processed row —
    including rows that error — with a secret-free payload dict.  It defaults to
    ``None`` for backward compatibility (the CLI and existing callers are
    unaffected).  If the callback raises, the exception is swallowed so it can
    never break enrichment.

    ``config.gate_full_enrichment_on_foreign_hq`` (default ``False``) is an
    explicit opt-in that reuses the two-phase gating pattern already proven in
    ``run_batch_foreign_hq_only`` (shared via ``_run_gated_full_enrichment``)
    INSIDE this regular batch path, instead of requiring a separate dedicated
    mode: cheap HQ-only screening runs for every row first (1 Serper call per
    company), and the full per-row v2 pipeline then runs ONLY for rows
    confirmed foreign-HQ (see ``is_confirmed_foreign_hq_for_full_enrichment``).
    Skipped rows are kept in the output with ``enrichment_skipped=True`` and a
    ``foreign_hq_skip_reason``, exactly like ``run_batch_foreign_hq_only``.
    Default ``False`` means every existing caller's behavior is completely
    unchanged.

    ``c5_enabled``/``c5_scoring_behavior``/``c5_scope``/``c5_model_used``/
    ``c5_model_tier`` (same meaning/defaults as ``run_batch_foreign_hq_only``)
    are ONLY meaningful when ``config.gate_full_enrichment_on_foreign_hq`` is
    True — they run C5 as part of Phase 1/2, BEFORE the Phase 3 eligibility
    decision (see ``_run_batch_dataframe_gated``), so a row C5 confirms as
    foreign-HQ becomes eligible for full enrichment even when the plain HQ
    score alone would not have qualified it. When gating is off, these
    params are silently ignored — C5 remains entirely the caller's own
    responsibility (an ``apply_c5_adjudication`` post-step), exactly as
    before; ``run_batch_dataframe`` still has no built-in C5 support outside
    the gated path.

    ``checkpoint_callback``/``checkpoint_every_rows`` (default ``None``/``0`` —
    disabled) are a crash-recovery safety net for long unattended runs (e.g.
    a Cloud Run Jobs task processing a large shard): every
    ``checkpoint_every_rows`` processed rows, ``checkpoint_callback`` is
    called with the current ``(enriched_rows, evidence_rows, signal_rows)``
    lists so a caller can persist progress-so-far to disk/GCS. Without this,
    a crash (OOM kill, uncaught exception) partway through a shard loses
    EVERY row processed so far, not just the one that failed, because
    nothing is written until the whole shard finishes. Mirrors
    ``config.use_enrichment_cache``'s periodic-save pattern; a raising
    callback is swallowed so a broken checkpoint can never break enrichment.
    """
    if config.gate_full_enrichment_on_foreign_hq:
        return _run_batch_dataframe_gated(
            df, config, serper_api_key, anthropic_api_key,
            progress_callback, openai_api_key, firecrawl_api_key,
            c5_enabled=c5_enabled, c5_scoring_behavior=c5_scoring_behavior,
            c5_scope=c5_scope, c5_model_used=c5_model_used, c5_model_tier=c5_model_tier,
            checkpoint_callback=checkpoint_callback,
            checkpoint_every_rows=checkpoint_every_rows,
        )

    flags = resolve_pipeline_flags(config.run_mode)
    # Step 3 AI caller-content composition is independent of run_mode — an
    # explicit opt-in on top of whatever the mode already enables.
    flags["compose_caller_content_flag"] = config.compose_caller_content
    # Rich ICP context is independent of run_mode AND of compose_caller_content
    # above — an explicit opt-in on top of whatever else is enabled.
    flags["compose_icp_context"] = config.rich_icp_context
    # Onderdeel 2: opt-in AI signal scoring, independent of every flag above.
    # Off by default; changes scores only when explicitly enabled.
    flags["ai_signal_scoring"] = config.ai_signal_scoring
    # Comparison feature: opt-in legacy-style enrichment, independent of
    # every flag above. Off by default; never changes v2 scores/signals.
    flags["legacy_enrichment_mode"] = config.legacy_enrichment_mode
    # Provider selection: only override the pipeline's own ai_model default
    # when the config explicitly sets one.
    ai_kwargs: dict = {
        "ai_provider": config.ai_provider,
        "openai_api_key": openai_api_key,
        # Firecrawl own-domain crawl as the PRIMARY HQ source (see
        # lead_hq_firecrawl_source.py). Empty key → Serper-only HQ, exactly as
        # before. Runs for every HQ detection regardless of run_mode.
        "firecrawl_api_key": firecrawl_api_key,
        # Public Source Signal Enrichment — independent opt-in (see
        # BatchRunConfig above); off by default, a missing Firecrawl key
        # blocks only this feature (yields no evidence), never the run.
        "public_source_signal_enrichment": config.public_source_signal_enrichment,
        "public_source_signal_query": config.public_source_signal_query,
        "public_source_base_url": config.public_source_base_url,
        "public_source_label": config.public_source_label,
        "public_source_max_pages": config.public_source_max_pages,
    }
    if config.ai_model:
        ai_kwargs["ai_model"] = config.ai_model
    selected = select_batch_rows(df, config)
    selected_rows = len(selected)

    # ── Shared enrichment cache (opt-in, GCS-backed) — see enrichment_cache.py.
    # Loaded ONCE per distinct country present in the selected rows (never
    # once per row, and never assuming the whole batch is one country) so a
    # mixed-country dataset (input_country varies per row) still gets the
    # right per-row index. Zero cost on the default path: nothing here runs
    # at all unless config.use_enrichment_cache is explicitly True.
    country_cache_indexes: dict = {}
    _cache_usage_before: Optional[dict] = None
    _cache_save_interval: Optional[int] = None
    if config.use_enrichment_cache:
        import enrichment_cache
        country_cache_indexes = _load_country_cache_indexes(
            [r.to_dict() for _, r in selected.iterrows()], config)
        _cache_usage_before = _cache_usage_counts_snapshot()
        _cache_save_interval = enrichment_cache.INTERMEDIATE_SAVE_INTERVAL

    enriched_rows: list[dict] = []
    evidence_rows: list[dict] = []
    signal_rows: list[dict] = []
    deep_dive_rows: list[dict] = []
    processed = success = error = 0

    for idx, row in selected.iterrows():
        original = row.to_dict()
        company = str(original.get(config.company_name_column, "") or "").strip()
        domain = resolve_row_domain(original, config)
        country = None
        if config.input_country_column:
            country = str(original.get(config.input_country_column, "") or "").strip() or None

        cache_index = _cache_index_for_row(original, config, country_cache_indexes)

        processed += 1
        run_success = True
        run_error = ""
        result = None
        try:
            result = prioritize_single_lead(
                LeadInput(company_name=company, domain=domain, input_country=country,
                          **_lusha_fields_from_row(original)),
                serper_api_key=serper_api_key,
                anthropic_api_key=anthropic_api_key,
                default_input_country=config.default_input_country,
                cache_index=cache_index,
                **ai_kwargs,
                **flags,
            )
            success += 1
        except Exception as exc:  # per-row isolation
            run_success = False
            run_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            error += 1

        enriched_rows.append(flatten_result_for_excel(
            result, original, idx, run_success, run_error, config.include_raw_ai_json,
        ))
        if result is not None:
            evidence_rows.extend(flatten_evidence_for_excel(result, idx))
            signal_rows.extend(flatten_signals_for_excel(result, idx))

            # ── Deep Dive (Step B, opt-in) — runs AFTER scoring, never
            # feeds back into evidence_items/signals/scoring. A failure here
            # is per-row only (DeepDiveResult.error) and never raises.
            if config.deep_dive:
                should_run, trigger_reason = should_run_deep_dive(result, config)
                if should_run:
                    dd_result = run_deep_dive(
                        company_name=result.company_name or company,
                        domain=domain,
                        country=result.input_country,
                        parent_company=result.ai_parent_company,
                        # No parent-domain field exists yet anywhere in the v2
                        # pipeline (only the parent's name/country/city are
                        # known) — Deep Dive still works from parent_company
                        # alone via its fallback queries.
                        parent_domain=None,
                        trigger_reason=trigger_reason,
                        serper_api_key=serper_api_key,
                        anthropic_api_key=anthropic_api_key,
                        firecrawl_api_key=firecrawl_api_key,
                        max_pages=config.deep_dive_max_pages,
                        verify_quotes=config.verify_quotes,
                        auto_correct_quotes=config.auto_correct_quotes,
                    )
                    deep_dive_rows.extend(flatten_deep_dive_for_excel(dd_result, idx))

        # Secret-free progress notification (never breaks the batch).
        if progress_callback is not None:
            try:
                progress_callback({
                    "processed_rows": processed,
                    "selected_rows": selected_rows,
                    "success_count": success,
                    "error_count": error,
                    "current_source_index": idx,
                    "current_company_name": company,
                    "current_domain": domain,
                    "run_success": run_success,
                    "run_error": run_error,
                })
            except Exception:
                pass  # a broken callback must never break enrichment

        # Periodic safety-net upload during a long run (crash protection) —
        # only when caching is on; a failed intermediate save is silently
        # retried at the next checkpoint / the final save below.
        if _cache_save_interval and processed % _cache_save_interval == 0:
            _save_country_cache_indexes(config, country_cache_indexes)

        # Periodic progress checkpoint (crash protection) — see
        # run_batch_dataframe's docstring. Never breaks the batch.
        if checkpoint_callback is not None and checkpoint_every_rows and (
            processed % checkpoint_every_rows == 0
        ):
            try:
                checkpoint_callback(enriched_rows, evidence_rows, signal_rows)
            except Exception:
                pass

        if not run_success and not config.continue_on_error:
            break

    # Final save — always runs when caching is on, even if the loop ended
    # early or the last periodic checkpoint already covered most of it.
    _save_country_cache_indexes(config, country_cache_indexes)

    run_summary = build_run_summary_dataframe(
        config,
        total_input_rows=len(df),
        selected_rows=len(selected),
        processed_rows=processed,
        success_count=success,
        error_count=error,
    )
    run_summary = _apply_cache_run_summary_counts(run_summary, _cache_usage_before)

    return {
        "enriched_leads": pd.DataFrame(enriched_rows),
        "evidence": pd.DataFrame(evidence_rows),
        "signals": pd.DataFrame(signal_rows),
        "deep_dive": pd.DataFrame(deep_dive_rows),
        "run_summary": run_summary,
    }


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------

_SHEET_NAMES = {
    "enriched_leads": "Enriched Leads",
    "evidence": "Evidence",
    "signals": "Signals",
    "run_summary": "Run Summary",
}


def build_excel_workbook_bytes(output_tables: dict) -> bytes:
    """Write the batch output tables to an xlsx workbook and return the bytes.

    The "Deep Dive" sheet (``output_tables["deep_dive"]``) is only written
    when it actually has at least one row — a run without Deep Dive enabled,
    or one where no row cleared the trigger gate, produces a workbook with
    no Deep Dive sheet at all rather than an empty one.
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for key, sheet_name in _SHEET_NAMES.items():
            frame = output_tables.get(key)
            if frame is None:
                frame = pd.DataFrame()
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
        deep_dive_frame = output_tables.get("deep_dive")
        if deep_dive_frame is not None and len(deep_dive_frame) > 0:
            deep_dive_frame.to_excel(writer, sheet_name="Deep Dive", index=False)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Optional C5 Sonnet HQ adjudication layer (country-agnostic)
# ---------------------------------------------------------------------------
#
# C5 runs AFTER normal batch processing, over the Enriched Leads rows. It is a
# separate, opt-in step: the core imports the C5 layer lazily inside
# ``apply_c5_adjudication`` so the base batch flow stays independent and C5
# remains removable. It adds no Serper calls and is country-agnostic — the
# per-row ``input_country`` is passed straight through to C5.

C5_SCORING_BEHAVIORS = ("append_only", "conservative_adjustment")
C5_SCOPES = (
    "all_rows",
    "score_3_only",
    "score_3_or_manual_review",
    "manual_review_or_suppressed",
)

# Full set of C5 columns with safe defaults for rows NOT sent to C5.
_C5_BLANK_DEFAULTS = {
    "c5_adjudication": "",
    "c5_confidence": "",
    "c5_target_company_match": "",
    "c5_parent_company": "",
    "c5_parent_hq_country": "",
    "c5_parent_hq_city": "",
    "c5_reason": "",
    "c5_sonnet_model": "",
    "c5_model_used": "",
    "c5_model_tier": "",
    "c5_call_attempted": False,
    "c5_call_success": False,
    "c5_error": "",
    "c5_recommended_hq_score": None,
    "c5_recommended_manual_review": False,
    "c5_recommendation_reason": "",
    "c5_possible_foreign_parent_for_review": False,
}


def _c5_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "y")


def _c5_score(v):
    """Parse a score to float, or None when absent/blank/unparseable."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def row_selected_for_c5(row: dict, scope: str) -> bool:
    """Country-agnostic C5 row selection based on the existing HQ output."""
    score = _c5_score(row.get("sig_foreign_hq_score_for_next_scoring"))
    is3 = (score == 3.0)
    review = _c5_truthy(row.get("needs_manual_review"))
    suppressed = _c5_truthy(row.get("hq_positive_score_suppressed_for_review"))
    if scope == "all_rows":
        return True
    if scope == "score_3_only":
        return is3
    if scope == "score_3_or_manual_review":
        return is3 or review
    if scope == "manual_review_or_suppressed":
        return review or suppressed
    return False


def _c5_blank_row(original: dict, include_raw: bool) -> dict:
    out = dict(original)
    out.update(_C5_BLANK_DEFAULTS)
    if include_raw:
        out["c5_raw_json"] = None
    return out


def _append_hq_reason(row: dict, note: str) -> None:
    prev = str(row.get("hq_reason") or "").strip()
    row["hq_reason"] = f"{prev} | {note}" if prev else note


def _apply_conservative_adjustment(enriched: dict, result, counts: dict) -> None:
    """Conservative C5 scoring: confirm/downgrade score-3 positives; upgrade
    a score-0 row to 3 only when C5 itself confirms a foreign parent (High/
    Medium confidence, target company matched) -- the same bar
    ``is_confirmed_foreign_hq_for_full_enrichment`` already uses to include a
    row in the foreign-HQ-only export. Before this, a C5-confirmed row could
    be exported/enriched as "confirmed foreign HQ" while its own commercial
    fit score still treated it as having no HQ signal at all (sig_foreign_hq
    carries the largest single coefficient in the scoring model), which
    silently pulled ~1/5 of a real foreign-HQ export below score 4 despite
    every one of them having a confirmed foreign parent. Mutates ``enriched``
    in place."""
    old = _c5_score(enriched.get("sig_foreign_hq_score_for_next_scoring"))
    confirmed = (
        bool(result.call_success)
        and result.adjudication == "foreign_parent_confirmed"
        and result.confidence in ("High", "Medium")
        and result.target_company_match == "yes"
    )

    if old == 3.0:
        if confirmed:
            return  # keep score 3
        enriched["sig_foreign_hq_score_for_next_scoring"] = 0.0
        enriched["needs_manual_review"] = True
        _append_hq_reason(
            enriched,
            "C5 downgraded previous HQ score-3 because foreign parent was not confirmed.")
        counts["c5_downgraded_score_3_count"] += 1
        return

    if old == 0.0:
        if confirmed:
            enriched["sig_foreign_hq_score_for_next_scoring"] = 3.0
            enriched["c5_possible_foreign_parent_for_review"] = True
            enriched["needs_manual_review"] = True
            _append_hq_reason(
                enriched,
                "C5 confirmed foreign parent; upgraded from suppressed HQ score-0.")
            counts["c5_possible_foreign_parent_for_review_count"] += 1
        else:
            enriched["sig_foreign_hq_score_for_next_scoring"] = 0.0
            if not result.call_success:
                # Row was selected for C5 but the call/parse failed → stay safe.
                enriched["needs_manual_review"] = True
        return

    if old is None and confirmed:
        # A row whose HQ signal was never determined at all (distinct from
        # old == 0.0's explicit suppression) is just as blind to
        # sig_foreign_hq_score -- the dominant scoring coefficient -- as a
        # suppressed 0 is. Without this, a row that reached C5 with a None
        # signal (e.g. selected via needs_manual_review rather than the
        # score-3 path) stayed unscored on this signal even after C5
        # confirmed a foreign parent, silently reproducing the same
        # "confirmed foreign HQ but scored as having none" bug the old ==
        # 0.0 branch above was written to fix. Unconfirmed/None stays
        # untouched (falls through), same as before this branch existed --
        # there is no prior explicit 0 to preserve or correct.
        enriched["sig_foreign_hq_score_for_next_scoring"] = 3.0
        enriched["c5_possible_foreign_parent_for_review"] = True
        enriched["needs_manual_review"] = True
        _append_hq_reason(
            enriched,
            "C5 confirmed foreign parent; upgraded from unresolved HQ signal.")
        counts["c5_possible_foreign_parent_for_review_count"] += 1
        return
    # Other/absent old scores: leave untouched under conservative mode.


def apply_c5_adjudication(
    enriched_rows,
    *,
    anthropic_api_key: str,
    model_used: str,
    model_tier: str,
    scoring_behavior: str = "append_only",
    scope: str = "score_3_or_manual_review",
    include_raw: bool = False,
    progress_callback=None,
) -> tuple:
    """Apply optional C5 Sonnet adjudication over Enriched Leads rows.

    Reuses the single-source ``adjudicate_row`` from the C5 probe (lazy import),
    so no C5 prompt/parser logic is duplicated. Country-agnostic: each row's
    ``input_country`` is passed straight to C5.

    Returns ``(out_rows, counts)``. ``append_only`` never changes
    ``sig_foreign_hq_score_for_next_scoring`` / ``needs_manual_review``;
    ``conservative_adjustment`` may confirm/downgrade score-3 positives, and
    upgrades a score-0 row to 3 only when C5 itself confirms a foreign
    parent with High/Medium confidence and a matched target company — see
    ``_apply_conservative_adjustment``.
    """
    from run_hq_sonnet_adjudication_probe import adjudicate_row  # lazy; keeps C5 removable

    if isinstance(enriched_rows, pd.DataFrame):
        rows = enriched_rows.to_dict("records")
    else:
        rows = [dict(r) for r in enriched_rows]

    counts = {
        "c5_rows_attempted": 0,
        "c5_success_count": 0,
        "c5_error_count": 0,
        "c5_foreign_parent_confirmed_count": 0,
        "c5_domestic_confirmed_count": 0,
        "c5_unclear_count": 0,
        "c5_recommended_score_3_count": 0,
        "c5_possible_foreign_parent_for_review_count": 0,
        "c5_downgraded_score_3_count": 0,
    }

    flags = [row_selected_for_c5(r, scope) for r in rows]
    total = sum(flags)
    done = 0
    out_rows: list[dict] = []

    for r, selected in zip(rows, flags):
        if not selected:
            out_rows.append(_c5_blank_row(r, include_raw))
            continue

        enriched, result, rec = adjudicate_row(
            r, anthropic_api_key, model_used, model_tier,
            source_index=r.get("source_index"), include_raw=include_raw,
        )
        enriched.setdefault("c5_possible_foreign_parent_for_review", False)

        counts["c5_rows_attempted"] += 1
        if result.call_success:
            counts["c5_success_count"] += 1
        else:
            counts["c5_error_count"] += 1
        if result.adjudication == "foreign_parent_confirmed":
            counts["c5_foreign_parent_confirmed_count"] += 1
        elif result.adjudication == "domestic_confirmed":
            counts["c5_domestic_confirmed_count"] += 1
        else:
            counts["c5_unclear_count"] += 1
        if rec["c5_recommended_hq_score"] == 3.0:
            counts["c5_recommended_score_3_count"] += 1

        if scoring_behavior == "conservative_adjustment":
            _apply_conservative_adjustment(enriched, result, counts)
        # append_only: never touch score / needs_manual_review.

        # Recompute the always-shown HQ location line now that C5's richer
        # parent HQ fields exist, so C5 takes priority (C5 > AI > detected).
        # Idempotent for rows where C5 added no parent info.
        _c5_summary = build_hq_location_summary_from_row(enriched)
        if _c5_summary:
            enriched["hq_location_summary"] = _c5_summary

        out_rows.append(enriched)
        done += 1
        if progress_callback is not None:
            try:
                progress_callback({
                    "c5_processed": done,
                    "c5_selected": total,
                    "current_company_name": str(r.get("company_name") or ""),
                })
            except Exception:
                pass

    return out_rows, counts


def add_c5_summary_fields(
    run_summary: pd.DataFrame,
    *,
    c5_enabled: bool,
    c5_scoring_behavior: str,
    c5_scope: str,
    c5_model_tier: str,
    c5_model_used: str,
    counts: dict,
) -> pd.DataFrame:
    """Return a copy of the run-summary DataFrame with C5 settings/counts added."""
    df = run_summary.copy() if run_summary is not None else pd.DataFrame([{}])
    if len(df) == 0:
        df = pd.DataFrame([{}])
    df["c5_enabled"] = c5_enabled
    df["c5_scoring_behavior"] = c5_scoring_behavior
    df["c5_scope"] = c5_scope
    df["c5_model_tier"] = c5_model_tier
    df["c5_model_used"] = c5_model_used
    for key in (
        "c5_rows_attempted", "c5_success_count", "c5_error_count",
        "c5_foreign_parent_confirmed_count", "c5_domestic_confirmed_count",
        "c5_unclear_count", "c5_recommended_score_3_count",
        "c5_possible_foreign_parent_for_review_count", "c5_downgraded_score_3_count",
    ):
        df[key] = counts.get(key, 0)
    return df


# ---------------------------------------------------------------------------
# "Full enrichment, confirmed foreign-HQ only" batch mode (country-agnostic)
# ---------------------------------------------------------------------------
#
# Reduces cost/noise for Brazil-and-similar runs: HQ detection (+ C4, and
# optionally C5) runs for every row first; full v2 enrichment (evidence,
# signals, scoring, caller fields) then runs ONLY for rows confirmed
# foreign-HQ. Everything is reused — no duplicated HQ, C4, or C5 logic:
#   Phase 1 delegates to run_batch_dataframe(run_mode="hq_only").
#   Phase 2 (optional) delegates to apply_c5_adjudication.
#   Phase 3 delegates to prioritize_single_lead(run_full_v2_pipeline=True) and
#   flatten_result_for_excel, but only for confirmed rows.

def _emit_batch_phase_progress(
    progress_callback: Optional[Callable[[dict], None]],
    phase_labels: dict,
    phase: int,
    phase_processed,
    phase_total,
    base: dict,
) -> None:
    """Forward a progress payload augmented with phase info; never raises.

    Shared by every mode in the foreign-HQ-only family so phase-1/2/3 progress
    payloads always carry the same ``phase`` / ``phase_count`` / ``phase_label``
    / ``phase_processed`` / ``phase_total`` shape.
    """
    if progress_callback is None:
        return
    payload = dict(base)
    payload.update({
        "phase": phase,
        "phase_count": 3,
        "phase_label": phase_labels[phase],
        "phase_processed": int(phase_processed or 0),
        "phase_total": int(phase_total or 0),
    })
    try:
        progress_callback(payload)
    except Exception:
        pass  # a broken callback must never break the run


def _run_hq_and_c5_screening(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool,
    c5_scoring_behavior: str,
    c5_scope: str,
    c5_model_used: str,
    c5_model_tier: str,
    phase_labels: dict,
    progress_callback: Optional[Callable[[dict], None]],
) -> tuple:
    """Phase 1 (HQ-only screening) + Phase 2 (optional C5) — shared by every
    mode in the foreign-HQ-only family so HQ/C4/C5 logic is never duplicated.

    Phase 1 delegates to ``run_batch_dataframe(run_mode="hq_only")``; Phase 2
    (only when ``c5_enabled``) delegates to ``apply_c5_adjudication`` exactly
    as before. Returns ``(rows, c5_counts)`` — ``rows`` is the Enriched-Leads
    dict list after HQ+C4 (and, if enabled, C5) has been applied. Phase 3
    (the mode-specific full-enrichment decision) is the caller's job.
    """
    def _phase1_cb(payload: dict) -> None:
        _emit_batch_phase_progress(
            progress_callback, phase_labels, 1,
            payload.get("processed_rows"), payload.get("selected_rows"), payload)

    def _phase2_cb(payload: dict) -> None:
        _emit_batch_phase_progress(
            progress_callback, phase_labels, 2,
            payload.get("c5_processed"), payload.get("c5_selected"), payload)

    # gate_full_enrichment_on_foreign_hq is forced off for this Phase-1 sub-
    # call regardless of the caller's config: Phase 1 is always a plain
    # hq_only pass, and a caller whose own config has the gate on (the new
    # run_batch_dataframe opt-in path routes back through here) must never
    # have run_batch_dataframe re-enter its own gated branch recursively.
    hq_config = replace(config, run_mode="hq_only", gate_full_enrichment_on_foreign_hq=False)
    hq_tables = run_batch_dataframe(
        df, hq_config, serper_api_key, anthropic_api_key,
        progress_callback=_phase1_cb if progress_callback is not None else None,
    )
    rows = hq_tables["enriched_leads"].to_dict("records")

    c5_counts: dict = {}
    if c5_enabled:
        rows, c5_counts = apply_c5_adjudication(
            rows,
            anthropic_api_key=anthropic_api_key,
            model_used=c5_model_used,
            model_tier=c5_model_tier,
            scoring_behavior=c5_scoring_behavior,
            scope=c5_scope,
            include_raw=config.include_raw_ai_json,
            progress_callback=_phase2_cb if progress_callback is not None else None,
        )
    return rows, c5_counts


def is_confirmed_foreign_hq_for_full_enrichment(row: dict, c5_enabled: bool) -> bool:
    """Phase-3 full-enrichment eligibility gate for the foreign-HQ-only mode.

    A row is eligible when either:
    - the FINAL post-C4/post-C5 HQ score is 3 (the pre-existing path; the only
      path when ``c5_enabled`` is False), or
    - C5 is enabled and explicitly confirmed a foreign parent for the target
      company: a successful C5 call with ``c5_adjudication ==
      "foreign_parent_confirmed"``, a yes/true ``c5_target_company_match``,
      and — when the column carries a value — ``c5_recommended_hq_score == 3``.

    C5 unclear/domestic results, target mismatches, and C5 errors are never
    auto-included. This gates enrichment eligibility only: it does not change
    ``sig_foreign_hq_score_for_next_scoring``, C4 suppression, or any C5
    output field. Bool/string values ("yes"/"true"/"1"/"y", any case) and
    numeric strings ("3", 3, 3.0) are normalized safely.
    """
    if _c5_score(row.get("sig_foreign_hq_score_for_next_scoring")) == 3.0:
        return True
    if not c5_enabled:
        return False
    if not _c5_truthy(row.get("c5_call_success")):
        return False
    adjudication = str(row.get("c5_adjudication") or "").strip().lower()
    if adjudication != "foreign_parent_confirmed":
        return False
    if not _c5_truthy(row.get("c5_target_company_match")):
        return False
    recommended = _c5_score(row.get("c5_recommended_hq_score"))
    if recommended is not None and recommended != 3.0:
        return False
    return True


def foreign_hq_skip_reason(row: dict, c5_enabled: bool) -> str:
    """Specific skip reason for a row not eligible for full enrichment."""
    if c5_enabled and _c5_truthy(row.get("c5_call_attempted")):
        if not _c5_truthy(row.get("c5_call_success")):
            return "C5 error"
        adjudication = str(row.get("c5_adjudication") or "").strip().lower()
        if adjudication == "unclear":
            return "C5 unclear"
        if adjudication == "foreign_parent_confirmed" and not _c5_truthy(
                row.get("c5_target_company_match")):
            return "C5 target mismatch"
    return "Not confirmed foreign HQ"


def _run_gated_full_enrichment(
    rows: list[dict],
    config: "BatchRunConfig",
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool,
    prioritize_kwargs: dict,
    phase_labels: dict,
    progress_callback: Optional[Callable[[dict], None]] = None,
    openai_api_key: str = "",
    firecrawl_api_key: str = "",
    run_deep_dive_step: bool = False,
    checkpoint_callback: Optional[Callable[[list, list, list], None]] = None,
    checkpoint_every_rows: int = 0,
) -> dict:
    """Phase 3 of the foreign-HQ gating pattern: per-row eligibility check,
    then full enrichment for eligible rows / a skip marker for the rest.

    Extracted from ``run_batch_foreign_hq_only`` so that mode and the opt-in
    ``BatchRunConfig.gate_full_enrichment_on_foreign_hq`` gate inside the
    regular ``run_batch_dataframe`` share ONE implementation of "cheap HQ
    screening for everyone (already done by the caller via
    ``_run_hq_and_c5_screening`` before this runs), expensive full
    enrichment only for confirmed-foreign-HQ rows, skip the rest with a
    reason" — no duplicated eligibility/skip/progress logic between the two
    callers.

    ``rows`` is the Enriched-Leads dict list AFTER Phase 1 (HQ screening) and
    optional Phase 2 (C5) have already run — this function only performs the
    eligibility check and Phase 3 itself; it never re-runs HQ/C4/C5.

    ``prioritize_kwargs`` is merged into every eligible row's
    ``prioritize_single_lead(...)`` call, on top of the base
    ``LeadInput``/``serper_api_key``/``anthropic_api_key``/
    ``default_input_country``. This is what lets each caller control exactly
    which optional v2 steps run for confirmed rows without the helper
    hardcoding one caller's flag set: ``run_batch_foreign_hq_only`` passes
    only ``{"run_full_v2_pipeline": True}`` (matching its existing, tested,
    minimal behavior exactly), while the new gated ``run_batch_dataframe``
    path passes its full per-row ``ai_kwargs``/``flags`` dict so nothing that
    path already supports (compose_caller_content, rich_icp_context, AI
    signal scoring, Public Source Signal Enrichment, ...) is silently dropped
    just because gating was turned on.

    ``run_deep_dive_step`` (default ``False``) additionally runs Deep Dive
    after scoring for eligible rows that clear ``should_run_deep_dive``. Kept
    as an explicit parameter — never read from ``config.deep_dive`` directly
    — so ``run_batch_foreign_hq_only``, which has never supported Deep Dive,
    keeps behaving exactly as before regardless of what a caller's config
    happens to contain.

    Returns a dict with ``out_rows``, ``evidence_rows``, ``signal_rows``,
    ``deep_dive_rows`` (lists of dicts) and ``attempted``/``skipped``/
    ``confirmed``/``total_confirmed``/``p3_success``/``p3_error`` (ints) —
    callers build their own ``enriched_leads``/``run_summary`` DataFrames
    from these, since the exact run_summary column names differ between
    ``run_batch_foreign_hq_only`` (its established, tested contract) and the
    new gated path (see ``_run_batch_dataframe_gated``).

    When ``config.use_enrichment_cache`` is True, this function runs its own
    independent per-country cache load/use/save cycle (see the module-level
    cache helpers) and additionally returns ``cache_usage_before`` — the
    ``usage_tracker`` cache-count snapshot taken before this phase started —
    so callers can compute their own hit/miss delta via
    ``_apply_cache_run_summary_counts``. ``cache_usage_before`` is ``None``
    when caching is off, matching that helper's no-op contract.
    """
    out_rows: list[dict] = []
    evidence_rows: list[dict] = []
    signal_rows: list[dict] = []
    deep_dive_rows: list[dict] = []
    attempted = skipped = confirmed = 0
    p3_success = p3_error = 0

    # ── Shared enrichment cache (opt-in, GCS-backed) — its own independent
    # load/save cycle, separate from Phase 1's (inside _run_hq_and_c5_screening
    # -> run_batch_dataframe(hq_config, ...), which already handles the HQ-
    # only queries' caching). This is safe because Phase 1 always completes
    # fully — including its own final upload — BEFORE this Phase-3 loop
    # starts (strictly sequential, never concurrent), so a fresh download
    # here always sees Phase 1's just-uploaded entries; there is no
    # lost-write race between the two phases' cache saves.
    country_cache_indexes: dict = {}
    _cache_usage_before: Optional[dict] = None
    _cache_save_interval: Optional[int] = None
    if config.use_enrichment_cache:
        import enrichment_cache
        country_cache_indexes = _load_country_cache_indexes(rows, config)
        _cache_usage_before = _cache_usage_counts_snapshot()
        _cache_save_interval = enrichment_cache.INTERMEDIATE_SAVE_INTERVAL

    # Eligibility precomputed so progress can report a fixed phase-3 total.
    confirmed_flags = [
        is_confirmed_foreign_hq_for_full_enrichment(r, c5_enabled=c5_enabled)
        for r in rows
    ]
    total_confirmed = sum(confirmed_flags)

    for row, is_confirmed in zip(rows, confirmed_flags):
        if not is_confirmed:
            out_row = dict(row)
            out_row["enrichment_skipped"] = True
            out_row["enrichment_skip_reason"] = foreign_hq_skip_reason(row, c5_enabled)
            out_row["full_enrichment_gate_reason"] = ""
            out_rows.append(out_row)
            skipped += 1
            continue

        confirmed += 1
        attempted += 1
        if _c5_score(row.get("sig_foreign_hq_score_for_next_scoring")) == 3.0:
            gate_reason = "Confirmed foreign HQ (final HQ score 3)"
        else:
            gate_reason = "Confirmed by C5 foreign-parent adjudication"
        company = str(row.get(config.company_name_column, "") or "").strip()
        domain = resolve_row_domain(row, config)
        country = None
        if config.input_country_column:
            country = str(row.get(config.input_country_column, "") or "").strip() or None
        source_index = row.get("source_index")
        cache_index = _cache_index_for_row(row, config, country_cache_indexes)

        result = None
        try:
            result = prioritize_single_lead(
                LeadInput(company_name=company, domain=domain, input_country=country,
                          **_lusha_fields_from_row(row)),
                serper_api_key=serper_api_key,
                anthropic_api_key=anthropic_api_key,
                default_input_country=config.default_input_country,
                cache_index=cache_index,
                **prioritize_kwargs,
            )
            out_row = flatten_result_for_excel(
                result, row, source_index, True, "", config.include_raw_ai_json,
            )
            # flatten_result_for_excel overwrites sig_foreign_hq_score_for_next_scoring
            # with this Phase-3 call's OWN fresh HQ (re)detection -- prioritize_single_lead
            # redoes the full C1-C4 HQ pass internally and doesn't know about Phase 1/2's
            # already-final score (including any C5 upgrade _apply_conservative_adjustment
            # just applied). Phase 1+2 already produced the authoritative, gating score for
            # this row (that's what got it into Phase 3 in the first place via
            # is_confirmed_foreign_hq_for_full_enrichment) -- restore it so Phase 3 can never
            # silently re-suppress (or otherwise change) the score it was gated on.
            out_row["sig_foreign_hq_score_for_next_scoring"] = row.get(
                "sig_foreign_hq_score_for_next_scoring")
            evidence_rows.extend(flatten_evidence_for_excel(result, source_index))
            signal_rows.extend(flatten_signals_for_excel(result, source_index))
            p3_success += 1
        except Exception as exc:  # per-row isolation, matching run_batch_dataframe
            out_row = dict(row)
            out_row["run_success"] = False
            out_row["run_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            p3_error += 1

        # ── Deep Dive (opt-in, mirrors run_batch_dataframe's own placement:
        # AFTER scoring, never feeds back into evidence_items/signals/scoring).
        if run_deep_dive_step and result is not None:
            should_run, trigger_reason = should_run_deep_dive(result, config)
            if should_run:
                dd_result = run_deep_dive(
                    company_name=result.company_name or company,
                    domain=domain,
                    country=result.input_country,
                    parent_company=result.ai_parent_company,
                    parent_domain=None,
                    trigger_reason=trigger_reason,
                    serper_api_key=serper_api_key,
                    anthropic_api_key=anthropic_api_key,
                    firecrawl_api_key=firecrawl_api_key,
                    max_pages=config.deep_dive_max_pages,
                    verify_quotes=config.verify_quotes,
                    auto_correct_quotes=config.auto_correct_quotes,
                )
                deep_dive_rows.extend(flatten_deep_dive_for_excel(dd_result, source_index))

        out_row["enrichment_skipped"] = False
        out_row["enrichment_skip_reason"] = ""
        out_row["full_enrichment_gate_reason"] = gate_reason
        out_rows.append(out_row)

        _emit_batch_phase_progress(progress_callback, phase_labels, 3,
                                   attempted, total_confirmed, {
            "success_count": p3_success,
            "error_count": p3_error,
            "current_company_name": company,
            # legacy keys kept for compatibility with earlier payload shape
            "foreign_hq_full_processed": attempted,
            "foreign_hq_full_selected": total_confirmed,
        })

        if _cache_save_interval and attempted % _cache_save_interval == 0:
            _save_country_cache_indexes(config, country_cache_indexes)

        # Periodic progress checkpoint (crash protection) — see
        # run_batch_dataframe's docstring. Never breaks the batch.
        if checkpoint_callback is not None and checkpoint_every_rows and (
            attempted % checkpoint_every_rows == 0
        ):
            try:
                checkpoint_callback(out_rows, evidence_rows, signal_rows)
            except Exception:
                pass

    if config.use_enrichment_cache:
        _save_country_cache_indexes(config, country_cache_indexes)

    return {
        "out_rows": out_rows,
        "evidence_rows": evidence_rows,
        "signal_rows": signal_rows,
        "deep_dive_rows": deep_dive_rows,
        "attempted": attempted,
        "skipped": skipped,
        "confirmed": confirmed,
        "total_confirmed": total_confirmed,
        "p3_success": p3_success,
        "p3_error": p3_error,
        "cache_usage_before": _cache_usage_before,
    }


def _run_batch_dataframe_gated(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    progress_callback: Optional[Callable[[dict], None]],
    openai_api_key: str,
    firecrawl_api_key: str,
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    checkpoint_callback: Optional[Callable[[list, list, list], None]] = None,
    checkpoint_every_rows: int = 0,
) -> dict:
    """``run_batch_dataframe``'s opt-in path when
    ``config.gate_full_enrichment_on_foreign_hq`` is True: cheap HQ-only
    screening for every row (Phase 1, 1 Serper call per company, plus
    optional C5 via ``_run_hq_and_c5_screening`` when ``c5_enabled``), then
    the full per-row v2 pipeline — using the SAME ``ai_kwargs``/``flags`` the
    default ``run_batch_dataframe`` path would build for every row — ONLY
    for rows confirmed foreign-HQ (Phase 3, via the shared
    ``_run_gated_full_enrichment``, also used by ``run_batch_foreign_hq_only``).
    Rows that are not confirmed are kept in the output with
    ``enrichment_skipped=True`` and a ``foreign_hq_skip_reason``.

    ``c5_enabled`` (and the four ``c5_*`` params, same meaning/defaults as
    ``run_batch_foreign_hq_only``) run C5 as part of Phase 1/2, BEFORE the
    Phase 3 eligibility decision — exactly the pattern
    ``run_batch_foreign_hq_only`` already uses. This matters: a row C5
    confirms as ``foreign_parent_confirmed`` (via
    ``is_confirmed_foreign_hq_for_full_enrichment``) becomes eligible for
    full enrichment even when its plain HQ score is not 3 — a borderline row
    C5 rescues is NOT stuck at ``enrichment_skipped=True`` the way an
    earlier version of this function (which ran C5 as a flat post-step after
    the gate decision) would have left it. Deep Dive, by contrast, mirrors
    the default path's own behavior exactly — it runs for eligible rows
    whenever ``config.deep_dive`` is set, same as an ungated
    ``run_batch_dataframe`` call.

    Returns the same dict shape as ``run_batch_dataframe`` (``enriched_leads``,
    ``evidence``, ``signals``, ``deep_dive``, ``run_summary``) — ``run_summary``
    additionally carries ``gated_full_enrichment_attempted_count`` /
    ``gated_full_enrichment_skipped_count`` /
    ``gated_estimated_serper_calls_saved`` (``skipped * 4``, matching the four
    extra non-HQ Serper calls a full v2 enrichment would otherwise have made)
    and, always, the C5 settings/counts columns (via ``add_c5_summary_fields``,
    same as ``run_batch_foreign_hq_only``).
    """
    rows, c5_counts = _run_hq_and_c5_screening(
        df, config, serper_api_key, anthropic_api_key,
        c5_enabled=c5_enabled, c5_scoring_behavior=c5_scoring_behavior,
        c5_scope=c5_scope, c5_model_used=c5_model_used, c5_model_tier=c5_model_tier,
        phase_labels=GATED_FULL_ENRICHMENT_PHASE_LABELS,
        progress_callback=progress_callback,
    )

    # Exactly the same ai_kwargs/flags construction as the default
    # run_batch_dataframe path below, so an eligible (confirmed foreign-HQ)
    # row gets identical treatment to what every row would get with gating off.
    flags = resolve_pipeline_flags(config.run_mode)
    flags["compose_caller_content_flag"] = config.compose_caller_content
    flags["compose_icp_context"] = config.rich_icp_context
    flags["ai_signal_scoring"] = config.ai_signal_scoring
    flags["legacy_enrichment_mode"] = config.legacy_enrichment_mode
    ai_kwargs: dict = {
        "ai_provider": config.ai_provider,
        "openai_api_key": openai_api_key,
        "firecrawl_api_key": firecrawl_api_key,
        "public_source_signal_enrichment": config.public_source_signal_enrichment,
        "public_source_signal_query": config.public_source_signal_query,
        "public_source_base_url": config.public_source_base_url,
        "public_source_label": config.public_source_label,
        "public_source_max_pages": config.public_source_max_pages,
    }
    if config.ai_model:
        ai_kwargs["ai_model"] = config.ai_model

    gated = _run_gated_full_enrichment(
        rows, config, serper_api_key, anthropic_api_key,
        c5_enabled=c5_enabled,
        prioritize_kwargs={**ai_kwargs, **flags},
        phase_labels=GATED_FULL_ENRICHMENT_PHASE_LABELS,
        progress_callback=progress_callback,
        openai_api_key=openai_api_key,
        firecrawl_api_key=firecrawl_api_key,
        run_deep_dive_step=config.deep_dive,
        checkpoint_callback=checkpoint_callback,
        checkpoint_every_rows=checkpoint_every_rows,
    )

    out_rows = gated["out_rows"]
    success_count = sum(1 for r in out_rows if r.get("run_success", True))
    error_count = len(out_rows) - success_count

    run_summary = build_run_summary_dataframe(
        config, total_input_rows=len(df), selected_rows=len(rows),
        processed_rows=len(rows), success_count=success_count, error_count=error_count,
    )
    # Gating audit counts (Step 4) — only present when gating is on; the
    # default (ungated) run_summary shape is completely unaffected.
    run_summary["gated_full_enrichment_attempted_count"] = gated["attempted"]
    run_summary["gated_full_enrichment_skipped_count"] = gated["skipped"]
    run_summary["gated_estimated_serper_calls_saved"] = gated["skipped"] * 4
    # Always record C5 settings/counts, matching run_batch_foreign_hq_only --
    # c5_counts came from Phase 1/2 (_run_hq_and_c5_screening) above, so this
    # reflects the SAME C5 calls that could rescue a row into Phase 3, not a
    # separate/duplicate C5 pass.
    run_summary = add_c5_summary_fields(
        run_summary,
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts=c5_counts,
    )
    run_summary = _apply_cache_run_summary_counts(run_summary, gated.get("cache_usage_before"))

    return {
        "enriched_leads": pd.DataFrame(out_rows),
        "evidence": pd.DataFrame(gated["evidence_rows"]),
        "signals": pd.DataFrame(gated["signal_rows"]),
        "deep_dive": pd.DataFrame(gated["deep_dive_rows"]),
        "run_summary": run_summary,
    }


def run_batch_foreign_hq_only(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the "Full enrichment, confirmed foreign-HQ only" batch mode.

    A row receives full enrichment when
    ``is_confirmed_foreign_hq_for_full_enrichment`` says so: either the FINAL
    post-C4/post-C5 ``sig_foreign_hq_score_for_next_scoring`` is 3.0 (the only
    path when C5 is disabled), or C5 explicitly confirmed a foreign parent for
    the target company (foreign_parent_confirmed + target match yes +
    recommended HQ score 3). ``_apply_conservative_adjustment`` (Phase 2)
    upgrades a C4-suppressed score-0 row to 3 exactly when C5 confirms it
    this way (High/Medium confidence) -- the same bar this eligibility gate
    uses -- so a row's commercial fit score is never computed as if it had
    no HQ signal while it's simultaneously being exported as a confirmed
    foreign HQ. Phase 3 (this function's own full-enrichment call) redoes
    its own HQ detection internally but never gets to overwrite that
    Phase 1+2 score: see ``_run_gated_full_enrichment``'s restore.

    Rows that are not eligible are kept in the output, unenriched, with
    ``enrichment_skipped=True`` and a specific ``enrichment_skip_reason``
    ("C5 unclear" / "C5 target mismatch" / "C5 error" / "Not confirmed
    foreign HQ"); eligible rows get ``enrichment_skipped=False`` / ``""``,
    a ``full_enrichment_gate_reason`` audit value, and the full v2 fields.

    Returns the same dict shape as ``run_batch_dataframe``: ``enriched_leads``,
    ``evidence``, ``signals``, ``run_summary`` (extended with the mode's own
    counts and, always, the C5 settings/counts columns).

    Phase 3 (the eligibility check + full-enrichment-or-skip loop below) is
    implemented by the shared ``_run_gated_full_enrichment`` helper — also
    used by the opt-in ``BatchRunConfig.gate_full_enrichment_on_foreign_hq``
    gate inside the regular ``run_batch_dataframe`` — so the two callers can
    never drift apart on eligibility/skip-reason/progress behavior. This
    function passes ``prioritize_kwargs={"run_full_v2_pipeline": True}`` and
    ``run_deep_dive_step=False``, matching its pre-refactor behavior exactly
    (Deep Dive has never been supported in this mode).
    """
    # Snapshotted BEFORE Phase 1 so the eventual run_summary counts cover
    # BOTH phases' cache usage (Phase 1's own run_batch_dataframe(hq_config)
    # call already does its own independent cache load/use/save cycle, whose
    # hit/miss counts would otherwise be silently dropped here since only
    # `rows` is kept from Phase 1's return value, not its run_summary).
    _cache_usage_before = _cache_usage_counts_snapshot() if config.use_enrichment_cache else None

    rows, c5_counts = _run_hq_and_c5_screening(
        df, config, serper_api_key, anthropic_api_key,
        c5_enabled=c5_enabled, c5_scoring_behavior=c5_scoring_behavior,
        c5_scope=c5_scope, c5_model_used=c5_model_used, c5_model_tier=c5_model_tier,
        phase_labels=FOREIGN_HQ_ONLY_PHASE_LABELS, progress_callback=progress_callback,
    )

    gated = _run_gated_full_enrichment(
        rows, config, serper_api_key, anthropic_api_key,
        c5_enabled=c5_enabled,
        prioritize_kwargs={"run_full_v2_pipeline": True},
        phase_labels=FOREIGN_HQ_ONLY_PHASE_LABELS,
        progress_callback=progress_callback,
        run_deep_dive_step=False,
    )
    out_rows = gated["out_rows"]
    evidence_rows = gated["evidence_rows"]
    signal_rows = gated["signal_rows"]
    attempted = gated["attempted"]
    skipped = gated["skipped"]
    confirmed = gated["confirmed"]

    success_count = sum(1 for r in out_rows if r.get("run_success", True))
    error_count = len(out_rows) - success_count

    run_summary = build_run_summary_dataframe(
        config, total_input_rows=len(df), selected_rows=len(rows),
        processed_rows=len(rows), success_count=success_count, error_count=error_count,
    )
    run_summary["total_processed_rows"] = len(rows)
    run_summary["full_enrichment_attempted_count"] = attempted
    run_summary["full_enrichment_skipped_count"] = skipped
    run_summary["confirmed_foreign_hq_count"] = confirmed
    run_summary = add_c5_summary_fields(
        run_summary,
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts=c5_counts,
    )
    run_summary = _apply_cache_run_summary_counts(run_summary, _cache_usage_before)

    return {
        "enriched_leads": pd.DataFrame(out_rows),
        "evidence": pd.DataFrame(evidence_rows),
        "signals": pd.DataFrame(signal_rows),
        "run_summary": run_summary,
    }


# ---------------------------------------------------------------------------
# Parent-HQ language/market classifier (country-agnostic filter layer)
# ---------------------------------------------------------------------------
#
# A small, standalone classification layer used by the non-English foreign-HQ
# mode. It knows nothing about any specific input country, HQ detection, C4,
# or C5 — it only maps a parent-HQ country string to a market bucket, so it
# stays reusable for any future "confirmed foreign-HQ in market X" mode. The
# "is this a good export candidate" gate (input country vs. parent country)
# lives in ``run_batch_non_english_foreign_hq_only``, not here.

_ENGLISH_SPEAKING_PARENT_COUNTRIES = frozenset({
    "australia", "united states", "usa", "us", "united kingdom", "uk",
    "canada", "new zealand", "ireland",
})

_NON_ENGLISH_SPEAKING_PARENT_COUNTRIES = frozenset({
    "japan", "china", "south korea", "korea", "germany", "france", "italy",
    "spain", "netherlands", "belgium", "switzerland", "austria", "sweden",
    "norway", "denmark", "finland", "brazil", "mexico", "argentina", "chile",
    "colombia", "turkey",
})

_REVIEW_PARENT_COUNTRIES = frozenset({
    "singapore", "india", "south africa", "united arab emirates", "uae",
    "hong kong", "malaysia", "philippines", "israel", "middle east",
})

PARENT_HQ_LANGUAGE_MARKET_TYPES = (
    "english_speaking", "non_english_speaking", "review", "unclear",
)


def _normalize_country_token(value) -> str:
    """Lowercase, whitespace-collapsed comparison key for a country string."""
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def classify_parent_hq_language_market(country) -> str:
    """Classify a parent-HQ country into one of ``PARENT_HQ_LANGUAGE_MARKET_TYPES``.

    - ``english_speaking``: Australia, US/USA, UK, Canada, New Zealand, Ireland
      — lower priority as an export trigger.
    - ``non_english_speaking``: e.g. Japan, China, Germany, France, Brazil —
      the commercial trigger this mode looks for.
    - ``review``: nuanced/multilingual markets (Singapore, India, South
      Africa, UAE, Hong Kong, Malaysia, Philippines, Israel, "Middle East")
      that are never auto-classified as non-English.
    - ``unclear``: blank, unrecognised, or otherwise unmapped country text.

    Blank/unknown never defaults to ``non_english_speaking`` — only an exact
    (normalised) match against the non-English list does.
    """
    norm = _normalize_country_token(country)
    if not norm:
        return "unclear"
    if norm in _ENGLISH_SPEAKING_PARENT_COUNTRIES:
        return "english_speaking"
    if norm in _NON_ENGLISH_SPEAKING_PARENT_COUNTRIES:
        return "non_english_speaking"
    if norm in _REVIEW_PARENT_COUNTRIES:
        return "review"
    return "unclear"


def resolve_parent_hq_country_for_export(row: dict) -> str:
    """Pick the parent-HQ country to export/classify: C5 > AI HQ > detected HQ.

    Priority: ``c5_parent_hq_country`` (if present/non-blank), else
    ``ai_parent_hq_country``, else ``hq_detected_country``. Returns "" when
    none are set. The picked value is only whitespace-trimmed here; matching
    against the classifier's lists is case/whitespace-normalised separately.
    """
    for key in ("c5_parent_hq_country", "ai_parent_hq_country", "hq_detected_country"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return ""


# ---------------------------------------------------------------------------
# "Full enrichment, confirmed non-English foreign-HQ only" batch mode
# ---------------------------------------------------------------------------
#
# Australia-specific companion to run_batch_foreign_hq_only: same Phase 1
# (HQ-only screening) + Phase 2 (optional C5) via _run_hq_and_c5_screening —
# no duplicated HQ/C4/C5 logic. Every row is additionally annotated with the
# language/market classification (regardless of country, so the fields stay
# meaningful for audit/export even outside Australia); Phase 3 (full v2
# enrichment) runs ONLY for rows that are simultaneously:
#   1. effective input_country == Australia,
#   2. confirmed foreign-HQ (final post-C4/post-C5 score == 3.0), and
#   3. parent_hq_language_market_type == "non_english_speaking".

_PARALLEL_NON_ENGLISH_COUNT_KEYS = (
    "confirmed_foreign_hq_count", "non_english_foreign_hq_count",
    "english_speaking_parent_hq_count", "review_parent_hq_count",
    "unclear_parent_hq_count", "full_enrichment_attempted_count",
    "full_enrichment_skipped_count",
    "direct_target_count", "manual_review_count",
    "high_priority_manual_review_count", "medium_priority_manual_review_count",
    "excluded_count", "skipped_not_relevant_count",
    "non_english_foreign_hq_review_count",
)


def _non_english_foreign_hq_reason(is_confirmed: bool, market_type: str, parent_country: str) -> str:
    """Country-agnostic explanation for ``non_english_foreign_hq_detected``."""
    if not is_confirmed:
        return "Not confirmed foreign HQ"
    if market_type == "non_english_speaking":
        return f"Confirmed foreign HQ with non-English parent market ({parent_country or 'unknown'})"
    if market_type == "english_speaking":
        return "Parent HQ country is English-speaking/lower-priority for language/training export"
    if market_type == "review":
        return "Parent HQ language market is review/nuanced"
    return "Parent HQ country unclear"


def _non_english_export_decision(row: dict) -> dict:
    """Country-agnostic export-eligibility decision for one HQ-screened row.

    Full enrichment is recommended only when, simultaneously:
      1. input_country is present,
      2. foreign parent/HQ is confirmed (final post-C4/post-C5 score == 3.0),
      3. parent_hq_country is present,
      4. parent_hq_country != input_country (guards against a same-country
         false positive being misread as "foreign"),
      5. parent_hq_language_market_type == "non_english_speaking".

    Works identically for Australia, New Zealand, or any other input country
    — nothing here references a specific country name.
    """
    input_country = str(row.get("input_country") or "").strip()
    is_confirmed = _c5_score(row.get("sig_foreign_hq_score_for_next_scoring")) == 3.0
    parent_country = resolve_parent_hq_country_for_export(row)
    market_type = classify_parent_hq_language_market(parent_country)

    same_as_input = (
        bool(input_country) and bool(parent_country)
        and _normalize_country_token(input_country) == _normalize_country_token(parent_country)
    )
    non_english_detected = is_confirmed and market_type == "non_english_speaking"
    reason = _non_english_foreign_hq_reason(is_confirmed, market_type, parent_country)

    recommended = (
        bool(input_country) and is_confirmed and bool(parent_country)
        and not same_as_input and non_english_detected
    )

    if not input_country:
        skip_reason = "Input country is missing"
    elif not is_confirmed:
        skip_reason = "Not confirmed foreign HQ"
    elif not parent_country:
        skip_reason = "Parent HQ country is missing"
    elif same_as_input:
        skip_reason = "Parent HQ country matches input country (not foreign)"
    elif market_type == "english_speaking":
        skip_reason = "Parent HQ country is English-speaking/lower-priority for language/training export"
    elif market_type == "review":
        skip_reason = "Parent HQ language market is review/nuanced"
    elif market_type == "unclear":
        skip_reason = "Parent HQ country unclear"
    else:
        skip_reason = ""  # recommended

    is_australia = _normalize_country_token(input_country) == "australia"

    return {
        "foreign_hq_detected_for_export": is_confirmed,
        "parent_hq_country_for_export": parent_country,
        "parent_hq_language_market_type": market_type,
        "non_english_foreign_hq_detected": non_english_detected,
        "non_english_foreign_hq_reason": reason,
        "recommended_for_non_english_foreign_hq_export": recommended,
        # Backward-compatible field: same semantics as before (Australia AND
        # eligible). Kept so older consumers/exports don't break, but the
        # generic field above is what now drives full enrichment.
        "recommended_for_australia_export": recommended and is_australia,
        "_skip_reason": skip_reason,
    }


# ---------------------------------------------------------------------------
# Post-C5 export/review buckets for the non-English foreign-HQ mode
# ---------------------------------------------------------------------------
#
# ``_non_english_export_decision`` above answers a single yes/no question
# ("does this row get full v2 enrichment?"). The audit of Australia runs
# showed that "no" was hiding good NEC-style leads: C5 can confirm
# ``foreign_parent_confirmed`` + a target-company match against a
# non-English parent, yet the FINAL post-C4/post-C5 HQ score used to stay 0
# regardless (the conservative C5 scoring rule never auto-upgraded a
# score-0 row). Those rows were being silently dropped into the same "Not
# confirmed foreign HQ" skip bucket as genuine non-matches.
#
# ``_apply_conservative_adjustment`` now upgrades a High/Medium-confidence
# C5 confirmation straight to score 3 (see its docstring — this was the
# Germany-export follow-up fix, so a row's commercial fit score can't be
# computed as HQ-signal-less while it's exported as confirmed foreign HQ),
# which resolves most of these at the score level: a still-score-0 row here
# means C5 either wasn't confident enough to upgrade it or wasn't run for
# this row at all, so this layer's manual-review bucket remains the catch
# for exactly that narrower case, not the general one it started as.
#
# This layer adds a strictly-additive export/review classification on top:
# it never changes C4, never changes C5, and never itself changes
# ``sig_foreign_hq_score_for_next_scoring``. It only decides which
# "bucket" a row is shown in and whether it gets full v2 enrichment
# (still ONLY ``direct_target`` rows do — identical gate to before).

EXPORT_BUCKETS = ("direct_target", "manual_review", "excluded", "skipped_not_relevant")

# Country -> characteristic wrong-country domain suffix, used by the safety
# check below. Only ever consulted for Australia/New Zealand input rows.
_WRONG_COUNTRY_HINTS = {
    "bolivia": ".bo",
    "costa rica": ".cr",
}


def wrong_country_export_exclusion_reason(row: dict) -> str:
    """Light safety net for obvious wrong-country data in AU/NZ export runs.

    Returns a human-readable exclude reason, or ``""`` when nothing looks
    wrong. Only checked when the row's (effective) ``input_country`` is
    Australia or New Zealand — for any other input country this is a no-op.
    """
    input_country = _normalize_country_token(row.get("input_country"))
    if input_country not in ("australia", "new zealand"):
        return ""
    domain = str(row.get("domain") or "").strip().lower()
    company = str(row.get("company_name") or "").strip().lower()
    for name, suffix in _WRONG_COUNTRY_HINTS.items():
        if domain.endswith(suffix):
            return f"Wrong country / {name.title()} domain"
        if name in company:
            return f"Wrong country / {name.title()} in company name"
    return ""


def classify_non_english_foreign_hq_export_row(row: dict) -> dict:
    """Assign export bucket / review fields for one non-English-HQ-mode row.

    Wraps ``_non_english_export_decision`` (the existing, unchanged
    country-agnostic gate) and layers the following on top, in order:

    1. Wrong-country safety exclusion (always checked first; overrides
       everything else) -> ``excluded``.
    2. The existing eligibility decision recommends full enrichment
       -> ``direct_target`` (unchanged gate/condition).
    3. Input country missing, or parent HQ country equals input country
       (genuinely domestic) -> kept as their own edge-case buckets with the
       original diagnostic text (not one of the six numbered rules).
    4. C5 confirmed a non-English foreign parent
       (``c5_adjudication == "foreign_parent_confirmed"`` + a truthy
       ``c5_target_company_match``) but the final score is not 3
       -> ``manual_review`` / high priority (the NEC-style miss this layer
       exists to fix).
    5. Parent HQ market is English-speaking -> ``skipped_not_relevant``.
    6. Parent HQ market is "review" (nuanced/multilingual) -> ``manual_review``
       / medium priority.
    7. Parent HQ market is "unclear" (blank/unrecognised country)
       -> ``manual_review`` / medium priority.
    8. Anything else left (a non-English parent market that simply isn't
       score-3 and wasn't C5-confirmed) -> ``manual_review`` / medium
       priority, so it stays visible instead of disappearing.

    Never mutates ``row``; never touches C4/C5 output or
    ``sig_foreign_hq_score_for_next_scoring``. Full v2 enrichment remains
    gated on ``export_bucket == "direct_target"`` only.
    """
    decision = _non_english_export_decision(row)
    skip_reason = decision["_skip_reason"]
    market_type = decision["parent_hq_language_market_type"]
    is_confirmed_score3 = decision["foreign_hq_detected_for_export"]
    base = {k: v for k, v in decision.items() if k != "_skip_reason"}

    def _result(export_bucket, *, review=False, review_reason="", priority="",
                excluded=False, exclude_reason="", recommended=None,
                skip=True, reason="") -> dict:
        return {
            **base,
            "export_bucket": export_bucket,
            "non_english_foreign_hq_review": review,
            "non_english_foreign_hq_review_reason": review_reason,
            "review_priority": priority,
            "exclude_from_export": excluded,
            "exclude_reason": exclude_reason,
            "recommended_for_non_english_foreign_hq_export": (
                base["recommended_for_non_english_foreign_hq_export"]
                if recommended is None else recommended),
            "enrichment_skipped": skip,
            "enrichment_skip_reason": reason,
        }

    wrong_country_reason = wrong_country_export_exclusion_reason(row)
    if wrong_country_reason:
        return _result("excluded", excluded=True, exclude_reason=wrong_country_reason,
                        recommended=False, skip=True, reason=wrong_country_reason)

    if decision["recommended_for_non_english_foreign_hq_export"]:
        return _result("direct_target", recommended=True, skip=False, reason="")

    if skip_reason == "Input country is missing":
        return _result("manual_review", priority="medium", recommended=False,
                        skip=True, reason=skip_reason)

    if skip_reason == "Parent HQ country matches input country (not foreign)":
        return _result("skipped_not_relevant", recommended=False, skip=True, reason=skip_reason)

    c5_adjudication = str(row.get("c5_adjudication") or "").strip().lower()
    c5_confirmed_foreign_parent = (
        c5_adjudication == "foreign_parent_confirmed"
        and _c5_truthy(row.get("c5_target_company_match")))

    if (not is_confirmed_score3 and c5_confirmed_foreign_parent
            and market_type == "non_english_speaking"):
        return _result(
            "manual_review", review=True, priority="high",
            review_reason="C5 confirmed a non-English foreign parent, but final HQ score was not 3.",
            recommended=False, skip=True,
            reason="Manual review: C5 confirmed non-English foreign parent, but final HQ score was not 3")

    if market_type == "english_speaking":
        return _result(
            "skipped_not_relevant", recommended=False, skip=True,
            reason="Parent HQ country is English-speaking/lower-priority for this non-English foreign-HQ run")

    if market_type == "review":
        return _result(
            "manual_review", priority="medium", recommended=False, skip=True,
            reason="Parent HQ country is review/nuanced for language-market trigger")

    if market_type == "unclear":
        return _result(
            "manual_review", priority="medium", recommended=False, skip=True,
            reason="Parent HQ country or language-market type unclear")

    # Non-English parent market, but not eligible for direct_target or the
    # high-priority C5-confirmed manual review above (e.g. C5 disabled, or
    # C5 did not confirm a foreign parent for the target company). Still
    # worth a medium-priority look rather than a silent drop.
    return _result("manual_review", priority="medium", recommended=False, skip=True,
                    reason=skip_reason or "Not confirmed foreign HQ")


def run_batch_non_english_foreign_hq_only(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the "Full enrichment, confirmed non-English foreign-HQ only" mode.

    Shares Phase 1/2 (HQ-only screening + optional C5) with
    ``run_batch_foreign_hq_only`` via ``_run_hq_and_c5_screening``. Every row
    gets the export/classification fields (``foreign_hq_detected_for_export``,
    ``parent_hq_country_for_export``, ``parent_hq_language_market_type``,
    ``non_english_foreign_hq_detected``, ``non_english_foreign_hq_reason``,
    ``recommended_for_non_english_foreign_hq_export``) regardless of country,
    so the classifier stays reusable/auditable for any input country.

    Full v2 enrichment (Phase 3) runs ONLY for rows where
    ``recommended_for_non_english_foreign_hq_export`` is True — see
    ``_non_english_export_decision`` for the exact (country-agnostic) gate:
    input_country present, foreign HQ confirmed (final post-C4/post-C5 score
    3.0), a parent HQ country present and different from input_country, and
    that parent country in a non-English-speaking market. Works identically
    for Australia, New Zealand, or any other input country. All other rows
    are kept, unenriched, with ``enrichment_skipped=True`` and a clear skip
    reason. ``recommended_for_australia_export`` is kept for backward
    compatibility only (same old Australia-specific semantics); it is no
    longer what drives full enrichment.

    Returns the same dict shape as ``run_batch_foreign_hq_only``.
    """
    rows, c5_counts = _run_hq_and_c5_screening(
        df, config, serper_api_key, anthropic_api_key,
        c5_enabled=c5_enabled, c5_scoring_behavior=c5_scoring_behavior,
        c5_scope=c5_scope, c5_model_used=c5_model_used, c5_model_tier=c5_model_tier,
        phase_labels=NON_ENGLISH_FOREIGN_HQ_ONLY_PHASE_LABELS,
        progress_callback=progress_callback,
    )

    # ── Classify every row first (country-agnostic; needed for both the
    # export/review buckets and the Phase-3 eligibility gate) ────────────────
    decisions: list[dict] = []
    confirmed_count = non_english_count = 0
    english_count = review_count = unclear_count = 0
    bucket_counts = {b: 0 for b in EXPORT_BUCKETS}
    high_priority_review_count = medium_priority_review_count = 0
    non_english_foreign_hq_review_count = 0
    for row in rows:
        classification = classify_non_english_foreign_hq_export_row(row)
        if classification["foreign_hq_detected_for_export"]:
            confirmed_count += 1
        market_type = classification["parent_hq_language_market_type"]
        if market_type == "english_speaking":
            english_count += 1
        elif market_type == "review":
            review_count += 1
        elif market_type == "unclear":
            unclear_count += 1
        if classification["non_english_foreign_hq_detected"]:
            non_english_count += 1
        bucket_counts[classification["export_bucket"]] += 1
        if classification["review_priority"] == "high":
            high_priority_review_count += 1
        elif classification["review_priority"] == "medium":
            medium_priority_review_count += 1
        if classification["non_english_foreign_hq_review"]:
            non_english_foreign_hq_review_count += 1
        decisions.append(classification)

    total_eligible = bucket_counts["direct_target"]

    out_rows: list[dict] = []
    evidence_rows: list[dict] = []
    signal_rows: list[dict] = []
    attempted = skipped = 0
    p3_success = p3_error = 0

    for row, classification in zip(rows, decisions):
        out_row = dict(row)
        out_row.update(classification)

        if classification["export_bucket"] != "direct_target":
            out_rows.append(out_row)
            skipped += 1
            continue

        attempted += 1
        company = str(row.get(config.company_name_column, "") or "").strip()
        domain = resolve_row_domain(row, config)
        country = None
        if config.input_country_column:
            country = str(row.get(config.input_country_column, "") or "").strip() or None
        source_index = row.get("source_index")

        try:
            result = prioritize_single_lead(
                LeadInput(company_name=company, domain=domain, input_country=country,
                          **_lusha_fields_from_row(row)),
                serper_api_key=serper_api_key,
                anthropic_api_key=anthropic_api_key,
                default_input_country=config.default_input_country,
                run_full_v2_pipeline=True,
            )
            out_row = flatten_result_for_excel(
                result, out_row, source_index, True, "", config.include_raw_ai_json,
            )
            # See _run_gated_full_enrichment's identical restore: this Phase-3 call
            # redoes its own HQ (re)detection internally and would otherwise silently
            # overwrite the authoritative Phase 1+2 score (including any C5 upgrade)
            # this row was already gated into full enrichment on.
            out_row["sig_foreign_hq_score_for_next_scoring"] = row.get(
                "sig_foreign_hq_score_for_next_scoring")
            evidence_rows.extend(flatten_evidence_for_excel(result, source_index))
            signal_rows.extend(flatten_signals_for_excel(result, source_index))
            p3_success += 1
        except Exception as exc:  # per-row isolation, matching run_batch_foreign_hq_only
            out_row["run_success"] = False
            out_row["run_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            p3_error += 1

        out_row["enrichment_skipped"] = False
        out_row["enrichment_skip_reason"] = ""
        out_rows.append(out_row)

        _emit_batch_phase_progress(
            progress_callback, NON_ENGLISH_FOREIGN_HQ_ONLY_PHASE_LABELS, 3,
            attempted, total_eligible, {
                "success_count": p3_success,
                "error_count": p3_error,
                "current_company_name": company,
            })

    success_count = sum(1 for r in out_rows if r.get("run_success", True))
    error_count = len(out_rows) - success_count

    run_summary = build_run_summary_dataframe(
        config, total_input_rows=len(df), selected_rows=len(rows),
        processed_rows=len(rows), success_count=success_count, error_count=error_count,
    )
    run_summary["confirmed_foreign_hq_count"] = confirmed_count
    run_summary["non_english_foreign_hq_count"] = non_english_count
    run_summary["english_speaking_parent_hq_count"] = english_count
    run_summary["review_parent_hq_count"] = review_count
    run_summary["unclear_parent_hq_count"] = unclear_count
    run_summary["full_enrichment_attempted_count"] = attempted
    run_summary["full_enrichment_skipped_count"] = skipped
    run_summary["direct_target_count"] = bucket_counts["direct_target"]
    run_summary["manual_review_count"] = bucket_counts["manual_review"]
    run_summary["high_priority_manual_review_count"] = high_priority_review_count
    run_summary["medium_priority_manual_review_count"] = medium_priority_review_count
    run_summary["excluded_count"] = bucket_counts["excluded"]
    run_summary["skipped_not_relevant_count"] = bucket_counts["skipped_not_relevant"]
    run_summary["non_english_foreign_hq_review_count"] = non_english_foreign_hq_review_count
    run_summary = add_c5_summary_fields(
        run_summary,
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts=c5_counts,
    )

    return {
        "enriched_leads": pd.DataFrame(out_rows),
        "evidence": pd.DataFrame(evidence_rows),
        "signals": pd.DataFrame(signal_rows),
        "run_summary": run_summary,
    }


# ---------------------------------------------------------------------------
# Optional parallel chunk processing (Streamlit batch app)
# ---------------------------------------------------------------------------
#
# Splits the selected rows into approximately equal chunks and runs each chunk
# through run_single_batch_unit — the exact same code path a normal smaller
# batch takes (run_batch_dataframe + optional C5, or run_batch_foreign_hq_only)
# — on a thread pool (API-bound work). No scoring/C4/C5 logic lives here; the
# sequential paths are untouched.

MAX_PARALLEL_WORKERS = 4

# How often (seconds) the parallel runner's main-thread polling loop wakes up
# to emit a progress event even if no chunk has finished yet. Keeps the UI
# from looking frozen during long chunks. Overridable per-call for tests.
DEFAULT_PARALLEL_HEARTBEAT_INTERVAL_SECONDS = 3.0

_PARALLEL_C5_COUNT_KEYS = (
    "c5_rows_attempted", "c5_success_count", "c5_error_count",
    "c5_foreign_parent_confirmed_count", "c5_domestic_confirmed_count",
    "c5_unclear_count", "c5_recommended_score_3_count",
    "c5_possible_foreign_parent_for_review_count", "c5_downgraded_score_3_count",
)
_PARALLEL_FHO_COUNT_KEYS = (
    "total_processed_rows", "full_enrichment_attempted_count",
    "full_enrichment_skipped_count", "confirmed_foreign_hq_count",
)
# Deduplicated union of every non-C5 mode-specific summary count key (FHO and
# non-English-FHO share several key names — a plain concatenation would
# double-count them when aggregating across parallel chunks).
_PARALLEL_MODE_COUNT_KEYS = tuple(sorted(
    set(_PARALLEL_FHO_COUNT_KEYS) | set(_PARALLEL_NON_ENGLISH_COUNT_KEYS)
))


def split_dataframe_into_chunks(df: pd.DataFrame, n_chunks: int) -> list:
    """Split a DataFrame into at most ``n_chunks`` order-preserving chunks.

    Chunk sizes differ by at most one row; the original index is preserved.
    Never produces empty chunks (fewer rows than chunks → fewer chunks).
    """
    n = len(df)
    if n == 0:
        return []
    k = max(1, min(int(n_chunks or 1), n))
    base, extra = divmod(n, k)
    chunks, start = [], 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        chunks.append(df.iloc[start:start + size])
        start += size
    return chunks


def run_single_batch_unit(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    openai_api_key: str = "",
    firecrawl_api_key: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run one batch unit through the same code path as a sequential run.

    ``full_foreign_hq_only`` delegates to ``run_batch_foreign_hq_only`` and
    ``full_non_english_foreign_hq_only`` delegates to
    ``run_batch_non_english_foreign_hq_only`` (both apply C5 internally);
    every other mode runs ``run_batch_dataframe`` plus the same optional C5
    post-step and Run Summary extension the Streamlit app performs
    sequentially. Used by the parallel runner for each chunk.

    ``config.gate_full_enrichment_on_foreign_hq`` needs NO extra plumbing
    here: ``run_batch_dataframe`` itself branches on that flag (see
    ``_run_batch_dataframe_gated``), so every existing caller — including
    this parallel path — picks up the opt-in gate automatically for the
    regular run modes, by just passing the same ``config`` through unchanged.

    When ``config.gate_full_enrichment_on_foreign_hq`` AND ``c5_enabled`` are
    both True, C5 is passed straight through to ``run_batch_dataframe`` so it
    runs INSIDE Phase 1/2, before the Phase 3 eligibility decision (see
    ``_run_batch_dataframe_gated``) — a row C5 confirms as foreign-HQ becomes
    eligible for full enrichment there, instead of being stuck
    ``enrichment_skipped=True`` with only C5's own fields added by a flat
    post-step. The post-step C5 application below is therefore SKIPPED in
    this combination (it would otherwise be a second, redundant C5 pass over
    a mix of fully-enriched and gate-skipped rows) — ``run_summary`` already
    carries the C5 settings/counts columns from the gated path.
    """
    if config.run_mode == FOREIGN_HQ_ONLY_MODE:
        return run_batch_foreign_hq_only(
            df, config, serper_api_key, anthropic_api_key,
            c5_enabled=c5_enabled,
            c5_scoring_behavior=c5_scoring_behavior,
            c5_scope=c5_scope,
            c5_model_used=c5_model_used,
            c5_model_tier=c5_model_tier,
            progress_callback=progress_callback,
        )
    if config.run_mode == NON_ENGLISH_FOREIGN_HQ_ONLY_MODE:
        return run_batch_non_english_foreign_hq_only(
            df, config, serper_api_key, anthropic_api_key,
            c5_enabled=c5_enabled,
            c5_scoring_behavior=c5_scoring_behavior,
            c5_scope=c5_scope,
            c5_model_used=c5_model_used,
            c5_model_tier=c5_model_tier,
            progress_callback=progress_callback,
        )

    gated_with_c5 = config.gate_full_enrichment_on_foreign_hq and c5_enabled
    tables = run_batch_dataframe(
        df, config, serper_api_key, anthropic_api_key,
        progress_callback=progress_callback,
        openai_api_key=openai_api_key,
        firecrawl_api_key=firecrawl_api_key,
        **(
            dict(c5_enabled=True, c5_scoring_behavior=c5_scoring_behavior,
                 c5_scope=c5_scope, c5_model_used=c5_model_used,
                 c5_model_tier=c5_model_tier)
            if gated_with_c5 else {}
        ),
    )
    if gated_with_c5:
        # C5 already ran inside the gated Phase 1/2 above (see the docstring)
        # -- run_summary already carries the C5 settings/counts columns, and
        # re-running C5 as a flat post-step here would double the API calls
        # for score-3/manual-review rows.
        return tables

    c5_counts: dict = {}
    if c5_enabled:
        rows, c5_counts = apply_c5_adjudication(
            tables["enriched_leads"],
            anthropic_api_key=anthropic_api_key,
            model_used=c5_model_used,
            model_tier=c5_model_tier,
            scoring_behavior=c5_scoring_behavior,
            scope=c5_scope,
            include_raw=config.include_raw_ai_json,
        )
        tables["enriched_leads"] = pd.DataFrame(rows)
    tables["run_summary"] = add_c5_summary_fields(
        tables["run_summary"],
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts=c5_counts,
    )
    return tables


def aggregate_parallel_chunk_progress(
    chunk_snapshot: dict,
    reports: list,
    *,
    total_selected_rows: int = 0,
) -> dict:
    """Pure aggregation of per-chunk progress into one summary dict.

    ``chunk_snapshot`` maps chunk index (0-based) to a live progress entry —
    typically a lock-copied snapshot of shared state written by row/phase
    progress callbacks running on worker threads (see
    ``run_batch_dataframe_parallel``). ``reports`` is the same list the
    parallel runner tracks per chunk (``success`` is ``None`` while running,
    ``True``/``False`` once finished). No threading, no I/O — safe to call
    from the main thread and safe to unit test directly with hand-built
    dicts.

    For a finished chunk, ``row_count`` (from ``reports``) is trusted as the
    processed count so completed chunks are never under/over-counted; for a
    still-running chunk, the live snapshot's ``processed`` count is used.
    Returns ``processed_rows`` / ``success_count`` / ``error_count`` (summed
    across all chunks), ``current_company_name`` (from the most recently
    updated still-running chunk), and ``active_chunks`` (one summary dict per
    still-running chunk: index, processed/selected, phase info if available,
    current company).
    """
    processed = success = error = 0
    active_chunks: list = []
    latest_company = ""
    latest_ts = -1.0

    for i, report in enumerate(reports):
        entry = chunk_snapshot.get(i) or {}
        row_count = int(report.get("row_count", 0) or 0)
        finished = report.get("success") is not None

        if finished and not report.get("success"):
            # Failed chunk: its rows become placeholder error rows downstream.
            processed += row_count
            error += row_count
            continue
        if finished:
            processed += row_count
            success += int(entry.get("success", row_count) or 0)
            error += int(entry.get("error", 0) or 0)
            continue

        entry_processed = int(entry.get("processed", 0) or 0)
        entry_success = int(entry.get("success", 0) or 0)
        entry_error = int(entry.get("error", 0) or 0)
        processed += entry_processed
        success += entry_success
        error += entry_error
        active_chunks.append({
            "chunk_index": report.get("chunk_index", i + 1),
            "processed": entry_processed,
            "selected": int(entry.get("selected") or row_count),
            "phase": entry.get("phase"),
            "phase_label": entry.get("phase_label"),
            "phase_processed": entry.get("phase_processed"),
            "phase_total": entry.get("phase_total"),
            "current_company_name": entry.get("current_company_name") or "",
        })
        ts = entry.get("last_update") or -1.0
        if entry.get("current_company_name") and ts >= latest_ts:
            latest_ts = ts
            latest_company = entry.get("current_company_name")

    return {
        "processed_rows": processed,
        "selected_rows": total_selected_rows,
        "success_count": success,
        "error_count": error,
        "current_company_name": latest_company,
        "active_chunks": active_chunks,
        "chunks_active_count": len(active_chunks),
    }


def run_batch_dataframe_parallel(
    df: pd.DataFrame,
    config: BatchRunConfig,
    serper_api_key: str,
    anthropic_api_key: str,
    *,
    workers: int,
    c5_enabled: bool = False,
    c5_scoring_behavior: str = "append_only",
    c5_scope: str = "score_3_or_manual_review",
    c5_model_used: str = "",
    c5_model_tier: str = "",
    openai_api_key: str = "",
    firecrawl_api_key: str = "",
    progress_callback: Optional[Callable[[dict], None]] = None,
    chunk_result_callback: Optional[Callable[[dict, dict], None]] = None,
    heartbeat_interval_seconds: float = DEFAULT_PARALLEL_HEARTBEAT_INTERVAL_SECONDS,
) -> dict:
    """Run the selected rows as parallel chunks and combine the outputs.

    - ``workers`` is capped at ``MAX_PARALLEL_WORKERS`` (and at the row count).
    - Row selection (start_row/row_limit), C5 config, and country behavior are
      identical to a sequential run; each chunk runs with selection already
      applied (chunk config gets ``start_row=0, row_limit=0``).
    - Output has the same table structure as ``run_batch_dataframe`` plus a
      ``chunk_reports`` list; Enriched Leads preserves the original selected-row
      order. A failed chunk contributes placeholder error rows and never
      discards other chunks' results.

    Progress reporting: each chunk's ``run_single_batch_unit`` gets its own
    row/phase-level ``progress_callback`` (reused, not duplicated — the same
    callback ``run_batch_dataframe`` / ``run_batch_foreign_hq_only`` /
    ``run_batch_non_english_foreign_hq_only`` already support). That callback
    runs on the WORKER thread, so it only ever writes into a
    ``threading.Lock``-protected shared snapshot — it never touches Streamlit
    or any other main-thread-only API. The MAIN thread polls
    ``concurrent.futures.wait(..., timeout=heartbeat_interval_seconds)`` in a
    loop: on every wake-up (whether a chunk just finished or the timeout
    merely elapsed with nothing done yet — a "heartbeat") it aggregates the
    shared snapshot via ``aggregate_parallel_chunk_progress`` and invokes the
    caller-supplied ``progress_callback`` — always from the main thread, so
    it's safe for that callback to call ``st.*``.

    ``progress_callback`` payloads always include: ``parallel_chunks_total``,
    ``parallel_chunks_completed``, ``parallel_workers``, ``heartbeat`` (True
    for a no-completion wake-up), ``selected_rows``, ``processed_rows``,
    ``success_count``, ``error_count``, ``current_company_name``,
    ``active_chunks`` (per-still-running-chunk summaries, including phase
    info when the mode is phase-based). Chunk-completion events additionally
    include ``chunk_index`` / ``chunk_row_count`` / ``chunk_success`` /
    ``chunk_error`` (same keys as before — existing consumers keep working).
    ``chunk_result_callback(report, tables)`` fires (main thread) for each
    successful chunk, e.g. for checkpoint autosave. Both callbacks are
    exception-proofed.
    """
    workers = max(1, min(int(workers or 1), MAX_PARALLEL_WORKERS))
    selected = select_batch_rows(df, config)
    chunks = split_dataframe_into_chunks(selected, workers)
    chunk_config = replace(config, start_row=0, row_limit=0)

    reports: list[dict] = [{
        "chunk_index": i + 1,
        "row_count": len(chunk),
        "source_index_first": chunk.index[0],
        "source_index_last": chunk.index[-1],
        "success": None,
        "error": "",
    } for i, chunk in enumerate(chunks)]

    results: list = [None] * len(chunks)
    completed = 0

    # ── Thread-safe shared per-chunk live progress ────────────────────────────
    # Written from worker threads (via each chunk's own progress_callback);
    # only ever read from the main thread, and only through ``_snapshot()``.
    progress_lock = threading.Lock()
    chunk_progress: dict = {
        i: {
            "processed": 0, "selected": len(chunk), "success": 0, "error": 0,
            "current_company_name": "", "phase": None, "phase_label": None,
            "phase_processed": 0, "phase_total": 0, "last_update": time.time(),
        }
        for i, chunk in enumerate(chunks)
    }

    def _make_chunk_progress_callback(chunk_idx: int):
        def _on_chunk_row_progress(payload: dict) -> None:
            # Runs on a WORKER thread — must never call Streamlit or anything
            # else that assumes the main thread. Only touches the lock-
            # protected shared snapshot.
            with progress_lock:
                entry = chunk_progress[chunk_idx]
                if "processed_rows" in payload:
                    entry["processed"] = int(payload.get("processed_rows") or 0)
                if "selected_rows" in payload:
                    entry["selected"] = int(payload.get("selected_rows") or 0)
                if "success_count" in payload:
                    entry["success"] = int(payload.get("success_count") or 0)
                if "error_count" in payload:
                    entry["error"] = int(payload.get("error_count") or 0)
                if payload.get("current_company_name"):
                    entry["current_company_name"] = payload["current_company_name"]
                if "phase" in payload:
                    entry["phase"] = payload.get("phase")
                    entry["phase_label"] = payload.get("phase_label")
                    entry["phase_processed"] = payload.get("phase_processed")
                    entry["phase_total"] = payload.get("phase_total")
                entry["last_update"] = time.time()
        return _on_chunk_row_progress

    def _snapshot() -> dict:
        with progress_lock:
            return {i: dict(v) for i, v in chunk_progress.items()}

    def _emit(*, heartbeat: bool, chunk_report: Optional[dict] = None) -> None:
        if progress_callback is None:
            return
        agg = aggregate_parallel_chunk_progress(
            _snapshot(), reports, total_selected_rows=len(selected))
        payload = {
            "parallel_chunks_total": len(chunks),
            "parallel_chunks_completed": completed,
            "parallel_workers": workers,
            "heartbeat": heartbeat,
            **agg,
        }
        if chunk_report is not None:
            payload["chunk_index"] = chunk_report["chunk_index"]
            payload["chunk_row_count"] = chunk_report["row_count"]
            payload["chunk_success"] = chunk_report["success"]
            payload["chunk_error"] = chunk_report["error"]
        try:
            progress_callback(payload)
        except Exception:
            pass  # a broken callback must never break the run

    if chunks:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_single_batch_unit, chunk, chunk_config,
                    serper_api_key, anthropic_api_key,
                    c5_enabled=c5_enabled,
                    c5_scoring_behavior=c5_scoring_behavior,
                    c5_scope=c5_scope,
                    c5_model_used=c5_model_used,
                    c5_model_tier=c5_model_tier,
                    openai_api_key=openai_api_key,
                    firecrawl_api_key=firecrawl_api_key,
                    progress_callback=_make_chunk_progress_callback(i),
                ): i
                for i, chunk in enumerate(chunks)
            }
            pending = set(futures)
            while pending:
                done, pending = wait(
                    pending, timeout=heartbeat_interval_seconds,
                    return_when=FIRST_COMPLETED)
                if not done:
                    _emit(heartbeat=True)  # timeout elapsed, nothing finished yet
                    continue
                for future in done:
                    i = futures[future]
                    try:
                        results[i] = future.result()
                        reports[i]["success"] = True
                    except Exception as exc:  # chunk-level isolation
                        reports[i]["success"] = False
                        reports[i]["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    completed += 1
                    _emit(heartbeat=False, chunk_report=reports[i])
                    if reports[i]["success"] and chunk_result_callback is not None:
                        try:
                            chunk_result_callback(dict(reports[i]), results[i])
                        except Exception:
                            pass  # checkpoint saving must never break the run

    # ── Combine in chunk order (original selected-row order) ─────────────────
    enriched_frames, evidence_frames, signal_frames, deep_dive_frames = [], [], [], []
    processed = success = error = 0
    for i, chunk in enumerate(chunks):
        if reports[i]["success"]:
            tables = results[i]
            enriched_frames.append(tables["enriched_leads"])
            if len(tables["evidence"]):
                evidence_frames.append(tables["evidence"])
            if len(tables["signals"]):
                signal_frames.append(tables["signals"])
            # Not every mode in the foreign-HQ-only family produces a
            # "deep_dive" table — only run_batch_dataframe does.
            dd_table = tables.get("deep_dive")
            if dd_table is not None and len(dd_table):
                deep_dive_frames.append(dd_table)
            summary = tables["run_summary"].iloc[0].to_dict() if len(tables["run_summary"]) else {}
            processed += int(summary.get("processed_rows", 0) or 0)
            success += int(summary.get("success_count", 0) or 0)
            error += int(summary.get("error_count", 0) or 0)
        else:
            # Placeholder error rows so the failed chunk's rows stay visible.
            placeholder = []
            for idx, row in chunk.iterrows():
                out = row.to_dict()
                out["source_index"] = idx
                out["run_success"] = False
                out["run_error"] = f"parallel_chunk_failed: {reports[i]['error']}"
                placeholder.append(out)
            enriched_frames.append(pd.DataFrame(placeholder))
            error += len(chunk)

    enriched = pd.concat(enriched_frames, ignore_index=True) if enriched_frames else pd.DataFrame()
    evidence = pd.concat(evidence_frames, ignore_index=True) if evidence_frames else pd.DataFrame()
    signals = pd.concat(signal_frames, ignore_index=True) if signal_frames else pd.DataFrame()
    deep_dive = pd.concat(deep_dive_frames, ignore_index=True) if deep_dive_frames else pd.DataFrame()

    # ── Combined Run Summary ──────────────────────────────────────────────────
    run_summary = build_run_summary_dataframe(
        config, total_input_rows=len(df), selected_rows=len(selected),
        processed_rows=processed, success_count=success, error_count=error,
    )
    agg: dict = {}
    for i in range(len(chunks)):
        if not reports[i]["success"]:
            continue
        summary = results[i]["run_summary"].iloc[0].to_dict() if len(results[i]["run_summary"]) else {}
        for key in _PARALLEL_C5_COUNT_KEYS + _PARALLEL_MODE_COUNT_KEYS:
            if key in summary:
                try:
                    agg[key] = agg.get(key, 0) + int(summary.get(key) or 0)
                except (TypeError, ValueError):
                    pass
    run_summary = add_c5_summary_fields(
        run_summary,
        c5_enabled=c5_enabled,
        c5_scoring_behavior=c5_scoring_behavior if c5_enabled else "",
        c5_scope=c5_scope if c5_enabled else "",
        c5_model_tier=c5_model_tier if c5_enabled else "",
        c5_model_used=c5_model_used if c5_enabled else "",
        counts={k: v for k, v in agg.items() if k in _PARALLEL_C5_COUNT_KEYS},
    )
    for key in _PARALLEL_MODE_COUNT_KEYS:
        if key in agg:
            run_summary[key] = agg[key]

    sizes = [len(c) for c in chunks]
    run_summary["parallel_processing_enabled"] = True
    run_summary["parallel_workers"] = workers
    run_summary["parallel_chunk_count"] = len(chunks)
    run_summary["parallel_chunk_size_min"] = min(sizes) if sizes else 0
    run_summary["parallel_chunk_size_max"] = max(sizes) if sizes else 0
    run_summary["parallel_failed_chunk_count"] = sum(
        1 for r in reports if r["success"] is False)
    run_summary["parallel_successful_chunk_count"] = sum(
        1 for r in reports if r["success"] is True)

    return {
        "enriched_leads": enriched,
        "evidence": evidence,
        "signals": signals,
        "deep_dive": deep_dive,
        "run_summary": run_summary,
        "chunk_reports": reports,
    }
