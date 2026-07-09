"""Tests for cloud_run_streamlit_app.py: GCS path construction, gcloud
command-list building (no subprocess/GCS/Streamlit — only argument-list
assertions), and run_streaming's char-by-char output + timeout behavior
(exercised against real short-lived `python -c` subprocesses, no gcloud
needed)."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from cloud_run_streamlit_app import (
    ProcessTimeout,
    _gcloud_executable,
    build_describe_job_command,
    build_download_command,
    build_execute_command,
    build_list_command,
    build_update_parallelism_command,
    build_upload_command,
    count_task_statuses,
    gcs_incoming_uri,
    gcs_output_dir,
    parse_parallelism_from_yaml,
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
    # --tasks must be passed explicitly: the TASK_COUNT env var alone doesn't
    # change the execution's task count (Cloud Run's own CLOUD_RUN_TASK_COUNT
    # — the deploy-time count — takes precedence in the runner), so without
    # this flag the sidebar's task count silently did nothing.
    assert cmd[cmd.index("--tasks") + 1] == "10"
    env_index = cmd.index("--update-env-vars") + 1
    env_vars = cmd[env_index]
    assert "INPUT_GCS_URI=gs://b/incoming/leads.xlsx" in env_vars
    assert "OUTPUT_GCS_DIR=gs://b/runs/run123" in env_vars
    assert "RUN_ID=run123" in env_vars
    assert "TASK_COUNT=10" in env_vars
    assert "MODE=full" in env_vars


def test_build_execute_command_appends_extra_env_vars():
    cmd = build_execute_command(
        job_name="myngle-lead-prioritizer",
        project="proj-1",
        region="europe-west4",
        input_uri="gs://b/incoming/leads.xlsx",
        output_dir="gs://b/runs/run123",
        run_id="run123",
        task_count=10,
        mode="full",
        extra_env={"DEEP_DIVE": "true", "C5_ENABLED": "true"},
    )
    env_vars = cmd[cmd.index("--update-env-vars") + 1]
    assert "RUN_ID=run123" in env_vars
    assert "DEEP_DIVE=true" in env_vars
    assert "C5_ENABLED=true" in env_vars


def test_build_download_command_targets_local_dir():
    cmd = build_download_command("gs://b/runs/run123/parts/*.xlsx", "C:\\out\\parts\\", "proj-1")
    assert cmd == [
        _gcloud_executable(), "storage", "cp", "gs://b/runs/run123/parts/*.xlsx", "C:\\out\\parts\\",
        "--project", "proj-1",
    ]


def test_build_list_command_targets_glob():
    cmd = build_list_command("gs://b/runs/run123/status/*.json", "proj-1")
    assert cmd == [
        _gcloud_executable(), "storage", "ls", "gs://b/runs/run123/status/*.json",
        "--project", "proj-1",
    ]


# ── count_task_statuses: classify status/ listing output ────────────────────

def test_count_task_statuses_classifies_by_suffix():
    listing = (
        "gs://b/runs/r/status/part_0000_done.json\n"
        "gs://b/runs/r/status/part_0001_done.json\n"
        "gs://b/runs/r/status/part_0002_failed.json\n"
        "gs://b/runs/r/status/part_0003_running.json\n"
    )
    assert count_task_statuses(listing) == {"done": 2, "failed": 1, "running": 1}


def test_count_task_statuses_empty_listing_returns_zeros():
    assert count_task_statuses("") == {"done": 0, "failed": 0, "running": 0}


# ── Parallelism: read back and update a Cloud Run Job's deploy-time setting ──

def test_build_describe_job_command_uses_yaml_format():
    cmd = build_describe_job_command("myngle-lead-prioritizer", "proj-1", "europe-west4")
    assert cmd == [
        _gcloud_executable(), "run", "jobs", "describe", "myngle-lead-prioritizer",
        "--project", "proj-1", "--region", "europe-west4", "--format", "yaml",
    ]


def test_build_update_parallelism_command():
    cmd = build_update_parallelism_command(
        "myngle-lead-prioritizer", "proj-1", "europe-west4", 50)
    assert cmd == [
        _gcloud_executable(), "run", "jobs", "update", "myngle-lead-prioritizer",
        "--project", "proj-1", "--region", "europe-west4",
        "--parallelism", "50",
    ]


class TestParseParallelismFromYaml:
    def test_extracts_top_level_parallelism(self):
        yaml_text = "apiVersion: run.googleapis.com/v1\nkind: Job\nparallelism: 10\n"
        assert parse_parallelism_from_yaml(yaml_text) == 10

    def test_extracts_nested_parallelism(self):
        yaml_text = (
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      parallelism: 50\n"
            "      taskCount: 50\n"
        )
        assert parse_parallelism_from_yaml(yaml_text) == 50

    def test_missing_field_returns_none(self):
        assert parse_parallelism_from_yaml("kind: Job\ntaskCount: 50\n") is None

    def test_blank_input_returns_none_without_raising(self):
        assert parse_parallelism_from_yaml("") is None
        assert parse_parallelism_from_yaml(None) is None

    def test_ignores_non_matching_line_containing_word(self):
        # e.g. an error message that happens to mention "parallelism" in
        # prose, not as a YAML key -- must not be mistaken for a value.
        text = "ERROR: could not update parallelism setting for job"
        assert parse_parallelism_from_yaml(text) is None


def test_count_task_statuses_ignores_unrelated_lines():
    listing = "No matches for pattern.\ngs://b/runs/r/status/part_0000_done.json\n"
    assert count_task_statuses(listing) == {"done": 1, "failed": 0, "running": 0}


def test_count_task_statuses_finished_task_not_double_counted_as_running():
    # part_0000 has BOTH a running and a done status file (cloud_job_runner.py
    # never deletes the running one once the task finishes) -- the finished
    # task must be counted as done only, not also as still-running.
    listing = (
        "gs://b/runs/r/status/part_0000_running.json\n"
        "gs://b/runs/r/status/part_0000_done.json\n"
    )
    assert count_task_statuses(listing) == {"done": 1, "failed": 0, "running": 0}


def test_count_task_statuses_order_independent_for_same_task():
    # done listed BEFORE running for the same task label -- still wins.
    listing = (
        "gs://b/runs/r/status/part_0000_done.json\n"
        "gs://b/runs/r/status/part_0000_running.json\n"
    )
    assert count_task_statuses(listing) == {"done": 1, "failed": 0, "running": 0}


# ── Regression: gcloud is a .cmd wrapper on Windows ──────────────────────────
#
# subprocess.run(["gcloud", ...]) without shell=True raises FileNotFoundError
# ([WinError 2]) on Windows, because CreateProcess (unlike a real shell)
# doesn't consult PATHEXT to resolve bare "gcloud" to "gcloud.cmd" — found via
# an actual Playwright-driven browser run of the app, where the upload step
# crashed immediately. _gcloud_executable() resolves the real path via
# shutil.which(), which IS PATHEXT-aware. This test invokes it for real (no
# mocking) to prove the resolved path is actually invocable via subprocess.

@pytest.mark.skipif(
    shutil.which("gcloud") is None,
    reason="gcloud CLI not installed here (CI/sandbox); the PATHEXT "
           "regression this guards against only exists where gcloud is.",
)
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


def test_run_streaming_calls_on_tick_repeatedly_even_without_new_output():
    # on_tick must fire on every poll-loop wake-up (~5x/second), independent
    # of whether the subprocess produced any output -- this is the only live
    # signal available during gcloud's silent "Starting execution...." phase.
    ticks: list[None] = []
    rc = run_streaming(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        on_tick=lambda: ticks.append(None),
    )
    assert rc == 0
    assert len(ticks) >= 2


def test_run_streaming_on_tick_exception_never_breaks_the_run():
    def _broken_tick():
        raise RuntimeError("boom")

    rc = run_streaming([sys.executable, "-c", "import sys; sys.exit(0)"], on_tick=_broken_tick)
    assert rc == 0


def test_run_streaming_kills_process_and_raises_on_timeout():
    with pytest.raises(ProcessTimeout):
        run_streaming(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.3,
        )
