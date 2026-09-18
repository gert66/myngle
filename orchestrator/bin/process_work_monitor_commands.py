#!/usr/bin/env python3
"""Execute Work Monitor owner commands from the Control Center."""
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
TOKEN_FILE = ORCH_ROOT / "secrets" / "orchestrator_ingest_token"
PUSH_URL_FILE = ORCH_ROOT / "config" / "ops_push_url.txt"
JOBS_DIR = ORCH_ROOT / "jobs"
SUPPRESSIONS_FILE = ORCH_ROOT / "state" / "work_monitor_suppressions.json"
TERMINAL_PHASES = {"DONE", "ERROR", "ABORTED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def commands_url(path=PUSH_URL_FILE):
    base = Path(path).read_text(encoding="utf-8").strip()
    suffix = "/api/public/orchestrator-snapshot"
    if not base.endswith(suffix):
        raise ValueError("ops_push_url.txt has unexpected format")
    return base[:-len(suffix)] + "/api/public/work-monitor-commands"


def _call(url, token, *, method="GET", payload=None, timeout=15):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Work-Monitor/1.0",
        "x-orchestrator-token": token,
    })
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_commands(*, url=None, token=None):
    return _call(url or commands_url(), token or TOKEN_FILE.read_text(encoding="utf-8").strip()).get("commands", [])


def ack(command_id, status, result_note="", *, url=None, token=None):
    return _call(url or commands_url(), token or TOKEN_FILE.read_text(encoding="utf-8").strip(), method="POST", payload={
        "command_id": command_id,
        "status": status,
        "result_note": result_note or None,
    })


def _safe_job_id(value: object) -> str:
    job_id = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", job_id):
        raise ValueError("invalid job id")
    return job_id


def _load_suppressions(path=SUPPRESSIONS_FILE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def suppress_job(job_id: str, *, path=SUPPRESSIONS_FILE, reason="removed from Work Monitor") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_suppressions(path)
    data[job_id] = {"hidden_at": utc_now(), "reason": reason}
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(data, h, indent=2, sort_keys=True)
            h.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_job(job_id: str, *, run=subprocess.run, jobs_dir=JOBS_DIR, suppressions_file=SUPPRESSIONS_FILE) -> str:
    job_id = _safe_job_id(job_id)
    state_path = Path(jobs_dir) / job_id / "state.json"
    result = "removed from monitor"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        phase = str(state.get("phase") or "UNKNOWN").upper()
        if phase not in TERMINAL_PHASES:
            proc = run(
                ["/usr/bin/python3", "-m", "core.cli", "abort", "--job-id", job_id,
                 "--reason", "removed from Work Monitor by owner"],
                cwd=ORCH_ROOT, text=True, capture_output=True,
            )
            if proc.returncode:
                raise RuntimeError((proc.stderr or proc.stdout or "abort failed")[-1200:])
            result = "orchestrator job stopped and removed from monitor"
        else:
            result = f"terminal orchestrator job ({phase}) removed from monitor"
    suppress_job(job_id, path=suppressions_file)
    return result


def process_once(*, url=None, token=None):
    rows = fetch_commands(url=url, token=token)
    results = []
    for row in rows:
        cid = str(row.get("command_id") or "")
        try:
            if row.get("action") != "remove":
                raise ValueError("unsupported action")
            try:
                ack(cid, "working", "removing job", url=url, token=token)
            except Exception:
                pass
            note = remove_job(str(row.get("job_id") or ""))
            ack(cid, "executed", note, url=url, token=token)
            results.append((cid, "executed"))
        except ValueError as exc:
            ack(cid, "stale", str(exc), url=url, token=token)
            results.append((cid, "stale"))
        except Exception as exc:
            try:
                ack(cid, "error", str(exc), url=url, token=token)
            except Exception:
                pass
            results.append((cid, "error"))
    return results


def main() -> int:
    try:
        rows = process_once()
    except (OSError, error.URLError, json.JSONDecodeError) as exc:
        print(f"work monitor command poll failed: {exc}", file=sys.stderr)
        return 1
    for cid, status in rows:
        print(cid, status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
