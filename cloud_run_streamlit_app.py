"""Streamlit UI for the mYngle Lead Prioritizer Cloud Run Jobs workflow.

Local Streamlit wrapper around the same manual steps documented in
docs/cloud_run_workflow.md (Optie A) and scripted in
run_cloud_lead_prioritizer.ps1: upload an Excel to the input bucket, execute
the deployed Cloud Run Job, merge the part-outputs into one final Excel, and
offer it for download — all from a browser instead of a terminal.

Every GCS/Cloud Run interaction goes through the ``gcloud`` CLI via
subprocess (never the Python ``google-cloud-storage``/``google-cloud-run``
clients), because those require Application Default Credentials that are not
set up on this machine — ``gcloud auth login`` is enough. The final merge
step downloads part-outputs locally first and calls
``cloud_merge_results.main()`` in-process against a local directory for the
same reason.

Nothing here re-implements sharding, enrichment, or scoring — this is
orchestration/UI only, same spirit as cloud_dispatcher.py / cloud_job_runner.py.

Run with:
    streamlit run cloud_run_streamlit_app.py
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cloud_dispatcher import build_run_id
from cloud_job_runner import join_path

DEFAULT_PROJECT = "project-979d7166-1016-40ce-94c"
DEFAULT_REGION = "europe-west4"
DEFAULT_BUCKET = "myngle-cloud-run-test"
DEFAULT_JOB_NAME = "myngle-lead-prioritizer"
FINAL_OUTPUT_NAME = "lead_prioritizer_final.xlsx"


@functools.lru_cache(maxsize=1)
def _gcloud_executable() -> str:
    """Resolve the actual gcloud executable path via PATH/PATHEXT lookup.

    On Windows, `gcloud` is a `gcloud.cmd` batch wrapper. subprocess.run(...)
    without shell=True calls CreateProcess directly, which — unlike a real
    shell — does NOT consult PATHEXT to resolve bare "gcloud" to "gcloud.cmd",
    so it raises FileNotFoundError ([WinError 2]) even though `gcloud` works
    fine when typed into PowerShell/Bash. shutil.which() does the same
    PATHEXT-aware lookup a shell would, so it finds gcloud.cmd correctly.
    Falls back to the bare name if not found, so the resulting FileNotFoundError
    still names "gcloud" rather than an empty string.
    """
    return shutil.which("gcloud") or "gcloud"


# =============================================================================
# Pure helpers — GCS path / gcloud command construction (unit-testable without
# gcloud or Streamlit installed; no subprocess is invoked in this section).
# =============================================================================

def gcs_incoming_uri(bucket: str, file_name: str) -> str:
    return join_path(f"gs://{bucket}", "incoming", file_name)


def gcs_output_dir(bucket: str, run_id: str) -> str:
    return join_path(f"gs://{bucket}", "runs", run_id)


def build_upload_command(local_path: str, dest_uri: str, project: str) -> list[str]:
    return [_gcloud_executable(), "storage", "cp", local_path, dest_uri, "--project", project]


def build_execute_command(
    job_name: str, project: str, region: str,
    input_uri: str, output_dir: str, run_id: str, task_count: int, mode: str,
    extra_env: Optional[dict] = None, wait: bool = False,
) -> list[str]:
    """``wait=False`` (the default) returns immediately once the execution is
    accepted — the Cloud Run Job itself keeps running server-side either
    way, ``--wait`` only controls whether THIS gcloud subprocess blocks until
    it finishes. Non-blocking is what makes the app laptop-independent: the
    caller stores the run_id (see main()'s "Status" panel) and can check
    back on it later instead of having to keep this process alive for the
    whole run."""
    env_pairs = [
        f"INPUT_GCS_URI={input_uri}", f"OUTPUT_GCS_DIR={output_dir}",
        f"RUN_ID={run_id}", f"TASK_COUNT={task_count}", f"MODE={mode}",
    ]
    for key, value in (extra_env or {}).items():
        env_pairs.append(f"{key}={value}")
    env_vars = ",".join(env_pairs)
    cmd = [
        _gcloud_executable(), "run", "jobs", "execute", job_name,
        "--project", project,
        "--region", region,
        # --tasks is what actually sets the number of tasks for THIS
        # execution. The TASK_COUNT env var alone does not: Cloud Run sets
        # CLOUD_RUN_TASK_COUNT to the job's deploy-time task count, and the
        # runner gives that precedence — without --tasks the job always ran
        # its deployed count (and the merge's --expected-task-count check
        # then failed for any other value chosen in the sidebar).
        "--tasks", str(task_count),
        "--update-env-vars", env_vars,
    ]
    if wait:
        cmd.append("--wait")
    return cmd


def build_download_command(src_glob: str, local_dir: str, project: str) -> list[str]:
    return [_gcloud_executable(), "storage", "cp", src_glob, local_dir, "--project", project]


def build_list_command(glob_pattern: str, project: str) -> list[str]:
    return [_gcloud_executable(), "storage", "ls", glob_pattern, "--project", project]


def list_existing_gcs_files(listing_output: str) -> list[str]:
    """Parse ``gcloud storage ls`` output into existing ``gs://`` object URIs.

    Ignores blank lines and gcloud's own "No matches" message on an empty/
    not-yet-existing prefix (a non-``gs://`` line is never a real object), so
    this returns ``[]`` — not a crash — for the common "nothing there yet"
    case. Callers should also treat a non-zero exit code as "nothing there"
    (gcloud exits non-zero on a glob with zero matches).
    """
    return [
        line.strip() for line in listing_output.splitlines()
        if line.strip().startswith("gs://")
    ]


_STATUS_SUFFIXES = (("_done.json", "done"), ("_failed.json", "failed"), ("_running.json", "running"))


def count_task_statuses(listing_output: str) -> dict:
    """Classify ``gcloud storage ls .../status/*.json`` output lines into
    per-TASK final state, then tally those states.

    cloud_job_runner.py never deletes a task's ``_running.json`` once it
    writes the matching ``_done.json``/``_failed.json`` (see its ``main()``),
    so both files coexist for every finished task — counting raw file
    suffixes would count every finished task as "still running" forever.
    Instead this groups lines by task label (the filename with the status
    suffix stripped) and lets ``done``/``failed`` (terminal) win over a
    leftover ``running`` marker for the same task, regardless of listing
    order. Ignores non-matching/blank lines (e.g. gcloud's "no matches"
    message on an empty/not-yet-existing prefix) so this never raises on a
    run that hasn't written anything yet.
    """
    task_state: dict[str, str] = {}
    for line in listing_output.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        for suffix, state in _STATUS_SUFFIXES:
            if name.endswith(suffix):
                label = name[: -len(suffix)]
                if state != "running" or label not in task_state:
                    task_state[label] = state
                break
    counts = {"running": 0, "done": 0, "failed": 0}
    for state in task_state.values():
        counts[state] += 1
    return counts


def build_cat_command(uri: str, project: str) -> list[str]:
    return [_gcloud_executable(), "storage", "cat", uri, "--project", project]


def read_gcs_json(uri: str, project: str) -> Optional[dict]:
    """Best-effort read of a small remote JSON file (manifest.json,
    final/manifest_done.json) via `gcloud storage cat` — no local download
    step, no ADC needed. None on any failure (file doesn't exist yet,
    invalid JSON, gcloud error): callers treat that as "not available yet",
    never a crash — this is polled repeatedly while a run is in progress."""
    rc, output = run_capture(build_cat_command(uri, project))
    if rc != 0:
        return None
    try:
        return json.loads(output)
    except Exception:
        return None


def parse_run_ids_from_manifest_listing(listing_output: str) -> list[str]:
    """Extract run_ids from `gcloud storage ls gs://bucket/runs/*/manifest.json`
    output, most recent first. A run_id embeds a sortable
    YYYYMMDD_HHMMSS_<slug> prefix (see cloud_dispatcher.build_run_id), so a
    plain descending string sort already orders by recency without needing
    to open/parse each manifest."""
    run_ids = []
    for line in listing_output.splitlines():
        line = line.strip()
        if not line.startswith("gs://") or not line.endswith("/manifest.json"):
            continue
        parts = line.split("/")
        try:
            run_ids.append(parts[parts.index("runs") + 1])
        except (ValueError, IndexError):
            continue
    return sorted(set(run_ids), reverse=True)


def determine_run_stage(counts: dict, expected_task_count: int, merge_manifest: Optional[dict]) -> str:
    """Classify a run's current stage from its status/*.json task counts and
    (if present) its final/manifest_done.json — kept pure/unit-tested so the
    Streamlit status panel is just a thin rendering of one of these values:

    - "merged": final/manifest_done.json reports status "done".
    - "merge_failed": final/manifest_done.json reports status "failed"
      (cloud_merge_results.py refused to merge — e.g. a failed task).
    - "has_failed_tasks": every task reported in, but at least one failed,
      and no merge has been attempted yet.
    - "ready_to_merge": every task reported "done", none failed.
    - "running": some, but not all, tasks have reported yet.
    - "no_status_yet": nothing reported yet (job execution may still be
      provisioning, or hasn't started).
    """
    if merge_manifest is not None:
        if merge_manifest.get("status") == "done":
            return "merged"
        if merge_manifest.get("status") == "failed":
            return "merge_failed"
    reported = counts.get("done", 0) + counts.get("failed", 0)
    if reported == 0:
        return "no_status_yet"
    if expected_task_count and reported >= expected_task_count:
        return "has_failed_tasks" if counts.get("failed", 0) > 0 else "ready_to_merge"
    return "running"


# =============================================================================
# Subprocess runners
# =============================================================================

class ProcessTimeout(Exception):
    """Raised by run_streaming when cmd exceeds timeout_seconds; the process
    has already been killed by the time this is raised."""


def _reader_thread(stream, q) -> None:
    """Push 1-char chunks onto q as they arrive; q.put(None) marks EOF.

    Runs in a daemon thread because stream.read(1) blocks until data or EOF
    — there is no cross-platform non-blocking way to poll a subprocess pipe
    (select() doesn't work on Windows pipes), so the only way to let the
    *caller* apply a timeout is to do the blocking read off the main thread
    and hand chunks over via a queue the caller can poll with its own
    timeout.
    """
    try:
        while True:
            chunk = stream.read(1)
            if chunk == "":
                break
            q.put(chunk)
    finally:
        q.put(None)


def run_streaming(
    cmd: list[str], on_chunk=None, timeout_seconds: Optional[float] = None, on_tick=None,
) -> int:
    """Run cmd, calling on_chunk(str) with each raw output chunk (stdout+stderr)
    as it arrives — one character at a time, NOT line-buffered.

    `gcloud run jobs execute --wait` prints progress as a long run of dots
    with no newline in between (e.g. "Provisioning resources....done"), so a
    line-buffered reader yields nothing for the entire multi-minute phase and
    then dumps it all at once — from the UI it looks completely frozen for
    the exact phase that takes longest. Char-by-char reading makes each dot
    show up live instead.

    Once the job execution actually starts, gcloud itself goes silent until
    the whole execution finishes — no output at all, for however long that
    takes — so ``on_chunk`` alone leaves the UI with zero visibility into
    per-task progress during exactly the phase that takes longest. ``on_tick``
    (if given) is called on every poll-loop wake-up (~5x/second) regardless of
    whether a chunk arrived, so a caller can poll something else (e.g. the
    run's GCS status/ folder) on its own, self-throttled cadence — see
    ``main()``'s status-panel use below. A raising ``on_tick`` never breaks
    the run; exceptions are swallowed.

    Kills the process and raises ProcessTimeout if timeout_seconds elapses
    without the process finishing — including if it produces NO output at
    all (a plain blocking read() would never even reach the timeout check in
    that case; reading happens in a background thread specifically so the
    timeout is checked on a fixed cadence regardless of subprocess output).
    """
    import queue
    import threading
    import time

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    q: "queue.Queue" = queue.Queue()
    threading.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True).start()

    start = time.monotonic()
    poll_interval = 0.2
    try:
        while True:
            try:
                chunk = q.get(timeout=poll_interval)
            except queue.Empty:
                chunk = ""
            if chunk is None:
                break
            if chunk and on_chunk is not None:
                on_chunk(chunk)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:
                    pass
            if timeout_seconds is not None and (time.monotonic() - start) > timeout_seconds:
                proc.kill()
                proc.wait()
                raise ProcessTimeout(
                    f"'{' '.join(cmd)}' overschreed timeout van {timeout_seconds:.0f}s en is gestopt."
                )
    finally:
        if proc.poll() is None:
            proc.kill()
    proc.wait()
    return proc.returncode


def run_capture(cmd: list[str]) -> tuple[int, str]:
    """Never raises: a missing/misconfigured gcloud executable (OSError,
    e.g. FileNotFoundError) comes back as (1, error message) instead of
    crashing the caller. This matters more than it used to -- the "Status
    van een run" panel and "Recente runs" listing now call this on every
    page load/rerun (not just from an explicit button click like upload/
    execute did before), so a broken gcloud setup must degrade to a normal
    st.error/st.caption rather than a raw traceback on every render."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output


# =============================================================================
# current/ merge: download existing data, combine with this run's fresh
# export (lovable_gcs_upload.merge_company_records/rebucket_company_details
# do the actual, unit-tested merge logic — this is the I/O glue around it).
# =============================================================================

def _download_existing_current_export(
    bucket: str, country_folder: str, project: str, local_dir: Path,
) -> tuple[list[dict], dict[str, dict]]:
    """Download and parse the existing current/companies.list.json +
    current/company-details-*.json for one country.

    Returns ``([], {})`` — never raises — when nothing is there yet (the
    normal first-ever export for this country/bucket) or any download/parse
    step fails: a merge must degrade to "nothing existing" rather than ever
    block or corrupt this run's own export, same philosophy as
    enrichment_cache.py's cache-load failure handling.
    """
    import lovable_gcs_upload as lovable_gcs

    tool_cmd = lovable_gcs.resolve_gcs_upload_tool()
    if tool_cmd is None:
        return [], {}
    local_dir.mkdir(parents=True, exist_ok=True)

    list_local = local_dir / "companies.list.json"
    list_result = lovable_gcs.download_file(
        tool_cmd,
        lovable_gcs.gcs_current_path(bucket, country_folder, "companies.list.json"),
        str(list_local),
    )
    if not list_result["success"]:
        return [], {}
    try:
        existing_list_items = json.loads(list_local.read_text(encoding="utf-8"))
    except Exception:
        return [], {}
    if not isinstance(existing_list_items, list):
        return [], {}

    bucket_glob = f"gs://{bucket}/{country_folder}/current/company-details-*.json"
    rc_ls, listing = run_capture(build_list_command(bucket_glob, project))
    bucket_uris = list_existing_gcs_files(listing) if rc_ls == 0 else []

    existing_details: dict[str, dict] = {}
    for uri in bucket_uris:
        local_bucket_path = local_dir / Path(uri).name
        result = lovable_gcs.download_file(tool_cmd, uri, str(local_bucket_path))
        if not result["success"]:
            continue
        try:
            bucket_data = json.loads(local_bucket_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(bucket_data, dict):
            existing_details.update(bucket_data)

    return existing_list_items, existing_details


def _merge_export_into_existing_current(
    local_run_dir: Path, export_dir: Path, manifest: dict,
    gcs_bucket_norm: str, gcs_prefix_norm: str, bucket_size: int, project: str,
) -> tuple[Path, list[str], dict]:
    """Build the merged current/ export in its own local directory.

    ``export_dir`` (this run's own, unmerged snapshot — also used as-is for
    the archive/ upload) is left untouched. Downloads the existing
    current/ data, merges in this run's freshly exported companies
    (``lovable_gcs_upload.merge_company_records``), re-buckets
    (``rebucket_company_details``), and writes ``companies.list.json`` +
    bucket files + an ``export_manifest.json`` extended with a
    ``merge_summary`` block to a new directory.

    Returns ``(merged_dir, filenames, merge_summary)``. Never raises: a
    failure downloading the EXISTING data degrades to "nothing existing
    yet" (this run's own export becomes the entire current/ set).
    """
    import lovable_gcs_upload as lovable_gcs

    new_list_items = json.loads(
        (export_dir / "companies.list.json").read_text(encoding="utf-8"))
    new_details: dict[str, dict] = {}
    for bucket_path in sorted(export_dir.glob("company-details-*.json")):
        new_details.update(json.loads(bucket_path.read_text(encoding="utf-8")))

    existing_dir = local_run_dir / "lovable_current_existing"
    existing_list_items, existing_details = _download_existing_current_export(
        gcs_bucket_norm, gcs_prefix_norm, project, existing_dir)

    merged_items, merged_details = lovable_gcs.merge_company_records(
        existing_list_items, existing_details, new_list_items, new_details)
    merged_items, merged_buckets = lovable_gcs.rebucket_company_details(
        merged_items, merged_details, bucket_size)

    merged_dir = local_run_dir / "lovable_export_merged_current"
    merged_dir.mkdir(parents=True, exist_ok=True)
    (merged_dir / "companies.list.json").write_text(
        json.dumps(merged_items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for bucket_file, bucket_data in merged_buckets.items():
        (merged_dir / bucket_file).write_text(
            json.dumps(bucket_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    existing_by_id = {i["company_id"]: i for i in existing_list_items}
    new_by_id = {i["company_id"]: i for i in new_list_items}
    existing_ids, new_ids = set(existing_by_id), set(new_by_id)
    merge_summary = {
        "companies_before": len(existing_list_items),
        "added": len(new_ids - existing_ids),
        "updated": 0,
        "kept_richer_existing": 0,
        "total_after": len(merged_items),
    }
    # Re-derive updated-vs-kept-richer for reporting by re-applying the same
    # winner rule merge_company_records already used — cheap, both sides are
    # already in hand, and this avoids merge_company_records having to
    # return bookkeeping counters alongside its actual merged data.
    for cid in (new_ids & existing_ids):
        new_skipped = bool(new_by_id[cid].get("enrichment_skipped"))
        old_skipped = bool(existing_by_id[cid].get("enrichment_skipped"))
        if (not new_skipped) or old_skipped:
            merge_summary["updated"] += 1
        else:
            merge_summary["kept_richer_existing"] += 1

    merged_manifest = dict(manifest)
    merged_manifest["merged_into_existing_current"] = True
    merged_manifest["merge_summary"] = merge_summary
    (merged_dir / "export_manifest.json").write_text(
        json.dumps(merged_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return merged_dir, sorted(f.name for f in merged_dir.iterdir()), merge_summary


# =============================================================================
# Run lifecycle: non-blocking dispatch + on-demand merge/export.
#
# main()'s "Start Cloud Run" button only uploads the input and starts the
# Cloud Run Job execution (build_execute_command's wait=False) -- it no
# longer blocks on the whole run. Everything below (merge, Lovable export,
# GCS upload) instead runs later, triggered from the "Status" panel keyed by
# run_id, which survives a closed/reopened browser tab via the URL's
# ?run_id= query param (see main()). The Cloud Run Job itself keeps running
# server-side regardless of whether this Streamlit process is even alive,
# so a laptop going to sleep mid-run never loses anything -- merging and
# exporting simply happen whenever you come back to this page.
# =============================================================================

def finish_run_merge(bucket: str, project: str, run_id: str, task_count: int, work_dir: Path) -> dict:
    """Download this run's part/status files locally, merge them into one
    final Excel (reusing cloud_merge_results.py's own merge logic), and push
    the result back to GCS under FINAL_OUTPUT_NAME so it's discoverable
    later purely from run_id. Returns
    ``{"ok": bool, "final_local": Path | None, "output_dir": str,
    "row_count": int | None, "error": str | None}`` -- never raises."""
    import cloud_merge_results as cmr

    output_dir = gcs_output_dir(bucket, run_id)
    local_run_dir = work_dir / "run"
    local_parts = local_run_dir / "parts"
    local_status = local_run_dir / "status"
    local_parts.mkdir(parents=True, exist_ok=True)
    local_status.mkdir(parents=True, exist_ok=True)

    rc, output = run_capture(build_download_command(
        join_path(output_dir, "parts", "*.xlsx"), str(local_parts) + "/", project))
    if rc != 0:
        return {"ok": False, "final_local": None, "output_dir": output_dir,
                "row_count": None, "error": f"Ophalen van part-bestanden mislukt:\n{output}"}
    run_capture(build_download_command(
        join_path(output_dir, "status", "*_done.json"), str(local_status) + "/", project))
    # No _failed.json files is the normal, successful case -- a non-zero
    # exit here (glob matched nothing) is expected, not an error.
    run_capture(build_download_command(
        join_path(output_dir, "status", "*_failed.json"), str(local_status) + "/", project))

    merge_rc = cmr.main([
        "--run-id", run_id,
        "--output-dir", str(local_run_dir),
        "--expected-task-count", str(int(task_count)),
    ])
    if merge_rc != 0:
        return {"ok": False, "final_local": None, "output_dir": output_dir,
                "row_count": None,
                "error": "Merge mislukt — controleer of er een gefaalde task tussen zit."}

    final_local = local_run_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME
    import pandas as pd
    try:
        row_count = len(pd.read_excel(final_local, sheet_name=cmr.ENRICHED_LEADS_SHEET_NAME))
    except Exception:
        row_count = None

    run_capture(build_upload_command(
        str(final_local), join_path(output_dir, "final", FINAL_OUTPUT_NAME), project))

    return {"ok": True, "final_local": final_local, "output_dir": output_dir,
            "row_count": row_count, "error": None}


def run_lovable_export_and_upload(
    final_local: Path, work_dir: Path, export_country: str, cold_callers_raw: str,
    foreign_hq_only_export: bool, bucket_size: int, content_language: str,
    export_gcs_bucket: str, export_gcs_prefix: str, export_gcs_run_folder: str,
    merge_current: bool, upload_current: bool, upload_archive: bool, project: str,
) -> dict:
    """Run the Lovable JSON export on an already-merged final Excel, then
    (if configured) merge with/overwrite current/ in GCS, upload current/ +
    the run's archive/ snapshot, and update the screened-domains ledger.
    Returns a dict describing what happened; every step's own failure is
    captured in the returned dict rather than raised, so one broken step
    (e.g. a bad GCS prefix) never hides whether the export itself worked."""
    import export_lead_prioritizer_to_lovable_json as lovable_export
    import lovable_gcs_upload as lovable_gcs
    import screened_domains_ledger
    import pandas as pd
    from lead_prioritizer_batch_app import parse_cold_callers

    cold_callers = parse_cold_callers(cold_callers_raw)
    if not export_country:
        return {"ok": False, "error": "Geen export country ingevuld."}
    if not cold_callers:
        return {"ok": False, "error": "Minimaal één cold caller is verplicht."}

    export_dir = work_dir / "lovable_export"
    try:
        manifest = lovable_export.export_workbook_to_lovable_json(
            input_xlsx=final_local,
            output_dir=export_dir,
            export_country=export_country,
            cold_callers=cold_callers,
            foreign_hq_only=bool(foreign_hq_only_export),
            bucket_size=int(bucket_size),
            content_language=content_language,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Lovable JSON-export mislukt: {exc}"}

    result: dict = {
        "ok": True, "manifest": manifest, "export_dir": export_dir,
        "uploaded": False, "merge_summary": None, "upload_failures": [],
        "ledger_updated_count": 0,
    }

    gcs_bucket_norm = export_gcs_bucket.strip()
    gcs_prefix_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_prefix)
    gcs_run_folder_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_run_folder)

    if gcs_bucket_norm and gcs_prefix_norm:
        try:
            enriched_rows = pd.read_excel(final_local, sheet_name="Enriched Leads").to_dict("records")
        except Exception:
            enriched_rows = []
        ledger_updates = screened_domains_ledger.build_ledger_updates(enriched_rows)
        if ledger_updates:
            save_result = screened_domains_ledger.save_ledger(
                gcs_bucket_norm, gcs_prefix_norm, ledger_updates)
            if save_result.get("success"):
                result["ledger_updated_count"] = len(ledger_updates)

    validation = manifest.get("validation_summary", {}) or {}
    validation_ok = (
        validation.get("status") == "ok"
        and int(validation.get("structural_errors", 0) or 0) == 0
    )
    can_upload = (
        validation_ok and bool(gcs_bucket_norm) and bool(gcs_prefix_norm)
        and (upload_current or upload_archive)
    )
    if not can_upload:
        result["upload_skipped_reason"] = (
            "Exportvalidatie of bucket/prefix-instellingen waren niet in orde."
        )
        return result

    output_filenames = sorted({Path(p).name for p in manifest.get("output_files", [])})
    current_export_dir = export_dir
    current_filenames = output_filenames
    if upload_current and merge_current:
        current_export_dir, current_filenames, merge_summary = \
            _merge_export_into_existing_current(
                work_dir, export_dir, manifest,
                gcs_bucket_norm, gcs_prefix_norm, int(bucket_size), project,
            )
        result["merge_summary"] = merge_summary

    current_jobs = lovable_gcs.build_upload_plan(
        current_export_dir, current_filenames, gcs_bucket_norm,
        gcs_prefix_norm, gcs_run_folder_norm,
        upload_current=upload_current, upload_archive=False,
    )
    archive_jobs = lovable_gcs.build_upload_plan(
        export_dir, output_filenames, gcs_bucket_norm, gcs_prefix_norm,
        gcs_run_folder_norm,
        upload_current=False, upload_archive=upload_archive,
    )
    upload_results = lovable_gcs.run_upload_plan(current_jobs + archive_jobs)
    result["uploaded"] = True
    result["upload_failures"] = [r for r in upload_results if not r["success"]]
    return result


# =============================================================================
# Skip-already-enriched pre-filter: cheaper reruns when merging (only
# processes rows whose company isn't already fully enriched in current/,
# instead of re-running the whole pipeline every time and merging after the
# fact). Pure/unit-tested; no GCS access here -- callers pass in the
# already-downloaded current/ list items.
# =============================================================================

def known_enriched_company_ids(existing_list_items: list[dict]) -> set[str]:
    """``company_id``s from an existing current/ export that are NOT
    ``enrichment_skipped`` — i.e. actually fully enriched, safe to skip
    re-processing. A thin/gate-skipped existing entry is deliberately
    excluded so it keeps getting retried by future runs instead of staying
    permanently thin."""
    return {
        item["company_id"] for item in existing_list_items
        if isinstance(item, dict) and item.get("company_id")
        and not item.get("enrichment_skipped")
    }


def split_rows_by_existing_enrichment(
    df, domain_column: str, known_company_ids: set[str],
    known_domestic_domains: Optional[set] = None,
):
    """Split ``df`` into ``(to_process, already_enriched)``.

    A row is already-known (goes to the second frame) when its domain
    matches EITHER:
      - ``known_company_ids`` — run through the exact same
        ``export_lead_prioritizer_to_lovable_json.slugify`` normalization
        ``make_company_id`` uses (a company already fully enriched in
        current/), or
      - ``known_domestic_domains`` — run through
        ``screened_domains_ledger.normalize_domain`` (a company already
        settled as definitely NOT foreign-HQ; only meaningful/passed by
        the caller when this run also excludes non-foreign companies from
        the export — see cloud_run_streamlit_app.main).

    A row with a blank/missing domain always goes to ``to_process`` (never
    silently dropped just because its id can't be determined). Never
    mutates ``df``.
    """
    import export_lead_prioritizer_to_lovable_json as lovable_export
    from screened_domains_ledger import normalize_domain

    known_domestic_domains = known_domestic_domains or set()

    def _row_known(value) -> bool:
        text = str(value if value is not None else "").strip()
        if not text or text.lower() == "nan":
            return False
        if lovable_export.slugify(text) in known_company_ids:
            return True
        return normalize_domain(text) in known_domestic_domains

    mask = df[domain_column].apply(_row_known)
    return df[~mask].copy(), df[mask].copy()


# =============================================================================
# Streamlit UI
# =============================================================================

_RUN_MODE_HELP = {
    "full": "Volledige v2-pipeline: HQ + non-HQ evidence/signalen + score + caller-app-velden.",
    "hq_only": "Alleen HQ-detectie (goedkoop screenen, geen scoring).",
    "evidence_only": "HQ + non-HQ evidence verzamelen, geen signalen/score.",
    "signals_no_score": "HQ + evidence + signalen + app-samenvatting, geen commercial-fit-score.",
    "full_no_score": "Alles behalve de commercial-fit-score (wel caller-app-velden).",
}


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import pandas as pd
    import streamlit as st

    import export_lead_prioritizer_to_lovable_json as lovable_export
    import lovable_gcs_upload as lovable_gcs
    import screened_domains_ledger
    from lead_prioritizer_batch_app import (
        DEFAULT_COLD_CALLERS_TEXT,
        default_gcs_country_prefix,
        suggest_country_from_filename,
        SUPPORTED_DEFAULT_INPUT_COUNTRIES,
    )

    st.set_page_config(page_title="Lead Prioritizer — Cloud Run", page_icon="☁️", layout="wide")
    st.title("☁️ Lead Prioritizer — Cloud Run Jobs")
    st.caption(
        "Upload een Excel, draai de v2-pipeline parallel op Cloud Run Jobs, en "
        "download het samengevoegde eindresultaat. Zie docs/cloud_run_workflow.md "
        "voor de architectuur; dit is een UI bovenop dezelfde stappen."
    )

    # Read BEFORE the sidebar (even though it visually renders in the main
    # area, via st.file_uploader not st.sidebar.file_uploader) so the
    # sidebar's "Export country"/"GCS prefix" defaults below can actually use
    # the filename-based guess. Doing this the other way around silently
    # exported every run to gs://<bucket>/unknown/current/ instead of e.g.
    # .../switzerland/current/ -- the guess was computed, just never fed back
    # into the GCS-prefix widget, which had already rendered by then.
    uploaded = st.file_uploader("Input-Excel (.xlsx)", type=["xlsx"])
    _guessed_export_country = (
        suggest_country_from_filename(uploaded.name, SUPPORTED_DEFAULT_INPUT_COUNTRIES)
        if uploaded is not None else ""
    )
    _uploaded_key = uploaded.name if uploaded is not None else "none"

    with st.sidebar:
        st.header("Instellingen")
        project = st.text_input("GCP-project", value=DEFAULT_PROJECT)
        region = st.text_input("Regio", value=DEFAULT_REGION)
        bucket = st.text_input("Bucket", value=DEFAULT_BUCKET)
        job_name = st.text_input("Cloud Run Job", value=DEFAULT_JOB_NAME)
        st.divider()
        task_count = st.number_input(
            "Task count", min_value=1, max_value=50, value=10, step=1,
            help="10 / 25 / 50 zijn de aanbevolen stappen — zie 'Eerste veilige "
                 "instellingen' in de doc. Hoger = sneller maar zwaardere "
                 "Firecrawl/Serper-belasting.",
        )
        try:
            from lead_prioritizer_batch_core import SUPPORTED_RUN_MODES
            mode_options = sorted(SUPPORTED_RUN_MODES)
        except Exception:
            mode_options = ["full", "hq_only", "evidence_only", "signals_no_score", "full_no_score"]
        mode = st.selectbox(
            "Mode", options=mode_options,
            index=mode_options.index("full") if "full" in mode_options else 0,
            help=_RUN_MODE_HELP.get(mode_options[0], ""),
        )
        st.caption(_RUN_MODE_HELP.get(mode, ""))

        total_row_limit = st.number_input(
            "Row limit (totaal, 0 = alle rijen)", min_value=0, value=0, step=1,
            help="Verwerk alleen de eerste N rijen van het bestand — dit gebeurt "
                 "VOORDAT het over de taken wordt verdeeld, dus N wordt evenredig "
                 "gespreid over 'Task count' hierboven (bv. 100 rijen / 50 taken = "
                 "~2 rijen per taak). 0 = het hele bestand.",
        )

        st.divider()
        st.subheader("Scope")
        # ONE choice drives both the enrichment gate AND the export filter --
        # deliberately not two separate checkboxes. Having them as two
        # independently-toggleable settings is exactly what let a run
        # fully-enrich AND publish confirmed-domestic companies (e.g.
        # "Molins", "Confirmed domestic") into the live Lovable app: the
        # cost gate was on, but the separate "Foreign-HQ-only export"
        # checkbox was left off, so nothing filtered them out of current/.
        # Tying them to one variable makes that mismatch structurally
        # impossible instead of relying on remembering to check both.
        scope_choice = st.radio(
            "Welke bedrijven wil je verwerken en publiceren?",
            options=["Alle bedrijven", "Alleen buitenlands HQ"],
            index=0,
            help="**Alle bedrijven**: iedereen krijgt de volledige v2-pipeline "
                 "en iedereen komt ook in de Lovable-lijst — simpelste optie, "
                 "duurste. **Alleen buitenlands HQ**: goedkope HQ-screening "
                 "voor iedereen eerst; de dure volledige pipeline "
                 "(Firecrawl/Anthropic-signalen/score/caller-content) draait "
                 "daarna ALLEEN nog voor bevestigd-buitenlandse bedrijven, en "
                 "ALLEEN die komen ook in de Lovable-lijst terecht. Deze ene "
                 "keuze stuurt zowel de kostengate als de Lovable-export-"
                 "filter tegelijk.",
        )
        gate_full_enrichment_on_foreign_hq = scope_choice == "Alleen buitenlands HQ"

        st.divider()
        st.subheader("Inhoud & AI-opties")
        compose_caller_content = st.checkbox(
            "Compose caller content via AI (Step 3)", value=True,
            help="why_relevant/what_is_hot/cold_caller_summary/caller_angle/call_starter "
                 "via Anthropic i.p.v. deterministische templates.")
        rich_icp_context = st.checkbox("Rijkere ICP-context via AI", value=True)
        ai_signal_scoring = st.checkbox(
            "AI-signaalscoring (opt-in) — verandert scores", value=True,
            help="Vervangt de deterministische keyword-signaalscoring door een "
                 "AI-oordeel; dit is de enige optie hier die final_commercial_fit_score "
                 "kan laten afwijken van de standaardmodus.")

        st.divider()
        use_enrichment_cache = st.checkbox(
            "Gebruik gedeelde enrichment-cache (GCS, per land)", value=True)
        enrichment_cache_bucket = st.text_input(
            "GCS bucket voor enrichment-cache",
            value=lovable_gcs.DEFAULT_GCS_BUCKET,
            disabled=not use_enrichment_cache,
        )

        st.divider()
        deep_dive = st.checkbox("Deep dive voor top-leads (opt-in)", value=True)
        deep_dive_min_score = 8.0
        deep_dive_on_foreign_hq = True
        if deep_dive:
            deep_dive_min_score = st.number_input(
                "Deep dive score threshold", value=8.0, step=0.5, format="%.2f",
                help="Alleen rijen met final_commercial_fit_score op of boven "
                     "deze drempel krijgen een deep dive (tot 6 extra "
                     "Firecrawl-pagina's per lead). 0 = álle rijen — in een "
                     "cloud-run met 10-50 parallelle tasks loopt dat hard "
                     "tegen de Firecrawl-rate-limits en de task-timeout aan; "
                     "8.0 is de default van de cloud-runner.")
            deep_dive_on_foreign_hq = st.checkbox(
                "Also trigger on confirmed foreign HQ", value=True)

        st.divider()
        c5_enabled = st.checkbox(
            "Use C5 Sonnet adjudication", value=True,
            help="Vaste instellingen (zoals de lokale app dagelijks gebruikt): "
                 "conservative_adjustment, rows=score_3_or_manual_review, "
                 "model tier=Sonnet, geen model-override.")
        if gate_full_enrichment_on_foreign_hq and c5_enabled:
            st.caption(
                "'Alleen buitenlands HQ volledig verrijken' + C5 samen: C5 "
                "draait bij deze combinatie VÓÓR de HQ-gate-beslissing, dus "
                "een grensgeval dat C5 als buitenlands HQ bevestigt, wordt "
                "alsnog volledig verrijkt in plaats van te blijven steken op "
                "enrichment_skipped=True."
            )

        st.divider()
        st.subheader("Lovable JSON-export + GCS-upload")
        st.caption(
            "Deze instellingen sturen alleen de pre-flight skip-filter en "
            "archive-conflict-check hieronder aan (vóór de dure run). De "
            "daadwerkelijke export/upload gebeurt niet meer automatisch na "
            "afloop — dat doe je on-demand in het 'Status van een run'-"
            "paneel bovenaan, zodra de run klaar is (ook nuttig als je "
            "laptop tussentijds sliep en je pas later terugkomt)."
        )
        auto_lovable_export_enabled = st.checkbox(
            "Skip-filter/archive-check hieronder gebruiken", value=True)
        export_country = ""
        # Derived from the "Scope" choice above, not a separate checkbox --
        # see the comment there for why these two must never drift apart.
        foreign_hq_only_export = gate_full_enrichment_on_foreign_hq
        auto_gcs_upload_enabled = True
        export_gcs_bucket = lovable_gcs.DEFAULT_GCS_BUCKET
        export_gcs_prefix = ""
        export_gcs_run_folder = ""
        upload_archive = True
        if auto_lovable_export_enabled:
            _country_options = [""] + list(SUPPORTED_DEFAULT_INPUT_COUNTRIES)
            _guessed_index = (
                _country_options.index(_guessed_export_country)
                if _guessed_export_country in _country_options else 0
            )
            export_country = st.selectbox(
                "Export country", options=_country_options,
                index=_guessed_index,
                # Keyed by the uploaded filename so a NEW file re-triggers the
                # guessed default instead of sticking to whatever an earlier
                # file's widget state was (Streamlit keeps widget state across
                # reruns once a user has interacted with it).
                key=f"export_country_{_uploaded_key}",
                help="Automatisch geraden uit de bestandsnaam; pas aan indien nodig. "
                     "Bepaalt ook het GCS-pad hieronder (gs://<bucket>/<dit land>/...) "
                     "— leeg laten stuurt de export naar .../unknown/.")
            st.caption(
                "Foreign-HQ-only export: "
                f"**{'aan' if foreign_hq_only_export else 'uit'}** "
                "(afgeleid van de Scope-keuze hierboven — "
                f"“{scope_choice}”)."
            )
            auto_gcs_upload_enabled = st.checkbox(
                "GCS-prefix hieronder ook gebruiken voor de archive-conflict-check", value=True)
            if auto_gcs_upload_enabled:
                export_gcs_bucket = st.text_input(
                    "GCS bucket (Lovable-export)", value=lovable_gcs.DEFAULT_GCS_BUCKET)
                export_gcs_prefix = st.text_input(
                    "GCS prefix/pad (bv. <land>)",
                    value=default_gcs_country_prefix(export_country),
                    # Keyed like "Export country" above -- without this, a
                    # different Export country on a later run (e.g. a second
                    # test with a different input file) would NOT refresh
                    # this field, since Streamlit text_input keeps whatever
                    # the user/previous default already put here.
                    key=f"export_gcs_prefix_{_uploaded_key}_{export_country}")
                export_gcs_run_folder = st.text_input(
                    "GCS run folder",
                    # Includes the time, not just the date -- default_gcs_run_folder()
                    # alone is <date>_<mode> (e.g. "2026-07-09_full"), so a second
                    # run of the same country/mode on the same day would target the
                    # SAME archive folder and trigger the overwrite-confirmation
                    # warning below every time. Appending HHMMSS makes each run's
                    # default archive folder unique, so that warning only fires when
                    # someone deliberately reuses a folder name (edit this field
                    # yourself to bundle several runs into one archive folder).
                    value=(
                        f"{lovable_gcs.default_gcs_run_folder(mode)}_"
                        f"{datetime.now().strftime('%H%M%S')}"
                    ))
                upload_archive = st.checkbox("Archive naar runs/<run_folder>/", value=True)

    # Built once, right after the sidebar, so both the "Start Cloud Run"
    # dispatch below AND the retry-failed-shards button in the status panel
    # can reuse the exact same env overrides without duplicating this dict.
    extra_env = {
        "COMPOSE_CALLER_CONTENT": str(bool(compose_caller_content)).lower(),
        "RICH_ICP_CONTEXT": str(bool(rich_icp_context)).lower(),
        "AI_SIGNAL_SCORING": str(bool(ai_signal_scoring)).lower(),
        "USE_ENRICHMENT_CACHE": str(bool(use_enrichment_cache)).lower(),
        "ENRICHMENT_CACHE_BUCKET": enrichment_cache_bucket if use_enrichment_cache else "",
        "DEEP_DIVE": str(bool(deep_dive)).lower(),
        "DEEP_DIVE_MIN_SCORE": str(deep_dive_min_score),
        "DEEP_DIVE_ON_FOREIGN_HQ": str(bool(deep_dive_on_foreign_hq)).lower(),
        "C5_ENABLED": str(bool(c5_enabled)).lower(),
        "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ": str(bool(gate_full_enrichment_on_foreign_hq)).lower(),
    }
    if total_row_limit:
        extra_env["TOTAL_ROW_LIMIT"] = str(int(total_row_limit))

    # =========================================================================
    # Status panel: keyed by run_id, not by browser session. A run dispatched
    # via the button below stores its run_id in the URL (?run_id=...) and
    # reruns into this same panel -- reopening that URL later (after a
    # laptop sleep/reboot/closed tab) lands right back here, because
    # everything it shows/does is re-derived from GCS, never from Streamlit
    # session state alone. "Recente runs" covers the case where the URL
    # itself got lost.
    # =========================================================================

    def _render_lovable_export_form(run_id: str, merge_manifest: Optional[dict]) -> None:
        guessed_country = suggest_country_from_filename(run_id, SUPPORTED_DEFAULT_INPUT_COUNTRIES)
        country_options = [""] + list(SUPPORTED_DEFAULT_INPUT_COUNTRIES)
        country = st.selectbox(
            "Export country", options=country_options,
            index=country_options.index(guessed_country) if guessed_country in country_options else 0,
            key=f"export_country_status_{run_id}")
        cold_callers_raw = st.text_input(
            "Cold callers (comma-separated)", value=DEFAULT_COLD_CALLERS_TEXT,
            key=f"cold_callers_status_{run_id}")
        foreign_hq_only = st.checkbox(
            "Foreign-HQ-only export", value=True, key=f"foreign_hq_status_{run_id}")
        gcs_bucket_export = st.text_input(
            "GCS bucket", value=lovable_gcs.DEFAULT_GCS_BUCKET, key=f"gcs_bucket_status_{run_id}")
        gcs_prefix_export = st.text_input(
            "GCS prefix (bv. <land>)", value=default_gcs_country_prefix(country),
            key=f"gcs_prefix_status_{run_id}")
        gcs_run_folder_export = st.text_input(
            "GCS run folder", value=run_id, key=f"gcs_run_folder_status_{run_id}")
        current_choice = st.radio(
            "current/-gedrag", ["Overschrijven", "Mergen (bestaande data behouden)"],
            horizontal=True, key=f"current_choice_status_{run_id}")

        if st.button("📤 Exporteren & uploaden", key=f"do_export_{run_id}"):
            final_bytes = st.session_state.get(f"final_bytes_{run_id}")
            if not final_bytes:
                final_output_uri = (merge_manifest or {}).get("final_output_uri")
                if not final_output_uri:
                    st.error("Kon het eindresultaat niet vinden — haal het eerst op via de downloadknop hierboven.")
                    st.stop()
                fetch_dir = Path(tempfile.mkdtemp(prefix="cloud_run_status_fetch_"))
                local_final = fetch_dir / Path(final_output_uri).name
                rc, output = run_capture(build_download_command(final_output_uri, str(local_final), project))
                if rc != 0:
                    st.error(f"Ophalen eindresultaat mislukt:\n{output}")
                    st.stop()
                final_bytes = local_final.read_bytes()
                st.session_state[f"final_bytes_{run_id}"] = final_bytes

            export_work_dir = Path(tempfile.mkdtemp(prefix="cloud_run_status_export_"))
            local_final_for_export = export_work_dir / "final.xlsx"
            local_final_for_export.write_bytes(final_bytes)
            with st.spinner("Lovable JSON exporteren + uploaden…"):
                export_result = run_lovable_export_and_upload(
                    local_final_for_export, export_work_dir, country, cold_callers_raw,
                    foreign_hq_only, 500, lovable_export.DEFAULT_CONTENT_LANGUAGE,
                    gcs_bucket_export, gcs_prefix_export, gcs_run_folder_export,
                    merge_current=current_choice.startswith("Mergen"),
                    upload_current=True, upload_archive=True, project=project,
                )
            if not export_result.get("ok"):
                st.error(export_result.get("error", "Onbekende fout bij export."))
            else:
                msg = "Export + upload voltooid."
                if export_result.get("merge_summary"):
                    ms = export_result["merge_summary"]
                    msg += (f" ({ms['added']} nieuw, {ms['updated']} bijgewerkt, "
                            f"{ms['kept_richer_existing']} bestaande versie behouden)")
                if export_result.get("upload_failures"):
                    st.warning(f"{msg} Let op: {len(export_result['upload_failures'])} upload(s) mislukten.")
                else:
                    st.success(msg)

    def _render_run_status(run_id: str) -> None:
        output_dir = gcs_output_dir(bucket, run_id)
        st.caption(f"`{output_dir}`")
        manifest = read_gcs_json(join_path(output_dir, "manifest.json"), project)
        expected_task_count = (
            int(manifest["task_count"]) if manifest and manifest.get("task_count") else int(task_count)
        )

        if st.button("🔄 Ververs status", key=f"refresh_{run_id}"):
            st.rerun()

        rc_ls, listing = run_capture(build_list_command(
            join_path(output_dir, "status", "*.json"), project))
        counts = count_task_statuses(listing) if rc_ls == 0 else {"running": 0, "done": 0, "failed": 0}
        merge_manifest = read_gcs_json(join_path(output_dir, "final", "manifest_done.json"), project)
        stage = determine_run_stage(counts, expected_task_count, merge_manifest)
        reported = counts["done"] + counts["failed"]

        if stage == "no_status_yet":
            st.info("⏳ Nog geen enkele taak heeft gerapporteerd — de job wordt mogelijk nog geprovisioneerd.")
        elif stage == "running":
            st.info(
                f"📊 Bezig: {counts['done']} klaar, {counts['failed']} gefaald, "
                f"{counts['running']} bezig ({reported}/{expected_task_count} gerapporteerd)."
            )
        elif stage == "ready_to_merge":
            st.success(f"✅ Alle {expected_task_count} taken zijn klaar, geen fouten.")
            if st.button("🔀 Mergen & afronden", key=f"merge_{run_id}", type="primary"):
                work_dir = Path(tempfile.mkdtemp(prefix="cloud_run_status_merge_"))
                with st.spinner("Resultaten mergen…"):
                    merge_result = finish_run_merge(bucket, project, run_id, expected_task_count, work_dir)
                if not merge_result["ok"]:
                    st.error(merge_result["error"])
                else:
                    st.session_state[f"final_bytes_{run_id}"] = merge_result["final_local"].read_bytes()
                    st.rerun()
        elif stage == "merged":
            row_count = merge_manifest.get("row_count") if merge_manifest else None
            st.success(f"✅ Klaar — {row_count if row_count is not None else '?'} rijen samengevoegd.")
            final_bytes = st.session_state.get(f"final_bytes_{run_id}")
            if not final_bytes and st.button("⬇️ Eindresultaat ophalen", key=f"fetch_final_{run_id}"):
                final_output_uri = (merge_manifest or {}).get("final_output_uri")
                fetch_dir = Path(tempfile.mkdtemp(prefix="cloud_run_status_fetch_"))
                local_final = fetch_dir / Path(final_output_uri).name
                rc, output = run_capture(build_download_command(final_output_uri, str(local_final), project))
                if rc != 0:
                    st.error(f"Ophalen mislukt:\n{output}")
                else:
                    st.session_state[f"final_bytes_{run_id}"] = local_final.read_bytes()
                    st.rerun()
            final_bytes = st.session_state.get(f"final_bytes_{run_id}")
            if final_bytes:
                st.download_button(
                    "💾 Download eindresultaat (.xlsx)", data=final_bytes,
                    file_name=f"{run_id}_prioritized.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"download_final_{run_id}",
                )
            with st.expander("📤 Lovable JSON-export (on-demand)"):
                _render_lovable_export_form(run_id, merge_manifest)
        elif stage == "merge_failed":
            st.error(f"❌ Merge mislukt: {(merge_manifest or {}).get('error', '')}")

        if counts["failed"] > 0 and stage != "merged":
            st.warning(
                f"⚠️ {counts['failed']} taak/taken definitief gefaald (na retries). "
                f"De {counts['done']} gelukte delen staan veilig in `{output_dir}/parts/` "
                "en worden bij een herstart automatisch overgeslagen."
            )
            if st.button(
                "🔁 Ontbrekende/mislukte shards opnieuw draaien (huidige sidebar-instellingen)",
                key=f"retry_{run_id}",
            ):
                input_uri = (manifest or {}).get("input_uri")
                if not input_uri:
                    st.error("Kon de input-URI van deze run niet vinden in manifest.json — kan niet herstarten.")
                else:
                    rc, output = run_capture(build_execute_command(
                        job_name, project, region, input_uri, output_dir,
                        run_id, expected_task_count, mode, extra_env=extra_env,
                    ))
                    if rc == 0:
                        st.success("Opnieuw gestart — ververs de status over een tijdje.")
                    else:
                        st.error(f"Herstart mislukt:\n{output}")

    st.divider()
    st.subheader("📡 Status van een run")
    run_id_from_url = st.query_params.get("run_id", "")
    run_id_input = st.text_input(
        "Run-ID (staat in de URL na het starten van een run, of kies hieronder)",
        value=run_id_from_url,
    )
    if run_id_input and run_id_input != run_id_from_url:
        st.query_params["run_id"] = run_id_input
    if run_id_from_url and st.button("🆕 Nieuwe run starten (status wissen)"):
        st.query_params.clear()
        st.rerun()

    with st.expander("🕘 Recente runs", expanded=not run_id_input):
        rc_ls, listing = run_capture(build_list_command(f"gs://{bucket}/runs/*/manifest.json", project))
        recent_run_ids = parse_run_ids_from_manifest_listing(listing) if rc_ls == 0 else []
        if not recent_run_ids:
            st.caption("Geen eerdere runs gevonden onder deze bucket/project (of nog niet opgehaald).")
        for rid in recent_run_ids[:20]:
            st.markdown(f"- [`{rid}`](?run_id={rid})")

    if run_id_input:
        _render_run_status(run_id_input)

    # ---- Explicit main-panel choice: overwrite or merge current/ ----------
    # Moved out of the sidebar so it can't be missed: this decides whether a
    # rerun of the same country replaces the existing current/ company list
    # in GCS, or combines it with what's already there (see
    # lovable_gcs_upload.merge_company_records for the merge rule). Shown
    # unconditionally (not only on a detected conflict, unlike the archive
    # check below) since it's a real decision on every run, not just an
    # edge case.
    merge_current = False
    skip_already_enriched = False
    if uploaded is not None and auto_lovable_export_enabled and auto_gcs_upload_enabled:
        st.subheader("current/-gedrag")
        current_choice = st.radio(
            "Wat moet er met de bestaande current/-lijst in GCS gebeuren?",
            options=["Overschrijven", "Mergen (bestaande data behouden)"],
            index=0,
            horizontal=True,
            help="**Overschrijven**: current/ volledig vervangen door "
                 "alleen het resultaat van DEZE run (het oude gedrag). "
                 "**Mergen**: bestaande current/-data eerst downloaden en "
                 "samenvoegen met deze run — nieuwe bedrijven worden "
                 "toegevoegd, bestaande bedrijven bijgewerkt met de nieuwe "
                 "data TENZIJ die dunner is (bv. door de foreign-HQ-gate "
                 "overgeslagen) dan wat er al stond, dan blijft de rijkere "
                 "oude versie staan. Cold-caller-toewijzingen van bedrijven "
                 "die al bestonden blijven ongewijzigd.",
        )
        merge_current = current_choice.startswith("Mergen")
        if merge_current:
            skip_already_enriched = st.checkbox(
                "Bedrijven die al volledig verrijkt in current/ staan overslaan "
                "(goedkoper: alleen nieuwe/nog-niet-verrijkte rijen verwerken)",
                value=True,
                help="Vóór de Cloud Run Job start: download current/, en "
                     "verwijder uit het invoerbestand elke rij waarvan het "
                     "bedrijf (op domein) al in current/ staat MET "
                     "enrichment_skipped=False (dus echt volledig verrijkt, "
                     "niet eerder door de foreign-HQ-gate overgeslagen — zo'n "
                     "dunne entry wordt gewoon opnieuw geprobeerd). Bespaart "
                     "Serper/Firecrawl/Anthropic-kosten voor bedrijven die al "
                     "goed staan. De overgeslagen bedrijven blijven na de "
                     "merge gewoon in current/ staan, ongewijzigd. Staat "
                     "'Foreign-HQ-only export' hieronder ook aan, dan worden "
                     "daarnaast bedrijven overgeslagen die in een vorige run "
                     "al definitief als NIET-buitenlands zijn vastgesteld "
                     "(uit een aparte, altijd-complete ledger — die bedrijven "
                     "komen bij 'Foreign-HQ-only export' toch nooit in "
                     "current/ terecht, dus current/ alleen zou ze steeds "
                     "opnieuw laten screenen).",
            )

    # ---- Pre-flight: warn if the Lovable ARCHIVE folder already has data ---
    # The archive folder (runs/<run_folder>/, keyed by date+mode by default)
    # is meant to be a per-run historical snapshot regardless of the
    # current/-gedrag choice above -- a second run of the same country/mode
    # on the same day would otherwise silently overwrite an earlier run's
    # archived export with no warning at all. Runs this check on every
    # rerun (not just on click) so the warning is visible BEFORE the user
    # commits to starting an expensive Cloud Run Job, not only at upload
    # time at the very end of the pipeline.
    archive_conflict_files: list[str] = []
    overwrite_confirmed = True
    if (uploaded is not None and auto_lovable_export_enabled
            and auto_gcs_upload_enabled and upload_archive):
        gcs_bucket_norm = export_gcs_bucket.strip()
        gcs_prefix_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_prefix)
        gcs_run_folder_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_run_folder)
        if gcs_bucket_norm and gcs_prefix_norm and gcs_run_folder_norm:
            archive_glob = f"gs://{gcs_bucket_norm}/{gcs_prefix_norm}/runs/{gcs_run_folder_norm}/*"
            rc_ls, listing = run_capture(build_list_command(archive_glob, project))
            archive_conflict_files = list_existing_gcs_files(listing) if rc_ls == 0 else []
        if archive_conflict_files:
            st.warning(
                f"⚠️ Er staat al Lovable-archiefdata in `{gcs_prefix_norm}/runs/"
                f"{gcs_run_folder_norm}/` ({len(archive_conflict_files)} bestand(en)) — "
                "waarschijnlijk van een eerdere run van dit land/deze mode, vandaag. "
                "Een nieuwe run overschrijft die archiefdata."
            )
            with st.expander("Bestaande archiefbestanden"):
                for f in archive_conflict_files:
                    st.code(f)
            overwrite_confirmed = st.checkbox(
                "Ja, ik wil de bestaande archiefdata overschrijven",
                key=f"overwrite_confirmed_{archive_glob}",
            )

    if uploaded is not None and st.button("🚀 Start Cloud Run", type="primary"):
        if archive_conflict_files and not overwrite_confirmed:
            st.error("Vink eerst de bevestiging hierboven aan om door te gaan.")
            st.stop()
        work_dir = Path(tempfile.mkdtemp(prefix="cloud_run_streamlit_"))
        local_input = work_dir / uploaded.name
        local_input.write_bytes(uploaded.getvalue())

        # ---- 0b. Optional pre-flight: skip rows already fully enriched -----
        # Only relevant together with Mergen -- a skipped row is never
        # touched by this run and is simply carried over as-is from current/
        # by the merge step later, so nothing is lost by not re-processing
        # it. This is what actually SAVES Serper/Firecrawl/Anthropic cost on
        # a rerun; the merge step by itself only avoids losing companies
        # from current/, it doesn't reduce what the Cloud Run Job processes.
        if merge_current and skip_already_enriched:
            gcs_bucket_norm_prefilter = export_gcs_bucket.strip()
            gcs_prefix_norm_prefilter = lovable_gcs.normalize_gcs_prefix(export_gcs_prefix)
            if gcs_bucket_norm_prefilter and gcs_prefix_norm_prefilter:
                with st.spinner(
                    "Bestaande current/-data checken op al volledig verrijkte bedrijven…"
                ):
                    existing_list_items, _ = _download_existing_current_export(
                        gcs_bucket_norm_prefilter, gcs_prefix_norm_prefilter, project,
                        work_dir / "lovable_current_existing_prefilter",
                    )
                    known_ids = known_enriched_company_ids(existing_list_items)

                    # Companies confirmed NOT foreign in a past run never
                    # land in current/ when foreign_hq_only_export is on
                    # (they get filtered out at export time), so known_ids
                    # alone can never "see" them -- the screened_domains_
                    # ledger is a separate, always-complete record of
                    # settled verdicts, independent of export filtering.
                    # Only useful here when THIS run also excludes non-
                    # foreign companies -- otherwise a domestic company
                    # still belongs in the output and must be (re)processed.
                    known_domestic: set = set()
                    if foreign_hq_only_export:
                        ledger = screened_domains_ledger.load_ledger(
                            gcs_bucket_norm_prefilter, gcs_prefix_norm_prefilter)
                        known_domestic = screened_domains_ledger.known_domestic_domains(ledger)

                fname_lower = uploaded.name.lower()
                df_prefilter = (
                    pd.read_csv(local_input) if fname_lower.endswith(".csv")
                    else pd.read_excel(local_input)
                )
                from lead_prioritizer_batch_app import DOMAIN_CANDIDATES, resolve_default_column
                domain_col = resolve_default_column(df_prefilter.columns, DOMAIN_CANDIDATES)
                if not domain_col:
                    st.caption(
                        "Skip-filter overgeslagen: kon de domeinkolom niet automatisch "
                        "herkennen in het invoerbestand."
                    )
                elif known_ids or known_domestic:
                    to_process, skipped_rows = split_rows_by_existing_enrichment(
                        df_prefilter, domain_col, known_ids, known_domestic)
                    domestic_note = (
                        f" (waarvan {len(known_domestic)} bekend-binnenlands uit de "
                        "gescreende-domeinen-ledger)" if known_domestic else ""
                    )
                    # "Row limit (totaal)" hierboven wordt pas SERVER-SIDE
                    # toegepast, in de Cloud Run Job zelf, ná deze upload --
                    # zonder deze correctie meldde dit bijvoorbeeld "5038
                    # rij(en) worden verwerkt" terwijl de job er door de
                    # rijlimiet daadwerkelijk maar 300 verwerkte (bevestigd
                    # door de merge-telling erna). Toon dus het werkelijke
                    # aantal, niet het aantal vóór die limiet.
                    effective_to_process = len(to_process)
                    row_limit_note = ""
                    if total_row_limit and len(to_process) > int(total_row_limit):
                        effective_to_process = int(total_row_limit)
                        row_limit_note = (
                            f" (van deze {len(to_process)} onbekende rijen verwerkt de "
                            f"job er, door de Row limit hierboven, uiteindelijk "
                            f"maximaal {effective_to_process})"
                        )
                    st.info(
                        f"Skip-filter: {len(skipped_rows)} van {len(df_prefilter)} rijen "
                        f"al bekend (volledig verrijkt in current/, of definitief "
                        f"binnenlands){domestic_note}, overgeslagen. "
                        f"{effective_to_process} rij(en) worden verwerkt{row_limit_note}."
                    )
                    if len(to_process) == 0:
                        st.success(
                            "Alle bedrijven in dit bestand zijn al bekend (verrijkt of "
                            "definitief binnenlands) — niets nieuws om te verwerken."
                        )
                        st.stop()
                    if fname_lower.endswith(".csv"):
                        to_process.to_csv(local_input, index=False)
                    else:
                        to_process.to_excel(local_input, index=False)

        run_id = build_run_id(uploaded.name)
        input_uri = gcs_incoming_uri(bucket, uploaded.name)
        output_dir = gcs_output_dir(bucket, run_id)

        st.info(f"Run-ID: `{run_id}`")

        # ---- 1. Upload -----------------------------------------------------
        with st.spinner("Uploaden naar Cloud Storage…"):
            rc, output = run_capture(build_upload_command(str(local_input), input_uri, project))
        if rc != 0:
            st.error("Upload mislukt:")
            st.code(output)
            st.stop()
        st.success(f"Geupload naar {input_uri}")

        # ---- 2. Write a manifest.json + start the job, WITHOUT --wait ------
        # The Cloud Run Job runs entirely server-side once execute accepts
        # it -- this process (and therefore this laptop) doesn't need to
        # stay alive for the run to finish. manifest.json is what lets the
        # "Status van een run" panel above (and "Recente runs") find this
        # run later purely from GCS, in a completely fresh browser session.
        run_manifest = {
            "run_id": run_id, "input_uri": input_uri, "output_dir": output_dir,
            "task_count": int(task_count), "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "streamlit",
        }
        manifest_local = work_dir / "manifest.json"
        manifest_local.write_text(json.dumps(run_manifest), encoding="utf-8")
        run_capture(build_upload_command(
            str(manifest_local), join_path(output_dir, "manifest.json"), project))

        with st.spinner("Cloud Run Job starten…"):
            rc, output = run_capture(build_execute_command(
                job_name, project, region, input_uri, output_dir,
                run_id, int(task_count), mode, extra_env=extra_env, wait=False,
            ))
        if rc != 0:
            st.error("Starten van de Cloud Run Job mislukt:")
            st.code(output)
            st.stop()
        st.success(
            "Job-executie gestart. De run loopt nu volledig op Cloud Run — dit "
            "tabblad (en je laptop) hoeft niet open te blijven. Volg de "
            "voortgang hierboven bij 'Status van een run'."
        )
        st.query_params["run_id"] = run_id
        st.rerun()


if __name__ == "__main__":  # pragma: no cover
    main()
