"""Durable intake/reconciliation for Sales Cockpit caller feedback."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request

from core.slack_approval import ApprovalProposal, build_blocks, load_record, save_proposal

ORCH_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = Path(os.getenv("FEEDBACK_CASES_DIR", "/home/myngle/orchestrator/feedback_cases"))
JOBS_DIR = ORCH_ROOT / "jobs"
COMPANY_REPO = "gert66/myngle-company-hub"
COMPANY_REPO_PATH = Path(os.getenv("FEEDBACK_REPO_PATH", "/home/myngle/autopilot-company-hub-sync"))
COMPANY_BRANCH = "work"
LOVABLE_PROJECT = "a4691ca7-4294-496a-af73-cdba24a5ac0f"
DEFAULT_API_URL = "https://myngle.whofirst.nl/api/feedback/autopilot"
TOKEN_FILE = ORCH_ROOT / "secrets" / "feedback_autopilot_token"
URL_FILE = ORCH_ROOT / "config" / "feedback_autopilot_url.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_feedback(text: str) -> str:
    t = " ".join((text or "").lower().split())
    if not t:
        return "unknown"
    thanks = any(x in t for x in ("thank you", "thanks", "really helping", "great job", "appreciate"))
    asks = any(x in t for x in ("?", "possible", "can you", "could you", "is it", "please", "invalid", "error", "not work", "doesn't", "doesnt", "cannot", "can't", "problem"))
    if thanks and not asks:
        return "positive"
    if any(x in t for x in ("invalid number", "wrong number", "invalid email", "wrong email", "bad contact")):
        return "data_quality"
    if any(x in t for x in ("is it possible", "would like", "can we", "could we", "feature", "automatically assign", "block countries")):
        return "feature_request"
    return "bug"


def _safe_id(feedback_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", feedback_id):
        raise ValueError("invalid feedback id")
    return feedback_id


def case_path(feedback_id: str) -> Path:
    return CASES_DIR / f"{_safe_id(feedback_id)}.json"


def save_case(case: dict[str, Any]) -> Path:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    target = case_path(str(case["feedback_id"]))
    case["updated_at"] = utc_now()
    fd, tmp = tempfile.mkstemp(prefix=target.name, dir=str(CASES_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(case, h, indent=2, ensure_ascii=False)
            h.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return target


def load_case(feedback_id: str) -> dict[str, Any]:
    return json.loads(case_path(feedback_id).read_text(encoding="utf-8"))


def ingest_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    fid = _safe_id(str(row["id"]))
    path = case_path(fid)
    if path.exists():
        return load_case(fid), False
    kind = classify_feedback(str(row.get("comment") or ""))
    status = "informational" if kind == "positive" else ("queued_research" if kind in {"data_quality", "feature_request"} else "queued")
    case = {
        "feedback_id": fid,
        "source_updated_at": row.get("updated_at"),
        "received_at": row.get("created_at") or utc_now(),
        "reporter_email": row.get("created_by_email"),
        "reporter_name": row.get("created_by_name") or row.get("reporter_name"),
        "company": row.get("company_name"),
        "country": row.get("country"),
        "source": row.get("source") or "Sales Cockpit feedback",
        "source_url": row.get("source_url"),
        "comment": row.get("comment") or "",
        "attachment_path": row.get("attachment_path"),
        "kind": kind,
        "risk": "GREEN" if kind == "bug" else "AMBER",
        "status": status,
        "created_at": utc_now(),
        "job_id": None,
        "proposal_id": None,
    }
    save_case(case)
    return case, True


def goal_for(case: dict[str, Any], *, research_only: bool = False) -> str:
    action = "Investigate one Sales Cockpit feedback item read-only and determine the safest concrete next action." if research_only else "Investigate and fix one Sales Cockpit feedback item on branch work only."
    constraint = "Do not edit files, commit, deploy, or change external/production data. Return concrete findings, likely implementation/data action, evidence, and any product decision needed." if research_only else "Work autonomously as far as evidence supports. Reproduce or trace the issue, make the smallest safe code change on work, add/update regression tests, run relevant tests and production build, and commit exactly the fix. Do not merge/push to main and do not deploy. Do not change production data. If this requires a product decision, protected external action, credentials, or the report is too ambiguous to fix safely, stop with NEEDS_HUMAN and state the concrete question. Avoid unrelated refactors."
    return f"""{action}
Feedback ID: {case['feedback_id']}
Reporter: {case.get('reporter_email') or 'unknown'}
Company: {case.get('company') or 'none'}
Country: {case.get('country') or 'unknown'}
Feedback: {case.get('comment') or ''}

{constraint}"""


def submit_case(case: dict[str, Any], *, run=subprocess.run) -> dict[str, Any]:
    if case.get("status") not in {"queued", "queued_research"}: return case
    research_only = case.get("status") == "queued_research"
    fid = str(case["feedback_id"])
    job_id = f"feedback-{fid[:12].lower()}"
    existing_job = JOBS_DIR / job_id
    if existing_job.exists():
        case["job_id"] = job_id
        case["status"] = "researching" if research_only else "investigating"
        case["job_mode"] = "read" if research_only else "write"
        case.setdefault("job_submitted_at", utc_now())
        save_case(case)
        return case

    args = [
        "/usr/bin/python3", "-m", "core.cli", "submit",
        "--job-id", job_id,
        "--repo", COMPANY_REPO,
        "--repo-path", str(COMPANY_REPO_PATH),
        "--branch", COMPANY_BRANCH,
        "--mode", "read" if research_only else "write",
        "--goal", goal_for(case, research_only=research_only),
        *([] if research_only else ["--test", "npm test", "--test", "npm run build"]),
        "--worker-provider", "auto",
        "--worker-complexity", "normal",
        "--resource-class", "dev",
        "--priority", "40",
        "--forbid", ".env*",
        "--forbid", "node_modules/**",
    ]
    proc = run(args, cwd=ORCH_ROOT, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[-1600:])
    case["job_id"] = job_id
    case["status"] = "researching" if research_only else "investigating"
    case["job_mode"] = "read" if research_only else "write"
    case["job_submitted_at"] = utc_now()
    save_case(case)
    return case


def _job_state(job_id: str) -> dict[str, Any] | None:
    path = JOBS_DIR / job_id / "state.json"
    if not path.exists(): return None
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_main_sha(repo_path: Path = COMPANY_REPO_PATH) -> str:
    subprocess.run(["git", "fetch", "origin", "main", "work"], cwd=repo_path, check=True, capture_output=True, text=True)
    return subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo_path, check=True, capture_output=True, text=True).stdout.strip()


def _latest_closed(state: dict[str, Any]) -> dict[str, Any]:
    history = (state.get("runtime") or {}).get("history") or []
    return history[-1] if history else {}


def build_proposal(case: dict[str, Any], state: dict[str, Any], *, expected_main_sha: str) -> ApprovalProposal:
    closed = _latest_closed(state)
    commit = str(closed.get("commit") or "")
    paths = list(closed.get("paths") or [])
    verdict = str(closed.get("verdict") or "")
    if state.get("phase") != "DONE" or verdict != "PASS" or not re.fullmatch(r"[0-9a-f]{40}", commit) or not paths:
        raise ValueError("job is not a committed PASS suitable for approval")
    detail = " ".join(str(closed.get("detail") or "").split())
    short_detail = detail[:650] + ("…" if len(detail) > 650 else "")
    reporter = (case.get("reporter_email") or "Reporter").split("@")[0]
    deployment = {
        "repo": COMPANY_REPO,
        "source_branch": COMPANY_BRANCH,
        "commit_sha": commit,
        "expected_main_sha": expected_main_sha,
        "lovable_project_id": LOVABLE_PROJECT,
        "test_commands": ["npm test", "npm run build"],
        "verification_commands": ["curl -fsS --max-time 30 https://myngle.whofirst.nl/ >/dev/null"],
    }
    return ApprovalProposal(
        proposal_id=f"FB-{case['feedback_id']}",
        reporter=reporter,
        company=case.get("company") or "General feedback",
        reported=case.get("comment") or "",
        diagnosis=short_detail or "The orchestrator reproduced/traced the report and prepared a verified fix.",
        prepared_fix=f"Prepared tested commit {commit[:8]} changing: {', '.join(paths[:6])}",
        checks=["Orchestrator verdict PASS", "npm test", "npm run build"],
        resolution_note="Thanks for reporting this. We found the issue and prepared a tested fix. It will be marked resolved after the production check passes.",
        reporter_reply=f"Hi {reporter}, thanks for flagging this. I found the issue and the tested fix is ready for approval.",
        changed_files=paths,
        risk="GREEN",
        version=1,
        deployment=deployment,
    )


def reconcile_case(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("status") == "resolved":
        return case
    if case.get("proposal_id"):
        try:
            if sync_verified_resolution(case):
                return case
        except Exception as exc:
            case["status"] = "resolution_sync_error"
            case["resolution_sync_error"] = str(exc)[:1200]
            save_case(case)
            return case
    job_id = case.get("job_id")
    if not job_id: return case
    state = _job_state(str(job_id))
    if not state: return case
    phase = str(state.get("phase") or "UNKNOWN")
    case["job_phase"] = phase
    if phase in {"QUEUED", "BRAIN", "WORKER", "TESTING", "REVIEW", "REPAIR", "WAITING"}:
        case["status"] = "researching" if case.get("job_mode") == "read" else "investigating"
    elif phase == "NEEDS_HUMAN":
        case["status"] = "needs_review"
        case["question"] = state.get("human_question")
        case["recommendation"] = "I need this one decision before I can continue safely."
    elif phase == "ERROR":
        case["status"] = "error"
        case["error"] = state.get("last_error")
        case["question"] = "The automated investigation failed. Should I retry this case?"
        case["recommendation"] = "I recommend retrying once before changing the product manually."
    elif phase == "DONE" and case.get("job_mode") == "read":
        closed = _latest_closed(state)
        detail = " ".join(str(closed.get("detail") or "").split())[:1800]
        case["status"] = "needs_review"
        case["investigation_summary"] = detail
        case["actions_taken"] = detail
        case.setdefault("ai_interpretation", f"I treated this as a {case.get('kind') or 'feedback'} case and investigated it without changing production.")
        case["recommendation"] = "I completed the investigation. I need your answer to the question below before taking a protected product or data action."
        case["question"] = "Do you want me to proceed with the product/data action described above?"
    elif phase == "DONE" and not case.get("proposal_id"):
        try:
            proposal = build_proposal(case, state, expected_main_sha=_expected_main_sha())
            save_proposal(proposal)
            case["proposal_id"] = proposal.proposal_id
            case["proposal_fingerprint"] = proposal.fingerprint
            case["status"] = "awaiting_approval"
            case["ai_interpretation"] = proposal.diagnosis
            case["actions_taken"] = proposal.prepared_fix
            case["recommendation"] = "I recommend approving the prepared fix, verifying production, then sending the reply below and closing this feedback."
            case["proposed_reply"] = proposal.reporter_reply or proposal.resolution_note
        except ValueError as exc:
            case["status"] = "needs_review"
            case["question"] = str(exc)
    save_case(case)
    return case



def sync_verified_resolution(case: dict[str, Any], *, opener=request.urlopen) -> bool:
    proposal_id = case.get("proposal_id")
    if not proposal_id:
        return False
    try:
        record = load_record(str(proposal_id))
    except FileNotFoundError:
        return False
    deployment = record.get("deployment_result") or {}
    if deployment.get("status") != "verified":
        return False
    proposal = record.get("proposal") or {}
    note = str(proposal.get("resolution_note") or "").strip()
    if not note:
        raise RuntimeError("verified proposal has no resolution note")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    url = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else DEFAULT_API_URL
    payload = json.dumps({
        "id": case["feedback_id"],
        "resolution_note": note,
        "expected_updated_at": case.get("source_updated_at"),
    }).encode("utf-8")
    req = request.Request(url, data=payload, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Autopilot/1.0",
        "x-feedback-autopilot-token": token,
    })
    with opener(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("status") != "resolved":
        raise RuntimeError("feedback resolution endpoint did not confirm resolved")
    row = result.get("row") or {}
    case["status"] = "resolved"
    case["resolution_note"] = note
    case["resolved_at"] = row.get("resolved_at") or utc_now()
    case.pop("resolution_sync_error", None)
    save_case(case)
    return True

def list_cases() -> list[dict[str, Any]]:
    if not CASES_DIR.exists(): return []
    rows=[]
    for path in sorted(CASES_DIR.glob("*.json")):
        try: rows.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception: continue
    return rows
