"""Tests for cloud_run_streamlit_app.py: GCS path construction, gcloud
command-list building (no subprocess/GCS/Streamlit — only argument-list
assertions), and run_streaming's char-by-char output + timeout behavior
(exercised against real short-lived `python -c` subprocesses, no gcloud
needed)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from cloud_run_streamlit_app import (
    ProcessTimeout,
    _gcloud_executable,
    build_download_command,
    build_execute_command,
    build_upload_command,
    gcs_incoming_uri,
    gcs_output_dir,
    run_streaming,
)


def test_gcs_incoming_uri_joins_bucket_prefix_and_filename():
    assert gcs_incoming_uri("my-bucket", "leads.xlsx") == "gs://my-bucket/incoming/leads.xlsx"


def test_gcs_output_dir_joins_bucket_and_run_id():
    assert gcs_output_dir("my-bucket", "run123") == "gs://my-bucket/runs/run123"


def test_build_upload_command_includes_project():
    cmd = build_upload_command("C:\\leads.xlsx", "gs://b/incoming/leads.xlsx", "proj-1")
    assert cmd == [
        _gcloud_executable(), "storage", "cp", "C:\\leads.xlsx", "gs://b/incoming/leads.xlsx",
        "--project", "proj-1",
    ]


def test_build_execute_command_sets_all_env_vars_and_waits():
    cmd = build_execute_command(
        job_name="myngle-lead-prioritizer",
        project="proj-1",
        region="europe-west4",
        input_uri="gs://b/incoming/leads.xlsx",
        output_dir="gs://b/runs/run123",
        run_id="run123",
        task_count=10,
        mode="full",
    )
    assert cmd[:5] == [_gcloud_executable(), "run", "jobs", "execute", "myngle-lead-prioritizer"]
    assert "--wait" in cmd
    env_index = cmd.index("--update-env-vars") + 1
    env_vars = cmd[env_index]
    assert "INPUT_GCS_URI=gs://b/incoming/leads.xlsx" in env_vars
    assert "OUTPUT_GCS_DIR=gs://b/runs/run123" in env_vars
    assert "RUN_ID=run123" in env_vars
    assert "TASK_COUNT=10" in env_vars
    assert "MODE=full" in env_vars


def test_build_download_command_targets_local_dir():
    cmd = build_download_command("gs://b/runs/run123/parts/*.xlsx", "C:\\out\\parts\\", "proj-1")
    assert cmd == [
        _gcloud_executable(), "storage", "cp", "gs://b/runs/run123/parts/*.xlsx", "C:\\out\\parts\\",
        "--project", "proj-1",
    ]


# ── Regression: gcloud is a .cmd wrapper on Windows ──────────────────────────
#
# subprocess.run(["gcloud", ...]) without shell=True raises FileNotFoundError
# ([WinError 2]) on Windows, because CreateProcess (unlike a real shell)
# doesn't consult PATHEXT to resolve bare "gcloud" to "gcloud.cmd" — found via
# an actual Playwright-driven browser run of the app, where the upload step
# crashed immediately. _gcloud_executable() resolves the real path via
# shutil.which(), which IS PATHEXT-aware. This test invokes it for real (no
# mocking) to prove the resolved path is actually invocable via subprocess.

def test_gcloud_executable_is_actually_invocable_via_subprocess():
    result = subprocess.run(
        [_gcloud_executable(), "--version"], capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Google Cloud SDK" in result.stdout


# ── run_streaming: char-by-char output + timeout ────────────────────────────
#
# Regression coverage for the real bug: gcloud prints multi-minute progress
# as a run of dots with no newline in between, so a line-buffered reader
# yields nothing until the whole phase finishes — the UI looked frozen for
# exactly the phase that takes longest. These assert output arrives as
# individual characters, not batched into one call at the end.

_NO_NEWLINE_DOTS = (
    "import sys, time\n"
    "for _ in range(5):\n"
    "    sys.stdout.write('.')\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.02)\n"
)


def test_run_streaming_delivers_output_without_a_trailing_newline_incrementally():
    chunks_received: list[str] = []
    rc = run_streaming([sys.executable, "-c", _NO_NEWLINE_DOTS], on_chunk=chunks_received.append)

    assert rc == 0
    # Delivered as separate chunks (proves char-by-char reading), not one
    # single batched call containing the whole "....." at once.
    assert len(chunks_received) >= 5
    assert "".join(chunks_received) == "....."


def test_run_streaming_returns_process_returncode():
    rc = run_streaming([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert rc == 3


def test_run_streaming_kills_process_and_raises_on_timeout():
    with pytest.raises(ProcessTimeout):
        run_streaming(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.3,
        )
