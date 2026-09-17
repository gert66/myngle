#!/usr/bin/env python3
"""Process durable operational-chat requests from Sales Cockpit Control Center.

Phase 3A is intentionally read-only. Free-text requests become durable
orchestrator jobs, but the worker cannot edit code, deploy, or mutate external
systems. Terminal job state is mirrored back into the same chat conversation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
STATE_DIR = ORCH_ROOT / "state" / "chat_commands"
JOBS_DIR = ORCH_ROOT / "jobs"
REPO = os.getenv("CHAT_REPO", "gert66/myngle")
REPO_PATH = Path(os.getenv("CHAT_REPO_PATH", "/home/myngle/worktrees/current-client-protection"))
BRANCH = os.getenv("CHAT_BRANCH", "work")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def commands_url(path: Path = PUSH_URL_FILE) -> str:
    base = Path(path).read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[: -len(suffix)] + "/api/public/chat-commands"


def _call(url: str, token: str, *, method: str = "GET", payload=None, timeout: int = 20):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Sales-Cockpit-Operational-Chat/1.0",
            "x-orchestrator-token": token,
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_commands(*, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token).get("commands", [])


def ack(command_id: str, status: str, *, job_id=None, result_note=None, assistant_message=None, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    payload = {"command_id": command_id, "status": status}
    if job_id is not None:
        payload["job_id"] = job_id
    if result_note is not None:
        payload["result_note"] = result_note
    if assistant_message is not None:
        payload["assistant_message"] = assistant_message
    return _call(url, token, method="POST", payload=payload)


def _safe_command_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", value or ""):
        raise ValueError("invalid chat command id")
    return value


def job_id_for(command_id: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", _safe_command_id(command_id)).lower()
    return f"chat-{compact[:12]}"


def _receipt_path(command_id: str) -> Path:
    return STATE_DIR / f"{_safe_command_id(command_id)}.json"


def save_receipt(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _receipt_path(str(data["command_id"]))
    fd, tmp = tempfile.mkstemp(prefix=target.name, dir=str(STATE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_receipts() -> list[dict]:
    if not STATE_DIR.exists():
        return []
    out = []
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _goal(body: str) -> str:
    return f"""Investigate one operational request from the Sales Cockpit Control Center in read-only mode.

Request:
{body}

Return a concise, concrete answer with evidence and the safest next action. You may inspect the repository, orchestrator state, logs and read-only system context available to the job. Do not edit files, commit, deploy, change production/external data, alter permissions, or execute destructive actions. If a write/change is needed, explain exactly what should be changed and why. If human input is required, ask one concrete question."""


def submit_command(command: dict, *, run=subprocess.run, ack_fn=ack) -> dict:
    cid = _safe_command_id(str(command.get("command_id") or ""))
    body = str(command.get("body") or "").strip()
    if not body:
        raise ValueError("chat command has no request body")
    jid = job_id_for(cid)
    job_dir = JOBS_DIR / jid

    if not job_dir.exists():
        args = [
            "/usr/bin/python3", "-m", "core.cli", "submit",
            "--job-id", jid,
            "--repo", REPO,
            "--repo-path", str(REPO_PATH),
            "--branch", BRANCH,
            "--mode", "read",
            "--goal", _goal(body),
            "--worker-provider", "auto",
            "--worker-complexity", "normal",
            "--resource-class", "read",
            "--priority", "35",
            "--forbid", ".env*",
            "--forbid", "secrets/**",
        ]
        proc = run(args, cwd=ORCH_ROOT, text=True, capture_output=True)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout)[-1600:])

    receipt = {
        "command_id": cid,
        "message_id": command.get("message_id"),
        "job_id": jid,
        "accepted_at": utc_now(),
        "terminal_sent": False,
    }
    save_receipt(receipt)
    ack_fn(cid, "working", job_id=jid, result_note="Read-only investigation started")
    return receipt


def _job_state(job_id: str) -> dict | None:
    path = JOBS_DIR / job_id / "state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _terminal_update(state: dict) -> tuple[str, str, str] | None:
    phase = str(state.get("phase") or "").upper()
    runtime = state.get("runtime") or {}
    if phase == "DONE":
        history = runtime.get("history") or []
        detail = str((history[-1] if history else {}).get("detail") or "Investigation completed.").strip()
        detail = detail[:4000]
        return "done", "Completed", detail
    if phase == "NEEDS_HUMAN":
        human = runtime.get("human") or {}
        question = str(state.get("human_question") or human.get("question") or "I need your input before I can continue.").strip()
        question = question[:3900]
        return "needs_input", "Needs your input", f"I need your input: {question}"
    if phase == "ERROR":
        message = str(state.get("last_error") or runtime.get("last_error") or "The investigation failed.").strip()
        message = message[:3900]
        return "error", "Investigation failed", message
    return None


def reconcile_receipt(receipt: dict, *, ack_fn=ack) -> bool:
    if receipt.get("terminal_sent"):
        return False
    state = _job_state(str(receipt.get("job_id") or ""))
    if not state:
        return False
    terminal = _terminal_update(state)
    if not terminal:
        return False
    status, note, message = terminal
    ack_fn(
        str(receipt["command_id"]),
        status,
        job_id=str(receipt.get("job_id") or ""),
        result_note=note,
        assistant_message=message,
    )
    receipt["terminal_sent"] = True
    receipt["terminal_status"] = status
    receipt["terminal_at"] = utc_now()
    save_receipt(receipt)
    return True


def process_once(*, url=None, token=None):
    def remote_ack(cid, status, **kwargs):
        return ack(cid, status, url=url, token=token, **kwargs)

    results = []
    for receipt in load_receipts():
        try:
            if reconcile_receipt(receipt, ack_fn=remote_ack):
                results.append((str(receipt["command_id"]), str(receipt.get("terminal_status") or "done")))
        except Exception as exc:
            results.append((str(receipt.get("command_id") or ""), f"reconcile_error:{exc}"))

    commands = fetch_commands(url=url, token=token)
    for command in commands:
        cid = str(command.get("command_id") or "")
        try:
            submit_command(command, ack_fn=remote_ack)
            results.append((cid, "working"))
        except Exception as exc:
            try:
                remote_ack(cid, "error", result_note=str(exc)[:2000], assistant_message=f"I could not start this request: {str(exc)[:3800]}")
            except Exception:
                pass
            results.append((cid, "error"))
    return results


def main() -> int:
    try:
        results = process_once()
    except (OSError, error.URLError, json.JSONDecodeError, ValueError) as exc:
        print(f"chat command poll failed: {exc}", file=sys.stderr)
        return 1
    for cid, status in results:
        print(f"{cid} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
