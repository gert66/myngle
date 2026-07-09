"""Cloud Run dispatcher service for the mYngle Lead Prioritizer.

Minimal FastAPI service meant to sit behind an Eventarc trigger on Cloud
Storage "object finalized" events for the ``incoming/`` prefix. On a new
Excel upload it:
  1. builds a run_id from timestamp + input filename stem
  2. picks a task_count based on row count (10 / 25 / 50)
  3. writes runs/<run_id>/manifest.json
  4. starts a Cloud Run Job execution (google-cloud-run v2 client) with
     INPUT_GCS_URI / OUTPUT_GCS_DIR / RUN_ID / TASK_COUNT env overrides

If the Cloud Run Job execution call fails (e.g. no ADC configured locally,
or the job/region/project isn't set), the manifest is still written and the
response reports the execution error — see docs/cloud_run_workflow.md for
the exact `gcloud run jobs execute` equivalent and required env vars:
  CLOUD_RUN_JOB_NAME, CLOUD_RUN_REGION, CLOUD_RUN_PROJECT, RUNS_GCS_DIR,
  DEFAULT_TASK_COUNT

Parallelism is NOT configurable here: the Cloud Run v2 RunJobRequest
overrides support task_count but not parallelism, so an execution always
runs with the job's deploy-time --parallelism setting.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cloud_job_runner import join_path, write_status_json, _gcs_client

app = FastAPI(title="mYngle Lead Prioritizer — Cloud Run dispatcher", version="0.1.0")

INCOMING_PREFIX = "incoming/"
EXCEL_SUFFIXES = (".xlsx", ".xls")


def pick_task_count(row_count: Optional[int]) -> int:
    if row_count is None:
        # Row counting only fails on an odd/broken input — pick the SAFEST
        # tier then (Firecrawl concurrency is the bottleneck), not the
        # heaviest: an oversized task count on a small file merely means
        # empty shards, but 50 tasks on an unknown file can exhaust the
        # Firecrawl tier. See "Rate-limit notities" in the workflow doc.
        return int(os.environ.get("DEFAULT_TASK_COUNT", "10"))
    if row_count <= 100:
        return 10
    if row_count <= 500:
        return 25
    return 50


def build_run_id(input_name: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    stem = Path(input_name).stem
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_") or "run"
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{slug}"


def _extract_gcs_object(event: dict) -> Optional[dict]:
    """Return {"bucket", "name"} from a CloudEvent envelope ({"data": {...}})
    or a bare GCS object-metadata payload — accepts both so the endpoint can
    be smoke-tested with a plain JSON POST as well as real Eventarc events."""
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    if not isinstance(data, dict):
        return None
    bucket = data.get("bucket")
    name = data.get("name")
    if not bucket or not name:
        return None
    return {"bucket": bucket, "name": name}


def _count_excel_rows(bucket: str, name: str) -> Optional[int]:
    """Best-effort row count via a temporary download; returns None on any failure
    (dispatch still proceeds, falling back to DEFAULT_TASK_COUNT)."""
    try:
        import pandas as pd
        with tempfile.TemporaryDirectory() as td:
            local_path = Path(td) / Path(name).name
            _gcs_client().bucket(bucket).blob(name).download_to_filename(str(local_path))
            fname = local_path.name.lower()
            df = pd.read_csv(local_path) if fname.endswith(".csv") else pd.read_excel(local_path)
            return len(df)
    except Exception as exc:
        print(f"[cloud_dispatcher] WARNING: could not count rows: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def start_cloud_run_job_execution(run_id: str, input_uri: str, output_dir: str, task_count: int) -> dict:
    """Start a Cloud Run Job execution with env overrides.

    Never raises: any failure (missing config, missing credentials, API
    error) comes back as {"started": False, "error": ...} so the dispatcher
    can still report a written manifest even without deploy credentials.
    """
    job_name = os.environ.get("CLOUD_RUN_JOB_NAME")
    region = os.environ.get("CLOUD_RUN_REGION")
    project = os.environ.get("CLOUD_RUN_PROJECT")
    if not (job_name and region and project):
        return {
            "started": False,
            "error": "CLOUD_RUN_JOB_NAME / CLOUD_RUN_REGION / CLOUD_RUN_PROJECT not fully configured.",
        }
    try:
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        job_path = client.job_path(project, region, job_name)

        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[
                        run_v2.EnvVar(name="INPUT_GCS_URI", value=input_uri),
                        run_v2.EnvVar(name="OUTPUT_GCS_DIR", value=output_dir),
                        run_v2.EnvVar(name="RUN_ID", value=run_id),
                        run_v2.EnvVar(name="TASK_COUNT", value=str(task_count)),
                    ]
                )
            ],
            task_count=task_count,
        )
        operation = client.run_job(
            request=run_v2.RunJobRequest(name=job_path, overrides=overrides)
        )
        raw_op = getattr(operation, "operation", None)
        execution_name = raw_op.name if raw_op is not None else str(operation)
        return {"started": True, "execution": execution_name}
    except Exception as exc:
        return {"started": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/")
async def handle_event(request: Request):
    body = await request.body()
    try:
        event = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse({"status": "ignored", "reason": "invalid JSON body"}, status_code=200)

    gcs_object = _extract_gcs_object(event)
    if not gcs_object:
        return JSONResponse({"status": "ignored", "reason": "no bucket/name in event payload"}, status_code=200)

    bucket, name = gcs_object["bucket"], gcs_object["name"]

    if not name.startswith(INCOMING_PREFIX):
        return JSONResponse({"status": "ignored", "reason": f"object not under {INCOMING_PREFIX}"}, status_code=200)

    if not name.lower().endswith(EXCEL_SUFFIXES):
        return JSONResponse({"status": "ignored", "reason": "not an Excel file"}, status_code=200)

    runs_bucket_dir = os.environ.get("RUNS_GCS_DIR", f"gs://{bucket}")
    input_uri = f"gs://{bucket}/{name}"

    row_count = _count_excel_rows(bucket, name)
    task_count = pick_task_count(row_count)
    run_id = build_run_id(name)
    output_dir = join_path(runs_bucket_dir, "runs", run_id)

    manifest = {
        "run_id": run_id,
        "input_uri": input_uri,
        "output_dir": output_dir,
        "row_count": row_count,
        "task_count": task_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_uri = join_path(output_dir, "manifest.json")
    try:
        write_status_json(manifest_uri, manifest)
    except Exception as exc:
        print(f"[cloud_dispatcher] ERROR: could not write manifest: {type(exc).__name__}: {exc}", file=sys.stderr)

    execution = start_cloud_run_job_execution(run_id, input_uri, output_dir, task_count)

    return JSONResponse(
        {
            "status": "dispatched",
            "run_id": run_id,
            "input_uri": input_uri,
            "output_dir": output_dir,
            "row_count": row_count,
            "task_count": task_count,
            "manifest_uri": manifest_uri,
            "execution": execution,
        },
        status_code=200,
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
