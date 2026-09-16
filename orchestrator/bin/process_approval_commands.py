#!/usr/bin/env python3
"""Process owner approval commands queued by Sales Cockpit Control Center."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error, request

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from core.slack_approval import update_status  # noqa: E402

TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"


def commands_url(path=PUSH_URL_FILE):
    base = Path(path).read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[:-len(suffix)] + "/api/public/approval-commands"


def _call(url, token, *, method="GET", payload=None, timeout=15):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-orchestrator-token": token,
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_commands(*, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token).get("commands", [])


def ack(command_id, status, result_note="", *, url=None, token=None):
    url = url or commands_url()
    token = token or TOKEN_FILE.read_text(encoding="utf-8").strip()
    return _call(url, token, method="POST", payload={
        "command_id": command_id,
        "status": status,
        "result_note": result_note or None,
    })


def apply_command(command):
    action = str(command.get("action") or "")
    target = {"approve": "approved", "reject": "rejected", "discuss": "discussion_requested"}.get(action)
    if not target:
        raise ValueError(f"unsupported action: {action}")
    actor = f"control-center:{command.get('created_by') or 'unknown'}"
    return update_status(
        str(command["proposal_id"]),
        str(command["fingerprint"]),
        target,
        actor,
        note=str(command.get("note") or "").strip(),
    )


def process_once(*, url=None, token=None):
    commands = fetch_commands(url=url, token=token)
    results = []
    for command in commands:
        cid = str(command.get("command_id") or "")
        try:
            record = apply_command(command)
            note = f"proposal status -> {record.get('status')}"
            ack(cid, "executed", note, url=url, token=token)
            results.append((cid, "executed"))
        except (ValueError, FileNotFoundError) as exc:
            ack(cid, "stale", str(exc), url=url, token=token)
            results.append((cid, "stale"))
        except Exception as exc:  # keep one bad command from blocking the queue
            try:
                ack(cid, "error", str(exc), url=url, token=token)
            except Exception:
                pass
            results.append((cid, "error"))
    return results


def main():
    try:
        results = process_once()
    except (OSError, error.URLError, json.JSONDecodeError) as exc:
        print(f"approval command poll failed: {exc}", file=sys.stderr)
        return 1
    for cid, status in results:
        print(f"{cid} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
