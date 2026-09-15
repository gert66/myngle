"""Read-only status projection for the mobile Orchestrator Control UI."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ORCH_ROOT / "jobs"
QUOTA_FILE = ORCH_ROOT / "config" / "claude_quota.json"
STORY_FILE = ORCH_ROOT / "config" / "ops_change_stories.json"
ACTIVE_PHASES = {"QUEUED", "PLANNING", "WORKING", "EVIDENCE", "REVIEWING", "REPAIRING"}
ATTENTION_PHASES = {"NEEDS_HUMAN", "ERROR", "FAILED", "RATE_LIMITED", "BLOCKED"}


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _events(path: Path):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def _safe_text(value, limit=700):
    if not value:
        return None
    text = str(value).replace("\n", " ")
    text = re.sub(r"/home/myngle/[^\s,;:]+", "[internal-path]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _safe_timeline(events):
    return [{"from": e.get("from"), "to": e.get("to"), "step_id": e.get("step_id"),
             "ts": e.get("ts"), "reason": _safe_text(e.get("reason"), 500)} for e in events]


def _duration_seconds(created_at, updated_at, now):
    start = _parse_ts(created_at)
    end = _parse_ts(updated_at) or now
    if not start:
        return None
    return max(0, round((end - start).total_seconds()))


def project_run(job_dir: Path, now=None, stories=None):
    now = now or datetime.now(timezone.utc)
    job = _load_json(job_dir / "job.json", {}) or {}
    state = _load_json(job_dir / "state.json", {}) or {}
    events = _events(job_dir / "events.jsonl")
    runtime = state.get("runtime") or {}
    totals = runtime.get("totals") or {}
    phase = state.get("phase") or "UNKNOWN"
    updated_at = state.get("updated_at") or (events[-1].get("ts") if events else job.get("created_at"))
    history = runtime.get("history") or []
    latest_history = history[-1] if history else {}
    batch = runtime.get("batch") or {}
    reviewer = batch.get("reviewer") or {}
    worker = batch.get("worker") or {}
    last_event = events[-1] if events else {}
    story = (stories or {}).get(job.get("job_id") or job_dir.name) or {}

    attention_reason = None
    if phase in ATTENTION_PHASES:
        attention_reason = state.get("human_question") or state.get("last_error") or last_event.get("reason")

    return {
        "run_id": job.get("job_id") or job_dir.name,
        "name": job.get("job_id") or job_dir.name,
        "repo": job.get("repo"),
        "branch": job.get("branch"),
        "mode": job.get("mode"),
        "display_title": story.get("title") or job.get("job_id") or job_dir.name,
        "change_story": story or None,
        "lifecycle": {"committed": bool(latest_history.get("commit") or batch.get("commit")), "pushed": None, "merged": None, "deployed": None},
        "phase": phase,
        "step_id": state.get("step_id"),
        "created_at": job.get("created_at") or state.get("created_at"),
        "updated_at": updated_at,
        "duration_seconds": _duration_seconds(job.get("created_at") or state.get("created_at"), updated_at, now),
        "active": phase in ACTIVE_PHASES,
        "needs_attention": bool(attention_reason) and not bool(story.get("attention_suppressed")),
        "attention_reason": _safe_text(attention_reason, 700),
        "last_transition": {"from": last_event.get("from"), "to": last_event.get("to"), "reason": _safe_text(last_event.get("reason"), 500), "ts": last_event.get("ts")},
        "timeline": _safe_timeline(events),
        "commit": latest_history.get("commit") or batch.get("commit"),
        "review_verdict": reviewer.get("verdict"),
        "worker_status": worker.get("status"),
        "counters": runtime.get("counters") or {},
        "usage": {
            "claude_calls": totals.get("claude_calls", 0),
            "cost_usd": totals.get("cost_usd", 0.0),
            "input_tokens": totals.get("input_tokens", 0),
            "output_tokens": totals.get("output_tokens", 0),
            "thinking_tokens": totals.get("thinking_tokens", 0),
            "cache_read_input_tokens": totals.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": totals.get("cache_creation_input_tokens", 0),
            "by_role": totals.get("by_role") or {},
        },
    }


def collect_runs(jobs_dir=JOBS_DIR, now=None, story_file=STORY_FILE):
    jobs_dir = Path(jobs_dir)
    if not jobs_dir.is_dir():
        return []
    stories = _load_json(Path(story_file), {}) or {}
    runs = []
    for job_dir in jobs_dir.iterdir():
        if job_dir.is_dir() and (job_dir / "state.json").is_file():
            runs.append(project_run(job_dir, now=now, stories=stories))
    return sorted(runs, key=lambda x: x.get("created_at") or "", reverse=True)


def collect_capacity(quota_file=QUOTA_FILE):
    data = _load_json(Path(quota_file), {}) or {}
    accounts = []
    for name, account in (data.get("accounts") or {}).items():
        all_bucket = (account.get("buckets") or {}).get("all", {})
        fable_bucket = (account.get("buckets") or {}).get("fable", {})
        accounts.append({
            "account": name,
            "plan": account.get("plan"),
            "plan_factor": account.get("plan_factor"),
            "session_remaining": account.get("session_remaining"),
            "session_reset_at": account.get("session_reset_at"),
            "weekly_remaining": all_bucket.get("remaining", account.get("weekly_remaining")),
            "weekly_reset_at": all_bucket.get("reset_at", account.get("weekly_reset_at")),
            "fable_remaining": fable_bucket.get("remaining"),
            "fable_reset_at": fable_bucket.get("reset_at"),
            "last_used_at": account.get("last_used_at"),
        })
    return {
        "accounts": accounts,
        "source": data.get("source"),
        "updated_at": data.get("updated_at"),
        "timezone": (data.get("clock") or {}).get("timezone"),
        "session_period_hours": (data.get("policy") or {}).get("session_period_hours"),
    }


def build_snapshot(jobs_dir=JOBS_DIR, quota_file=QUOTA_FILE, now=None):
    now = now or datetime.now(timezone.utc)
    runs = collect_runs(jobs_dir=jobs_dir, now=now)
    live = [r for r in runs if r["active"]]
    attention = [r for r in runs if r["needs_attention"]]
    history = [r for r in runs if not r["active"]]
    done = [r for r in runs if r["phase"] == "DONE"]
    return {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": {"running": len(live), "attention": len(attention), "completed": len(done), "total": len(runs)},
        "live": live,
        "attention": attention,
        "history": history,
        "capacity": collect_capacity(quota_file=quota_file),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Read-only Orchestrator Control status projection")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    snapshot = build_snapshot()
    if args.compact:
        print(json.dumps(snapshot, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
