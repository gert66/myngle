"""Watch durable intake receipts and alert when one was never submitted."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.intake import DEFAULT_INTAKE_DIR, _atomic_write
from core.notify import notify


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def scan(*, intake_dir=DEFAULT_INTAKE_DIR, stale_minutes=10, notifier=notify,
         now=None):
    now = now or datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=stale_minutes)
    alerts = []
    root = Path(intake_dir)
    if not root.exists():
        return alerts
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") != "RECEIVED" or payload.get("notified_at"):
            continue
        received = _parse(payload["received_at"])
        if received > stale_before:
            continue
        detail = (
            f"Orchestrator intake {payload['receipt_id']} is still RECEIVED after "
            f"{stale_minutes} minutes and has no submitted job. Title: {payload['title']}"
        )
        notifier("NEEDS_HUMAN", payload["receipt_id"], detail)
        payload["notified_at"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        _atomic_write(path, payload)
        alerts.append(payload["receipt_id"])
    return alerts


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.intake_watch")
    p.add_argument("--intake-dir", default=str(DEFAULT_INTAKE_DIR))
    p.add_argument("--stale-minutes", type=int, default=10)
    args = p.parse_args(argv)
    alerts = scan(intake_dir=args.intake_dir, stale_minutes=args.stale_minutes)
    print(json.dumps({"alerts": alerts, "count": len(alerts)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
