"""Deterministic cross-job scheduler for orchestrator supervisors.

The scheduler is intentionally model-free. It admits jobs based on fixed
capacity limits, explicit dependencies and conservative repository conflict
rules. Supervisors may all be started by systemd; only admitted jobs are
allowed to enter the Brain/Worker/Reviewer state machine.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOBS_DIR = ORCH_ROOT / "jobs"
DEFAULT_CONFIG = ORCH_ROOT / "config" / "scheduler.json"
DEFAULT_STATE = ORCH_ROOT / "state" / "scheduler_state.json"
DEFAULT_LOCK = ORCH_ROOT / "state" / "scheduler.lock"
TERMINAL = {"DONE", "ERROR"}
PAUSED = {"NEEDS_HUMAN"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _plain_prefix(pattern):
    if not isinstance(pattern, str):
        return None
    text = pattern.strip().replace("\\", "/").lstrip("./").rstrip("/")
    if not text or any(ch in text for ch in "*?["):
        return None
    return text


def scopes_overlap(a, b):
    """Conservative overlap check. Unknown/glob scope means possible conflict."""
    a = list(a or ["*"])
    b = list(b or ["*"])
    if "*" in a or "*" in b:
        return True
    for x in a:
        px = _plain_prefix(x)
        if px is None:
            return True
        for y in b:
            py = _plain_prefix(y)
            if py is None:
                return True
            if px == py or px.startswith(py + "/") or py.startswith(px + "/"):
                return True
    return False


def infer_resource(job, meta=None):
    meta = meta or {}
    explicit = meta.get("resource_class")
    if explicit in {"read", "dev", "heavy"}:
        return explicit
    return "read" if job.get("mode") == "read" else "dev"


def load_job_descriptor(job_dir):
    job_dir = Path(job_dir)
    job = _read(job_dir / "job.json", {}) or {}
    state = _read(job_dir / "state.json", {}) or {}
    runtime = state.get("runtime") or {}
    cfg = runtime.get("config") or {}
    meta = _read(job_dir / "scheduler.json", {}) or {}
    return {
        "job_id": job.get("job_id") or job_dir.name,
        "repo": job.get("repo"),
        "repo_path": cfg.get("repo_path"),
        "mode": job.get("mode"),
        "allowed_paths": cfg.get("allowed_paths") or ["*"],
        "phase": state.get("phase"),
        "created_at": job.get("created_at") or state.get("created_at") or utc_now(),
        "resource_class": infer_resource(job, meta),
        "priority": int(meta.get("priority", 0) or 0),
        "dependencies": list(meta.get("dependencies") or []),
    }


def write_job_policy(job_dir, *, resource_class=None, priority=0, dependencies=None):
    if resource_class is not None and resource_class not in {"read", "dev", "heavy"}:
        raise ValueError("resource_class must be read, dev or heavy")
    payload = {"schema_version": 1, "resource_class": resource_class,
               "priority": int(priority), "dependencies": list(dependencies or [])}
    _atomic(Path(job_dir) / "scheduler.json", payload)
    return payload


class Scheduler:
    def __init__(self, config_path=DEFAULT_CONFIG, state_path=None, lock_path=None,
                 jobs_dir=DEFAULT_JOBS_DIR, clock=utc_now):
        self.config_path = Path(config_path)
        self.jobs_dir = Path(jobs_dir)
        if state_path is None:
            state_path = DEFAULT_STATE if self.jobs_dir.resolve() == DEFAULT_JOBS_DIR.resolve() else self.jobs_dir / ".scheduler_state.json"
        if lock_path is None:
            lock_path = DEFAULT_LOCK if self.jobs_dir.resolve() == DEFAULT_JOBS_DIR.resolve() else self.jobs_dir / ".scheduler.lock"
        self.state_path = Path(state_path)
        self.lock_path = Path(lock_path)
        self.clock = clock

    def config(self):
        cfg = _read(self.config_path, {}) or {}
        return {
            "max_total_jobs": int(cfg.get("max_total_jobs", 3)),
            "max_dev_jobs": int(cfg.get("max_dev_jobs", 2)),
            "max_heavy_jobs": int(cfg.get("max_heavy_jobs", 1)),
            "poll_seconds": int(cfg.get("poll_seconds", 15)),
        }

    @contextmanager
    def _locked_state(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            state = _read(self.state_path, {}) or {}
            state.setdefault("schema_version", 1)
            state.setdefault("jobs", {})
            self._reap(state)
            yield state
            state["updated_at"] = self.clock()
            _atomic(self.state_path, state)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _reap(self, state):
        for job_id, row in list(state.get("jobs", {}).items()):
            if row.get("status") == "RUNNING" and not _pid_alive(row.get("pid")):
                row.update({"status": "STALE", "released_at": self.clock(), "reason": "supervisor pid exited"})

    def _dependency_reason(self, desc):
        for dep in desc["dependencies"]:
            dep_state = _read(self.jobs_dir / dep / "state.json", {}) or {}
            phase = dep_state.get("phase")
            if phase != "DONE":
                return f"waiting for dependency {dep} (phase {phase or 'missing'})"
        return None

    @staticmethod
    def _conflict(desc, active):
        if desc.get("job_id") == active.get("job_id"):
            return None
        if desc.get("mode") == "read" and active.get("mode") == "read":
            return None
        if desc.get("repo_path") and desc.get("repo_path") == active.get("repo_path"):
            return f"same checkout as {active['job_id']}"
        if desc.get("repo") and desc.get("repo") == active.get("repo"):
            if desc.get("mode") == "write" and active.get("mode") == "write":
                if scopes_overlap(desc.get("allowed_paths"), active.get("allowed_paths")):
                    return f"overlapping write scope in repo with {active['job_id']}"
        return None

    def _capacity_reason(self, desc, running, cfg):
        if len(running) >= cfg["max_total_jobs"]:
            return f"total capacity full ({len(running)}/{cfg['max_total_jobs']})"
        dev_count = sum(1 for r in running if r.get("resource_class") in {"dev", "heavy"})
        heavy_count = sum(1 for r in running if r.get("resource_class") == "heavy")
        if desc["resource_class"] in {"dev", "heavy"} and dev_count >= cfg["max_dev_jobs"]:
            return f"development capacity full ({dev_count}/{cfg['max_dev_jobs']})"
        if desc["resource_class"] == "heavy" and heavy_count >= cfg["max_heavy_jobs"]:
            return f"heavy capacity full ({heavy_count}/{cfg['max_heavy_jobs']})"
        return None

    def try_acquire(self, job_dir, pid=None):
        desc = load_job_descriptor(job_dir)
        pid = int(pid or os.getpid())
        cfg = self.config()
        with self._locked_state() as state:
            jobs = state["jobs"]
            existing = jobs.get(desc["job_id"])
            if existing and existing.get("status") == "RUNNING" and existing.get("pid") == pid:
                return True, None
            dep_reason = self._dependency_reason(desc)
            running = [r for r in jobs.values() if r.get("status") == "RUNNING"]
            reason = dep_reason or self._capacity_reason(desc, running, cfg)
            if reason is None:
                for row in running:
                    reason = self._conflict(desc, row)
                    if reason:
                        break
            # Stable queue ordering: a runnable higher-priority/older waiter
            # gets the slot first. No model can influence this decision.
            if reason is None:
                waiters = [r for r in jobs.values() if r.get("status") == "QUEUED" and r.get("job_id") != desc["job_id"]]
                waiters.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("queued_at") or r.get("created_at") or ""))
                mine_key = (-int(desc.get("priority", 0)), (existing or {}).get("queued_at") or desc.get("created_at") or "")
                for other in waiters:
                    other_key = (-int(other.get("priority", 0)), other.get("queued_at") or other.get("created_at") or "")
                    if other_key >= mine_key:
                        continue
                    if self._dependency_reason(other) or self._capacity_reason(other, running, cfg):
                        continue
                    if any(self._conflict(other, row) for row in running):
                        continue
                    reason = f"higher-priority/older job {other['job_id']} is ahead in queue"
                    break
            base = dict(desc)
            base.update({"pid": pid, "updated_at": self.clock()})
            if reason:
                previous = jobs.get(desc["job_id"], {})
                base.update({"status": "QUEUED", "reason": reason,
                             "queued_at": previous.get("queued_at") or self.clock()})
                jobs[desc["job_id"]] = base
                return False, reason
            base.update({"status": "RUNNING", "reason": None, "started_at": self.clock()})
            jobs[desc["job_id"]] = base
            return True, None

    def release(self, job_id, pid=None, reason="supervisor released slot"):
        pid = int(pid or os.getpid())
        with self._locked_state() as state:
            row = state["jobs"].get(job_id)
            if row and row.get("status") == "RUNNING" and row.get("pid") == pid:
                row.update({"status": "RELEASED", "released_at": self.clock(), "reason": reason})
                return True
        return False

    def wait_for_slot(self, job_dir, *, sleep=time.sleep, log=print, pid=None):
        desc = load_job_descriptor(job_dir)
        poll = max(1, self.config()["poll_seconds"])
        last_reason = None
        while True:
            state = _read(Path(job_dir) / "state.json", {}) or {}
            if state.get("phase") in TERMINAL | PAUSED:
                return False
            ok, reason = self.try_acquire(job_dir, pid=pid)
            if ok:
                log(f"scheduler admitted {desc['job_id']} as {desc['resource_class']}")
                return True
            if reason != last_reason:
                log(f"scheduler queued {desc['job_id']}: {reason}")
                last_reason = reason
            sleep(poll)

    def snapshot(self):
        with self._locked_state() as state:
            rows = list(state["jobs"].values())
        rows.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("queued_at") or r.get("created_at") or ""))
        return {"config": self.config(), "jobs": rows,
                "summary": {"running": sum(r.get("status") == "RUNNING" for r in rows),
                            "queued": sum(r.get("status") == "QUEUED" for r in rows)}}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.scheduler")
    p.add_argument("command", choices=("status",))
    args = p.parse_args(argv)
    if args.command == "status":
        print(json.dumps(Scheduler().snapshot(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
