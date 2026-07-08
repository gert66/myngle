"""Tests for rescore_from_gcs — pure re-scoring logic + GCS CLI plumbing.

No real network calls, no real Google Cloud SDK required (subprocess is
always mocked, same style as test_lovable_gcs_upload.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commercial_fit_scoring import LEAN_COEFFICIENTS, score_company
from rescore_from_gcs import (
    CURRENT_MANIFEST_FILENAME,
    LIST_FILENAME,
    build_rescore_manifest,
    default_rescore_run_folder,
    download_current_run,
    download_file,
    gcs_current_dir,
    gcs_run_dir,
    list_country_folders,
    list_current_files,
    rehydrate_scoring_row,
    rescore_all_countries,
    rescore_country,
    rescore_detail_record,
    rescore_details_bucket,
    rescore_list_items,
    resolve_gcs_tool,
    tier_distribution,
    upload_file,
)


# ---------------------------------------------------------------------------
# Fixtures mimicking the scoring_inputs / company-details schema exported by
# export_lead_prioritizer_to_lovable_json._build_scoring_inputs /
# _build_detail_record.
# ---------------------------------------------------------------------------

def fixture_scoring_inputs(**signal_overrides) -> dict:
    signals = {field: 0.0 for field in LEAN_COEFFICIENTS}
    signals.update(signal_overrides)
    return {
        "schema_version": 1,
        "signals": signals,
        "employee_range": "1001-5000",
    }


def fixture_detail(company_id="c1", **signal_overrides) -> dict:
    scoring_inputs = fixture_scoring_inputs(**signal_overrides)
    row = rehydrate_scoring_row(scoring_inputs)
    scored = score_company(row)
    return {
        "company_id": company_id,
        "company_name": f"Company {company_id}",
        "commercial_fit_score": scored["final_commercial_fit_score"],
        "commercial_tier": scored["commercial_tier"],
        "scoring_inputs": scoring_inputs,
    }


# ---------------------------------------------------------------------------
# Pure re-scoring logic
# ---------------------------------------------------------------------------

class TestRehydrateScoringRow:
    def test_signals_and_employee_range_carried_over(self):
        scoring_inputs = fixture_scoring_inputs(sig_foreign_hq_score=3.0)
        row = rehydrate_scoring_row(scoring_inputs)
        assert row["sig_foreign_hq_score"] == 3.0
        assert row["employee_range"] == "1001-5000"

    def test_missing_signal_stays_none_not_coerced_to_zero(self):
        scoring_inputs = fixture_scoring_inputs()
        scoring_inputs["signals"]["sig_rapid_growth_score"] = None
        row = rehydrate_scoring_row(scoring_inputs)
        assert row["sig_rapid_growth_score"] is None


class TestRescoreDetailRecord:
    def test_round_trip_reproduces_pipeline_score_with_same_params(self):
        """The whole point of scoring_inputs: read it back out, feed it into
        score_company() with the SAME params as the original run, and get
        the identical final_commercial_fit_score/commercial_tier."""
        detail = fixture_detail(
            sig_foreign_hq_score=3, sig_explicit_lnd_score=2,
            sig_intl_footprint_score=3, sig_employer_branding_score=1,
            sig_lnd_onboarding_score=2, ti_onboarding_score=1,
            sig_rapid_growth_score=0,
        )
        expected = score_company(rehydrate_scoring_row(detail["scoring_inputs"]))

        rescored = rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")

        assert rescored["commercial_fit_score"] == expected["final_commercial_fit_score"]
        assert rescored["commercial_tier"] == expected["commercial_tier"]
        assert rescored["commercial_fit_score"] == detail["commercial_fit_score"]
        assert rescored["commercial_tier"] == detail["commercial_tier"]

    def test_different_params_change_the_score(self):
        detail = fixture_detail(sig_foreign_hq_score=3, sig_explicit_lnd_score=3)
        rescored = rescore_detail_record(
            detail, params={"intercept": -99.0}, now_iso="2026-07-08T00:00:00Z")
        assert rescored["commercial_fit_score"] != detail["commercial_fit_score"]
        assert rescored["commercial_tier"] == "❄️ Pass"

    def test_does_not_mutate_input_detail(self):
        detail = fixture_detail()
        original = json.loads(json.dumps(detail))
        rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")
        assert detail == original

    def test_missing_scoring_inputs_raises(self):
        detail = {"company_id": "c1"}
        with pytest.raises(ValueError, match="scoring_inputs"):
            rescore_detail_record(detail, params={}, now_iso="2026-07-08T00:00:00Z")

    def test_rescore_audit_records_before_after_and_missing_signals(self):
        detail = fixture_detail(sig_foreign_hq_score=3)
        detail["scoring_inputs"]["signals"]["sig_rapid_growth_score"] = None
        rescored = rescore_detail_record(
            detail, params={"intercept": -1.0}, now_iso="2026-07-08T00:00:00Z")

        audit = rescored["rescore_audit"]
        assert audit["schema_version"] == 1
        assert audit["rescored_at"] == "2026-07-08T00:00:00Z"
        assert audit["params"] == {"intercept": -1.0}
        assert audit["previous_commercial_fit_score"] == detail["commercial_fit_score"]
        assert audit["previous_commercial_tier"] == detail["commercial_tier"]
        assert audit["final_commercial_fit_score"] == rescored["commercial_fit_score"]
        assert audit["commercial_tier"] == rescored["commercial_tier"]
        assert "sig_rapid_growth_score" in audit["missing_scoring_signals"]
        assert isinstance(audit["scoring_notes"], str) and audit["scoring_notes"]


class TestRescoreDetailsBucket:
    def test_rescores_every_company_and_returns_new_dict(self):
        bucket = {
            "c1": fixture_detail("c1", sig_foreign_hq_score=3),
            "c2": fixture_detail("c2", sig_foreign_hq_score=0),
        }
        rescored = rescore_details_bucket(bucket, params={}, now_iso="2026-07-08T00:00:00Z")
        assert set(rescored) == {"c1", "c2"}
        assert rescored is not bucket
        assert all("rescore_audit" in d for d in rescored.values())


class TestRescoreListItems:
    def test_mirrors_new_score_and_tier_onto_matching_list_item(self):
        list_items = [{"company_id": "c1", "commercial_fit_score": 1.0, "commercial_tier": "X"}]
        rescored_by_id = {"c1": {"commercial_fit_score": 9.9, "commercial_tier": "\U0001f947 Hot"}}
        updated = rescore_list_items(list_items, rescored_by_id)
        assert updated[0]["commercial_fit_score"] == 9.9
        assert updated[0]["commercial_tier"] == "\U0001f947 Hot"
        assert updated is not list_items

    def test_item_without_rescored_counterpart_passes_through_unchanged(self):
        list_items = [{"company_id": "c-missing", "commercial_fit_score": 1.0, "commercial_tier": "X"}]
        updated = rescore_list_items(list_items, rescored_by_id={})
        assert updated[0] == list_items[0]
        assert updated[0] is not list_items[0]


class TestTierDistribution:
    def test_counts_by_tier(self):
        details = {
            "c1": {"commercial_tier": "Hot"},
            "c2": {"commercial_tier": "Hot"},
            "c3": {"commercial_tier": "Cool"},
        }
        assert tier_distribution(details) == {"Hot": 2, "Cool": 1}

    def test_empty_details_yields_empty_distribution(self):
        assert tier_distribution({}) == {}


class TestBuildRescoreManifest:
    def test_shape(self):
        original = {"c1": {"commercial_tier": "Cool"}}
        rescored = {"c1": {"commercial_tier": "Hot"}}
        manifest = build_rescore_manifest(
            country_folder="brazil",
            source_current_manifest={"generated_at": "2026-01-01T00:00:00Z"},
            params={"intercept": -1.0},
            run_folder="2026-07-08_rescore",
            original_details_by_id=original,
            rescored_details_by_id=rescored,
            generated_at="2026-07-08T00:00:00Z",
        )
        assert manifest["schema_version"] == 1
        assert manifest["country_folder"] == "brazil"
        assert manifest["run_folder"] == "2026-07-08_rescore"
        assert manifest["companies_rescored"] == 1
        assert manifest["tier_distribution_before"] == {"Cool": 1}
        assert manifest["tier_distribution_after"] == {"Hot": 1}
        assert manifest["promoted_to_current"] is False
        assert manifest["source_current_manifest"]["generated_at"] == "2026-01-01T00:00:00Z"


class TestDefaultRescoreRunFolder:
    def test_format(self):
        now = datetime(2026, 7, 8, 10, 30, tzinfo=timezone.utc)
        assert default_rescore_run_folder(now) == "2026-07-08_rescore"


# ---------------------------------------------------------------------------
# GCS CLI plumbing — subprocess always mocked
# ---------------------------------------------------------------------------

class TestResolveGcsTool:
    def test_prefers_gcloud_storage(self):
        with patch("rescore_from_gcs.shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            assert resolve_gcs_tool() == ["/usr/bin/gcloud", "storage"]

    def test_falls_back_to_gsutil(self):
        def _which(name):
            return "/usr/bin/gsutil" if name == "gsutil" else None
        with patch("rescore_from_gcs.shutil.which", side_effect=_which):
            assert resolve_gcs_tool() == ["/usr/bin/gsutil"]

    def test_none_when_neither_present(self):
        with patch("rescore_from_gcs.shutil.which", return_value=None):
            assert resolve_gcs_tool() is None


class TestPathBuilders:
    def test_current_dir(self):
        assert gcs_current_dir("bucket-a", "brazil") == "gs://bucket-a/brazil/current"

    def test_run_dir(self):
        assert gcs_run_dir("bucket-a", "brazil", "2026-07-08_rescore") == \
            "gs://bucket-a/brazil/runs/2026-07-08_rescore"


class TestListCountryFolders:
    def test_parses_folder_names(self):
        stdout = "gs://bucket-a/brazil/\ngs://bucket-a/italy/\ngs://bucket-a/countries.index.json\n"
        mock_proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_country_folders("bucket-a") == ["brazil", "italy"]

    def test_no_tool_returns_empty_without_subprocess(self):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None), \
             patch("rescore_from_gcs.subprocess.run") as mock_run:
            assert list_country_folders("bucket-a") == []
        mock_run.assert_not_called()

    def test_failed_listing_returns_empty(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="AccessDenied")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_country_folders("bucket-a") == []


class TestListCurrentFiles:
    def test_parses_filenames(self):
        stdout = (
            "gs://bucket-a/brazil/current/companies.list.json\n"
            "gs://bucket-a/brazil/current/company-details-000.json\n"
            "gs://bucket-a/brazil/current/export_manifest.json\n"
        )
        mock_proc = MagicMock(returncode=0, stdout=stdout, stderr="")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            files = list_current_files("bucket-a", "brazil")
        assert files == [
            "companies.list.json", "company-details-000.json", "export_manifest.json",
        ]

    def test_empty_current_folder_returns_empty(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="not found")
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            assert list_current_files("bucket-a", "brazil") == []


class TestDownloadFile:
    def test_successful_download_no_shell(self):
        mock_proc = MagicMock(returncode=0, stdout="Copying...", stderr="")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc) as mock_run:
            result = download_file(["gcloud", "storage"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is True
        args, kwargs = mock_run.call_args
        assert args[0] == ["gcloud", "storage", "cp", "gs://b/f.json", "/tmp/f.json"]
        assert "shell" not in kwargs or kwargs["shell"] is False

    def test_failed_download_captures_stderr(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="NotFound")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            result = download_file(["gsutil"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is False
        assert result["stderr"] == "NotFound"

    def test_subprocess_exception_does_not_raise(self):
        with patch("rescore_from_gcs.subprocess.run", side_effect=OSError("boom")):
            result = download_file(["gcloud", "storage"], "gs://b/f.json", "/tmp/f.json")
        assert result["success"] is False
        assert "boom" in result["error"]


class TestUploadFile:
    def test_missing_local_file_fails_without_subprocess(self, tmp_path):
        missing = tmp_path / "nope.json"
        with patch("rescore_from_gcs.subprocess.run") as mock_run:
            result = upload_file(["gcloud", "storage"], str(missing), "gs://b/f.json")
        assert result["success"] is False
        assert "not found" in result["error"]
        mock_run.assert_not_called()

    def test_successful_upload(self, tmp_path):
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        mock_proc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("rescore_from_gcs.subprocess.run", return_value=mock_proc):
            result = upload_file(["gcloud", "storage"], str(local), "gs://b/f.json")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# download_current_run / rescore_country — end-to-end with mocked subprocess
# ---------------------------------------------------------------------------

def _write_fixture_current_run(country_dir: Path) -> dict:
    """Write a small fixture 'current' run (manifest + list + one details
    bucket) to a local directory, mimicking what would live in
    gs://bucket/<country>/current/. Returns the parsed detail records by id."""
    country_dir.mkdir(parents=True, exist_ok=True)
    detail_a = fixture_detail("company-a", sig_foreign_hq_score=3, sig_explicit_lnd_score=3)
    detail_b = fixture_detail("company-b", sig_foreign_hq_score=0, sig_explicit_lnd_score=0)
    bucket = {"company-a": detail_a, "company-b": detail_b}

    list_items = [
        {
            "company_id": cid,
            "commercial_fit_score": d["commercial_fit_score"],
            "commercial_tier": d["commercial_tier"],
        }
        for cid, d in bucket.items()
    ]
    manifest = {"generated_at": "2026-07-01T00:00:00Z", "export_country": "Brazil"}

    (country_dir / LIST_FILENAME).write_text(json.dumps(list_items), encoding="utf-8")
    (country_dir / "company-details-000.json").write_text(json.dumps(bucket), encoding="utf-8")
    (country_dir / CURRENT_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return bucket


def _fake_gcs_tool_for_local_dir(remote_root: Path):
    """Build a subprocess.run stand-in that serves 'ls'/'cp' from a local
    directory tree as if it were gs://bucket/... — keeps the download tests
    honest about the ls-then-cp flow without touching a real bucket."""

    def _run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[-2] == "ls" or "ls" in cmd:
            pass
        if "ls" in cmd:
            target = cmd[-1]
            rel = target.replace("gs://bucket-a/", "").rstrip("/")
            local_dir = remote_root / rel
            if not local_dir.is_dir():
                return MagicMock(returncode=1, stdout="", stderr="not found")
            lines = [f"{target.rstrip('/')}/{p.name}" for p in sorted(local_dir.iterdir())]
            return MagicMock(returncode=0, stdout="\n".join(lines) + ("\n" if lines else ""), stderr="")
        if "cp" in cmd:
            source, dest = cmd[-2], cmd[-1]
            if source.startswith("gs://"):
                rel = source.replace("gs://bucket-a/", "")
                local_source = remote_root / rel
                Path(dest).write_bytes(local_source.read_bytes())
            else:
                rel = dest.replace("gs://bucket-a/", "")
                remote_path = remote_root / rel
                remote_path.parent.mkdir(parents=True, exist_ok=True)
                remote_path.write_bytes(Path(source).read_bytes())
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


class TestDownloadCurrentRun:
    def test_downloads_manifest_list_and_detail_files(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        work_dir = tmp_path / "work"

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            current = download_current_run("bucket-a", "brazil", work_dir)

        assert current["manifest"]["export_country"] == "Brazil"
        assert len(current["list_items"]) == 2
        assert "company-details-000.json" in current["detail_files"]
        assert set(current["detail_files"]["company-details-000.json"]) == {"company-a", "company-b"}

    def test_no_tool_raises_clear_error(self, tmp_path):
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=None):
            with pytest.raises(RuntimeError, match="gcloud"):
                download_current_run("bucket-a", "brazil", tmp_path / "work")

    def test_empty_current_folder_raises(self, tmp_path):
        remote_root = tmp_path / "remote"
        (remote_root / "brazil" / "current").mkdir(parents=True)
        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            with pytest.raises(RuntimeError, match="No re-scorable files"):
                download_current_run("bucket-a", "brazil", tmp_path / "work")


class TestRescoreCountry:
    def test_end_to_end_writes_to_new_run_folder_never_current(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={},
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        assert manifest["companies_rescored"] == 2
        assert manifest["run_folder"] == "2026-07-08_rescore"
        destinations = {r["destination"] for r in manifest["upload_results"]}
        assert all(d.startswith("gs://bucket-a/brazil/runs/2026-07-08_rescore/") for d in destinations)
        assert all("/current/" not in d for d in destinations)
        assert all(r["success"] for r in manifest["upload_results"])

        # current/ on the fake remote must be untouched.
        current_dir = remote_root / "brazil" / "current"
        original_bucket = json.loads((current_dir / "company-details-000.json").read_text())
        assert original_bucket["company-a"]["commercial_fit_score"] == \
            fixture_detail("company-a", sig_foreign_hq_score=3, sig_explicit_lnd_score=3)["commercial_fit_score"]

    def test_same_params_reproduce_current_scores_in_new_run(self, tmp_path):
        remote_root = tmp_path / "remote"
        original_bucket = _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={},
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        new_bucket = json.loads(
            (remote_root / "brazil" / "runs" / "2026-07-08_rescore" / "company-details-000.json")
            .read_text())
        for cid, original_detail in original_bucket.items():
            assert new_bucket[cid]["commercial_fit_score"] == original_detail["commercial_fit_score"]
            assert new_bucket[cid]["commercial_tier"] == original_detail["commercial_tier"]

    def test_dry_run_skips_upload(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={}, upload=False,
                run_folder="2026-07-08_rescore",
                now=datetime(2026, 7, 8, tzinfo=timezone.utc),
            )

        assert manifest["upload_results"] == []
        assert not (remote_root / "brazil" / "runs").exists()

    def test_work_dir_output_kept_when_explicitly_provided(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        work_dir = tmp_path / "keep-me"

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            manifest = rescore_country(
                "bucket-a", "brazil", params={}, work_dir=work_dir, upload=False,
                run_folder="2026-07-08_rescore",
            )

        assert manifest["local_output_dir"] == str(work_dir / "out")
        assert (work_dir / "out" / LIST_FILENAME).exists()


class TestRescoreAllCountries:
    def test_one_country_failing_does_not_stop_the_others(self, tmp_path):
        remote_root = tmp_path / "remote"
        _write_fixture_current_run(remote_root / "brazil" / "current")
        # "italy" has no current/ folder at all -> should error, not raise.

        with patch("rescore_from_gcs.resolve_gcs_tool", return_value=["gcloud", "storage"]), \
             patch("rescore_from_gcs.subprocess.run", side_effect=_fake_gcs_tool_for_local_dir(remote_root)):
            results = rescore_all_countries(
                "bucket-a", params={}, countries=["brazil", "italy"],
                run_folder="2026-07-08_rescore",
            )

        assert results["brazil"]["companies_rescored"] == 2
        assert "error" in results["italy"]
