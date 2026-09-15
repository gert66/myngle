"""Notification wrapper for the existing Slack shell hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOTIFY_SCRIPT = ORCH_ROOT / "bin" / "notify_slack.sh"
NOTIFIABLE_PHASES = frozenset({"DONE", "ERROR", "NEEDS_HUMAN"})


class NotifyError(RuntimeError):
    pass


def notify(status, job_id, detail="", script=DEFAULT_NOTIFY_SCRIPT, timeout=20):
    if status not in NOTIFIABLE_PHASES:
        return False
    script = Path(script)
    if not script.is_file():
        raise NotifyError(f"notification script not found: {script}")
    try:
        result = subprocess.run(
            [str(script), status, job_id, detail or ""],
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotifyError(f"notification failed: {exc}") from exc
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "unknown error").strip()
        raise NotifyError(f"notification script exited {result.returncode}: {msg}")
    return True
