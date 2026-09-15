"""Boot recovery and user-systemd helpers for orchestrator jobs."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from core.machine import ORCH_ROOT, RUNNABLE_PHASES
from core.state import StateError, load_state

DEFAULT_JOBS_DIR = ORCH_ROOT / "jobs"
RECOVERABLE_PHASES = frozenset(RUNNABLE_PHASES) | {"WAITING"}


class RecoverError(RuntimeError):
    pass


def user_systemd_env(base=None):
    env = dict(base or os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env


def start_job_service(job_id, run=subprocess.run):
    unit = f"orchestrator@{job_id}.service"
    result = run(
        ["systemctl", "--user", "start", unit],
        text=True, capture_output=True, check=False, env=user_systemd_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown systemctl error").strip()
        raise RecoverError(f"cannot start {unit}: {detail}")
    return unit


def discover_recoverable(jobs_dir=DEFAULT_JOBS_DIR):
    jobs_dir = Path(jobs_dir)
    found, warnings = [], []
    if not jobs_dir.exists():
        return found, warnings
    for job_dir in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        state_path = job_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = load_state(state_path)
        except (StateError, OSError) as exc:
            warnings.append(f"{job_dir.name}: cannot read state: {exc}")
            continue
        if state.get("job_id") != job_dir.name:
            warnings.append(f"{job_dir.name}: state job_id mismatch ({state.get('job_id')!r})")
            continue
        if state["phase"] in RECOVERABLE_PHASES:
            found.append(job_dir.name)
    return found, warnings


def recover_jobs(jobs_dir=DEFAULT_JOBS_DIR, starter=start_job_service):
    jobs, warnings = discover_recoverable(jobs_dir)
    started, failures = [], []
    for job_id in jobs:
        try:
            starter(job_id)
            started.append(job_id)
        except Exception as exc:
            failures.append(f"{job_id}: {exc}")
    return {"started": started, "warnings": warnings, "failures": failures}


def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m core.recover")
    p.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.dry_run:
        jobs, warnings = discover_recoverable(args.jobs_dir)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        for job_id in jobs:
            print(f"would start orchestrator@{job_id}.service")
        return 0
    result = recover_jobs(args.jobs_dir)
    for w in result["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    for job_id in result["started"]:
        print(f"started orchestrator@{job_id}.service")
    for failure in result["failures"]:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
