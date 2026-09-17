#!/usr/bin/env python3
"""Conversation layer between Control Center feedback threads and the orchestrator."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib import request, error

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from core.claude_runner import run_claude  # noqa: E402
from core.classify import classify, SUCCESS  # noqa: E402
from core.feedback_autopilot import (  # noqa: E402
    COMPANY_BRANCH,
    COMPANY_REPO,
    COMPANY_REPO_PATH,
    JOBS_DIR,
    load_case,
    list_cases,
    reconcile_case,
    save_case,
    utc_now,
)

TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
CLAUDE_BIN = os.getenv("FEEDBACK_CONVERSATION_CLAUDE_BIN", "/home/myngle/bin/claude-gert66")
RUNNING_PHASES = {"QUEUED", "BRAIN", "WORKER", "TESTING", "REVIEW", "REPAIR", "WAITING", "EVIDENCE"}
TERMINAL_PHASES = {"DONE", "ERROR", "NEEDS_HUMAN", "ABORTED"}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["reply", "investigate"]},
        "assistant_reply": {"type": "string"},
        "investigation_instruction": {"type": "string"},
        "proposed_reply": {"type": "string"},
    },
    "required": ["mode", "assistant_reply", "investigation_instruction", "proposed_reply"],
    "additionalProperties": False,
}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "assistant_reply": {"type": "string"},
        "proposed_reply": {"type": "string"},
    },
    "required": ["assistant_reply", "proposed_reply"],
    "additionalProperties": False,
}


def messages_url(path: Path = PUSH_URL_FILE) -> str:
    base = path.read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[: -len(suffix)] + "/api/public/feedback-messages"


def _call(url: str, token: str, *, method="GET", payload=None, timeout=25):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Conversation/1.0",
        "x-orchestrator-token": token,
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_messages(*, url=None, token=None):
    url = url or messages_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token).get("messages", [])


def update_message(message_id: str, status: str, *, assistant_body=None, result_note=None, url=None, token=None):
    url = url or messages_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    payload = {"message_id": message_id, "status": status}
    if assistant_body:
        payload["assistant_body"] = assistant_body[:4000]
    if result_note:
        payload["result_note"] = result_note[:2000]
    return _call(url, token, method="POST", payload=payload)


def _compact(value, limit=1800):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _thread_text(thread):
    lines = []
    for row in thread or []:
        role = "YOU" if row.get("role") == "user" else "SALES COCKPIT AI"
        body = _compact(row.get("body"), 1400)
        if body:
            lines.append(f"{role}: {body}")
    return "\n".join(lines[-30:]) or "No prior conversation."


def _case_context(case):
    fields = {
        "status": case.get("status"),
        "kind": case.get("kind"),
        "reporter": case.get("reporter_name") or case.get("reporter_email"),
        "company": case.get("company"),
        "original_feedback": case.get("comment"),
        "current_question": case.get("question"),
        "recommendation": case.get("recommendation"),
        "interpretation": case.get("ai_interpretation"),
        "investigation": case.get("investigation_summary"),
        "actions_taken": case.get("actions_taken"),
        "current_proposed_reply": case.get("proposed_reply"),
    }
    return json.dumps({k: _compact(v) for k, v in fields.items() if v}, ensure_ascii=False, indent=2)


def _invoke_structured(prompt: str, schema: dict) -> dict:
    result = run_claude(
        prompt,
        cwd=COMPANY_REPO_PATH,
        timeout=240,
        claude_bin=CLAUDE_BIN,
        structured=True,
        json_schema=schema,
        permission_mode="dontAsk",
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"],
        model="sonnet",
        effort="low",
    )
    cls = classify(result)
    if cls.category != SUCCESS or not isinstance(cls.payload, dict):
        detail = result.stderr or result.stdout or cls.reason
        raise RuntimeError(f"conversation AI failed: {_compact(detail, 1000)}")
    return cls.payload


def decide_message(case: dict, message: dict) -> dict:
    prompt = f"""You are Sales Cockpit AI, the conversational layer for one customer-feedback case.
The owner should experience a concise, natural conversation like ChatGPT. Speak to the owner in Dutch.
Never expose raw Git, branch, file, commit, stack trace, job-state or orchestrator jargon in assistant_reply.
Do not claim production changed unless the context proves it. Never send or close feedback yourself.
A final reply to the reporter is only sent later by an explicit Send & close button.

Choose mode=reply when the owner is discussing wording, asking for an explanation, refining a proposed reply, or when you can answer from the existing evidence.
Choose mode=investigate when the owner asks you to check, test, retry, trace, fix, implement, or continue technical work, or answers yes to a pending technical action.
For investigate, make investigation_instruction a concrete safe job instruction and keep assistant_reply short; it is only an acknowledgement.
For reply, investigation_instruction must be an empty string.
If a reporter reply is ready, proposed_reply should be concise and in the language of the reporter's original feedback. Otherwise use an empty string.

CASE CONTEXT:
{_case_context(case)}

THREAD:
{_thread_text(message.get('thread'))}

LATEST OWNER MESSAGE:
{_compact(message.get('body'), 3000)}
"""
    return _invoke_structured(prompt, DECISION_SCHEMA)


def _job_state(job_id: str):
    path = JOBS_DIR / job_id / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_detail(state: dict) -> str:
    history = (state.get("runtime") or {}).get("history") or []
    last = history[-1] if history else {}
    bits = [
        f"phase={state.get('phase')}",
        f"human_question={state.get('human_question') or ''}",
        f"last_error={state.get('last_error') or ''}",
        f"verdict={last.get('verdict') or ''}",
        f"detail={last.get('detail') or ''}",
        f"paths={last.get('paths') or []}",
        f"commit={last.get('commit') or ''}",
    ]
    return _compact(" | ".join(bits), 5000)


def summarize_result(case: dict, message: dict, state: dict) -> dict:
    prompt = f"""You are Sales Cockpit AI returning after technical work on a feedback case.
Write a short natural Dutch answer to the owner, like a good ChatGPT response. Lead with what matters.
Translate technical evidence into ordinary language. Never dump branch names, file paths, commit hashes, stack traces, raw tool output, or orchestrator jargon unless the owner explicitly asked for technical detail.
Be precise about what is only investigated/prepared/tested versus actually deployed or changed in production.
If one decision is still required, ask exactly one plain-language question.
If a reporter reply is ready, proposed_reply should be concise and in the language of the reporter's original feedback. If it is not ready, return an empty string.
Never send or close the feedback yourself.

CASE CONTEXT AFTER WORK:
{_case_context(case)}

TECHNICAL RESULT (private source material; do not copy verbatim):
{_latest_detail(state)}

THREAD:
{_thread_text(message.get('thread'))}
"""
    return _invoke_structured(prompt, RESULT_SCHEMA)


def _submit_follow_up(case: dict, instruction: str, *, run=subprocess.run) -> str:
    iteration = int(case.get("iteration") or 0) + 1
    fid = str(case["feedback_id"])
    jid = f"feedback-{fid[:8].lower()}-c{iteration}"
    while (JOBS_DIR / jid).exists():
        iteration += 1
        jid = f"feedback-{fid[:8].lower()}-c{iteration}"
    previous = case.get("actions_taken") or case.get("investigation_summary") or "No prior summary available."
    goal = f"""Continue one Sales Cockpit feedback case after an owner conversation.
Feedback ID: {fid}
Reporter: {case.get('reporter_email') or 'unknown'}
Company: {case.get('company') or 'none'}
Original feedback: {case.get('comment') or ''}
Previous work: {previous}
Owner instruction: {instruction}

Work autonomously as far as evidence supports. On branch work only, reproduce or trace the issue, make the smallest safe code change if appropriate, add/update regression tests, run relevant tests and production build, and commit exactly the fix. Do not merge or push to main, deploy, or change production/external data. If a protected action or product decision is still required, stop with NEEDS_HUMAN and ask exactly one concrete question. Avoid unrelated refactors."""
    args = [
        "/usr/bin/python3", "-m", "core.cli", "submit",
        "--job-id", jid,
        "--repo", COMPANY_REPO,
        "--repo-path", str(COMPANY_REPO_PATH),
        "--branch", COMPANY_BRANCH,
        "--mode", "write",
        "--goal", goal,
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
    case["question"] = None
    case["recommendation"] = "Sales Cockpit AI is working on your request."
    case["proposed_reply"] = None
    save_case(case)
    return jid


def _older_processing_exists(message: dict) -> bool:
    current = str(message.get("message_id") or "")
    for row in message.get("thread") or []:
        if row.get("message_id") == current:
            break
        if row.get("role") == "user" and row.get("status") == "processing":
            return True
    return False


def handle_pending(message: dict, *, url=None, token=None) -> str:
    mid = str(message.get("message_id") or "")
    fid = str(message.get("feedback_id") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", mid):
        raise ValueError("invalid feedback message id")
    if _older_processing_exists(message):
        return "deferred"
    case = load_case(fid)
    if case.get("status") == "resolved":
        update_message(mid, "error", assistant_body="Deze feedbackcase is inmiddels gesloten.", result_note="case already resolved", url=url, token=token)
        return "error"
    decision = decide_message(case, message)
    proposed = str(decision.get("proposed_reply") or "").strip()
    if proposed:
        case["proposed_reply"] = proposed
    case["conversation_summary"] = str(decision.get("assistant_reply") or "").strip()
    case["conversation_summary_at"] = utc_now()
    if decision.get("mode") == "reply":
        save_case(case)
        reply = case["conversation_summary"] or "Ik heb je bericht verwerkt."
        update_message(mid, "done", assistant_body=reply, result_note="conversation reply", url=url, token=token)
        return "done"
    instruction = str(decision.get("investigation_instruction") or message.get("body") or "").strip()
    jid = _submit_follow_up(case, instruction)
    case = load_case(fid)
    case["conversation_message_id"] = mid
    case["conversation_job_id"] = jid
    save_case(case)
    update_message(mid, "processing", result_note=f"job:{jid}", url=url, token=token)
    return "processing"


def handle_processing(message: dict, *, url=None, token=None) -> str:
    mid = str(message.get("message_id") or "")
    fid = str(message.get("feedback_id") or "")
    case = load_case(fid)
    if case.get("conversation_message_id") not in {None, mid}:
        return "deferred"
    job_id = case.get("conversation_job_id") or case.get("job_id")
    if not job_id:
        update_message(mid, "error", assistant_body="Ik kan de bijbehorende AI-run niet meer vinden. Ik heb de case open gelaten.", result_note="missing job", url=url, token=token)
        return "error"
    state = _job_state(str(job_id))
    if not state:
        return "waiting"
    phase = str(state.get("phase") or "UNKNOWN")
    if phase in RUNNING_PHASES:
        return "waiting"
    if phase not in TERMINAL_PHASES:
        return "waiting"
    try:
        case = reconcile_case(case)
    except Exception as exc:
        case["conversation_reconcile_error"] = _compact(exc, 800)
        save_case(case)
    result = summarize_result(case, message, state)
    assistant = str(result.get("assistant_reply") or "").strip() or "Ik heb de nieuwe ronde afgerond."
    proposed = str(result.get("proposed_reply") or "").strip()
    if proposed:
        case["proposed_reply"] = proposed
    elif phase in {"ERROR", "NEEDS_HUMAN", "ABORTED"}:
        case["proposed_reply"] = None
    case["conversation_summary"] = assistant
    case["conversation_summary_at"] = utc_now()
    case["conversation_message_id"] = None
    case["conversation_job_id"] = None
    save_case(case)
    update_message(mid, "done", assistant_body=assistant, result_note=f"job finished: {phase}", url=url, token=token)
    return "done"


def _summary_fingerprint(case: dict) -> str:
    keys = [
        "status", "job_id", "job_phase", "question", "recommendation",
        "ai_interpretation", "investigation_summary", "actions_taken",
    ]
    raw = json.dumps({k: case.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_case_summary(case: dict) -> bool:
    if case.get("status") in {"resolved", "informational", "queued", "queued_research", "researching", "investigating"}:
        return False
    fingerprint = _summary_fingerprint(case)
    if case.get("conversation_summary_fingerprint") == fingerprint and case.get("conversation_summary"):
        return False
    state = _job_state(str(case.get("job_id") or "")) if case.get("job_id") else {"phase": case.get("job_phase") or case.get("status")}
    state = state or {"phase": case.get("job_phase") or case.get("status")}
    prompt = f"""You are Sales Cockpit AI giving the owner the current update on one feedback case.
Write a concise natural Dutch message like ChatGPT. Explain the practical conclusion, what has or has not been changed, and if needed ask one plain-language question.
Do not expose branch names, file paths, commit hashes, stack traces, raw tool output, or orchestrator jargon.
Do not claim something is deployed or fixed in production unless the evidence proves it.
If a reporter reply is ready, proposed_reply should be concise and in the language of the reporter's original feedback. Otherwise return an empty string.
Never send or close the case yourself.

CASE CONTEXT:
{_case_context(case)}

TECHNICAL STATE (private source material):
{_latest_detail(state)}
"""
    result = _invoke_structured(prompt, RESULT_SCHEMA)
    assistant = str(result.get("assistant_reply") or "").strip()
    if not assistant:
        return False
    proposed = str(result.get("proposed_reply") or "").strip()
    case["conversation_summary"] = assistant
    case["conversation_summary_at"] = utc_now()
    case["conversation_summary_fingerprint"] = fingerprint
    if proposed:
        case["proposed_reply"] = proposed
    save_case(case)
    return True


def process_once(*, url=None, token=None):
    messages = fetch_messages(url=url, token=token)
    results = []
    for message in messages:
        mid = str(message.get("message_id") or "")
        try:
            status = str(message.get("status") or "pending")
            result = handle_processing(message, url=url, token=token) if status == "processing" else handle_pending(message, url=url, token=token)
            results.append((mid, result))
        except FileNotFoundError:
            try:
                update_message(mid, "error", assistant_body="Ik kan deze feedbackcase niet meer vinden. Ik heb niets gewijzigd.", result_note="case missing", url=url, token=token)
            except Exception:
                pass
            results.append((mid, "error"))
        except Exception as exc:
            try:
                update_message(mid, "error", assistant_body="Deze ronde liep technisch vast. De feedbackcase blijft open zodat we niets verliezen.", result_note=_compact(exc, 1000), url=url, token=token)
            except Exception:
                pass
            results.append((mid, "error"))
    for case in list_cases():
        try:
            if ensure_case_summary(case):
                results.append((str(case.get("feedback_id") or ""), "summary"))
        except Exception as exc:
            print(f"feedback summary failed for {case.get('feedback_id')}: {_compact(exc, 500)}", file=sys.stderr)
    return results


def main() -> int:
    try:
        results = process_once()
    except (OSError, error.URLError, json.JSONDecodeError, ValueError) as exc:
        print(f"feedback conversation poll failed: {exc}", file=sys.stderr)
        return 1
    for mid, status in results:
        print(f"{mid} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
