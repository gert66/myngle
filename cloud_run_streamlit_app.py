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
    extra_env: Optional[dict] = None,
) -> list[str]:
    env_pairs = [
        f"INPUT_GCS_URI={input_uri}", f"OUTPUT_GCS_DIR={output_dir}",
        f"RUN_ID={run_id}", f"TASK_COUNT={task_count}", f"MODE={mode}",
    ]
    for key, value in (extra_env or {}).items():
        env_pairs.append(f"{key}={value}")
    env_vars = ",".join(env_pairs)
    return [
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
        "--wait",
    ]


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
    result = subprocess.run(cmd, capture_output=True, text=True)
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

    import cloud_merge_results as cmr
    import export_lead_prioritizer_to_lovable_json as lovable_export
    import lovable_gcs_upload as lovable_gcs
    import screened_domains_ledger
    from lead_prioritizer_batch_app import (
        DEFAULT_COLD_CALLERS_TEXT,
        default_gcs_country_prefix,
        parse_cold_callers,
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
        st.subheader("Kostenbesparing")
        gate_full_enrichment_on_foreign_hq = st.checkbox(
            "Alleen bedrijven met buitenlands HQ volledig verrijken", value=False,
            help="Screent EERST elk bedrijf goedkoop op HQ (1 Serper-call per "
                 "bedrijf); de volledige v2-pipeline (non-HQ evidence via "
                 "Firecrawl/Serper + AI-signalen + score + caller-content, "
                 "~4 extra Serper-calls + Firecrawl/Anthropic-kosten per rij) "
                 "draait daarna ALLEEN nog voor rijen met bevestigd buitenlands "
                 "HQ. Rijen zonder buitenlands HQ blijven in de output staan "
                 "(enrichment_skipped=True) maar kosten verder niets. Werkt met "
                 "elke Mode hierboven — dit is de goedkoopste manier om alleen "
                 "een foreign-HQ-lijst te bouwen, in plaats van iedereen volledig "
                 "verrijken en pas bij de Lovable-export filteren. Zet ook "
                 "'Foreign-HQ-only export' hieronder aan om de niet-buitenlandse "
                 "rijen ook uit de uiteindelijke lijst te weren.")

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
        st.subheader("Lovable JSON-export + GCS-upload (na afloop)")
        st.caption(
            "Draait één keer na de merge, op het volledige samengevoegde "
            "eindresultaat — net als de lokale app."
        )
        auto_lovable_export_enabled = st.checkbox(
            "Na afloop automatisch Lovable JSON exporteren", value=True)
        export_country = ""
        cold_callers_raw = DEFAULT_COLD_CALLERS_TEXT
        foreign_hq_only_export = False
        bucket_size = 500
        content_language = lovable_export.DEFAULT_CONTENT_LANGUAGE
        auto_gcs_upload_enabled = True
        export_gcs_bucket = lovable_gcs.DEFAULT_GCS_BUCKET
        export_gcs_prefix = ""
        export_gcs_run_folder = ""
        upload_current = True
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
            cold_callers_raw = st.text_input(
                "Cold callers (comma-separated)", value=DEFAULT_COLD_CALLERS_TEXT)
            fc1, fc2 = st.columns(2)
            foreign_hq_only_export = fc1.checkbox("Foreign-HQ-only export", value=False)
            bucket_size = fc2.number_input("Bucket size", min_value=1, value=500, step=50)
            content_language = st.selectbox(
                "Lovable content language", list(lovable_export.SUPPORTED_CONTENT_LANGUAGES),
                index=list(lovable_export.SUPPORTED_CONTENT_LANGUAGES).index(
                    lovable_export.DEFAULT_CONTENT_LANGUAGE),
            )
            auto_gcs_upload_enabled = st.checkbox(
                "Na Lovable JSON-export uploaden naar Google Cloud Storage", value=True)
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
                    value=lovable_gcs.default_gcs_run_folder(mode))
                # current/ zelf altijd uploaden zodra Lovable-export + GCS-
                # upload allebei aanstaan -- de vraag is niet "wel of niet",
                # maar "overschrijven of mergen", en die keuze staat expliciet
                # in het hoofdveld hieronder (niet hier in de sidebar), vlak
                # vóór de Start-knop, waar hij niet gemist kan worden.
                upload_current = True
                upload_archive = st.checkbox("Archive naar runs/<run_folder>/", value=True)

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
                    st.info(
                        f"Skip-filter: {len(skipped_rows)} van {len(df_prefilter)} rijen "
                        f"al bekend (volledig verrijkt in current/, of definitief "
                        f"binnenlands){domestic_note}, overgeslagen. "
                        f"{len(to_process)} rij(en) worden verwerkt."
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
        resolved_export_country = export_country or suggest_country_from_filename(
            uploaded.name, SUPPORTED_DEFAULT_INPUT_COUNTRIES)

        st.info(f"Run-ID: `{run_id}`")

        # ---- 1. Upload -----------------------------------------------------
        with st.spinner("Uploaden naar Cloud Storage…"):
            rc, output = run_capture(build_upload_command(str(local_input), input_uri, project))
        if rc != 0:
            st.error("Upload mislukt:")
            st.code(output)
            st.stop()
        st.success(f"Geupload naar {input_uri}")

        # ---- 2. Execute the Cloud Run Job, streaming progress ---------------
        import time

        st.subheader("Job-executie")
        elapsed_placeholder = st.empty()
        status_placeholder = st.empty()
        log_placeholder = st.empty()
        buf: list[str] = []
        state = {"last_update": 0.0, "start": time.monotonic(), "last_status_poll": 0.0}
        MIN_UPDATE_INTERVAL = 0.3  # seconds — avoid hammering the UI on every single character
        STATUS_POLL_INTERVAL = 15.0  # seconds — gcloud storage ls is a real subprocess call

        def _on_chunk(chunk: str) -> None:
            buf.append(chunk)
            now = time.monotonic()
            if now - state["last_update"] < MIN_UPDATE_INTERVAL and "\n" not in chunk:
                return
            state["last_update"] = now
            elapsed_placeholder.caption(f"⏱️ bezig: {now - state['start']:.0f}s")
            log_placeholder.code("".join(buf)[-4000:])

        def _on_tick() -> None:
            # gcloud run jobs execute --wait goes silent for the entire
            # execution phase (the part that takes longest) — this is the
            # ONLY live signal the UI has during that phase, pulled straight
            # from the same status/ JSON files the Cloud Console reads.
            now = time.monotonic()
            if now - state["last_status_poll"] < STATUS_POLL_INTERVAL:
                return
            state["last_status_poll"] = now
            rc_ls, listing = run_capture(build_list_command(
                join_path(output_dir, "status", "*.json"), project))
            if rc_ls != 0:
                return  # normal early on: the status/ prefix may not exist yet
            counts = count_task_statuses(listing)
            reported = counts["done"] + counts["failed"] + counts["running"]
            status_placeholder.info(
                f"📊 Taken: {counts['done']} klaar, {counts['failed']} gefaald, "
                f"{counts['running']} bezig ({reported}/{int(task_count)} gerapporteerd)"
            )

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

        job_timeout_seconds = 3600  # 1 uur — ruim boven de verwachte duur, voorkomt een oneindige hang
        try:
            rc = run_streaming(
                build_execute_command(
                    job_name, project, region, input_uri, output_dir,
                    run_id, int(task_count), mode, extra_env=extra_env,
                ),
                on_chunk=_on_chunk,
                timeout_seconds=job_timeout_seconds,
                on_tick=_on_tick,
            )
        except ProcessTimeout as exc:
            st.error(
                f"{exc} De Cloud Run Job zelf loopt mogelijk nog door op GCP — "
                f"check `gcloud run jobs executions list --job={job_name} --region={region}`."
            )
            st.stop()
        if rc != 0:
            st.error(
                "Job-executie mislukt of gefaald. Check de status-JSON's in "
                f"`{output_dir}/status/` en Cloud Logging voor 429's/errors."
            )
            st.stop()
        st.success(f"Job-executie voltooid in {time.monotonic() - state['start']:.0f}s.")

        # ---- 3. Download part/status files locally (avoids needing ADC) ----
        local_run_dir = work_dir / "run"
        local_parts = local_run_dir / "parts"
        local_status = local_run_dir / "status"
        local_parts.mkdir(parents=True, exist_ok=True)
        local_status.mkdir(parents=True, exist_ok=True)

        with st.spinner("Deel-resultaten ophalen…"):
            rc, output = run_capture(build_download_command(
                join_path(output_dir, "parts", "*.xlsx"), str(local_parts) + "/", project))
            if rc != 0:
                st.error("Ophalen van part-bestanden mislukt:")
                st.code(output)
                st.stop()
            run_capture(build_download_command(
                join_path(output_dir, "status", "*_done.json"), str(local_status) + "/", project))
            # No _failed.json files is the normal, successful case — a non-zero
            # exit here (glob matched nothing) is expected and not an error.
            run_capture(build_download_command(
                join_path(output_dir, "status", "*_failed.json"), str(local_status) + "/", project))

        # ---- 4. Merge locally (reuses the actual pipeline's merge logic) ---
        with st.spinner("Resultaten mergen…"):
            merge_rc = cmr.main([
                "--run-id", run_id,
                "--output-dir", str(local_run_dir),
                "--expected-task-count", str(int(task_count)),
            ])
        if merge_rc != 0:
            st.error(
                "Merge mislukt — controleer of er een gefaalde task tussen zit "
                f"(status-bestanden in `{local_status}`)."
            )
            st.stop()

        final_local = local_run_dir / "final" / cmr.DEFAULT_FINAL_OUTPUT_NAME
        st.success(f"Merge voltooid: {final_local.name}")

        # ---- 4b. Combined API-usage + cache-hit report across every task ---
        # Each task's own lead_prioritizer_batch_cli.py subprocess tracks its
        # own usage (usage_tracker.py, one process = one tracker) and folds a
        # JSON snapshot into its own _done.json (cloud_job_runner.py); this
        # just sums all of those already-downloaded status files into one
        # run-wide report — no extra GCS calls.
        import json as _json
        import usage_tracker

        task_snapshots = []
        for status_path in sorted(local_status.glob("*_done.json")):
            try:
                payload = _json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("usage"):
                task_snapshots.append(payload["usage"])
        if task_snapshots:
            combined_usage = usage_tracker.merge_snapshots(task_snapshots)
            with st.expander(
                f"📊 API-verbruik + cache-hitrate — {len(task_snapshots)}/"
                f"{int(task_count)} taken rapporteerden dit"
            ):
                st.code(usage_tracker.format_summary_text(combined_usage))
        else:
            st.caption(
                "Geen API-verbruiksdata gevonden in de status-bestanden "
                "(oudere image zonder --usage-output-ondersteuning?)."
            )

        # ---- 4c. Update the screened-domains ledger (all HQ-screened rows) --
        # Independent of Lovable export/foreign_hq_only_export filtering --
        # every row this run screened for HQ (gated or not) is a candidate,
        # so a future run's skip-filter can recognize a settled-domestic
        # company even though it never lands in current/. Always attempted
        # whenever GCS export is configured, regardless of whether THIS run
        # itself uses the skip-filter -- future runs benefit either way.
        if auto_lovable_export_enabled and auto_gcs_upload_enabled:
            gcs_bucket_norm_ledger = export_gcs_bucket.strip()
            gcs_prefix_norm_ledger = lovable_gcs.normalize_gcs_prefix(export_gcs_prefix)
            if gcs_bucket_norm_ledger and gcs_prefix_norm_ledger:
                try:
                    enriched_rows = pd.read_excel(
                        final_local, sheet_name=cmr.ENRICHED_LEADS_SHEET_NAME
                    ).to_dict("records")
                except Exception:
                    enriched_rows = []
                ledger_updates = screened_domains_ledger.build_ledger_updates(enriched_rows)
                if ledger_updates:
                    save_result = screened_domains_ledger.save_ledger(
                        gcs_bucket_norm_ledger, gcs_prefix_norm_ledger, ledger_updates)
                    if save_result.get("success"):
                        st.caption(
                            f"Gescreende-domeinen-ledger bijgewerkt: "
                            f"{len(ledger_updates)} definitief-binnenlands bedrijf/"
                            "bedrijven vastgelegd."
                        )

        # ---- 5. Push the final Excel back to GCS for consistency -----------
        run_capture(build_upload_command(
            str(final_local), join_path(output_dir, "final", FINAL_OUTPUT_NAME), project))

        # ---- 6. Optional: Lovable JSON export + GCS upload, once, on the ---
        # full merged result (same shape as the local app's after-run export).
        if auto_lovable_export_enabled:
            export_dir = local_run_dir / "lovable_export"
            cold_callers = parse_cold_callers(cold_callers_raw)
            export_error = None
            if not resolved_export_country:
                export_error = (
                    "Lovable-export: geen export country ingevuld en kon er geen "
                    "raden uit de bestandsnaam."
                )
            elif not cold_callers:
                export_error = "Lovable-export: minimaal één cold caller is verplicht."
            if export_error:
                st.error(export_error)
            else:
                manifest = None
                try:
                    with st.spinner("Lovable JSON exporteren…"):
                        manifest = lovable_export.export_workbook_to_lovable_json(
                            input_xlsx=final_local,
                            output_dir=export_dir,
                            export_country=resolved_export_country,
                            cold_callers=cold_callers,
                            foreign_hq_only=bool(foreign_hq_only_export),
                            bucket_size=int(bucket_size),
                            content_language=content_language,
                        )
                    st.success(
                        f"Lovable JSON-export voltooid: "
                        f"{len(manifest.get('output_files', []))} bestand(en)."
                    )
                except Exception as exc:
                    st.error(f"Lovable JSON-export mislukt: {exc}")

                if manifest is not None and auto_gcs_upload_enabled:
                    validation = manifest.get("validation_summary", {}) or {}
                    validation_ok = (
                        validation.get("status") == "ok"
                        and int(validation.get("structural_errors", 0) or 0) == 0
                    )
                    gcs_bucket_norm = export_gcs_bucket.strip()
                    gcs_prefix_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_prefix)
                    gcs_run_folder_norm = lovable_gcs.normalize_gcs_prefix(export_gcs_run_folder)
                    can_upload = (
                        validation_ok and bool(gcs_bucket_norm) and bool(gcs_prefix_norm)
                        and (upload_current or upload_archive)
                    )
                    if not can_upload:
                        st.warning(
                            "GCS-upload overgeslagen: exportvalidatie of bucket/prefix-"
                            "instellingen waren niet in orde. De Lovable JSON staat wel "
                            "lokaal klaar."
                        )
                    else:
                        output_filenames = sorted(
                            {Path(p).name for p in manifest.get("output_files", [])})

                        # ---- 6a. current/: merge with existing data, or plain overwrite ---
                        current_export_dir = export_dir
                        current_filenames = output_filenames
                        if upload_current and merge_current:
                            with st.spinner(
                                "Bestaande current/-data downloaden en samenvoegen…"
                            ):
                                current_export_dir, current_filenames, merge_summary = \
                                    _merge_export_into_existing_current(
                                        local_run_dir, export_dir, manifest,
                                        gcs_bucket_norm, gcs_prefix_norm,
                                        int(bucket_size), project,
                                    )
                            st.info(
                                f"Merge: {merge_summary['added']} nieuw, "
                                f"{merge_summary['updated']} bijgewerkt, "
                                f"{merge_summary['kept_richer_existing']} bestaande "
                                "(rijkere) versie behouden, "
                                f"{merge_summary['total_after']} totaal in current/."
                            )

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
                        jobs = current_jobs + archive_jobs
                        with st.spinner("Lovable JSON uploaden naar Google Cloud Storage…"):
                            upload_results = lovable_gcs.run_upload_plan(jobs)
                        failures = [r for r in upload_results if not r["success"]]
                        if failures:
                            st.error(
                                f"GCS-upload: {len(failures)} van {len(upload_results)} "
                                "uploads mislukt."
                            )
                            for r in failures:
                                st.code(f"{r['destination']}: {r.get('error') or r.get('stderr') or ''}")
                        else:
                            st.success(
                                f"GCS-upload voltooid: {len(upload_results)} bestand(en).")

        # ---- 7. Preview + download ------------------------------------------
        df = pd.read_excel(final_local)
        st.subheader(f"Resultaat — {len(df)} rijen")
        preview_cols = [c for c in ["company_name", "domain", "run_success", "final_commercial_fit_score"] if c in df.columns]
        st.dataframe(df[preview_cols] if preview_cols else df, use_container_width=True)

        st.download_button(
            "⬇️ Download eindresultaat (.xlsx)",
            data=final_local.read_bytes(),
            file_name=f"{Path(uploaded.name).stem}_prioritized.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.caption(f"Ook beschikbaar in GCS: `{output_dir}/final/{FINAL_OUTPUT_NAME}`")


if __name__ == "__main__":  # pragma: no cover
    main()
