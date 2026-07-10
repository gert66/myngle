"""Tests for cloud_dispatcher.py's sidecar-config and auto-merge-on-completion
logic (see docs/cloud_run_workflow.md's "Per-run config" / "Auto-merge +
auto-export" sections).

Everything here uses local paths only (never gs://), so no GCS client is
ever constructed and no live calls happen. An autouse fixture clears every
cloud-related env var so tests never pick up leftover config from the host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloud_dispatcher as disp
from cloud_job_runner import ROW_INDEX_COL

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
