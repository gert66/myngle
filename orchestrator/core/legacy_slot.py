"""Safety/scheduler bridge for legacy run_claude_job.sh jobs.

Keeps old tmux/script entry points inside the same deterministic capacity and
branch-freshness control plane as phase-3 supervisors.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from core import gitsafe
from core.repo_policy import DEFAULT_POLICY_PATH, load_repo_policy
from core.scheduler import DEFAULT_JOBS_DIR, Scheduler, write_job_policy

ORCH_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DIR = ORCH_ROOT / "state" / "legacy_jobs"


def _atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def repo_name_for_path(repo_path):
    actual = gitsafe.origin_url(repo_path)
    if not actual:
        raise ValueError("remote origin is missing")
    data = json.loads(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    matches = []
    for name, item in (data.get("repos") or {}).items():
        expected = item.get("expected_origin")
        if expected and gitsafe.normalize_remote_url(actual) == gitsafe.normalize_remote_url(expected):
            matches.append(name)
    if len(matches) != 1:
        raise ValueError(f"origin {actual!r} does not match exactly one configured repo policy")
    return matches[0]


def descriptor(job_id, mode, repo_path, resource_class=None):
    repo_path = str(Path(repo_path).resolve())
    repo_name = repo_name_for_path(repo_path)
    branch = gitsafe.current_branch(repo_path)
    if not branch:
        raise ValueError("HEAD is detached or unborn")
    job_dir = LEGACY_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _atomic(job_dir / "job.json", {"job_id": job_id, "repo": repo_name, "mode": mode,
                                   "branch": branch, "created_at": __import__('core.state', fromlist=['utc_now']).utc_now()})
    _atomic(job_dir / "state.json", {"phase": "QUEUED", "runtime": {"config": {
        "repo_path": repo_path, "allowed_paths": ["*"],
    }}})
    write_job_policy(job_dir, resource_class=resource_class, priority=0, dependencies=[])
    return job_dir, repo_name, branch


def acquire(job_id, mode, repo_path, pid, resource_class=None):
    job_dir, repo_name, branch = descriptor(job_id, mode, repo_path, resource_class)
    sched = Scheduler(jobs_dir=DEFAULT_JOBS_DIR)
    if not sched.wait_for_slot(job_dir, pid=pid):
        return 4
    if mode == "write":
        policy = load_repo_policy(repo_name)
        gate = gitsafe.branch_freshness_gate(
            repo_path, repo_name, policy["expected_origin"], branch,
            policy["canonical_base_branch"], mode="write", fetch_remote=True,
        )
        if not gate.ok:
            detail = "; ".join(f"{c.name}: {c.detail}" for c in gate.failures)
            print(f"[SAFETY BLOCK] Branch freshness gate: {detail}")
            print("[SAFETY ADVICE] Use a fresh branch/worktree from the current canonical base; no automatic merge/rebase was attempted.")
            sched.release(job_id, pid=pid, reason="legacy branch freshness blocked")
            st = json.loads((job_dir / "state.json").read_text())
            st["phase"] = "NEEDS_HUMAN"
            st["freshness"] = gate.to_dict()
            _atomic(job_dir / "state.json", st)
            return 6
    st = json.loads((job_dir / "state.json").read_text())
    st["phase"] = "WORKING"
    _atomic(job_dir / "state.json", st)
    return 0


def release(job_id, pid, result="DONE"):
    sched = Scheduler(jobs_dir=DEFAULT_JOBS_DIR)
    sched.release(job_id, pid=pid, reason=f"legacy job ended: {result}")
    path = LEGACY_DIR / job_id / "state.json"
    if path.is_file():
        st = json.loads(path.read_text())
        st["phase"] = result if result in {"DONE", "ERROR", "NEEDS_HUMAN"} else "DONE"
        _atomic(path, st)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.legacy_slot")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("--job-id", required=True)
    a.add_argument("--mode", choices=("read", "write"), required=True)
    a.add_argument("--repo-path", required=True)
    a.add_argument("--pid", type=int, required=True)
    a.add_argument("--resource-class", choices=("read", "dev", "heavy"))
    r = sub.add_parser("release")
    r.add_argument("--job-id", required=True)
    r.add_argument("--pid", type=int, required=True)
    r.add_argument("--result", default="DONE")
    args = p.parse_args(argv)
    try:
        if args.cmd == "acquire":
            rc = args.resource_class or ("read" if args.mode == "read" else "dev")
            return acquire(args.job_id, args.mode, args.repo_path, args.pid, rc)
        return release(args.job_id, args.pid, args.result)
    except Exception as exc:
        print(f"[SAFETY BLOCK] legacy scheduler bridge failed closed: {exc}")
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
