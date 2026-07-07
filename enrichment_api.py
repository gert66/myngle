"""
Myngle Enrichment API
=====================
FastAPI wrapper around enrich_clients_claude.py so the React/Lovable UI
can upload a company cohort and trigger enrichment.

Local run:
    uvicorn enrichment_api:app --reload --port 8000

Environment variables required (never exposed to frontend):
    ANTHROPIC_API_KEY   – Claude API key
    SERPER_API_KEY      – Serper web-search key

Manual test (upload a file, start a job):
    curl -X POST http://localhost:8000/api/enrichment/jobs \
         -F "file=@test.csv" \
         -F "max_rows=5"

Check status:
    curl http://localhost:8000/api/enrichment/jobs/<job_id>

Download result:
    curl -OJ http://localhost:8000/api/enrichment/jobs/<job_id>/download
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Myngle Enrichment API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNS_ROOT = Path("runs/enrichment_jobs")
ENRICHER_SCRIPT = Path(__file__).parent / "enrich_clients_claude.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    return RUNS_ROOT / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def _read_meta(job_id: str) -> dict:
    path = _meta_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(path.read_text())


def _write_meta(job_id: str, meta: dict) -> None:
    _meta_path(job_id).write_text(json.dumps(meta, indent=2, default=str))


def _find_output_xlsx(output_dir: Path) -> list[str]:
    return [str(p) for p in output_dir.glob("*.xlsx")]


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

def _run_enrichment(job_id: str, input_file: Path, output_dir: Path, max_rows: int) -> None:
    meta = _read_meta(job_id)
    meta["status"] = "running"
    _write_meta(job_id, meta)

    cmd = [
        sys.executable,
        str(ENRICHER_SCRIPT),
        "--input", str(input_file),
        "--output-dir", str(output_dir),
        "--no-eta",
    ]
    if max_rows and max_rows > 0:
        cmd += ["--max-rows", str(max_rows)]

    env = os.environ.copy()
    # Keys must already be present in the environment; we do not accept them
    # from the request to keep them server-side only.
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    serper_key = env.get("SERPER_API_KEY", "")
    if anthropic_key:
        cmd += ["--anthropic-key", anthropic_key]
    if serper_key:
        cmd += ["--serper-key", serper_key]

    log_path = _job_dir(job_id) / "run.log"
    try:
        with log_path.open("w") as log_fh:
            result = subprocess.run(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )

        output_files = _find_output_xlsx(output_dir)
        if result.returncode == 0 and output_files:
            meta["status"] = "completed"
            meta["completed_at"] = datetime.now(timezone.utc).isoformat()
            meta["output_files"] = output_files
        else:
            meta["status"] = "failed"
            meta["completed_at"] = datetime.now(timezone.utc).isoformat()
            tail = ""
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                tail = "\n".join(lines[-30:])
            meta["error"] = f"Process exited with code {result.returncode}.\n{tail}"
    except Exception as exc:
        meta["status"] = "failed"
        meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        meta["error"] = str(exc)

    _write_meta(job_id, meta)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/enrichment/jobs", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    max_rows: int = Form(default=0),
):
    """Upload a .xlsx or .csv file and start an enrichment job."""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        raise HTTPException(status_code=400, detail="Only .xlsx and .csv files are accepted.")

    job_id = str(uuid.uuid4())
    input_dir = _job_dir(job_id) / "input"
    output_dir = _job_dir(job_id) / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_file = input_dir / file.filename
    input_file.write_bytes(await file.read())

    meta = {
        "job_id": job_id,
        "status": "uploaded",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "input_file": str(input_file),
        "output_dir": str(output_dir),
        "max_rows": max_rows,
        "output_files": [],
        "error": None,
    }
    _write_meta(job_id, meta)

    background_tasks.add_task(_run_enrichment, job_id, input_file, output_dir, max_rows)

    return {"job_id": job_id, "status": "uploaded"}


@app.get("/api/enrichment/jobs/{job_id}")
def get_job(job_id: str):
    """Return the current status and metadata for a job."""
    meta = _read_meta(job_id)
    response = {
        "job_id": meta["job_id"],
        "status": meta["status"],
        "created_at": meta.get("created_at"),
        "completed_at": meta.get("completed_at"),
    }
    if meta["status"] == "completed":
        response["output_files"] = meta.get("output_files", [])
    if meta["status"] == "failed":
        response["error"] = meta.get("error")
    return response


@app.get("/api/enrichment/jobs/{job_id}/download")
def download_job(job_id: str):
    """Download the enriched .xlsx result for a completed job."""
    meta = _read_meta(job_id)
    if meta["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not completed yet (current status: {meta['status']}).",
        )
    output_files = meta.get("output_files", [])
    if not output_files:
        raise HTTPException(status_code=404, detail="No output file found for this job.")
    return FileResponse(
        path=output_files[0],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(output_files[0]).name,
    )


# ---------------------------------------------------------------------------
# Company Hub API  — read-only endpoints for the Lovable frontend
#
# NOTE: All data below is MOCK / in-memory placeholder data.
#       It will be replaced by database-backed queries once the Company Hub
#       data model and persistence layer are in place.
# ---------------------------------------------------------------------------

from typing import Optional  # noqa: E402  (stdlib, safe to import late)


# ── Pydantic models ──────────────────────────────────────────────────────────

from pydantic import BaseModel  # already a FastAPI transitive dep


class CompanyListItem(BaseModel):
    company_id: str
    company_name: str
    domain: str
    country: str
    industry: str
    employee_range: str
    commercial_fit_score: float
    commercial_tier: str          # e.g. "Tier 1", "Tier 2", "Tier 3"
    outreach_readiness_status: str  # e.g. "Ready", "Needs review", "Not ready"
    has_detail: bool
    has_contacts: bool
    last_updated: str             # ISO-8601 date string


class CompanyDetail(BaseModel):
    # Core fields (same as CompanyListItem)
    company_id: str
    company_name: str
    domain: str
    country: str
    industry: str
    employee_range: str
    commercial_fit_score: float
    commercial_tier: str
    outreach_readiness_status: str
    has_detail: bool
    has_contacts: bool
    last_updated: str
    # Detail-only fields
    why_relevant: str
    whats_hot: str
    whats_not_hot: str
    caller_angle: str
    evidence_summary: str
    key_source_links: list[str]
    recommended_next_action: str
    warnings: list[str]


# ── Mock data (TEMPORARY — replace with DB queries) ──────────────────────────

_MOCK_COMPANIES: list[dict] = [
    {
        "company_id": "quanta-robotics",
        "company_name": "Quanta Robotics S.r.l.",
        "domain": "quantarobotics.it",
        "country": "Italy",
        "industry": "Industrial Automation",
        "employee_range": "50–200",
        "commercial_fit_score": 7.8,
        "commercial_tier": "Tier 1",
        "outreach_readiness_status": "Ready",
        "has_detail": True,
        "has_contacts": True,
        "last_updated": "2025-06-01",
        # Detail fields
        "why_relevant": "Fast-growing cobot integrator with EU expansion plans.",
        "whats_hot": "New €4M Series A closed Q1 2025; hiring sales in DE and NL.",
        "whats_not_hot": "No established HR tech stack yet — learning opportunity.",
        "caller_angle": (
            "Lead with ROI on reduced ramp time for technical sales reps "
            "in new markets (DE, NL)."
        ),
        "evidence_summary": (
            "LinkedIn: 12 open roles in DACH region. "
            "Press release: Series A, March 2025. "
            "Website: product page updated with EN/DE copy."
        ),
        "key_source_links": [
            "https://quantarobotics.it/en/news/series-a",
            "https://linkedin.com/company/quanta-robotics",
        ],
        "recommended_next_action": "Send intro email to CMO; reference DACH expansion.",
        "warnings": [],
    },
    {
        "company_id": "meridian-foods",
        "company_name": "Meridian Foods S.p.A.",
        "domain": "meridianfoods.it",
        "country": "Italy",
        "industry": "Food & Beverage",
        "employee_range": "200–500",
        "commercial_fit_score": 5.4,
        "commercial_tier": "Tier 2",
        "outreach_readiness_status": "Needs review",
        "has_detail": True,
        "has_contacts": False,
        "last_updated": "2025-05-15",
        "why_relevant": "Mid-sized food exporter with growing international sales team.",
        "whats_hot": "Expanding into Benelux; new sales director hired Feb 2025.",
        "whats_not_hot": "Ownership change pending — decision-making may be slow.",
        "caller_angle": "Focus on onboarding efficiency for the new Benelux team.",
        "evidence_summary": (
            "LinkedIn: sales director profile updated Feb 2025. "
            "Crunchbase: M&A activity flagged."
        ),
        "key_source_links": [
            "https://linkedin.com/company/meridian-foods",
        ],
        "recommended_next_action": "Hold outreach until ownership situation is clearer.",
        "warnings": ["Ownership change pending — verify decision-maker status first."],
    },
    {
        "company_id": "alphastream-tech",
        "company_name": "AlphaStream Technologies B.V.",
        "domain": "alphastream.io",
        "country": "Netherlands",
        "industry": "SaaS / FinTech",
        "employee_range": "10–50",
        "commercial_fit_score": 6.1,
        "commercial_tier": "Tier 2",
        "outreach_readiness_status": "Ready",
        "has_detail": False,
        "has_contacts": False,
        "last_updated": "2025-04-20",
        "why_relevant": "Series-B fintech scaling a European sales team rapidly.",
        "whats_hot": "30 % headcount growth YoY; new VP Sales hired Q4 2024.",
        "whats_not_hot": "CRM appears to be custom-built — integration complexity.",
        "caller_angle": "Lead with peer benchmarks from similar-stage FinTech scaleups.",
        "evidence_summary": "LinkedIn headcount signal. Pitchbook Series B record.",
        "key_source_links": [
            "https://alphastream.io/about",
            "https://pitchbook.com/profiles/company/alphastream",
        ],
        "recommended_next_action": "Request intro via shared VC connection.",
        "warnings": [],
    },
]

# Fast lookup by company_id
_MOCK_BY_ID: dict[str, dict] = {c["company_id"]: c for c in _MOCK_COMPANIES}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Basic liveness check for the Lovable frontend."""
    return {"status": "ok", "service": "myngle-api"}


@app.get("/api/companies/light")
def list_companies():
    """
    Return a lightweight list of all companies for the Company Hub overview.

    MOCK DATA — will be replaced by a database query.
    """
    items = [
        CompanyListItem(
            company_id=c["company_id"],
            company_name=c["company_name"],
            domain=c["domain"],
            country=c["country"],
            industry=c["industry"],
            employee_range=c["employee_range"],
            commercial_fit_score=c["commercial_fit_score"],
            commercial_tier=c["commercial_tier"],
            outreach_readiness_status=c["outreach_readiness_status"],
            has_detail=c["has_detail"],
            has_contacts=c["has_contacts"],
            last_updated=c["last_updated"],
        )
        for c in _MOCK_COMPANIES
    ]
    return {"companies": [i.model_dump() for i in items]}


@app.get("/api/companies/{company_id}")
def get_company(company_id: str):
    """
    Return full detail for a single company.

    MOCK DATA — will be replaced by a database query.
    """
    c = _MOCK_BY_ID.get(company_id)
    if not c:
        raise HTTPException(status_code=404, detail=f"Company '{company_id}' not found.")
    detail = CompanyDetail(
        company_id=c["company_id"],
        company_name=c["company_name"],
        domain=c["domain"],
        country=c["country"],
        industry=c["industry"],
        employee_range=c["employee_range"],
        commercial_fit_score=c["commercial_fit_score"],
        commercial_tier=c["commercial_tier"],
        outreach_readiness_status=c["outreach_readiness_status"],
        has_detail=c["has_detail"],
        has_contacts=c["has_contacts"],
        last_updated=c["last_updated"],
        why_relevant=c["why_relevant"],
        whats_hot=c["whats_hot"],
        whats_not_hot=c["whats_not_hot"],
        caller_angle=c["caller_angle"],
        evidence_summary=c["evidence_summary"],
        key_source_links=c["key_source_links"],
        recommended_next_action=c["recommended_next_action"],
        warnings=c["warnings"],
    )
    return {"company": detail.model_dump()}
