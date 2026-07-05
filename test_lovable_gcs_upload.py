"""Tests for lovable_gcs_upload — pure path/command builders + subprocess
plumbing. No real network calls, no real Google Cloud SDK required."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest

from lovable_gcs_upload import (
    COUNTRIES_INDEX_FILENAME,
    DEFAULT_GCS_BUCKET,
    build_flat_upload_plan,
    check_gcloud_available,
    country_folder_slug,
    default_gcs_run_folder,
    describe_gcloud_environment,
    gcs_current_path,
    gcs_archive_path,
    gcs_flat_path,
    gcs_manifest_path,
    normalize_gcs_prefix,
    public_manifest_url,
    public_url,
    public_url_flat,
    resolve_gcs_upload_tool,
    select_lovable_export_files,
    validate_gcs_bucket,
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
        assert country_folder_slug("Netherlands") == "netherlands"

    def test_new_manifest_countries(self):
        assert country_folder_slug("Japan") == "japan"
        assert country_folder_slug("South Korea") == "south-korea"
        assert country_folder_slug("Switzerland") == "switzerland"
        assert country_folder_slug("Test") == "test"

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


class TestManifestPathBuilders:
    def test_gcs_manifest_path_defaults_to_countries_index_filename(self):
        assert gcs_manifest_path("bucket-a") == "gs://bucket-a/countries.index.json"
        assert gcs_manifest_path("bucket-a") == f"gs://bucket-a/{COUNTRIES_INDEX_FILENAME}"

    def test_public_manifest_url_defaults_to_countries_index_filename(self):
        assert public_manifest_url("bucket-a") == \
            "https://storage.googleapis.com/bucket-a/countries.index.json"

    def test_manifest_helpers_accept_explicit_filename(self):
        assert gcs_manifest_path("bucket-a", "other.json") == "gs://bucket-a/other.json"
        assert public_manifest_url("bucket-a", "other.json") == \
            "https://storage.googleapis.com/bucket-a/other.json"


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

    def test_full_windows_executable_path_preserved(self):
        # The resolved tool_cmd may carry a full .cmd shim path (see
        # TestResolveGcsUploadTool) — build_upload_command must pass it
        # through untouched as command[0].
        windows_path = r"C:\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
        cmd = build_upload_command(
            [windows_path, "storage", "cp"], r"C:\out\companies.list.json",
            "gs://bucket/brazil/current/companies.list.json")
        assert cmd == [
            windows_path, "storage", "cp", r"C:\out\companies.list.json",
            "gs://bucket/brazil/current/companies.list.json",
        ]


class TestResolveGcsUploadTool:
    def test_prefers_gcloud_when_both_present(self):
        with patch("lovable_gcs_upload.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            assert resolve_gcs_upload_tool() == ["/usr/bin/gcloud", "storage", "cp"]

    def test_falls_back_to_gsutil(self):
        def _which(name):
            return "/usr/bin/gsutil" if name == "gsutil" else None
        with patch("lovable_gcs_upload.shutil.which", side_effect=_which):
            assert resolve_gcs_upload_tool() == ["/usr/bin/gsutil", "cp"]

    def test_none_when_neither_present(self):
        with patch("lovable_gcs_upload.shutil.which", return_value=None):
            assert resolve_gcs_upload_tool() is None

    def test_windows_uses_exact_gcloud_cmd_shim_path(self):
        # shutil.which("gcloud") on Windows resolves to the .cmd shim — that
        # exact path must become command[0], since subprocess.run (no
        # shell=True) cannot find the bare "gcloud" name on Windows.
        windows_path = r"C:\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

        def _which(name):
            return windows_path if name == "gcloud" else None

        with patch("lovable_gcs_upload.shutil.which", side_effect=_which):
            cmd = resolve_gcs_upload_tool()
        assert cmd[0] == windows_path
        assert cmd == [windows_path, "storage", "cp"]

    def test_windows_uses_exact_gsutil_cmd_shim_path(self):
        windows_path = r"C:\Google\Cloud SDK\google-cloud-sdk\bin\gsutil.cmd"

        def _which(name):
            return windows_path if name == "gsutil" else None

        with patch("lovable_gcs_upload.shutil.which", side_effect=_which):
            cmd = resolve_gcs_upload_tool()
        assert cmd[0] == windows_path
        assert cmd == [windows_path, "cp"]


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


class TestNormalizeGcsPrefix:
    def test_strips_leading_and_trailing_slashes(self):
        assert normalize_gcs_prefix("/brazil/current/") == "brazil/current"

    def test_collapses_double_slashes(self):
        assert normalize_gcs_prefix("brazil//current") == "brazil/current"

    def test_blank_never_raises(self):
        assert normalize_gcs_prefix("") == ""
        assert normalize_gcs_prefix(None) == ""

    def test_whitespace_stripped(self):
        assert normalize_gcs_prefix("  brazil/current  ") == "brazil/current"


class TestCheckGcloudAvailable:
    def test_available_reports_tool_and_version(self):
        mock_proc = MagicMock(returncode=0, stdout="Google Cloud SDK 500.0.0\n", stderr="")
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("lovable_gcs_upload.subprocess.run", return_value=mock_proc):
            info = check_gcloud_available()
        assert info["available"] is True
        assert info["tool"] == "gcloud"
        assert info["version"] == "Google Cloud SDK 500.0.0"

    def test_unavailable_when_no_tool_on_path(self):
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool", return_value=None):
            info = check_gcloud_available()
        assert info["available"] is False
        assert info["tool"] is None

    def test_version_check_failure_does_not_raise(self):
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("lovable_gcs_upload.subprocess.run", side_effect=OSError("boom")):
            info = check_gcloud_available()
        assert info["available"] is True
        assert info["version"] == ""


class TestDescribeGcloudEnvironment:
    def test_reads_account_and_project(self):
        def _run(cmd, **kwargs):
            if "auth" in cmd:
                return MagicMock(returncode=0, stdout="user@example.com\n", stderr="")
            return MagicMock(returncode=0, stdout="my-project\n", stderr="")

        with patch("lovable_gcs_upload.shutil.which", return_value="/usr/bin/gcloud"), \
             patch("lovable_gcs_upload.subprocess.run", side_effect=_run):
            info = describe_gcloud_environment()
        assert info["account"] == "user@example.com"
        assert info["project"] == "my-project"

    def test_no_gcloud_returns_blank_without_subprocess(self):
        with patch("lovable_gcs_upload.shutil.which", return_value=None), \
             patch("lovable_gcs_upload.subprocess.run") as mock_run:
            info = describe_gcloud_environment()
        assert info == {"account": "", "project": ""}
        mock_run.assert_not_called()

    def test_subprocess_failure_does_not_raise(self):
        with patch("lovable_gcs_upload.shutil.which", return_value="/usr/bin/gcloud"), \
             patch("lovable_gcs_upload.subprocess.run", side_effect=OSError("boom")):
            info = describe_gcloud_environment()
        assert info == {"account": "", "project": ""}

    def test_never_returns_secret_like_values(self):
        # Defensive: account/project must be plain identifiers, never a
        # token-like string that could be mistaken for a secret.
        def _run(cmd, **kwargs):
            if "auth" in cmd:
                return MagicMock(returncode=0, stdout="user@example.com\n", stderr="")
            return MagicMock(returncode=0, stdout="my-project\n", stderr="")

        with patch("lovable_gcs_upload.shutil.which", return_value="/usr/bin/gcloud"), \
             patch("lovable_gcs_upload.subprocess.run", side_effect=_run):
            info = describe_gcloud_environment()
        assert "ya29." not in info["account"]
        assert "ya29." not in info["project"]


class TestSelectLovableExportFiles:
    def test_selects_only_allowed_patterns(self, tmp_path):
        (tmp_path / "companies.list.json").write_text("[]")
        (tmp_path / "company-details-000.json").write_text("{}")
        (tmp_path / "company-details-001.json").write_text("{}")
        (tmp_path / "export_manifest.json").write_text("{}")
        (tmp_path / "old_run.xlsx").write_text("stale")
        (tmp_path / "notes.txt").write_text("unrelated")

        files = select_lovable_export_files(tmp_path)

        assert files == [
            "companies.list.json", "company-details-000.json",
            "company-details-001.json", "export_manifest.json",
        ]

    def test_missing_directory_returns_empty_list(self, tmp_path):
        assert select_lovable_export_files(tmp_path / "does_not_exist") == []

    def test_empty_directory_returns_empty_list(self, tmp_path):
        assert select_lovable_export_files(tmp_path) == []

    def test_never_selects_unrelated_files(self, tmp_path):
        (tmp_path / "companies.list.json").write_text("[]")
        (tmp_path / "secrets.env").write_text("API_KEY=x")
        (tmp_path / "workbook.xlsx").write_text("stale")
        files = select_lovable_export_files(tmp_path)
        assert files == ["companies.list.json"]
        assert "secrets.env" not in files
        assert "workbook.xlsx" not in files


class TestValidateGcsBucket:
    def test_blank_bucket_is_invalid(self):
        assert validate_gcs_bucket("") is not None
        assert validate_gcs_bucket(None) is not None

    def test_valid_bucket_names_pass(self):
        assert validate_gcs_bucket("myngle-company-data-104527058436") is None
        assert validate_gcs_bucket("my.bucket_name-123") is None

    def test_uppercase_bucket_is_invalid(self):
        assert validate_gcs_bucket("MyBucket") is not None

    def test_bucket_with_spaces_is_invalid(self):
        assert validate_gcs_bucket("my bucket") is not None

    def test_error_message_never_empty(self):
        err = validate_gcs_bucket("")
        assert err and "required" in err.lower()


class TestFlatGcsPathBuilders:
    def test_gcs_flat_path_with_prefix(self):
        assert gcs_flat_path("bucket-a", "brazil/current", "companies.list.json") == \
            "gs://bucket-a/brazil/current/companies.list.json"

    def test_gcs_flat_path_normalizes_prefix(self):
        assert gcs_flat_path("bucket-a", "/brazil//current/", "f.json") == \
            "gs://bucket-a/brazil/current/f.json"

    def test_gcs_flat_path_blank_prefix(self):
        assert gcs_flat_path("bucket-a", "", "f.json") == "gs://bucket-a/f.json"

    def test_public_url_flat_matches_destination_layout(self):
        assert public_url_flat("bucket-a", "brazil/current", "companies.list.json") == \
            "https://storage.googleapis.com/bucket-a/brazil/current/companies.list.json"


class TestBuildFlatUploadPlan:
    def test_builds_one_job_per_file(self, tmp_path):
        jobs = build_flat_upload_plan(
            tmp_path, ["companies.list.json", "export_manifest.json"],
            "bucket-a", "brazil/current")
        assert len(jobs) == 2
        assert jobs[0]["local_path"] == str(tmp_path / "companies.list.json")
        assert jobs[0]["destination"] == \
            "gs://bucket-a/brazil/current/companies.list.json"
        assert all(j["target"] == "flat" for j in jobs)

    def test_empty_filenames_yields_no_jobs(self, tmp_path):
        assert build_flat_upload_plan(tmp_path, [], "bucket-a", "brazil/current") == []


class TestFlatPlanRunsWithoutRealGcloud:
    def test_flat_plan_runs_through_run_upload_plan(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        jobs = build_flat_upload_plan(
            tmp_path, ["companies.list.json"], "bucket-a", "brazil/current")
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("lovable_gcs_upload.subprocess.run", return_value=mock_proc) as mock_run:
            results = run_upload_plan(jobs)
        assert all(r["success"] for r in results)
        mock_run.assert_called_once()

    def test_flat_plan_reports_missing_gcloud_without_subprocess(self, tmp_path):
        jobs = build_flat_upload_plan(
            tmp_path, ["companies.list.json"], "bucket-a", "brazil/current")
        with patch("lovable_gcs_upload.resolve_gcs_upload_tool", return_value=None), \
             patch("lovable_gcs_upload.subprocess.run") as mock_run:
            results = run_upload_plan(jobs)
        assert all(r["success"] is False for r in results)
        assert all("Google Cloud SDK" in r["error"] for r in results)
        mock_run.assert_not_called()
