#!/usr/bin/env python3
"""Process human feedback decisions queued by the Control Center."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib import error, request

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from core.approval_deploy import DeploymentError, deploy_approved  # noqa: E402
from core.feedback_autopilot import (  # noqa: E402
    COMPANY_BRANCH,
    COMPANY_REPO,
    COMPANY_REPO_PATH,
    DEFAULT_API_URL,
    TOKEN_FILE as FEEDBACK_TOKEN_FILE,
    URL_FILE as FEEDBACK_URL_FILE,
    load_case,
    save_case,
    utc_now,
)
from core.slack_approval import load_record, record_deployment, update_status  # noqa: E402
TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
JOBS_DIR = ORCH_ROOT / "jobs"


def commands_url(path: Path = PUSH_URL_FILE) -> str:
    base = path.read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[: -len(suffix)] + "/api/public/feedback-commands"


def _call(url: str, token: str, *, method="GET", payload=None, timeout=20):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Commands/1.0",
        "x-orchestrator-token": token,
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_commands(*, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token).get("commands", [])
def ack(command_id: str, status: str, result_note: str = "", *, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token, method="POST", payload={
        "command_id": command_id,
        "status": status,
        "result_note": result_note or None,
    })


def _safe_command_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value or ""):
        raise ValueError("invalid feedback command id")
    return value


def _post_resolution(case: dict, note: str) -> dict:
    token = FEEDBACK_TOKEN_FILE.read_text(encoding="utf-8").strip()
    url = FEEDBACK_URL_FILE.read_text(encoding="utf-8").strip() if FEEDBACK_URL_FILE.exists() else DEFAULT_API_URL
    payload = json.dumps({
        "id": case["feedback_id"],
        "resolution_note": note,
        "expected_updated_at": case.get("source_updated_at"),
    }).encode("utf-8")
    req = request.Request(url, data=payload, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Commands/1.0",
        "x-feedback-autopilot-token": token,
    })
    with request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("status") != "resolved":
        raise RuntimeError("feedback resolution endpoint did not confirm resolved")
    return result


def _ensure_verified_deployment(case: dict, command: dict) -> str:
    proposal_id = str(case.get("proposal_id") or "")
    if not proposal_id:
        return "no protected deployment required"

    record = load_record(proposal_id)
    deployment = record.get("deployment_result") or {}
    if deployment.get("status") == "verified":
        return "existing deployment already verified"

    fingerprint = str(case.get("proposal_fingerprint") or record.get("fingerprint") or "")
    if not fingerprint:
        raise ValueError("feedback proposal fingerprint missing")
    actor = f"feedback-control:{command.get('created_by') or 'unknown'}"
    if record.get("status") != "approved":
        record = update_status(
            proposal_id,
            fingerprint,
            "approved",
            actor,
            note="Approved via Feedback Send & close",
        )

    if not (record.get("proposal") or {}).get("deployment"):
        return "proposal has no deployment step"
    record_deployment(proposal_id, fingerprint, status="deploying")
    deployment = deploy_approved(record)
    if deployment.get("status") != "not_configured":
        record_deployment(proposal_id, fingerprint, **deployment)
    if deployment.get("status") not in {"verified", "not_configured"}:
        raise DeploymentError(f"unexpected deployment status: {deployment.get('status')}")
    return f"deployment -> {deployment.get('status')}"


def _follow_up_goal(case: dict, body: str) -> str:
    previous = case.get("actions_taken") or case.get("investigation_summary") or "No prior summary available."
    return f"""Continue one Sales Cockpit feedback case after human review.

Feedback ID: {case['feedback_id']}
Reporter: {case.get('reporter_email') or 'unknown'}
Company: {case.get('company') or 'none'}
Original feedback: {case.get('comment') or ''}
Previous work: {previous}
Human follow-up/change request: {body}

Work autonomously as far as evidence supports. On branch work only, reproduce or trace the remaining issue, make the smallest safe code change if appropriate, add/update regression tests, run relevant tests and the production build, and commit exactly the fix. Do not merge or push to main, deploy, or change production/external data. If a protected action or product decision is still required, stop with NEEDS_HUMAN and ask exactly one concrete question. In the final detail, state what you changed or learned and give a concise suggested reply to the reporter."""


def _submit_follow_up(case: dict, body: str, *, run=subprocess.run) -> str:
    iteration = int(case.get("iteration") or 0) + 1
    fid = str(case["feedback_id"])
    jid = f"feedback-{fid[:8].lower()}-r{iteration}"
    while (JOBS_DIR / jid).exists():
        iteration += 1
        jid = f"feedback-{fid[:8].lower()}-r{iteration}"
    args = [
        "/usr/bin/python3", "-m", "core.cli", "submit",
        "--job-id", jid,
        "--repo", COMPANY_REPO,
        "--repo-path", str(COMPANY_REPO_PATH),
        "--branch", COMPANY_BRANCH,
        "--mode", "write",
        "--goal", _follow_up_goal(case, body),
        "--test", "npm test",
        "--test", "npm run build",
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

    case["iteration"] = iteration
    case["job_id"] = jid
    case["job_mode"] = "write"
    case["job_submitted_at"] = utc_now()
    case["status"] = "investigating"
    case["human_follow_up"] = body
    case["question"] = None
    case["recommendation"] = "Working on your requested change."
    case["proposed_reply"] = None
    save_case(case)
    return jid
def apply_command(command: dict) -> str:
    cid = _safe_command_id(str(command.get("command_id") or ""))
    feedback_id = str(command.get("feedback_id") or "")
    action = str(command.get("action") or "")
    body = str(command.get("body") or "").strip()
    if not feedback_id or not body:
        raise ValueError("feedback command is missing feedback_id or body")

    case = load_case(feedback_id)
    if action == "follow_up":
        jid = _submit_follow_up(case, body)
        return f"follow-up queued as {jid}"

    if action != "send_close":
        raise ValueError(f"unsupported feedback action: {action}")
    if case.get("status") == "resolved":
        raise FileNotFoundError("feedback case is already resolved")

    deployment_note = _ensure_verified_deployment(case, command)
    result = _post_resolution(case, body)
    row = result.get("row") or {}
    case["status"] = "resolved"
    case["resolution_note"] = body
    case["proposed_reply"] = body
    case["resolved_at"] = row.get("resolved_at") or utc_now()
    case["closed_at"] = case["resolved_at"]
    case["question"] = None
    case["recommendation"] = "Sent and closed."
    save_case(case)
    return f"{deployment_note}; resolution sent and case closed"


def process_once(*, url=None, token=None):
    commands = fetch_commands(url=url, token=token)
    results = []
    for command in commands:
        cid = str(command.get("command_id") or "")
        try:
            note = apply_command(command)
            ack(cid, "executed", note, url=url, token=token)
            results.append((cid, "executed"))
        except (ValueError, FileNotFoundError) as exc:
            ack(cid, "stale", str(exc), url=url, token=token)
            results.append((cid, "stale"))
        except DeploymentError as exc:
            ack(cid, "error", str(exc), url=url, token=token)
            results.append((cid, "error"))
        except Exception as exc:
            try:
                ack(cid, "error", str(exc)[:2000], url=url, token=token)
            except Exception:
                pass
            results.append((cid, "error"))
    return results
def main() -> int:
    try:
        results = process_once()
    except (OSError, error.URLError, json.JSONDecodeError, ValueError) as exc:
        print(f"feedback command poll failed: {exc}", file=sys.stderr)
        return 1
    for cid, status in results:
        print(f"{cid} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
