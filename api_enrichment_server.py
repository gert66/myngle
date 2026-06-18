"""
mYngle · Single-company enrichment API
======================================

Run locally:
    uvicorn api_enrichment_server:app --reload --port 8008

Example requests (both field-name styles accepted):
    curl -X POST http://127.0.0.1:8008/api/enrich/company \
      -H "Content-Type: application/json" \
      -H "X-Enrichment-Api-Token: <token>" \
      -d '{"companyName":"Technogym S.p.A.","domain":"technogym.com"}'

    curl -X POST http://127.0.0.1:8008/api/enrich/company \
      -H "Content-Type: application/json" \
      -H "X-Enrichment-Api-Token: <token>" \
      -d '{"company_name":"Technogym S.p.A.","domain":"technogym.com"}'

Environment variables:
    ANTHROPIC_API_KEY        required — Claude Step 1 + Step 2
    SERPER_API_KEY           required — Google search Step 2
    ENRICHMENT_API_TOKEN     optional — shared secret for X-Enrichment-Api-Token header
    ALLOWED_ORIGINS          optional — comma-separated list of allowed CORS origins
                             default: localhost dev origins only

Correct Lovable integration (do NOT call this server directly from the browser):

    Browser calls Lovable server route:
        POST /api/enrich/company

    Lovable server-side route calls this Python backend:
        POST ${process.env.PYTHON_ENRICHMENT_API_URL}/api/enrich/company
        Header: X-Enrichment-Api-Token: process.env.ENRICHMENT_API_TOKEN

    ENRICHMENT_API_TOKEN must never be exposed to the browser.
    Do not use VITE_ prefix for this token.
    Do not call this Python server directly from frontend code.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator

# ── Logging (server-side only, no secrets) ────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api_enrichment")

# ── CORS origins ──────────────────────────────────────────────────────────────
_DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

def _cors_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return _DEFAULT_ORIGINS

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="mYngle Enrichment API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# ── Auth helper ───────────────────────────────────────────────────────────────
def _check_token(request: Request) -> bool:
    """Return True if auth passes. Logs a warning when token auth is disabled."""
    configured = os.environ.get("ENRICHMENT_API_TOKEN", "")
    if not configured:
        log.warning("ENRICHMENT_API_TOKEN not set — token auth disabled.")
        return True
    provided = request.headers.get("X-Enrichment-Api-Token", "")
    return provided == configured

# ── Lazy imports from the enricher ───────────────────────────────────────────
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
    _enrich_one_row                     = _ec.enrich_one_row
    _resolve_employee_range             = _ec.resolve_employee_range
    _resolve_employee_range_from_serper = _ec.resolve_employee_range_from_serper
    _apply_results_compatible_scoring   = _ec.apply_results_compatible_scoring
    _apply_competitor_icp_override      = _ec.apply_competitor_icp_override
    _EMPLOYEE_RANGE_RESOLVER_FIELDS     = _ec.EMPLOYEE_RANGE_RESOLVER_FIELDS
    _DEFAULT_EMPLOYEE_RANGE_FOR_SCORING = _ec.DEFAULT_EMPLOYEE_RANGE_FOR_SCORING
    _ALL_ENRICHMENT_FIELDS              = _ec.ALL_ENRICHMENT_FIELDS
    _normalize_url                      = _ec.normalize_url
    _clean_domain                       = _ec.clean_domain
    _enricher_loaded = True
    log.info("Enricher module loaded.")


# ── Domain normaliser ─────────────────────────────────────────────────────────
_DOMAIN_STRIP = re.compile(r"^https?://(www\.)?", re.IGNORECASE)

def _normalize_domain(raw: str) -> str:
    """Return hostname only: strip scheme, www, path, query, trailing slash."""
    if not raw:
        return ""
    host = _DOMAIN_STRIP.sub("", raw.strip())
    host = host.split("/")[0].split("?")[0].split("#")[0].strip().rstrip(".")
    return host.lower()


# ── Request model (accepts both camelCase and snake_case) ─────────────────────
class EnrichRequest(BaseModel):
    # camelCase (Lovable preferred)
    companyName: str = ""
    # snake_case (backward-compat)
    company_name: str = ""

    domain: str = ""
    url: str = ""
    country: str = ""
    notes: str = ""
    scoring_profile: str = "default"

    @model_validator(mode="after")
    def _resolve_aliases(self) -> "EnrichRequest":
        # companyName wins; fall back to company_name
        if not self.companyName and self.company_name:
            self.companyName = self.company_name
        elif self.companyName and not self.company_name:
            self.company_name = self.companyName
        return self

    @property
    def resolved_company_name(self) -> str:
        return (self.companyName or self.company_name).strip()


# ── Priority output fields ────────────────────────────────────────────────────
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
    if v is None or (isinstance(v, float) and str(v) == "nan"):
        return ""
    return str(v)


# Labels that look like field names, not real hostnames — reject as domain values.
_DOMAIN_LABEL_REJECT = frozenset({
    "original_domain", "suggested_domain", "validated_domain",
    "none", "unknown", "n/a", "-", "",
})

def _is_domain_like(value: Any) -> bool:
    """Return True only when value looks like a real hostname (no scheme, no spaces)."""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if not v or " " in v:
        return False
    if v.startswith("http://") or v.startswith("https://"):
        return False
    if v in _DOMAIN_LABEL_REJECT:
        return False
    return "." in v


def _pick_domain(*candidates: Any) -> str:
    """Return the first domain-like candidate, normalised, or empty string."""
    for c in candidates:
        s = _safe_str(c).strip()
        if _is_domain_like(s):
            return _normalize_domain(s)
    return ""


def _pick_nonempty(*candidates: Any) -> str:
    """Return the first non-empty string candidate."""
    for c in candidates:
        s = _safe_str(c).strip()
        if s:
            return s
    return ""


def _normalize_row_compatibility(row: dict, req: "EnrichRequest") -> dict:
    """
    Fill empty identity fields in the output row using a safe fallback chain.
    Only overwrites fields that are missing or empty — never replaces good values.
    """
    req_domain = _normalize_domain(req.domain)
    req_name   = req.resolved_company_name

    # 1. company_name
    row["company_name"] = _pick_nonempty(
        row.get("company_name"),
        row.get("canonical_company_name"),
        row.get("input_company_name"),
        req_name,
    )

    # 2. domain
    row["domain"] = _pick_domain(
        row.get("domain"),
        row.get("validated_domain"),
        row.get("canonical_company_domain"),
        row.get("input_domain"),
        req_domain,
        row.get("domain_used_for_enrichment"),  # last: often a label, not a hostname
    )

    # 3. validated_domain
    row["validated_domain"] = _pick_domain(
        row.get("validated_domain"),
        row.get("canonical_company_domain"),
        row.get("domain"),
    )

    # 4. canonical_company_domain
    row["canonical_company_domain"] = _pick_domain(
        row.get("canonical_company_domain"),
        row.get("validated_domain"),
        row.get("domain"),
    )

    # 5. canonical_company_url
    if not _pick_nonempty(row.get("canonical_company_url")):
        _cdn = row.get("canonical_company_domain") or row.get("domain") or ""
        if _cdn:
            row["canonical_company_url"] = f"https://{_cdn}"

    # 6. input_company_name
    row["input_company_name"] = _pick_nonempty(
        row.get("input_company_name"),
        req_name,
        row.get("company_name"),
    )

    # 7. input_domain
    row["input_domain"] = _pick_domain(
        row.get("input_domain"),
        req_domain,
        row.get("domain"),
    )

    # 8. commercial_fit_score
    row["commercial_fit_score"] = _pick_nonempty(
        row.get("commercial_fit_score"),
        row.get("final_commercial_fit_score"),
        row.get("final_commercial_fit_score_75_25_legacy"),
    )

    return row


def _build_row_response(raw_row: dict, scoring_profile: str, req: EnrichRequest) -> dict:
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

    if (
        not str(raw_row.get("lusha_employee_range", "")).strip()
        and er.get("employee_range_resolved")
        and er.get("employee_range_confidence") in ("High", "Medium")
    ):
        raw_row["lusha_employee_range"] = er["employee_range_resolved"]

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

    if "commercial_fit_score" not in scored or scored["commercial_fit_score"] == "":
        scored["commercial_fit_score"] = scored.get("final_commercial_fit_score", "")

    scored["input_company_name"] = req.resolved_company_name
    scored["input_domain"]       = req.domain
    scored["input_url"]          = req.url
    scored["input_country"]      = req.country
    scored["input_notes"]        = req.notes
    scored["enrichment_source"]  = "manual_single"
    scored["enriched_at"]        = datetime.now(timezone.utc).isoformat()

    out: dict[str, Any] = {}
    for f in _PRIORITY_FIELDS:
        out[f] = _safe_str(scored.get(f, ""))

    for f in _ALL_ENRICHMENT_FIELDS:
        if f not in out:
            out[f] = _safe_str(scored.get(f, ""))

    for f, v in scored.items():
        if f not in out:
            out[f] = _safe_str(v)

    out = _normalize_row_compatibility(out, req)

    return out


# ── POST /api/enrich/company ──────────────────────────────────────────────────
@app.post("/api/enrich/company")
async def enrich_company(req: EnrichRequest, request: Request) -> JSONResponse:
    warnings: list[str] = []

    # ── Auth ──────────────────────────────────────────────────────────────────
    if not _check_token(request):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": "Unauthorized", "warnings": []},
        )

    # ── Validation ────────────────────────────────────────────────────────────
    company_name = req.resolved_company_name
    if not company_name:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "companyName is required", "warnings": []},
        )

    # Normalise domain: strip scheme, www, path
    domain = _normalize_domain(req.domain)
    url    = req.url.strip()

    if url:
        raw_url = url
    elif domain:
        raw_url = f"https://{domain}"
    else:
        raw_url = ""
        warnings.append("No domain or URL supplied; enricher will attempt domain discovery.")

    # ── Secrets ───────────────────────────────────────────────────────────────
    api_key    = os.environ.get("ANTHROPIC_API_KEY", "")
    serper_key = os.environ.get("SERPER_API_KEY", "")

    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "ANTHROPIC_API_KEY not configured on server", "warnings": []},
        )

    # ── Load enricher ─────────────────────────────────────────────────────────
    try:
        _load_enricher()
    except Exception:
        log.error("Failed to load enricher: %s", traceback.format_exc())
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "Enricher module failed to load", "warnings": []},
        )

    log.info("Enriching company=%r domain=%r", company_name, domain)

    # ── Enrich ────────────────────────────────────────────────────────────────
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
            content={"status": "error", "error": "Enrichment failed — see server logs", "warnings": warnings},
        )

    # ── Post-process ──────────────────────────────────────────────────────────
    try:
        row_out = _build_row_response(raw_row, req.scoring_profile, req)
    except Exception:
        log.error("Post-processing failed: %s", traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Post-processing failed — see server logs", "warnings": warnings},
        )

    return JSONResponse(content={
        "status": "ok",
        "row":    row_out,
        "debug":  {
            "step1_status": _safe_str(dbg.get("step1_status", "")),
            "step2_status": _safe_str(dbg.get("step2_status", "")),
            "warnings":     warnings,
        },
        "warnings": warnings,
    })


# ── GET /health ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── GET /config-check ─────────────────────────────────────────────────────────
@app.get("/config-check")
async def config_check() -> dict:
    return {
        "anthropic":               bool(os.environ.get("ANTHROPIC_API_KEY")),
        "serper":                  bool(os.environ.get("SERPER_API_KEY")),
        "apiTokenConfigured":      bool(os.environ.get("ENRICHMENT_API_TOKEN")),
        "allowedOriginsConfigured": bool(os.environ.get("ALLOWED_ORIGINS")),
    }
