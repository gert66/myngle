"""Cloud Run dispatcher service for the mYngle Lead Prioritizer.

Minimal FastAPI service meant to sit behind an Eventarc trigger on Cloud
Storage "object finalized" events for the ``incoming/`` prefix. On a new
Excel upload it:
  1. builds a run_id from timestamp + input filename stem
  2. picks a task_count based on row count (10 / 25 / 50), unless a sidecar
     run-config JSON (see below) explicitly sets one
  3. writes runs/<run_id>/manifest.json (including the sidecar config, if any)
  4. starts a Cloud Run Job execution (google-cloud-run v2 client) with
     INPUT_GCS_URI / OUTPUT_GCS_DIR / RUN_ID / TASK_COUNT env overrides, plus
     any feature-flag overrides from the sidecar config

If the Cloud Run Job execution call fails (e.g. no ADC configured locally,
or the job/region/project isn't set), the manifest is still written and the
response reports the execution error — see docs/cloud_run_workflow.md for
the exact `gcloud run jobs execute` equivalent and required env vars:
  CLOUD_RUN_JOB_NAME, CLOUD_RUN_REGION, CLOUD_RUN_PROJECT, RUNS_GCS_DIR,
  DEFAULT_TASK_COUNT

Parallelism is NOT configurable here: the Cloud Run v2 RunJobRequest
overrides support task_count but not parallelism, so an execution always
runs with the job's deploy-time --parallelism setting.

## Per-run config (sidecar JSON)

Uploading ``incoming/<file>.xlsx`` together with an optional
``incoming/<file>.xlsx.config.json`` lets a single deployed job run two (or
more) different profiles — e.g. "foreign-HQ-only" vs. "full" — without
redeploying, closing the gap noted in "Twee ingangen, twee default-profielen"
in docs/cloud_run_workflow.md. See ``build_env_overrides_from_config`` for
the supported keys and ``docs/cloud_run_workflow.md`` for a full example.
Missing/invalid sidecar JSON is never an error — the run just falls back to
the job's deploy-time defaults, exactly like the pre-existing behavior.

## Auto-merge + auto-export on completion

A second Eventarc trigger, on Cloud Storage "object finalized" events for
the *runs* bucket routed to the ``/status-event`` path (see
docs/cloud_run_workflow.md), lets this same service notice when every task
of a run has reported a terminal status and then:
  1. run ``cloud_merge_results.main()`` in-process
  2. if the sidecar config's ``lovable_export.enabled`` is true, run
     ``export_lead_prioritizer_to_lovable_json.py`` in-process on the merged
     Excel and upload the resulting JSON files to
     ``runs/<run_id>/final/lovable_export/``
Both steps are best-effort and never raise past the route handler — a
broken export config just reports its own error, it never blocks the merge
from being recorded as done. A GCS ``if_generation_match=0`` claim file
(``final/_merge_claimed.json``) makes sure only the status event that
actually completes the run triggers the merge, even though Eventarc will
call this route once per task's status write.
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

from cloud_job_runner import (
    is_gcs_uri,
    join_path,
    parse_gcs_uri,
    upload_output_file,
    write_status_json,
    _gcs_client,
)

app = FastAPI(title="mYngle Lead Prioritizer — Cloud Run dispatcher", version="0.1.0")

INCOMING_PREFIX = "incoming/"
EXCEL_SUFFIXES = (".xlsx", ".xls")
SIDECAR_CONFIG_SUFFIX = ".config.json"

# Sidecar config keys whose value is a bool, mapped to the matching
# cloud_job_runner.py env var (see docs/cloud_run_workflow.md's env var
# table). Absent/None keys leave the job-deploy default untouched.
_BOOL_ENV_KEYS = {
    "gate_full_enrichment_on_foreign_hq": "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ",
    "deep_dive": "DEEP_DIVE",
    "deep_dive_on_foreign_hq": "DEEP_DIVE_ON_FOREIGN_HQ",
    "compose_caller_content": "COMPOSE_CALLER_CONTENT",
    "c5_enabled": "C5_ENABLED",
    "use_enrichment_cache": "USE_ENRICHMENT_CACHE",
    "ai_signal_scoring": "AI_SIGNAL_SCORING",
    "rich_icp_context": "RICH_ICP_CONTEXT",
    "force_rerun": "FORCE_RERUN",
}
# Sidecar config keys whose value is passed through as a plain string.
_SCALAR_ENV_KEYS = {
    "mode": "MODE",
    "deep_dive_min_score": "DEEP_DIVE_MIN_SCORE",
    "enrichment_cache_bucket": "ENRICHMENT_CACHE_BUCKET",
    "company_column": "COMPANY_COLUMN",
    "domain_column": "DOMAIN_COLUMN",
    "input_country_column": "INPUT_COUNTRY_COLUMN",
    "default_country": "DEFAULT_COUNTRY",
    "total_row_limit": "TOTAL_ROW_LIMIT",
    "checkpoint_every_rows": "CHECKPOINT_EVERY_ROWS",
}

_STATUS_OBJECT_RE = re.compile(r"^runs/(?P<run_id>[^/]+)/status/.*_(?:done|failed)\.json$")


def sidecar_config_name(input_name: str) -> str:
    return input_name + SIDECAR_CONFIG_SUFFIX


def build_env_overrides_from_config(config: dict) -> dict[str, str]:
    """Translate a sidecar run-config dict into job env-var overrides.

    Only keys present (and not None) in ``config`` produce an override —
    an empty/missing sidecar config produces {} and the run behaves exactly
    like the pre-existing Eventarc path (all job-deploy defaults).
    """
    overrides: dict[str, str] = {}
    for key, env_name in _BOOL_ENV_KEYS.items():
        if config.get(key) is not None:
            overrides[env_name] = "true" if config[key] else "false"
    for key, env_name in _SCALAR_ENV_KEYS.items():
        if config.get(key) is not None:
            overrides[env_name] = str(config[key])
    return overrides


def _extract_run_id_from_status_object(name: str) -> Optional[str]:
    match = _STATUS_OBJECT_RE.match(name)
    return match.group("run_id") if match else None


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


def start_cloud_run_job_execution(
    run_id: str, input_uri: str, output_dir: str, task_count: int,
    extra_env: Optional[dict[str, str]] = None,
) -> dict:
    """Start a Cloud Run Job execution with env overrides.

    ``extra_env`` (from a sidecar run-config, see
    ``build_env_overrides_from_config``) is layered on top of the always-set
    INPUT_GCS_URI/OUTPUT_GCS_DIR/RUN_ID/TASK_COUNT — it never overrides those
    four, only adds to them.

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

        env_vars = {
            "INPUT_GCS_URI": input_uri,
            "OUTPUT_GCS_DIR": output_dir,
            "RUN_ID": run_id,
            "TASK_COUNT": str(task_count),
        }
        env_vars.update(extra_env or {})

        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name=name, value=value) for name, value in env_vars.items()]
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


def resolve_default_country(name: str, config: dict) -> tuple[Optional[str], str]:
    """Return ``(country, source)`` for the DEFAULT_COUNTRY env override.

    Priority: an explicit sidecar ``default_country`` > the sidecar's own
    ``lovable_export.country`` (already required for the export step, so
    reusing it here means the enrichment country and the export bucket label
    can never again silently disagree, which is exactly how one country's
    entire batch previously got enriched/adjudicated against the wrong home
    country with zero errors or warnings) > a guess from the uploaded
    filename (the same ``suggest_country_from_filename`` heuristic the
    interactive Streamlit uploader already uses to pre-fill its "Default
    input country" dropdown, so "upload a file with 'Luxembourg' in the
    name" behaves the same whether a human drives the Streamlit UI or the
    unattended incoming/ dispatcher does).

    ``source`` is one of "sidecar_default_country", "sidecar_lovable_export",
    "filename_guess", or "undetermined" -- always returned (never raises) so
    the caller can record/warn regardless of outcome.
    """
    from lead_prioritizer_batch_app import (
        SUPPORTED_DEFAULT_INPUT_COUNTRIES,
        suggest_country_from_filename,
    )

    sidecar_default = config.get("default_country")
    if sidecar_default:
        return str(sidecar_default), "sidecar_default_country"

    lovable_country = (config.get("lovable_export") or {}).get("country")
    if lovable_country:
        return str(lovable_country), "sidecar_lovable_export"

    filename_guess = suggest_country_from_filename(name, SUPPORTED_DEFAULT_INPUT_COUNTRIES)
    if filename_guess:
        return filename_guess, "filename_guess"

    return None, "undetermined"


def load_sidecar_config(bucket: str, name: str) -> dict:
    """Best-effort: {} if the sidecar ``<name>.config.json`` is missing or
    invalid — a run without one behaves exactly like before this feature
    existed (all job-deploy defaults, row-count-based task_count)."""
    config_name = sidecar_config_name(name)
    try:
        blob = _gcs_client().bucket(bucket).blob(config_name)
        if not blob.exists():
            return {}
        config = json.loads(blob.download_as_text())
        return config if isinstance(config, dict) else {}
    except Exception as exc:
        print(
            f"[cloud_dispatcher] WARNING: could not load sidecar config {config_name}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return {}


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

    if name.endswith(SIDECAR_CONFIG_SUFFIX):
        return JSONResponse({"status": "ignored", "reason": "sidecar config upload, not an input file"}, status_code=200)

    if not name.lower().endswith(EXCEL_SUFFIXES):
        return JSONResponse({"status": "ignored", "reason": "not an Excel file"}, status_code=200)

    runs_bucket_dir = os.environ.get("RUNS_GCS_DIR", f"gs://{bucket}")
    input_uri = f"gs://{bucket}/{name}"

    config = load_sidecar_config(bucket, name)
    row_count = _count_excel_rows(bucket, name)
    task_count = int(config["task_count"]) if config.get("task_count") else pick_task_count(row_count)
    run_id = build_run_id(name)
    output_dir = join_path(runs_bucket_dir, "runs", run_id)
    extra_env = build_env_overrides_from_config(config)

    # Auto-detect a fallback input country (sidecar > lovable_export.country >
    # filename guess) so the unattended path never has to fall back to
    # cloud_job_runner.py's hard "no country column and no default -> fail"
    # guard just because nobody set a sidecar 'default_country'. Recorded in
    # the manifest either way so a human reviewing the run can see where the
    # country came from -- or that it couldn't be determined at all.
    detected_country, country_source = resolve_default_country(name, config)
    if "DEFAULT_COUNTRY" not in extra_env and detected_country:
        extra_env["DEFAULT_COUNTRY"] = detected_country
    if country_source == "undetermined":
        print(
            f"[cloud_dispatcher] WARNING: could not determine a default country for "
            f"{name!r} (no sidecar 'default_country'/'lovable_export.country', filename "
            "doesn't match a supported country). If the source file also has no "
            "resolvable country column, this run's tasks will fail fast instead of "
            "silently defaulting to Italy -- add a sidecar default_country to fix.",
            file=sys.stderr,
        )

    # Job execution is started BEFORE the manifest is written (not after, as
    # before) so the manifest can carry execution_name for the stop button --
    # the manifest is still always written, even when the execution call
    # fails, so "manifest exists even without deploy credentials" still holds.
    execution = start_cloud_run_job_execution(run_id, input_uri, output_dir, task_count, extra_env=extra_env)

    manifest = {
        "run_id": run_id,
        "input_uri": input_uri,
        "output_dir": output_dir,
        "row_count": row_count,
        "task_count": task_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "execution_name": execution.get("execution") if execution.get("started") else None,
        "country_detection": {
            "detected_country": detected_country,
            "source": country_source,
        },
    }
    manifest_uri = join_path(output_dir, "manifest.json")
    try:
        write_status_json(manifest_uri, manifest)
    except Exception as exc:
        print(f"[cloud_dispatcher] ERROR: could not write manifest: {type(exc).__name__}: {exc}", file=sys.stderr)

    return JSONResponse(
        {
            "status": "dispatched",
            "run_id": run_id,
            "input_uri": input_uri,
            "output_dir": output_dir,
            "row_count": row_count,
            "task_count": task_count,
            "manifest_uri": manifest_uri,
            "extra_env": extra_env,
            "execution": execution,
            "country_detection": manifest["country_detection"],
        },
        status_code=200,
    )


def _read_json(uri: str) -> Optional[dict]:
    """Best-effort JSON read from a gs:// URI or local path; None if missing/invalid."""
    try:
        if is_gcs_uri(uri):
            bucket_name, blob_name = parse_gcs_uri(uri)
            blob = _gcs_client().bucket(bucket_name).blob(blob_name)
            if not blob.exists():
                return None
            return json.loads(blob.download_as_text())
        path = Path(uri)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _claim_completion(claim_uri: str, run_id: str) -> bool:
    """Compare-and-swap: True if THIS call won the claim to merge this run,
    False if another concurrent status event already claimed it. Same
    if_generation_match=0 pattern as enrichment_cache.py's read-merge-write.

    Local (non-GCS) paths have no atomic precondition, so they fall back to
    a plain existence check — fine because local/smoke-test runs are always
    sequential, never concurrent."""
    payload = json.dumps({"run_id": run_id, "claimed_at": datetime.now(timezone.utc).isoformat()})
    if is_gcs_uri(claim_uri):
        bucket_name, blob_name = parse_gcs_uri(claim_uri)
        blob = _gcs_client().bucket(bucket_name).blob(blob_name)
        try:
            blob.upload_from_string(payload, content_type="application/json", if_generation_match=0)
            return True
        except Exception:
            return False
    path = Path(claim_uri)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return True


def _release_claim(claim_uri: str) -> None:
    """Undo a claim taken by _claim_completion whose merge attempt then
    failed, so a later status event -- e.g. once a task's own "_done.json"
    from a Cloud Run-driven retry supersedes the "_failed.json" that made
    this attempt look complete-but-failed -- can claim and retry instead of
    being permanently locked out by a claim that turned out to be premature.
    Only a *failed* attempt releases its claim; a successful merge's claim
    is left in place forever, same as before. Best-effort: if the delete
    itself fails, the next event just sees "already_claimed" and the run
    needs a manual retry, same as today -- not silently losing data."""
    if is_gcs_uri(claim_uri):
        bucket_name, blob_name = parse_gcs_uri(claim_uri)
        try:
            _gcs_client().bucket(bucket_name).blob(blob_name).delete()
        except Exception:
            pass
        return
    try:
        Path(claim_uri).unlink()
    except FileNotFoundError:
        pass


def _download_existing_current_via_client(bucket: str, country_folder: str) -> tuple[list[dict], dict[str, dict]]:
    """GCS-client equivalent of cloud_run_streamlit_app._download_existing_current_export
    (that one shells out to gcloud/gsutil, which isn't available in this
    lean Cloud Run service). Returns ``([], {})`` -- never raises -- when
    nothing is there yet or any read/parse step fails, same "merge degrades
    to nothing existing" philosophy."""
    prefix = f"{country_folder}/current/"
    try:
        blobs = list(_gcs_client().bucket(bucket).list_blobs(prefix=prefix))
    except Exception:
        return [], {}

    list_items: list[dict] = []
    details: dict[str, dict] = {}
    for blob in blobs:
        name = blob.name.rsplit("/", 1)[-1]
        if name != "companies.list.json" and not name.startswith("company-details-"):
            continue
        data = _read_json(f"gs://{bucket}/{blob.name}")
        if data is None:
            continue
        if name == "companies.list.json" and isinstance(data, list):
            list_items = data
        elif name.startswith("company-details-") and isinstance(data, dict):
            details.update(data)
    return list_items, details


def _run_lovable_export(final_output_uri: str, output_dir: str, lovable_cfg: dict) -> dict:
    """Download the merged final Excel, run
    export_lead_prioritizer_to_lovable_json.py in-process, and:
      1. archive the raw export to <output_dir>/final/lovable_export/
         (per-run snapshot inside the runs bucket, as before)
      2. merge it into the country's live <country>/current/ bucket (the one
         the Lovable app actually reads from) and archive the same merged
         snapshot under <country>/runs/<run_folder>/ -- mirroring
         cloud_run_streamlit_app.run_lovable_export_and_upload, since that
         was the only place this "go live" step existed before. Always
         merges rather than overwrites current/ (never the reverse) because
         this runs unattended -- an unattended overwrite of a live country
         bucket is the riskier default.

    Never raises: any failure (missing country/cold_callers, bad workbook,
    export exception, current/-merge failure) comes back as {"started": ...,
    "ok": False, "error": ...} so a broken export config never blocks the
    merge itself from being reported as done — the merged Excel is still
    available either way."""
    country = lovable_cfg.get("country")
    cold_callers = lovable_cfg.get("cold_callers")
    if not country or not cold_callers:
        return {
            "started": False,
            "ok": False,
            "error": "lovable_export.enabled is true but 'country'/'cold_callers' missing in sidecar config",
        }
    cold_callers_arg = (
        ",".join(str(c) for c in cold_callers) if isinstance(cold_callers, list) else str(cold_callers)
    )

    from cloud_merge_results import _download_to_local
    from export_lead_prioritizer_to_lovable_json import (
        main as export_main,
        manifest_has_country_mismatch_warning,
    )
    import lovable_gcs_upload as lovable_gcs

    with tempfile.TemporaryDirectory() as td:
        # _download_to_local returns the path to actually use -- for a gs://
        # URI that's the local_path it just downloaded to; for an already-
        # local path (only relevant in tests -- production always passes a
        # gs:// final_output_uri) it's the ORIGINAL path unchanged, not the
        # local_xlsx target, so the return value must be captured, not
        # assumed to equal local_xlsx.
        local_xlsx = Path(td) / Path(final_output_uri).name
        try:
            local_xlsx = _download_to_local(final_output_uri, local_xlsx)
        except Exception as exc:
            return {"started": False, "ok": False, "error": f"could not download {final_output_uri}: {exc}"}

        local_export_dir = Path(td) / "lovable_export"
        argv = [
            "--input-xlsx", str(local_xlsx),
            "--output-dir", str(local_export_dir),
            "--country", str(country),
            "--cold-callers", cold_callers_arg,
        ]
        if lovable_cfg.get("include_skipped"):
            argv.append("--include-skipped")
        if lovable_cfg.get("foreign_hq_only") is False:
            argv.append("--no-foreign-hq-only")
        bucket_size = int(lovable_cfg["bucket_size"]) if lovable_cfg.get("bucket_size") else 500
        argv += ["--bucket-size", str(bucket_size)]
        if lovable_cfg.get("content_language"):
            argv += ["--content-language", str(lovable_cfg["content_language"])]

        try:
            exit_code = export_main(argv)
        except Exception as exc:
            return {"started": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if exit_code != 0:
            return {"started": True, "ok": False, "error": f"export exited with code {exit_code}"}

        dest_dir = join_path(output_dir, "final", "lovable_export")
        uploaded = []
        for local_file in sorted(local_export_dir.glob("*.json")):
            dest_uri = join_path(dest_dir, local_file.name)
            upload_output_file(local_file, dest_uri)
            uploaded.append(dest_uri)

        result = {"started": True, "ok": True, "files": uploaded, "dest_dir": dest_dir, "live_upload": None}

        # A country-label/source-data mismatch (export_lead_prioritizer_to_
        # lovable_json._export_country_mismatch_warning) is deliberately
        # non-blocking in the export step itself -- the interactive
        # Streamlit "Test" bucket comparison feature relies on exactly that.
        # But there's no human here to recognize "oh, that's just a test
        # export" before it goes live: this unattended path previously
        # promoted a whole country's Lusha upload into current/ while every
        # row had actually been enriched against a different country (see
        # the Luxembourg-labeled-as-Italy incident). Read the manifest this
        # export run just wrote and refuse to auto-promote when it carries
        # that warning -- the per-run archive above still happened, so
        # nothing is lost, it just isn't published as the live dataset.
        export_manifest_path = local_export_dir / "export_manifest.json"
        try:
            export_manifest = json.loads(export_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            export_manifest = {}
        if manifest_has_country_mismatch_warning(export_manifest):
            result["live_upload"] = {
                "ok": False,
                "blocked": True,
                "error": (
                    "Auto-promotion to current/ skipped: this export's rows were "
                    "enriched against a different country than the export label "
                    f"{country!r} (see export_manifest.json warnings). The per-run "
                    "archive was still uploaded to final/lovable_export/ for manual "
                    "review -- fix the source country and re-run before publishing."
                ),
            }
            return result

        # ---- Go live: merge into <country>/current/ + archive snapshot ----
        try:
            gcs_bucket = lovable_gcs.DEFAULT_GCS_BUCKET
            country_folder = lovable_gcs.country_folder_slug(str(country))
            run_folder = lovable_gcs.default_gcs_run_folder(
                str(lovable_cfg.get("mode") or "full"),
                now=datetime.now(timezone.utc),
            )

            new_list_items = json.loads((local_export_dir / "companies.list.json").read_text(encoding="utf-8"))
            new_details: dict = {}
            for bucket_path in sorted(local_export_dir.glob("company-details-*.json")):
                new_details.update(json.loads(bucket_path.read_text(encoding="utf-8")))

            existing_list_items, existing_details = _download_existing_current_via_client(
                gcs_bucket, country_folder)
            merged_items, merged_details = lovable_gcs.merge_company_records(
                existing_list_items, existing_details, new_list_items, new_details)
            merged_items, merged_buckets = lovable_gcs.rebucket_company_details(
                merged_items, merged_details, bucket_size)

            merged_dir = Path(td) / "lovable_export_merged_current"
            merged_dir.mkdir(parents=True, exist_ok=True)
            (merged_dir / "companies.list.json").write_text(
                json.dumps(merged_items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            for bucket_file, bucket_data in merged_buckets.items():
                (merged_dir / bucket_file).write_text(
                    json.dumps(bucket_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            merged_filenames = ["companies.list.json"] + sorted(merged_buckets.keys())

            for filename in merged_filenames:
                upload_output_file(
                    merged_dir / filename,
                    lovable_gcs.gcs_current_path(gcs_bucket, country_folder, filename))
            for local_file in sorted(local_export_dir.glob("*.json")):
                upload_output_file(
                    local_file,
                    lovable_gcs.gcs_archive_path(gcs_bucket, country_folder, run_folder, local_file.name))

            result["live_upload"] = {
                "ok": True,
                "current_dir": lovable_gcs.gcs_current_path(gcs_bucket, country_folder, ""),
                "archive_dir": lovable_gcs.gcs_archive_path(gcs_bucket, country_folder, run_folder, ""),
                "companies_total_after": len(merged_items),
            }
        except Exception as exc:
            result["live_upload"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        return result


def check_and_trigger_completion(output_dir: str, run_id: str) -> dict:
    """Idempotent completion check: called on every status-file write for a
    run, but only the write that completes the LAST expected task actually
    triggers the merge (+ optional Lovable export) — every other call
    returns {"status": "waiting", ...} cheaply.

    Never raises: any error comes back as {"status": "error", "error": ...}
    so a single bad event never crashes the Eventarc-triggered service.
    """
    try:
        manifest_uri = join_path(output_dir, "manifest.json")
        manifest = _read_json(manifest_uri)
        if manifest is None:
            return {"status": "ignored", "reason": "no manifest.json yet for this run"}
        expected = manifest.get("task_count")
        if not expected:
            return {"status": "ignored", "reason": "manifest has no task_count"}

        import cloud_merge_results as cmr

        # Dedup by task label (not raw file count): a task retried after an
        # initial failure (Cloud Run's own task-level retry) leaves both a
        # stale "_failed.json" and a fresh "_done.json" for the same label,
        # which would otherwise inflate "reported" past "expected" early.
        task_states = cmr.resolve_task_states(output_dir)
        reported = len(task_states)
        if reported < expected:
            return {"status": "waiting", "run_id": run_id, "reported": reported, "expected": expected}

        claim_uri = join_path(output_dir, "final", "_merge_claimed.json")
        if not _claim_completion(claim_uri, run_id):
            return {"status": "already_claimed", "run_id": run_id}

        merge_exit = cmr.main([
            "--run-id", run_id,
            "--output-dir", output_dir,
            "--expected-task-count", str(expected),
        ])
        if merge_exit != 0:
            # Release the claim: a task counted as "failed" right now might
            # still be mid-retry (Cloud Run's own task-level retry runs
            # independently of this event), so a later status event for the
            # same run must be able to claim and retry the merge once that
            # retry's "_done.json" actually lands -- not find the slot
            # permanently taken by this premature attempt.
            _release_claim(claim_uri)
            return {"status": "merge_failed", "run_id": run_id}

        export_result = None
        lovable_cfg = (manifest.get("config") or {}).get("lovable_export") or {}
        if lovable_cfg.get("enabled"):
            merge_manifest = _read_json(join_path(output_dir, "final", "manifest_done.json"))
            final_output_uri = (merge_manifest or {}).get("final_output_uri")
            if final_output_uri:
                export_result = _run_lovable_export(final_output_uri, output_dir, lovable_cfg)
            else:
                export_result = {"started": False, "ok": False, "error": "no final_output_uri in merge manifest"}

        return {"status": "merged", "run_id": run_id, "export": export_result}
    except Exception as exc:
        return {"status": "error", "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/status-event")
async def handle_status_event(request: Request):
    body = await request.body()
    try:
        event = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse({"status": "ignored", "reason": "invalid JSON body"}, status_code=200)

    gcs_object = _extract_gcs_object(event)
    if not gcs_object:
        return JSONResponse({"status": "ignored", "reason": "no bucket/name in event payload"}, status_code=200)

    bucket, name = gcs_object["bucket"], gcs_object["name"]
    run_id = _extract_run_id_from_status_object(name)
    if not run_id:
        return JSONResponse({"status": "ignored", "reason": "not a run status(done/failed) object"}, status_code=200)

    output_dir = f"gs://{bucket}/runs/{run_id}"
    result = check_and_trigger_completion(output_dir, run_id)
    return JSONResponse(result, status_code=200)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
