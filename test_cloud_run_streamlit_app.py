"""Tests for cloud_run_streamlit_app.py: GCS path construction, gcloud
command-list building (no subprocess/GCS/Streamlit — only argument-list
assertions), and run_streaming's char-by-char output + timeout behavior
(exercised against real short-lived `python -c` subprocesses, no gcloud
needed)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from cloud_run_streamlit_app import (
    ProcessTimeout,
    _download_existing_current_export,
    _gcloud_executable,
    _merge_export_into_existing_current,
    build_download_command,
    build_execute_command,
    build_list_command,
    build_upload_command,
    count_task_statuses,
    gcs_incoming_uri,
    gcs_output_dir,
    known_enriched_company_ids,
    list_existing_gcs_files,
    run_streaming,
    split_rows_by_existing_enrichment,
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


# ── list_existing_gcs_files: archive-folder overwrite pre-flight check ──────

def test_list_existing_gcs_files_extracts_gs_uris():
    listing = (
        "gs://b/spain/runs/2026-07-09_full/companies.list.json\n"
        "gs://b/spain/runs/2026-07-09_full/company-details-0001.json\n"
    )
    assert list_existing_gcs_files(listing) == [
        "gs://b/spain/runs/2026-07-09_full/companies.list.json",
        "gs://b/spain/runs/2026-07-09_full/company-details-0001.json",
    ]


def test_list_existing_gcs_files_ignores_no_matches_message():
    # gcloud's own "nothing here" message on an empty/not-yet-existing
    # prefix -- must never be mistaken for a real object.
    listing = "ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\n"
    assert list_existing_gcs_files(listing) == []


def test_list_existing_gcs_files_empty_output_returns_empty_list():
    assert list_existing_gcs_files("") == []


def test_list_existing_gcs_files_strips_whitespace():
    listing = "  gs://b/spain/runs/2026-07-09_full/companies.list.json  \n"
    assert list_existing_gcs_files(listing) == [
        "gs://b/spain/runs/2026-07-09_full/companies.list.json"]


# ── current/ merge: download + combine with a fresh export ──────────────────

def _fake_gcloud_for_current_download(existing_list, existing_buckets: dict):
    """subprocess.run side_effect covering every gcloud call
    _download_existing_current_export makes: the companies.list.json
    download, the company-details-*.json `storage ls` listing, and each
    bucket file's own download -- keyed off the command shape, since both
    cloud_run_streamlit_app.run_capture and lovable_gcs_upload.download_file
    ultimately call the same patched subprocess.run."""
    def _run(cmd, capture_output=True, text=True, timeout=None):
        if "ls" in cmd:
            uris = [f"gs://b/spain/current/{name}" for name in existing_buckets]
            return MagicMock(returncode=0 if uris else 1, stdout="\n".join(uris), stderr="")
        # A `storage cp SOURCE DEST` download -- DEST is the last argv.
        source, dest = cmd[-2], cmd[-1]
        if source.endswith("companies.list.json"):
            Path(dest).write_text(json.dumps(existing_list), encoding="utf-8")
            return MagicMock(returncode=0, stdout="", stderr="")
        for name, data in existing_buckets.items():
            if source.endswith(name):
                Path(dest).write_text(json.dumps(data), encoding="utf-8")
                return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="No URLs matched")
    return _run


class TestDownloadExistingCurrentExport:
    def test_nothing_there_yet_degrades_to_empty(self, tmp_path):
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="", stderr="not found")):
            items, details = _download_existing_current_export(
                "b", "spain", "proj-1", tmp_path)
        assert items == []
        assert details == {}

    def test_no_gcloud_tool_degrades_to_empty_without_subprocess(self, tmp_path):
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool", return_value=None), \
             patch("subprocess.run") as m_run:
            items, details = _download_existing_current_export(
                "b", "spain", "proj-1", tmp_path)
        assert items == []
        assert details == {}
        m_run.assert_not_called()

    def test_downloads_and_parses_list_plus_buckets(self, tmp_path):
        existing_list = [{"company_id": "acme-com", "enrichment_skipped": False}]
        existing_buckets = {"company-details-000.json": {"acme-com": {"v": 1}}}
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run",
                   side_effect=_fake_gcloud_for_current_download(existing_list, existing_buckets)):
            items, details = _download_existing_current_export(
                "b", "spain", "proj-1", tmp_path)
        assert items == existing_list
        assert details == {"acme-com": {"v": 1}}


class TestMergeExportIntoExistingCurrent:
    def _write_export_dir(self, export_dir, list_items, buckets):
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "companies.list.json").write_text(json.dumps(list_items), encoding="utf-8")
        for name, data in buckets.items():
            (export_dir / name).write_text(json.dumps(data), encoding="utf-8")

    def test_merges_new_run_with_downloaded_existing_data(self, tmp_path):
        export_dir = tmp_path / "export"
        new_list = [
            {"company_id": "acme-com", "enrichment_skipped": False, "score": 9},  # updates
            {"company_id": "beta-com", "enrichment_skipped": False, "score": 5},  # new
        ]
        self._write_export_dir(export_dir, new_list, {
            "company-details-000.json": {
                "acme-com": {"v": "new-acme"}, "beta-com": {"v": "new-beta"}},
        })
        existing_list = [
            {"company_id": "acme-com", "enrichment_skipped": False, "score": 1},
            {"company_id": "gamma-com", "enrichment_skipped": False, "score": 2},
        ]
        existing_details = {"acme-com": {"v": "old-acme"}, "gamma-com": {"v": "old-gamma"}}

        with patch("cloud_run_streamlit_app._download_existing_current_export",
                   return_value=(existing_list, existing_details)):
            merged_dir, filenames, summary = _merge_export_into_existing_current(
                tmp_path, export_dir, {"generated_at": "now"}, "b", "spain", 500, "proj-1")

        merged_list = json.loads((merged_dir / "companies.list.json").read_text(encoding="utf-8"))
        merged_ids = {i["company_id"] for i in merged_list}
        assert merged_ids == {"acme-com", "beta-com", "gamma-com"}
        acme = next(i for i in merged_list if i["company_id"] == "acme-com")
        assert acme["score"] == 9  # new run's update won (both fully enriched)

        manifest = json.loads((merged_dir / "export_manifest.json").read_text(encoding="utf-8"))
        assert manifest["merge_summary"] == summary
        assert summary["added"] == 1       # beta-com
        assert summary["updated"] == 1     # acme-com
        assert summary["kept_richer_existing"] == 0
        assert summary["total_after"] == 3
        assert "companies.list.json" in filenames
        assert "export_manifest.json" in filenames

    def test_new_gated_thin_row_never_downgrades_existing_rich_company(self, tmp_path):
        export_dir = tmp_path / "export"
        new_list = [{"company_id": "acme-com", "enrichment_skipped": True, "score": 0}]
        self._write_export_dir(export_dir, new_list, {
            "company-details-000.json": {"acme-com": {"v": "new-thin"}},
        })
        existing_list = [{"company_id": "acme-com", "enrichment_skipped": False, "score": 9}]
        existing_details = {"acme-com": {"v": "old-rich"}}

        with patch("cloud_run_streamlit_app._download_existing_current_export",
                   return_value=(existing_list, existing_details)):
            merged_dir, _, summary = _merge_export_into_existing_current(
                tmp_path, export_dir, {}, "b", "spain", 500, "proj-1")

        merged_list = json.loads((merged_dir / "companies.list.json").read_text(encoding="utf-8"))
        assert merged_list[0]["score"] == 9
        assert merged_list[0]["enrichment_skipped"] is False
        assert summary["kept_richer_existing"] == 1
        assert summary["updated"] == 0

    def test_first_ever_run_with_no_existing_data(self, tmp_path):
        export_dir = tmp_path / "export"
        new_list = [{"company_id": "acme-com", "enrichment_skipped": False}]
        self._write_export_dir(export_dir, new_list, {
            "company-details-000.json": {"acme-com": {"v": 1}}})

        with patch("cloud_run_streamlit_app._download_existing_current_export",
                   return_value=([], {})):
            merged_dir, filenames, summary = _merge_export_into_existing_current(
                tmp_path, export_dir, {}, "b", "spain", 500, "proj-1")

        assert summary == {
            "companies_before": 0, "added": 1, "updated": 0,
            "kept_richer_existing": 0, "total_after": 1,
        }
        merged_list = json.loads((merged_dir / "companies.list.json").read_text(encoding="utf-8"))
        assert [i["company_id"] for i in merged_list] == ["acme-com"]


# ── Skip-already-enriched pre-filter (cheaper reruns with Mergen) ───────────

class TestKnownEnrichedCompanyIds:
    def test_only_non_skipped_ids_included(self):
        items = [
            {"company_id": "acme-com", "enrichment_skipped": False},
            {"company_id": "beta-com", "enrichment_skipped": True},
            {"company_id": "gamma-com"},  # missing key treated as not-skipped
        ]
        assert known_enriched_company_ids(items) == {"acme-com", "gamma-com"}

    def test_empty_list_returns_empty_set(self):
        assert known_enriched_company_ids([]) == set()

    def test_ignores_malformed_entries(self):
        items = [{"company_id": "acme-com", "enrichment_skipped": False}, "not-a-dict", {}]
        assert known_enriched_company_ids(items) == {"acme-com"}


class TestSplitRowsByExistingEnrichment:
    def test_known_domains_split_to_already_enriched(self):
        df = pd.DataFrame({
            "company_name": ["Acme", "Beta", "Gamma"],
            "domain": ["acme.com", "beta.com", "gamma.com"],
        })
        known_ids = {"acme-com", "gamma-com"}  # slugify("acme.com") == "acme-com"
        to_process, skipped = split_rows_by_existing_enrichment(df, "domain", known_ids)
        assert list(to_process["company_name"]) == ["Beta"]
        assert list(skipped["company_name"]) == ["Acme", "Gamma"]

    def test_blank_domain_always_goes_to_process(self):
        df = pd.DataFrame({"company_name": ["NoDomain"], "domain": [""]})
        to_process, skipped = split_rows_by_existing_enrichment(df, "domain", {"acme-com"})
        assert len(to_process) == 1
        assert len(skipped) == 0

    def test_nan_domain_always_goes_to_process(self):
        df = pd.DataFrame({"company_name": ["NoDomain"], "domain": [float("nan")]})
        to_process, skipped = split_rows_by_existing_enrichment(df, "domain", {"acme-com"})
        assert len(to_process) == 1
        assert len(skipped) == 0

    def test_no_known_ids_keeps_everything_in_to_process(self):
        df = pd.DataFrame({"domain": ["acme.com", "beta.com"]})
        to_process, skipped = split_rows_by_existing_enrichment(df, "domain", set())
        assert len(to_process) == 2
        assert len(skipped) == 0

    def test_never_mutates_input_dataframe(self):
        df = pd.DataFrame({"domain": ["acme.com"]})
        df_copy = df.copy()
        split_rows_by_existing_enrichment(df, "domain", {"acme-com"})
        pd.testing.assert_frame_equal(df, df_copy)

    def test_matches_make_company_id_slugify_normalization(self):
        # www./scheme are NOT stripped by slugify (matches
        # export_lead_prioritizer_to_lovable_json.make_company_id's actual
        # basis -- normalized_domain is never set in practice, so it always
        # falls back to the raw domain column value) -- a differently-cased
        # domain still matches since slugify lowercases.
        df = pd.DataFrame({"domain": ["ACME.com", "https://www.beta.com"]})
        known_ids = {"acme-com", "https-www-beta-com"}
        to_process, skipped = split_rows_by_existing_enrichment(df, "domain", known_ids)
        assert len(to_process) == 0
        assert len(skipped) == 2


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
