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
