"""Persistent job supervisor for phase-3 orchestrator jobs."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.claude_runner import DEFAULT_CLAUDE_BIN, DEFAULT_TIMEOUT_SECONDS
from core.machine import Machine
from core.notify import NotifyError, notify
from core.providers import DEFAULT_CODEX_BIN, build_runner
from core.state import load_state
from core.state import load_json, save_json
from core.scheduler import Scheduler

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOBS_DIR = ORCH_ROOT / "jobs"
TERMINAL_PHASES = frozenset({"DONE", "ERROR", "NEEDS_HUMAN"})
MARKER_FILE = "notification_state.json"


def _parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds_until(value, now=None):
    target = _parse_ts(value)
    if target is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - now).total_seconds())


def _usage_suffix(status):
    totals = status.get("totals") or {}
    calls = totals.get("claude_calls", 0)
    cost = totals.get("cost_usd", 0) or 0
    inp = totals.get("input_tokens", 0)
    out = totals.get("output_tokens", 0)
    cache = totals.get("cache_read_input_tokens", 0)
    if not any((calls, cost, inp, out, cache)):
        return ""
    return f" | Claude: {calls} call(s), ${cost:.4f}, in {inp}, out {out}, cache-read {cache}"


def _detail(status):
    if status["phase"] == "NEEDS_HUMAN":
        detail = status.get("human_question") or "Human input required"
    elif status["phase"] == "ERROR":
        detail = status.get("last_error") or "Orchestrator error"
    else:
        detail = "Job completed successfully"
    return detail + _usage_suffix(status)


def _notification_key(status):
    return {
        "phase": status["phase"],
        "updated_at": status.get("updated_at"),
        "step_id": status.get("step_id"),
    }


def notify_once(job_dir, status, notifier=notify):
    if status["phase"] not in TERMINAL_PHASES:
        return False
    marker = Path(job_dir) / MARKER_FILE
    key = _notification_key(status)
    if marker.is_file():
        try:
            previous = load_json(marker)
        except Exception:
            previous = None
        if previous == key:
            return False
    notifier(status["phase"], status["job_id"], _detail(status))
    save_json(marker, key)
    return True


def supervise(job_dir, runner=None, sleep=time.sleep, max_cycles=None, log=print, scheduler=None):
    job_dir = Path(job_dir)
    scheduler = scheduler or Scheduler(jobs_dir=job_dir.parent)
    cycles = 0
    while True:
        machine = Machine(job_dir, runner=runner)
        status = machine.status()
        phase = status["phase"]
        if phase in TERMINAL_PHASES:
            try:
                notify_once(job_dir, status)
            except NotifyError as exc:
                log(f"notification warning: {exc}")
            return phase
        if max_cycles is not None and cycles >= max_cycles:
            return phase
        cycles += 1
        if phase == "WAITING":
            delay = seconds_until(status.get("wait_until"))
            if delay > 0:
                log(f"waiting {int(delay)}s for {status.get('retry_phase')}")
                sleep(delay)
            # Capacity is reacquired before resuming work. Long quota/backoff
            # waits therefore do not occupy a scheduler slot.
            if not scheduler.wait_for_slot(job_dir, sleep=sleep, log=log):
                return Machine(job_dir, runner=runner).status()["phase"]
            try:
                Machine(job_dir, runner=runner).resume(force=True)
            finally:
                scheduler.release(status["job_id"], reason="run segment ended")
        else:
            if not scheduler.wait_for_slot(job_dir, sleep=sleep, log=log):
                return Machine(job_dir, runner=runner).status()["phase"]
            try:
                Machine(job_dir, runner=runner).run()
            finally:
                scheduler.release(status["job_id"], reason="run segment ended")


def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m core.supervisor")
    p.add_argument("--job-id", required=True)
    p.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    p.add_argument("--claude-bin", default=None, help="override job-pinned Claude executable")
    p.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN,
                   help="Codex executable used only when a job's worker_provider is codex")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    job_dir = Path(args.jobs_dir) / args.job_id
    state = load_state(job_dir / "state.json")
    cfg = state.get("runtime", {}).get("config", {})
    selected_bin = args.claude_bin or cfg.get("claude_bin") or DEFAULT_CLAUDE_BIN
    account_router = None
    if cfg.get("quota_routing"):
        from core.quota_router import QuotaRouter
        account_router = QuotaRouter(cfg.get("quota_state"))
    runner = build_runner(claude_bin=selected_bin, timeout=args.timeout, codex_bin=args.codex_bin, account_router=account_router)
    try:
        phase = supervise(job_dir, runner=runner)
    except Exception as exc:
        print(f"supervisor error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.job_id}: supervisor stopped at {phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
