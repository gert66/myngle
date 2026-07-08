"""Tests for cloud_job_runner.py's main() flow: config resolution, sharding,
subprocess invocation, part-output upload, and status-JSON writing.

Everything here uses local paths only (never gs://), so no GCS client is
ever constructed. subprocess.run is always mocked — lead_prioritizer_batch_cli.py
never actually runs, so there are no live Anthropic/Serper/Firecrawl calls
and no API keys are needed. An autouse fixture clears every cloud-related
env var so tests never pick up real secrets or leftover config from the host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloud_job_runner as cjr

_CLOUD_ENV_VARS = [
    "INPUT_GCS_URI", "OUTPUT_GCS_DIR", "RUN_ID", "TASK_COUNT",
    "CLOUD_RUN_TASK_INDEX", "CLOUD_RUN_TASK_COUNT",
    "ANTHROPIC_API_KEY", "SERPER_API_KEY", "FIRECRAWL_API_KEY",
    "MAX_ROWS", "FORCE_RERUN", "MODE",
    "COMPANY_COLUMN", "DOMAIN_COLUMN", "INPUT_COUNTRY_COLUMN",
    "DEEP_DIVE", "RICH_ICP_CONTEXT", "AI_SIGNAL_SCORING",
]


@pytest.fixture(autouse=True)
def _clean_cloud_env(monkeypatch):
    for name in _CLOUD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_synthetic_excel(path: Path, n_rows: int) -> None:
    df = pd.DataFrame({
        "company_name": [f"Company {i}" for i in range(n_rows)],
        "domain": [f"company{i}.example.com" for i in range(n_rows)],
    })
    df.to_excel(path, index=False)


def _fake_batch_cli_subprocess(returncode: int = 0, write_output: bool = True):
    """Stand-in for subprocess.run(["python", "lead_prioritizer_batch_cli.py", ...]).

    Never runs the real CLI. On a "successful" call it writes an output Excel
    at the ``--output`` path, exactly like lead_prioritizer_batch_cli.py would,
    so the runner's post-subprocess logic (locate output, upload) can still be
    exercised without any live enrichment.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd, env=None, **kwargs):
        calls.append(list(cmd))
        if returncode == 0 and write_output:
            input_path = Path(cmd[cmd.index("--input") + 1])
            output_path = Path(cmd[cmd.index("--output") + 1])
            df = pd.read_excel(input_path)
            df["final_commercial_fit_score"] = 1  # stand-in for a real enrichment column
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_excel(output_path, index=False)
        return SimpleNamespace(returncode=returncode)

    _fake_run.calls = calls
    return _fake_run


# ── 1. Happy path: 100 rows / 10 tasks, task 0 ─────────────────────────────

def test_happy_path_task0_writes_part_and_done_status(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 100)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--run-id", "happy-path",
    ])

    assert rc == 0
    assert len(fake_run.calls) == 1
    cmd = fake_run.calls[0]
    assert str(cjr.BATCH_CLI_SCRIPT) in cmd
    assert cmd[cmd.index("--company-column") + 1] == "company_name"
    assert cmd[cmd.index("--domain-column") + 1] == "domain"
    assert cmd[cmd.index("--mode") + 1] == "full"
    assert cmd[cmd.index("--row-limit") + 1] == "0"
    assert "--yes" in cmd

    part_path = output_dir / "parts" / "part_0000.xlsx"
    assert part_path.exists()
    part_df = pd.read_excel(part_path)
    assert len(part_df) == 10
    assert list(part_df[cjr.ROW_INDEX_COL]) == list(range(10))

    status_path = output_dir / "status" / "part_0000_done.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "done"
    assert status["rows_requested"] == 10
    assert status["rows_processed"] == 10
    assert status["row_start"] == 0
    assert status["row_end"] == 10
    assert status["error"] is None

    # A "running" status must have been written before the subprocess call.
    assert (output_dir / "status" / "part_0000_running.json").exists()


def test_api_keys_passed_via_env_not_cmd(tmp_path, monkeypatch):
    """Keys must never appear as plain CLI args (visible in process listings);
    lead_prioritizer_batch_cli.py reads them from the environment."""
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 10)
    output_dir = tmp_path / "out"

    captured_env = {}
    fake_run = _fake_batch_cli_subprocess()

    def _wrapped(cmd, env=None, **kwargs):
        captured_env.update(env or {})
        return fake_run(cmd, env=env, **kwargs)

    monkeypatch.setattr(cjr.subprocess, "run", _wrapped)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "keys-via-env",
        "--anthropic-key", "ant-secret",
        "--serper-key", "serper-secret",
        "--firecrawl-key", "fc-secret",
    ])

    assert rc == 0
    cmd = fake_run.calls[0]
    assert "ant-secret" not in cmd
    assert "serper-secret" not in cmd
    assert "fc-secret" not in cmd
    assert captured_env["ANTHROPIC_API_KEY"] == "ant-secret"
    assert captured_env["SERPER_API_KEY"] == "serper-secret"
    assert captured_env["FIRECRAWL_API_KEY"] == "fc-secret"


def test_max_rows_caps_rows_processed_but_not_rows_requested(tmp_path, monkeypatch):
    """Regression test: lead_prioritizer_batch_cli.py applies --row-limit itself
    (head-of-shard truncation), so rows_processed must reflect that even
    though the shard handed to it (rows_requested) is larger."""
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 100)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--max-rows", "3",
        "--run-id", "max-rows",
    ])

    assert rc == 0
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["rows_requested"] == 10
    assert status["rows_processed"] == 3

    cmd = fake_run.calls[0]
    assert cmd[cmd.index("--row-limit") + 1] == "3"


def test_mode_and_column_overrides_are_forwarded(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({
        "Company Name": ["Acme"],
        "Website": ["acme.example.com"],
        "Land": ["Italy"],
    })
    df.to_excel(input_path, index=False)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "overrides",
        "--mode", "hq_only",
        "--company-column", "Company Name",
        "--domain-column", "Website",
        "--input-country-column", "Land",
        "--deep-dive",
        "--rich-icp-context",
        "--ai-signal-scoring",
    ])

    assert rc == 0
    cmd = fake_run.calls[0]
    assert cmd[cmd.index("--mode") + 1] == "hq_only"
    assert cmd[cmd.index("--company-column") + 1] == "Company Name"
    assert cmd[cmd.index("--domain-column") + 1] == "Website"
    assert cmd[cmd.index("--input-country-column") + 1] == "Land"
    assert "--deep-dive" in cmd
    assert "--rich-icp-context" in cmd
    assert "--ai-signal-scoring" in cmd


def test_unresolvable_columns_fail_before_subprocess(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({"foo": ["a"], "bar": ["b"]})
    df.to_excel(input_path, index=False)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "unresolvable-columns",
    ])

    assert rc == 1
    assert fake_run.calls == []
    status = json.loads((output_dir / "status" / "part_0000_failed.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "Could not resolve company/domain columns" in status["error"]


def test_unknown_mode_fails_fast(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 10)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "unknown-mode",
        "--mode", "not_a_real_mode",
    ])

    assert rc == 1
    assert fake_run.calls == []
    # Fails before the "running" status is even written.
    assert not (output_dir / "status" / "part_0000_running.json").exists()


# ── 2. Idempotency ──────────────────────────────────────────────────────────

def test_existing_part_output_skips_subprocess(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 100)
    output_dir = tmp_path / "out"

    part_path = output_dir / "parts" / "part_0000.xlsx"
    part_path.parent.mkdir(parents=True)
    _write_synthetic_excel(part_path, 3)  # pre-existing, unrelated content
    original_bytes = part_path.read_bytes()

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--run-id", "idempotent",
    ])

    assert rc == 0
    assert fake_run.calls == []  # subprocess never invoked
    assert part_path.read_bytes() == original_bytes  # untouched

    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["status"] == "skipped"


def test_force_rerun_reprocesses_existing_part(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 100)
    output_dir = tmp_path / "out"

    part_path = output_dir / "parts" / "part_0000.xlsx"
    part_path.parent.mkdir(parents=True)
    _write_synthetic_excel(part_path, 3)

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--run-id", "force-rerun",
        "--force-rerun",
    ])

    assert rc == 0
    assert len(fake_run.calls) == 1
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["status"] == "done"


# ── 3. Empty task (task_index beyond row range) ─────────────────────────────

def test_task_beyond_row_range_skips_subprocess_and_reports_zero_rows(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 3)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "9",
        "--task-count", "10",
        "--run-id", "empty-task",
    ])

    assert rc == 0
    assert fake_run.calls == []
    assert not (output_dir / "parts" / "part_0009.xlsx").exists()

    status = json.loads((output_dir / "status" / "part_0009_done.json").read_text(encoding="utf-8"))
    assert status["status"] == "done"
    assert status["rows_processed"] == 0


# ── 4. Failure paths ─────────────────────────────────────────────────────────

def test_subprocess_nonzero_exit_writes_failed_status(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 100)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess(returncode=1)
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--run-id", "failure-path",
    ])

    assert rc == 1
    assert len(fake_run.calls) == 1
    assert not (output_dir / "parts" / "part_0000.xlsx").exists()

    status = json.loads((output_dir / "status" / "part_0000_failed.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "exited with code 1" in status["error"]
    assert status["row_start"] == 0
    assert status["row_end"] == 10

    assert not (output_dir / "status" / "part_0000_done.json").exists()


def test_missing_local_input_writes_failed_status(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(tmp_path / "does_not_exist.xlsx"),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "10",
        "--run-id", "missing-input",
    ])

    assert rc == 1
    assert fake_run.calls == []
    status = json.loads((output_dir / "status" / "part_0000_failed.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "Local input not found" in status["error"]


def test_missing_output_after_subprocess_success_writes_failed_status(tmp_path, monkeypatch):
    """lead_prioritizer_batch_cli.py exits 0 but (for whatever reason) never
    wrote an .xlsx — the runner must still fail loudly instead of uploading
    nothing."""
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 10)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess(returncode=0, write_output=False)
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "no-output",
    ])

    assert rc == 1
    status = json.loads((output_dir / "status" / "part_0000_failed.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "No output written" in status["error"]
