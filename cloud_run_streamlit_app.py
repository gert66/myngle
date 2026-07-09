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
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from cloud_dispatcher import build_run_id, pick_task_count
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
) -> list[str]:
    env_vars = (
        f"INPUT_GCS_URI={input_uri},OUTPUT_GCS_DIR={output_dir},"
        f"RUN_ID={run_id},TASK_COUNT={task_count},MODE={mode}"
    )
    return [
        _gcloud_executable(), "run", "jobs", "execute", job_name,
        "--project", project,
        "--region", region,
        "--update-env-vars", env_vars,
        "--wait",
    ]


def build_download_command(src_glob: str, local_dir: str, project: str) -> list[str]:
    return [_gcloud_executable(), "storage", "cp", src_glob, local_dir, "--project", project]


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


def run_streaming(cmd: list[str], on_chunk=None, timeout_seconds: Optional[float] = None) -> int:
    """Run cmd, calling on_chunk(str) with each raw output chunk (stdout+stderr)
    as it arrives — one character at a time, NOT line-buffered.

    `gcloud run jobs execute --wait` prints progress as a long run of dots
    with no newline in between (e.g. "Provisioning resources....done"), so a
    line-buffered reader yields nothing for the entire multi-minute phase and
    then dumps it all at once — from the UI it looks completely frozen for
    the exact phase that takes longest. Char-by-char reading makes each dot
    show up live instead.

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
# Streamlit UI
# =============================================================================

def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import pandas as pd
    import streamlit as st

    import cloud_merge_results as cmr

    st.set_page_config(page_title="Lead Prioritizer — Cloud Run", page_icon="☁️", layout="wide")
    st.title("☁️ Lead Prioritizer — Cloud Run Jobs")
    st.caption(
        "Upload een Excel, draai de v2-pipeline parallel op Cloud Run Jobs, en "
        "download het samengevoegde eindresultaat. Zie docs/cloud_run_workflow.md "
        "voor de architectuur; dit is een UI bovenop dezelfde stappen."
    )

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
        mode = st.selectbox("Mode", options=mode_options, index=mode_options.index("full") if "full" in mode_options else 0)

    uploaded = st.file_uploader("Input-Excel (.xlsx)", type=["xlsx"])

    if uploaded is not None and st.button("🚀 Start Cloud Run", type="primary"):
        work_dir = Path(tempfile.mkdtemp(prefix="cloud_run_streamlit_"))
        local_input = work_dir / uploaded.name
        local_input.write_bytes(uploaded.getvalue())

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

        # ---- 2. Execute the Cloud Run Job, streaming progress ---------------
        import time

        st.subheader("Job-executie")
        elapsed_placeholder = st.empty()
        log_placeholder = st.empty()
        buf: list[str] = []
        state = {"last_update": 0.0, "start": time.monotonic()}
        MIN_UPDATE_INTERVAL = 0.3  # seconds — avoid hammering the UI on every single character

        def _on_chunk(chunk: str) -> None:
            buf.append(chunk)
            now = time.monotonic()
            if now - state["last_update"] < MIN_UPDATE_INTERVAL and "\n" not in chunk:
                return
            state["last_update"] = now
            elapsed_placeholder.caption(f"⏱️ bezig: {now - state['start']:.0f}s")
            log_placeholder.code("".join(buf)[-4000:])

        job_timeout_seconds = 3600  # 1 uur — ruim boven de verwachte duur, voorkomt een oneindige hang
        try:
            rc = run_streaming(
                build_execute_command(
                    job_name, project, region, input_uri, output_dir,
                    run_id, int(task_count), mode,
                ),
                on_chunk=_on_chunk,
                timeout_seconds=job_timeout_seconds,
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

        # ---- 5. Push the final Excel back to GCS for consistency -----------
        run_capture(build_upload_command(
            str(final_local), join_path(output_dir, "final", FINAL_OUTPUT_NAME), project))

        # ---- 6. Preview + download ------------------------------------------
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
