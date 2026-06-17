"""
mYngle · Single-company enrichment API
======================================

Run locally:
    uvicorn api_enrichment_server:app --reload --port 8008

Example request:
    curl -X POST http://127.0.0.1:8008/api/enrich/company \
      -H "Content-Type: application/json" \
      -d '{"company_name":"Example S.p.A.","domain":"example.com","country":"Italy"}'

Secrets:
    ANTHROPIC_API_KEY  — required for Step 1 + Step 2 (Claude)
    SERPER_API_KEY     — required for Step 2 Google search

Never expose these in responses or logs.
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Logging (server-side only, no secrets) ────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api_enrichment")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="mYngle Enrichment API", version="0.1.0")

# Allow the future Lovable/React frontend to call this from localhost or
# a preview URL.  Tighten origins before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Lazy imports from the enricher (heavy module — load once at first request) ─
_enricher_loaded = False
_enrich_one_row = None
_resolve_employee_range = None
_resolve_employee_range_from_serper = None
_apply_results_compatible_scoring = None
_apply_competitor_icp_override = None
_EMPLOYEE_RANGE_RESOLVER_FIELDS: list[str] = []
_DEFAULT_EMPLOYEE_RANGE_FOR_SCORING: str = "51 - 200"
_ALL_ENRICHMENT_FIELDS: list[str] = []
_normalize_url = None
_clean_domain = None


def _load_enricher() -> None:
    global _enricher_loaded, _enrich_one_row
    global _resolve_employee_range, _resolve_employee_range_from_serper
    global _apply_results_compatible_scoring, _apply_competitor_icp_override
    global _EMPLOYEE_RANGE_RESOLVER_FIELDS, _DEFAULT_EMPLOYEE_RANGE_FOR_SCORING
    global _ALL_ENRICHMENT_FIELDS, _normalize_url, _clean_domain

    if _enricher_loaded:
        return

    import enrich_clients_claude as _ec  # type: ignore
    _enrich_one_row                    = _ec.enrich_one_row
    _resolve_employee_range            = _ec.resolve_employee_range
    _resolve_employee_range_from_serper = _ec.resolve_employee_range_from_serper
    _apply_results_compatible_scoring  = _ec.apply_results_compatible_scoring
    _apply_competitor_icp_override     = _ec.apply_competitor_icp_override
    _EMPLOYEE_RANGE_RESOLVER_FIELDS    = _ec.EMPLOYEE_RANGE_RESOLVER_FIELDS
    _DEFAULT_EMPLOYEE_RANGE_FOR_SCORING = _ec.DEFAULT_EMPLOYEE_RANGE_FOR_SCORING
    _ALL_ENRICHMENT_FIELDS             = _ec.ALL_ENRICHMENT_FIELDS
    _normalize_url                     = _ec.normalize_url
    _clean_domain                      = _ec.clean_domain
    _enricher_loaded = True
    log.info("Enricher module loaded.")


# ── Request / response models ─────────────────────────────────────────────────
class EnrichRequest(BaseModel):
    company_name: str
    domain: str = ""
    url: str = ""
    country: str = ""
    notes: str = ""
    scoring_profile: str = "default"


# ── Priority output fields (always present in response, even if empty) ────────
_PRIORITY_FIELDS = [
    "company_name",
    "domain",
    "validated_domain",
    "canonical_company_domain",
    "canonical_company_url",
    "country",
    "industry",
    "employee_range",
    "employee_range_for_scoring",
    "commercial_fit_score",
    "commercial_tier",
    "outreach_readiness_status",
    "icp_why_relevant",
    "top_positive_signals",
    "gaps_missing_signals",
    "icp_evidence",
    "raw_evidence_summary",
    "caller_angle",
    "raw_google_evidence_json",
    "raw_google_evidence_json_01",
    "raw_google_evidence_json_02",
    "raw_google_evidence_json_03",
    "raw_google_evidence_count",
    "raw_google_evidence_urls",
    "input_company_name",
    "input_domain",
    "input_url",
    "input_country",
    "input_notes",
    "enrichment_source",
    "enriched_at",
]


def _safe_str(v: Any) -> str:
    """Convert any value to a JSON-safe string, never None."""
    if v is None or (isinstance(v, float) and str(v) == "nan"):
        return ""
    return str(v)


def _build_row_response(
    raw_row: dict,
    scoring_profile: str,
    req: EnrichRequest,
) -> dict:
    """
    Apply employee-range resolution + scoring + competitor-ICP override to the
    raw dict returned by enrich_one_row, then assemble the flat response dict.
    """
    # ── Employee range resolution ──────────────────────────────────────────────
    cname  = str(raw_row.get("lusha_company_name") or raw_row.get("company_name") or "")
    domain = str(raw_row.get("canonical_company_domain") or raw_row.get("domain") or "")

    serper_key = os.environ.get("SERPER_API_KEY", "")

    er = _resolve_employee_range(raw_row, company_name=cname)  # type: ignore[misc]

    if er.get("employee_range_confidence") in ("None", "Low") and serper_key:
        er_s = _resolve_employee_range_from_serper(cname, domain, serper_key)  # type: ignore[misc]
        if er_s.get("employee_range_resolved"):
            er = er_s

    if er.get("employee_range_resolved") and er.get("employee_range_confidence") in ("High", "Medium"):
        er["employee_range_for_scoring"]        = er["employee_range_resolved"]
        er["employee_range_for_scoring_source"] = er["employee_range_source"]
    else:
        er["employee_range_for_scoring"]        = _DEFAULT_EMPLOYEE_RANGE_FOR_SCORING
        er["employee_range_for_scoring_source"] = "default_commercial_minimum_assumption"

    for col in _EMPLOYEE_RANGE_RESOLVER_FIELDS:
        raw_row[col] = er.get(col, "")

    # ── Back-fill lusha_employee_range when blank ──────────────────────────────
    if (
        not str(raw_row.get("lusha_employee_range", "")).strip()
        and er.get("employee_range_resolved")
        and er.get("employee_range_confidence") in ("High", "Medium")
    ):
        raw_row["lusha_employee_range"] = er["employee_range_resolved"]

    # ── Scoring + competitor ICP override ─────────────────────────────────────
    df_single = pd.DataFrame([raw_row])
    try:
        df_single = _apply_results_compatible_scoring(df_single, scoring_profile)  # type: ignore[misc]
    except Exception:
        log.warning("Scoring failed: %s", traceback.format_exc())
    try:
        df_single = _apply_competitor_icp_override(df_single)  # type: ignore[misc]
    except Exception:
        log.warning("Competitor ICP override failed: %s", traceback.format_exc())

    scored = df_single.iloc[0].to_dict()

    # ── Map final_commercial_fit_score → commercial_fit_score ─────────────────
    if "commercial_fit_score" not in scored or scored["commercial_fit_score"] == "":
        scored["commercial_fit_score"] = scored.get("final_commercial_fit_score", "")

    # ── Inject request echo + metadata ────────────────────────────────────────
    scored["input_company_name"] = req.company_name
    scored["input_domain"]       = req.domain
    scored["input_url"]          = req.url
    scored["input_country"]      = req.country
    scored["input_notes"]        = req.notes
    scored["enrichment_source"]  = "manual_single"
    scored["enriched_at"]        = datetime.now(timezone.utc).isoformat()

    # ── Build flat output: priority fields first, then remaining enrichment ────
    out: dict[str, Any] = {}
    for f in _PRIORITY_FIELDS:
        out[f] = _safe_str(scored.get(f, ""))

    # Append all other enrichment fields (signal scores, snippet columns, etc.)
    for f in _ALL_ENRICHMENT_FIELDS:
        if f not in out:
            out[f] = _safe_str(scored.get(f, ""))

    # Also carry through any scored columns not in ALL_ENRICHMENT_FIELDS
    for f, v in scored.items():
        if f not in out:
            out[f] = _safe_str(v)

    return out


# ── Route ─────────────────────────────────────────────────────────────────────
@app.post("/api/enrich/company")
async def enrich_company(req: EnrichRequest) -> JSONResponse:
    warnings: list[str] = []

    # ── Validation ────────────────────────────────────────────────────────────
    company_name = req.company_name.strip()
    if not company_name:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "company_name is required", "warnings": []},
        )

    domain = req.domain.strip()
    url    = req.url.strip()

    # Build the raw_url that enrich_one_row expects (prefers url, falls back to domain)
    if url:
        raw_url = url
    elif domain:
        raw_url = f"https://{domain}" if not domain.startswith("http") else domain
    else:
        raw_url = ""
        warnings.append("No domain or URL supplied; enricher will attempt domain discovery.")

    # ── Secrets ───────────────────────────────────────────────────────────────
    api_key    = os.environ.get("ANTHROPIC_API_KEY", "")
    serper_key = os.environ.get("SERPER_API_KEY", "")

    if not api_key:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "ANTHROPIC_API_KEY not configured on server",
                "warnings": [],
            },
        )

    # ── Load enricher (once) ─────────────────────────────────────────────────
    try:
        _load_enricher()
    except Exception:
        log.error("Failed to load enricher: %s", traceback.format_exc())
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "Enricher module failed to load", "warnings": []},
        )

    # ── Enrich ────────────────────────────────────────────────────────────────
    log.info("Enriching company=%r domain=%r url=%r", company_name, domain, url)

    try:
        raw_row, dbg = _enrich_one_row(  # type: ignore[misc]
            company_name=company_name,
            raw_url=raw_url,
            api_key=api_key,
            delay=0.0,
            use_playwright=False,
            search_provider="serper",
            serper_key=serper_key,
            dry_run=False,
            enable_lusha_api=False,
            lusha_api_key="",
            extract_model_signals=True,
            include_signal_evidence=True,
            run_step1_enrichment=True,
            run_step2_enrichment=True,
            scoring_profile=req.scoring_profile,
        )
    except Exception:
        log.error("enrich_one_row failed: %s", traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": "Enrichment failed — see server logs for details",
                "warnings": warnings,
            },
        )

    # ── Post-process + build response ─────────────────────────────────────────
    try:
        row_out = _build_row_response(raw_row, req.scoring_profile, req)
    except Exception:
        log.error("Post-processing failed: %s", traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": "Post-processing failed — see server logs for details",
                "warnings": warnings,
            },
        )

    debug_out = {
        "step1_status": _safe_str(dbg.get("step1_status", "")),
        "step2_status": _safe_str(dbg.get("step2_status", "")),
        "warnings":     warnings,
    }

    return JSONResponse(content={"status": "ok", "row": row_out, "debug": debug_out})


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
