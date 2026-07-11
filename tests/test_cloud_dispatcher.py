"""Tests for cloud_dispatcher.py's sidecar-config and auto-merge-on-completion
logic (see docs/cloud_run_workflow.md's "Per-run config" / "Auto-merge +
auto-export" sections).

Most tests here use local paths only (never gs://), so no GCS client is
constructed and no live calls happen. An autouse fixture clears every
cloud-related env var so tests never pick up leftover config from the host.
The "go live" tests (TestDownloadExistingCurrent / TestRunLovableExportLive)
are the exception -- they exercise the <country>/current/ merge-and-upload
path, which always targets real gs:// destinations regardless of output_dir,
so they patch both cloud_dispatcher._gcs_client and cloud_job_runner._gcs_client
(upload_output_file resolves the latter from its own module) to a shared fake.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloud_dispatcher as disp
import cloud_job_runner as cjr
from cloud_job_runner import ROW_INDEX_COL
from test_export_lead_prioritizer_to_lovable_json import enriched_row, write_workbook

_CLOUD_ENV_VARS = [
    "CLOUD_RUN_JOB_NAME", "CLOUD_RUN_REGION", "CLOUD_RUN_PROJECT",
    "RUNS_GCS_DIR", "DEFAULT_TASK_COUNT",
]


@pytest.fixture(autouse=True)
def _clean_cloud_env(monkeypatch):
    for name in _CLOUD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_part(path: Path, names: list[str], indices: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"company_name": names, ROW_INDEX_COL: indices}).to_excel(
        path, sheet_name="Enriched Leads", index=False)


def _write_status(output_dir: Path, task_index: int, status: str) -> None:
    status_dir = output_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / f"part_{task_index:04d}_{status}.json").write_text(
        json.dumps({"task_index": task_index, "status": status}), encoding="utf-8")


def _write_manifest(output_dir: Path, run_id: str, task_count: int, config: dict | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"run_id": run_id, "task_count": task_count, "config": config or {}}
    (output_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


# ── build_env_overrides_from_config ─────────────────────────────────────────

def test_env_overrides_empty_config_produces_no_overrides():
    assert disp.build_env_overrides_from_config({}) == {}


def test_env_overrides_bool_and_scalar_keys_are_mapped():
    config = {
        "gate_full_enrichment_on_foreign_hq": True,
        "deep_dive": False,
        "mode": "hq_only",
        "deep_dive_min_score": 7.5,
    }
    overrides = disp.build_env_overrides_from_config(config)
    assert overrides == {
        "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ": "true",
        "DEEP_DIVE": "false",
        "MODE": "hq_only",
        "DEEP_DIVE_MIN_SCORE": "7.5",
    }


def test_env_overrides_absent_and_none_keys_are_skipped():
    config = {"mode": None, "c5_enabled": None}
    assert disp.build_env_overrides_from_config(config) == {}


# ── sidecar_config_name ─────────────────────────────────────────────────────

def test_sidecar_config_name_appends_suffix():
    assert disp.sidecar_config_name("incoming/Spain_cleaned.xlsx") == \
        "incoming/Spain_cleaned.xlsx.config.json"


# ── _extract_run_id_from_status_object ──────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("runs/20260710_abc/status/part_0000_done.json", "20260710_abc"),
    ("runs/20260710_abc/status/part_0007_failed.json", "20260710_abc"),
    ("runs/20260710_abc/status/part_0000_running.json", None),
    ("runs/20260710_abc/status/part_0000_checkpoint.json", None),
    ("runs/20260710_abc/final/manifest_done.json", None),
    ("runs/20260710_abc/parts/part_0000.xlsx", None),
    ("incoming/klant.xlsx", None),
])
def test_extract_run_id_from_status_object(name, expected):
    assert disp._extract_run_id_from_status_object(name) == expected


# ── _claim_completion (local path, no GCS) ──────────────────────────────────

def test_claim_completion_only_the_first_caller_wins(tmp_path):
    claim_uri = str(tmp_path / "final" / "_merge_claimed.json")
    assert disp._claim_completion(claim_uri, "run-1") is True
    assert disp._claim_completion(claim_uri, "run-1") is False


# ── check_and_trigger_completion ────────────────────────────────────────────

def test_completion_check_ignored_without_manifest(tmp_path):
    result = disp.check_and_trigger_completion(str(tmp_path / "out"), "run-1")
    assert result["status"] == "ignored"


def test_completion_check_waits_until_all_tasks_reported(tmp_path):
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=2)
    _write_status(output_dir, 0, "done")

    result = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert result["status"] == "waiting"
    assert result["reported"] == 1
    assert result["expected"] == 2


def test_completion_check_merges_once_all_tasks_are_done(tmp_path):
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=2)
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_part(output_dir / "parts" / "part_0001.xlsx", ["B"], [1])
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "done")

    result = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert result["status"] == "merged"
    assert result["export"] is None
    final_path = output_dir / "final" / "lead_prioritizer_final.xlsx"
    assert final_path.exists()
    merge_manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert merge_manifest["status"] == "done"


def test_completion_check_is_claimed_only_once(tmp_path):
    """A second status event arriving after the run already merged (e.g. a
    duplicate/late Eventarc delivery) must not re-trigger the merge."""
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=1)
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")

    first = disp.check_and_trigger_completion(str(output_dir), "run-1")
    second = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert first["status"] == "merged"
    assert second["status"] == "already_claimed"


def test_completion_check_reports_merge_failure_without_crashing(tmp_path):
    """All tasks reported, but one failed -- cloud_merge_results.main()
    refuses to merge; the dispatcher must surface that, not raise."""
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=2)
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "failed")

    result = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert result["status"] == "merge_failed"


def test_completion_check_retries_after_a_later_retry_succeeds(tmp_path):
    """Task 1 fails first, so the merge attempt correctly fails and claims
    the run -- but Cloud Run's own task-level retry then succeeds for that
    same task index (its "_done.json" lands alongside the stale
    "_failed.json"), which fires another status event. That event must be
    able to claim and merge instead of being permanently locked out by the
    first, premature attempt's claim."""
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=2)
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")
    _write_status(output_dir, 1, "failed")

    first = disp.check_and_trigger_completion(str(output_dir), "run-1")
    assert first["status"] == "merge_failed"

    # Cloud Run retries task 1; it succeeds and writes _done.json (the stale
    # _failed.json from the first attempt is never deleted).
    _write_part(output_dir / "parts" / "part_0001.xlsx", ["B"], [1])
    _write_status(output_dir, 1, "done")

    second = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert second["status"] == "merged"
    merge_manifest = json.loads((output_dir / "final" / "manifest_done.json").read_text(encoding="utf-8"))
    assert merge_manifest["status"] == "done"
    assert merge_manifest["row_count"] == 2


def test_completion_check_does_not_double_count_a_retried_task_as_reported_twice(tmp_path):
    """Task 0 has both a stale _failed.json and a fresh _done.json (from a
    Cloud Run-driven retry) -- reported must count it once, as done, not
    twice against expected_task_count."""
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=1)
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "failed")
    _write_status(output_dir, 0, "done")

    result = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert result["status"] == "merged"


def test_completion_check_reports_lovable_export_config_error(tmp_path):
    """lovable_export.enabled=true without country/cold_callers must not
    crash the pipeline or block the merge -- only the export sub-result
    reports the error."""
    output_dir = tmp_path / "out"
    _write_manifest(output_dir, "run-1", task_count=1, config={"lovable_export": {"enabled": True}})
    _write_part(output_dir / "parts" / "part_0000.xlsx", ["A"], [0])
    _write_status(output_dir, 0, "done")

    result = disp.check_and_trigger_completion(str(output_dir), "run-1")

    assert result["status"] == "merged"
    assert result["export"]["ok"] is False
    assert "country" in result["export"]["error"] or "cold_callers" in result["export"]["error"]


# ── _download_existing_current_via_client / _run_lovable_export (go-live) ──
# Fakes mirror test_screened_domains_ledger.py / test_enrichment_cache.py,
# extended with list_blobs (not needed there) and upload_from_filename
# (upload_output_file uses filename upload, not upload_from_string).

class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_text(self) -> str:
        return self._store[self.name]

    def upload_from_filename(self, local_path: str) -> None:
        self._store[self.name] = Path(local_path).read_text(encoding="utf-8")

    def upload_from_string(self, data, content_type=None) -> None:
        self._store[self.name] = data


class _FakeBucket:
    def __init__(self, store: dict):
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)

    def list_blobs(self, prefix: str = ""):
        return [_FakeBlob(self._store, name) for name in self._store if name.startswith(prefix)]


class _FakeClient:
    def __init__(self, store: dict):
        self._store = store

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store)


class TestDownloadExistingCurrentViaClient:
    def test_nothing_existing_returns_empty(self):
        with patch.object(disp, "_gcs_client", return_value=_FakeClient({})):
            items, details = disp._download_existing_current_via_client("bucket", "brazil")
        assert items == []
        assert details == {}

    def test_parses_existing_list_and_detail_buckets(self):
        store = {
            "brazil/current/companies.list.json": json.dumps(
                [{"company_id": "acme-com-br", "company_name": "Acme Brasil"}]),
            "brazil/current/company-details-000.json": json.dumps(
                {"acme-com-br": {"company_id": "acme-com-br", "company_name": "Acme Brasil"}}),
            # A file under a DIFFERENT country's current/ must never leak in.
            "italy/current/companies.list.json": json.dumps([{"company_id": "other"}]),
        }
        with patch.object(disp, "_gcs_client", return_value=_FakeClient(store)):
            items, details = disp._download_existing_current_via_client("bucket", "brazil")
        assert items == [{"company_id": "acme-com-br", "company_name": "Acme Brasil"}]
        assert "acme-com-br" in details
        assert "other" not in details


class TestRunLovableExportLive:
    """_run_lovable_export must not just archive the raw export into the
    runs bucket -- it must also merge it into <country>/current/ (the
    bucket the Lovable app actually reads from) and archive that same
    merged snapshot under <country>/runs/<run_folder>/."""

    def _export_and_upload(self, tmp_path, store, rows):
        xlsx = tmp_path / "final.xlsx"
        write_workbook(xlsx, rows)
        with patch.object(disp, "_gcs_client", return_value=_FakeClient(store)), \
             patch.object(cjr, "_gcs_client", return_value=_FakeClient(store)):
            return disp._run_lovable_export(
                str(xlsx), str(tmp_path / "out"),
                {"country": "Brazil", "cold_callers": ["Jantje", "Pietje"], "foreign_hq_only": False},
            )

    def test_first_ever_export_goes_live_with_no_prior_current(self, tmp_path):
        store: dict = {}
        result = self._export_and_upload(tmp_path, store, [enriched_row()])

        assert result["ok"] is True
        assert result["live_upload"]["ok"] is True
        assert result["live_upload"]["companies_total_after"] == 1
        current_list = json.loads(store["brazil/current/companies.list.json"])
        assert [c["company_name"] for c in current_list] == ["Acme Brasil"]
        # Archived under brazil/runs/<run_folder>/, not just the runs-bucket
        # staging copy under <output_dir>/final/lovable_export/.
        assert any(k.startswith("brazil/runs/") and k.endswith("companies.list.json") for k in store)

    def test_merges_into_existing_current_instead_of_overwriting(self, tmp_path):
        store = {
            "brazil/current/companies.list.json": json.dumps(
                [{"company_id": "existing-co", "company_name": "Existing Co", "enrichment_skipped": False}]),
            "brazil/current/company-details-000.json": json.dumps(
                {"existing-co": {"company_id": "existing-co", "company_name": "Existing Co"}}),
        }
        result = self._export_and_upload(
            tmp_path, store, [enriched_row(source_index=1, company_name="Acme Brasil", domain="acme.com.br")])

        assert result["live_upload"]["ok"] is True
        current_list = json.loads(store["brazil/current/companies.list.json"])
        current_ids = {c["company_id"] for c in current_list}
        assert "existing-co" in current_ids, "pre-existing company must survive the merge"
        assert len(current_list) == 2
        assert result["live_upload"]["companies_total_after"] == 2

    def test_live_upload_failure_does_not_flip_the_base_result_to_not_ok(self, tmp_path):
        """A broken current/-merge step (e.g. a bad bucket) must not hide
        that the archive-to-runs-bucket export itself succeeded."""
        xlsx = tmp_path / "final.xlsx"
        write_workbook(xlsx, [enriched_row()])
        with patch.object(disp, "_gcs_client", return_value=_FakeClient({})), \
             patch.object(cjr, "_gcs_client", side_effect=RuntimeError("boom")):
            result = disp._run_lovable_export(
                str(xlsx), str(tmp_path / "out"),
                {"country": "Brazil", "cold_callers": ["Jantje"], "foreign_hq_only": False},
            )
        assert result["ok"] is True
        assert result["live_upload"]["ok"] is False
        assert "boom" in result["live_upload"]["error"]
