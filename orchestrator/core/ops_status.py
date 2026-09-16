"""Read-only status projection for the mobile Orchestrator Control UI."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from core.ops_metrics import collect_usage_analytics, collect_usage_meta, collect_usage_trends, collect_vm_health, collect_vm_trends

ORCH_ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ORCH_ROOT / "jobs"
QUOTA_FILE = ORCH_ROOT / "config" / "claude_quota.json"
STORY_FILE = ORCH_ROOT / "config" / "ops_change_stories.json"
INTAKE_DIR = ORCH_ROOT / "intake"
APPROVALS_DIR = ORCH_ROOT / "approvals"
FEEDBACK_CASES_DIR = ORCH_ROOT / "feedback_cases"
GEMINI_LEDGER_FILE = ORCH_ROOT / "logs" / "gemini_usage.jsonl"
APPROVAL_POLLER_HEARTBEAT = ORCH_ROOT / "state" / "approval-command-poller.heartbeat"
HEARTBEAT_STALE_SECONDS = 75
APPROVAL_POLLER_STALE_SECONDS = 150
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


def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _heartbeat(job_dir: Path, now, phase_active):
    data = _load_json(job_dir / "heartbeat.json", {}) or {}
    ts = _parse_ts(data.get("ts"))
    age = max(0, round((now - ts).total_seconds())) if ts else None
    pid = data.get("pid")
    alive = _pid_alive(pid) if pid is not None else None
    stale = bool(phase_active and (ts is None or age > HEARTBEAT_STALE_SECONDS or alive is False))
    return {
        "heartbeat_at": data.get("ts"),
        "heartbeat_age_seconds": age,
        "heartbeat_pid": pid,
        "process_alive": alive,
        "stale": stale,
    }




def collect_gemini_usage(path=GEMINI_LEDGER_FILE, limit=12):
    rows = _events(Path(path))
    rows.sort(key=lambda x: x.get("ts") or "", reverse=True)
    totals = {
        "records": len(rows),
        "completed": sum(1 for r in rows if r.get("outcome") == "completed"),
        "escalated": sum(1 for r in rows if str(r.get("outcome") or "").startswith("escalated_to_")),
        "failed": sum(1 for r in rows if r.get("outcome") == "gemini_failed"),
        "flex_attempts": sum(1 for r in rows if "flex" in (r.get("tiers_attempted") or []) or r.get("service_tier") == "flex"),
        "standard_attempts": sum(1 for r in rows if "standard" in (r.get("tiers_attempted") or []) or r.get("service_tier") == "standard"),
        "fallbacks": sum(len(r.get("fallback_events") or []) for r in rows),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "thinking_tokens": sum(int(r.get("thinking_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
    }
    recent = [{
        "ts": r.get("ts"), "job_id": r.get("job_id"), "step_id": r.get("step_id"),
        "role": r.get("role"), "outcome": r.get("outcome"), "validated": r.get("validated"),
        "model": r.get("model"), "service_tier": r.get("service_tier"),
        "tiers_attempted": r.get("tiers_attempted") or [], "fallback_events": r.get("fallback_events") or [],
        "input_tokens": r.get("input_tokens", 0), "output_tokens": r.get("output_tokens", 0),
        "thinking_tokens": r.get("thinking_tokens", 0), "total_tokens": r.get("total_tokens", 0),
    } for r in rows[:max(0, int(limit))]]
    return {"totals": totals, "recent": recent}

def collect_control_health(now=None, approval_heartbeat=APPROVAL_POLLER_HEARTBEAT):
    now = now or datetime.now(timezone.utc)
    path = Path(approval_heartbeat)
    try:
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = max(0, round((now - ts).total_seconds()))
        return {
            "approval_command_poller": {
                "heartbeat_at": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "heartbeat_age_seconds": age,
                "healthy": age <= APPROVAL_POLLER_STALE_SECONDS,
            }
        }
    except OSError:
        return {
            "approval_command_poller": {
                "heartbeat_at": None,
                "heartbeat_age_seconds": None,
                "healthy": False,
            }
        }

def collect_approvals(approvals_dir=APPROVALS_DIR):
    approvals_dir = Path(approvals_dir)
    if not approvals_dir.is_dir():
        return []
    rows = []
    for path in approvals_dir.glob("*.json"):
        item = _load_json(path, {}) or {}
        proposal = item.get("proposal") or {}
        status = item.get("status") or "unknown"
        if status not in {"pending", "discussion_requested", "approved", "rejected"}:
            continue
        deployment = item.get("deployment_result") or {}
        rows.append({
            "proposal_id": proposal.get("proposal_id") or path.stem,
            "fingerprint": item.get("fingerprint"),
            "status": status,
            "deployment_status": deployment.get("status"),
            "deployment_commit_sha": deployment.get("deployment_commit_sha"),
            "deployment_error": _safe_text(deployment.get("error"), 700),
            "reporter": _safe_text(proposal.get("reporter"), 120),
            "company": _safe_text(proposal.get("company"), 180),
            "reported": _safe_text(proposal.get("reported"), 700),
            "diagnosis": _safe_text(proposal.get("diagnosis"), 700),
            "prepared_fix": _safe_text(proposal.get("prepared_fix"), 700),
            "resolution_note": _safe_text(proposal.get("resolution_note"), 700),
            "reporter_reply": _safe_text(proposal.get("reporter_reply"), 700),
            "risk": proposal.get("risk"),
            "version": proposal.get("version"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "note": _safe_text(item.get("note"), 700),
        })
    return sorted(rows, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)


def collect_feedback_cases(cases_dir=FEEDBACK_CASES_DIR):
    cases_dir = Path(cases_dir)
    if not cases_dir.is_dir():
        return []
    rows = []
    for path in cases_dir.glob("*.json"):
        item = _load_json(path, {}) or {}
        rows.append({
            "feedback_id": item.get("feedback_id") or path.stem,
            "status": item.get("status") or "unknown",
            "kind": item.get("kind"),
            "risk": item.get("risk"),
            "reporter_email": _safe_text(item.get("reporter_email"), 160),
            "company": _safe_text(item.get("company"), 180),
            "country": _safe_text(item.get("country"), 80),
            "comment": _safe_text(item.get("comment"), 700),
            "job_id": item.get("job_id"),
            "job_phase": item.get("job_phase"),
            "proposal_id": item.get("proposal_id"),
            "question": _safe_text(item.get("question"), 900),
            "investigation_summary": _safe_text(item.get("investigation_summary"), 1200),
            "received_at": item.get("received_at"),
            "updated_at": item.get("updated_at"),
        })
    return sorted(rows, key=lambda x: x.get("updated_at") or x.get("received_at") or "", reverse=True)


def collect_handoffs(intake_dir=INTAKE_DIR, jobs_dir=JOBS_DIR, now=None):
    now = now or datetime.now(timezone.utc)
    intake_dir = Path(intake_dir)
    jobs_dir = Path(jobs_dir)
    if not intake_dir.is_dir():
        return []
    rows = []
    for path in intake_dir.glob("*.json"):
        item = _load_json(path, {}) or {}
        if item.get("status") != "RECEIVED":
            continue
        received = _parse_ts(item.get("received_at"))
        age = max(0, round((now - received).total_seconds())) if received else None
        rows.append({
            "receipt_id": item.get("receipt_id") or path.stem,
            "title": _safe_text(item.get("title"), 180),
            "request": _safe_text(item.get("request"), 700),
            "source": item.get("source"),
            "status": "HANDOFF",
            "received_at": item.get("received_at"),
            "age_seconds": age,
            "job_id": item.get("job_id"),
        })
    return sorted(rows, key=lambda x: x.get("received_at") or "", reverse=True)


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

    phase_active = phase in ACTIVE_PHASES
    hb = _heartbeat(job_dir, now, phase_active)
    attention_reason = None
    if phase in ATTENTION_PHASES:
        attention_reason = state.get("human_question") or state.get("last_error") or last_event.get("reason")
    elif hb["stale"]:
        if hb["heartbeat_age_seconds"] is None:
            attention_reason = "Active phase has no supervisor heartbeat."
        elif hb["process_alive"] is False:
            attention_reason = f"Supervisor heartbeat process is no longer alive; last heartbeat {hb['heartbeat_age_seconds']}s ago."
        else:
            attention_reason = f"Supervisor heartbeat is stale; last heartbeat {hb['heartbeat_age_seconds']}s ago."

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
        "phase_active": phase_active,
        "active": bool(phase_active and not hb["stale"]),
        "heartbeat_at": hb["heartbeat_at"],
        "heartbeat_age_seconds": hb["heartbeat_age_seconds"],
        "heartbeat_pid": hb["heartbeat_pid"],
        "process_alive": hb["process_alive"],
        "stale": hb["stale"],
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


def build_snapshot(jobs_dir=JOBS_DIR, quota_file=QUOTA_FILE, now=None, intake_dir=INTAKE_DIR, approvals_dir=APPROVALS_DIR):
    now = now or datetime.now(timezone.utc)
    runs = collect_runs(jobs_dir=jobs_dir, now=now)
    live = [r for r in runs if r.get("phase_active")]
    attention = [r for r in runs if r["needs_attention"]]
    history = [r for r in runs if not r.get("phase_active")]
    done = [r for r in runs if r["phase"] == "DONE"]
    done.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    recent = done[:5]
    handoffs = collect_handoffs(intake_dir=intake_dir, jobs_dir=jobs_dir, now=now)
    approvals = collect_approvals(approvals_dir=approvals_dir)
    feedback_cases = collect_feedback_cases()
    running = sum(1 for r in live if r.get("active"))
    stale = sum(1 for r in live if r.get("stale"))
    return {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": {
            "running": running,
            "handoff": len(handoffs),
            "stale": stale,
            "attention": len(attention),
            "completed": len(done),
            "approvals_pending": sum(1 for a in approvals if a.get("status") in {"pending", "discussion_requested"}),
            "feedback_open": sum(1 for f in feedback_cases if f.get("status") not in {"informational", "resolved"}),
            "total": len(runs),
        },
        "handoffs": handoffs,
        "approvals": approvals,
        "feedback_cases": feedback_cases,
        "live": live,
        "attention": attention,
        "recent": recent,
        "history": history,
        "capacity": collect_capacity(quota_file=quota_file),
        "usage_analytics": collect_usage_analytics(jobs_dir, quota_file, now=now),
        "usage_meta": collect_usage_meta(jobs_dir),
        "usage_trends": collect_usage_trends(jobs_dir, now=now),
        "vm_health": collect_vm_health(now=now),
        "vm_trends": collect_vm_trends(now=now),
        "control_health": collect_control_health(now=now),
        "gemini_usage": collect_gemini_usage(),
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
