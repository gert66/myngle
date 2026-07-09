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
    "MAX_ROWS", "TOTAL_ROW_LIMIT", "FORCE_RERUN", "MODE",
    "COMPANY_COLUMN", "DOMAIN_COLUMN", "INPUT_COUNTRY_COLUMN",
    "COMPOSE_CALLER_CONTENT", "DEEP_DIVE", "DEEP_DIVE_MIN_SCORE",
    "DEEP_DIVE_ON_FOREIGN_HQ", "RICH_ICP_CONTEXT", "AI_SIGNAL_SCORING",
    "USE_ENRICHMENT_CACHE", "ENRICHMENT_CACHE_BUCKET", "C5_ENABLED",
    "GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ",
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


def _fake_batch_cli_subprocess(returncode: int = 0, write_output: bool = True, write_usage: bool = True):
    """Stand-in for subprocess.run(["python", "lead_prioritizer_batch_cli.py", ...]).

    Never runs the real CLI. On a "successful" call it writes an output Excel
    at the ``--output`` path, exactly like lead_prioritizer_batch_cli.py would,
    so the runner's post-subprocess logic (locate output, upload) can still be
    exercised without any live enrichment. ``write_usage`` (default True,
    matching real behavior) also writes a small usage_tracker-shaped JSON at
    ``--usage-output``; set False to simulate an old deployed image that
    predates the --usage-output flag.
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
        if returncode == 0 and write_usage and "--usage-output" in cmd:
            usage_path = Path(cmd[cmd.index("--usage-output") + 1])
            usage_path.parent.mkdir(parents=True, exist_ok=True)
            usage_path.write_text(
                json.dumps({"serper_total": 3, "anthropic_calls": 2}), encoding="utf-8")
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


def test_usage_output_flag_forwarded_and_folded_into_done_status(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 5)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "usage-report",
    ])

    assert rc == 0
    cmd = fake_run.calls[0]
    assert "--usage-output" in cmd
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["usage"] == {"serper_total": 3, "anthropic_calls": 2}


def test_missing_usage_output_leaves_usage_none_without_failing_task(tmp_path, monkeypatch):
    """An old deployed image without --usage-output support (or any other
    reason the file never appears) must not fail an otherwise-successful task."""
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 5)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess(write_usage=False)
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "no-usage-report",
    ])

    assert rc == 0
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["usage"] is None


def test_total_row_limit_shrinks_shards_before_sharding(tmp_path, monkeypatch):
    """--total-row-limit truncates the file to N rows BEFORE task_count shards
    are computed, so the N rows are distributed proportionally across tasks --
    distinct from --max-rows, which only caps rows WITHIN one already-computed
    shard. 500 rows, total_row_limit=100, task_count=50 -> shard size 2, not 10."""
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 500)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "50",
        "--total-row-limit", "100",
        "--run-id", "total-row-limit",
    ])

    assert rc == 0
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["row_start"] == 0
    assert status["row_end"] == 2
    assert status["rows_requested"] == 2


def test_total_row_limit_unset_uses_full_file(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    _write_synthetic_excel(input_path, 500)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "50",
        "--run-id", "no-total-row-limit",
    ])

    assert rc == 0
    status = json.loads((output_dir / "status" / "part_0000_done.json").read_text(encoding="utf-8"))
    assert status["row_end"] == 10  # 500 / 50, unaffected by any limit


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


def test_new_opt_in_flags_are_forwarded(tmp_path, monkeypatch):
    """compose-caller-content / deep-dive tuning / enrichment cache / C5 all
    reach the lead_prioritizer_batch_cli.py subprocess command unchanged."""
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({
        "Company Name": ["Acme"],
        "Website": ["acme.example.com"],
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
        "--run-id", "new-flags",
        "--company-column", "Company Name",
        "--domain-column", "Website",
        "--compose-caller-content",
        "--deep-dive",
        "--deep-dive-min-score", "0",
        "--no-deep-dive-on-foreign-hq",
        "--use-enrichment-cache",
        "--enrichment-cache-bucket", "my-bucket",
        "--c5-enabled",
        "--gate-full-enrichment-on-foreign-hq",
    ])

    assert rc == 0
    cmd = fake_run.calls[0]
    assert "--compose-caller-content" in cmd
    assert cmd[cmd.index("--deep-dive-min-score") + 1] == "0.0"
    assert "--no-deep-dive-on-foreign-hq" in cmd
    assert "--use-enrichment-cache" in cmd
    assert cmd[cmd.index("--enrichment-cache-bucket") + 1] == "my-bucket"
    assert "--c5-enabled" in cmd
    assert "--gate-full-enrichment-on-foreign-hq" in cmd


def test_gate_full_enrichment_on_foreign_hq_defaults_off(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({"Company Name": ["Acme"], "Website": ["acme.example.com"]})
    df.to_excel(input_path, index=False)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "no-gate",
        "--company-column", "Company Name",
        "--domain-column", "Website",
    ])

    assert rc == 0
    assert "--gate-full-enrichment-on-foreign-hq" not in fake_run.calls[0]


def test_gate_full_enrichment_on_foreign_hq_via_env_var(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({"Company Name": ["Acme"], "Website": ["acme.example.com"]})
    df.to_excel(input_path, index=False)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)
    monkeypatch.setenv("GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ", "true")

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "gate-via-env",
        "--company-column", "Company Name",
        "--domain-column", "Website",
    ])

    assert rc == 0
    assert "--gate-full-enrichment-on-foreign-hq" in fake_run.calls[0]


def test_deep_dive_on_foreign_hq_stays_on_by_default(tmp_path, monkeypatch):
    input_path = tmp_path / "input.xlsx"
    df = pd.DataFrame({"Company Name": ["Acme"], "Website": ["acme.example.com"]})
    df.to_excel(input_path, index=False)
    output_dir = tmp_path / "out"

    fake_run = _fake_batch_cli_subprocess()
    monkeypatch.setattr(cjr.subprocess, "run", fake_run)

    rc = cjr.main([
        "--input", str(input_path),
        "--output-dir", str(output_dir),
        "--task-index", "0",
        "--task-count", "1",
        "--run-id", "default-foreign-hq-trigger",
        "--company-column", "Company Name",
        "--domain-column", "Website",
        "--deep-dive",
    ])

    assert rc == 0
    cmd = fake_run.calls[0]
    assert "--no-deep-dive-on-foreign-hq" not in cmd
    assert cmd[cmd.index("--deep-dive-min-score") + 1] == "8.0"


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
