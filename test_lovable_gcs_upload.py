"""Tests for lovable_gcs_upload — pure path/command builders + subprocess
plumbing. No real network calls, no real Google Cloud SDK required."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest

from lovable_gcs_upload import (
    DEFAULT_GCS_BUCKET,
    country_folder_slug,
    default_gcs_run_folder,
    gcs_current_path,
    gcs_archive_path,
    public_url,
    resolve_gcs_upload_tool,
    build_upload_command,
    build_upload_plan,
    upload_file,
    run_upload_plan,
)


class TestCountryFolderSlug:
    def test_known_countries(self):
        assert country_folder_slug("Brazil") == "brazil"
        assert country_folder_slug("Italy") == "italy"
        assert country_folder_slug("Australia") == "australia"
        assert country_folder_slug("Uruguay") == "uruguay"
        assert country_folder_slug("New Zealand") == "new-zealand"

    def test_case_and_whitespace_insensitive(self):
        assert country_folder_slug("  new   zealand  ") == "new-zealand"
        assert country_folder_slug("BRAZIL") == "brazil"

    def test_unknown_country_falls_back_to_generic_slug(self):
        assert country_folder_slug("South Africa") == "south-africa"

    def test_blank_country_never_raises(self):
        assert country_folder_slug("") == "unknown"
        assert country_folder_slug(None) == "unknown"


class TestDefaultGcsRunFolder:
    def test_format(self):
        now = datetime(2026, 7, 3, 10, 30)
        assert default_gcs_run_folder("full_foreign_hq_only", now) == \
            "2026-07-03_full_foreign_hq_only"

    def test_blank_run_mode_falls_back(self):
        now = datetime(2026, 7, 3)
        assert default_gcs_run_folder("", now) == "2026-07-03_run"


class TestDestinationPathBuilders:
    def test_current_path(self):
        assert gcs_current_path("bucket-a", "brazil", "companies.list.json") == \
            "gs://bucket-a/brazil/current/companies.list.json"

    def test_archive_path(self):
        assert gcs_archive_path(
            "bucket-a", "brazil", "2026-07-03_full_foreign_hq_only",
            "company-details-000.json") == (
            "gs://bucket-a/brazil/runs/2026-07-03_full_foreign_hq_only/"
            "company-details-000.json")

    def test_public_url(self):
        assert public_url(DEFAULT_GCS_BUCKET, "italy", "export_manifest.json") == (
            f"https://storage.googleapis.com/{DEFAULT_GCS_BUCKET}/italy/"
            "current/export_manifest.json")


class TestUploadCommandBuilder:
    def test_gcloud_storage_command(self):
        cmd = build_upload_command(
            ["gcloud", "storage", "cp"], "/tmp/companies.list.json",
            "gs://bucket/brazil/current/companies.list.json")
        assert cmd == [
            "gcloud", "storage", "cp", "/tmp/companies.list.json",
            "gs://bucket/brazil/current/companies.list.json",
        ]
        assert isinstance(cmd, list)  # never a shell string

    def test_gsutil_command(self):
        cmd = build_upload_command(
            ["gsutil", "cp"], "/tmp/companies.list.json",
            "gs://bucket/brazil/current/companies.list.json")
        assert cmd == [
            "gsutil", "cp", "/tmp/companies.list.json",
            "gs://bucket/brazil/current/companies.list.json",
        ]


class TestResolveGcsUploadTool:
    def test_prefers_gcloud_when_both_present(self):
        with patch("lovable_gcs_upload.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            assert resolve_gcs_upload_tool() == ["gcloud", "storage", "cp"]

    def test_falls_back_to_gsutil(self):
        def _which(name):
            return "/usr/bin/gsutil" if name == "gsutil" else None
        with patch("lovable_gcs_upload.shutil.which", side_effect=_which):
            assert resolve_gcs_upload_tool() == ["gsutil", "cp"]

    def test_none_when_neither_present(self):
        with patch("lovable_gcs_upload.shutil.which", return_value=None):
            assert resolve_gcs_upload_tool() is None


class TestBuildUploadPlan:
    def test_current_and_archive_jobs(self, tmp_path):
        filenames = ["companies.list.json", "company-details-000.json"]
        jobs = build_upload_plan(
            tmp_path, filenames, "bucket-a", "brazil",
            "2026-07-03_full_foreign_hq_only")
        assert len(jobs) == 4
        current_jobs = [j for j in jobs if j["target"] == "current"]
        archive_jobs = [j for j in jobs if j["target"] == "archive"]
        assert len(current_jobs) == 2
        assert len(archive_jobs) == 2
        assert current_jobs[0]["destination"] == \
            f"gs://bucket-a/brazil/current/companies.list.json"
        assert current_jobs[0]["local_path"] == str(tmp_path / "companies.list.json")
        assert archive_jobs[0]["destination"] == (
            "gs://bucket-a/brazil/runs/2026-07-03_full_foreign_hq_only/"
            "companies.list.json")

    def test_current_only(self, tmp_path):
        jobs = build_upload_plan(
            tmp_path, ["companies.list.json"], "bucket-a", "brazil", "run1",
            upload_current=True, upload_archive=False)
        assert len(jobs) == 1
        assert jobs[0]["target"] == "current"

    def test_neither_toggle_yields_no_jobs(self, tmp_path):
        jobs = build_upload_plan(
            tmp_path, ["companies.list.json"], "bucket-a", "brazil", "run1",
            upload_current=False, upload_archive=False)
        assert jobs == []


class TestUploadFile:
    def test_missing_local_file_fails_without_subprocess(self, tmp_path):
        missing = tmp_path / "nope.json"
        with patch("lovable_gcs_upload.subprocess.run") as mock_run:
            result = upload_file(["gcloud", "storage", "cp"], str(missing), "gs://b/f.json")
        assert result["success"] is False
        assert "not found" in result["error"]
        mock_run.assert_not_called()

    def test_successful_upload_no_shell(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        mock_proc = MagicMock(returncode=0, stdout="Copying...\n", stderr="")
        with patch("lovable_gcs_upload.subprocess.run", return_value=mock_proc) as mock_run:
            result = upload_file(["gcloud", "storage", "cp"], str(local), "gs://b/f.json")
        assert result["success"] is True
        args, kwargs = mock_run.call_args
        assert args[0] == ["gcloud", "storage", "cp", str(local), "gs://b/f.json"]
        assert "shell" not in kwargs or kwargs["shell"] is False

    def test_failed_upload_captures_stderr(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        mock_proc = MagicMock(returncode=1, stdout="", stderr="AccessDenied")
        with patch("lovable_gcs_upload.subprocess.run", return_value=mock_proc):
            result = upload_file(["gsutil", "cp"], str(local), "gs://b/f.json")
        assert result["success"] is False
        assert result["stderr"] == "AccessDenied"

    def test_subprocess_exception_does_not_raise(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        with patch("lovable_gcs_upload.subprocess.run", side_effect=OSError("boom")):
            result = upload_file(["gcloud", "storage", "cp"], str(local), "gs://b/f.json")
        assert result["success"] is False
        assert "boom" in result["error"]


class TestRunUploadPlan:
    def test_no_tool_available_fails_every_job_without_subprocess(self, tmp_path):
        jobs = build_upload_plan(tmp_path, ["companies.list.json"], "b", "brazil", "run1")
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool", return_value=None), \
             patch("lovable_gcs_upload.subprocess.run") as mock_run:
            results = run_upload_plan(jobs)
        assert all(r["success"] is False for r in results)
        assert all("Google Cloud SDK" in r["error"] for r in results)
        mock_run.assert_not_called()

    def test_runs_every_job_with_resolved_tool(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        jobs = build_upload_plan(tmp_path, ["companies.list.json"], "b", "brazil", "run1")
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("lovable_gcs_upload.subprocess.run", return_value=mock_proc) as mock_run:
            results = run_upload_plan(jobs)
        assert len(results) == len(jobs)
        assert all(r["success"] for r in results)
        assert mock_run.call_count == len(jobs)
