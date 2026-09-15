"""Durable first-hop intake receipts for orchestrated requests.

The purpose is simple: persist the user's instruction before any long planning
or implementation work begins. A later ChatGPT interruption can then no longer
make the request disappear silently.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTAKE_DIR = ORCH_ROOT / "intake"
VALID_STATUSES = {"RECEIVED", "SUBMITTED", "CANCELLED"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value[:48] or "request"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def receive(title, request, *, intake_dir=DEFAULT_INTAKE_DIR, receipt_id=None, source="chatgpt"):
    if not title.strip() or not request.strip():
        raise ValueError("title and request must be non-empty")
    now = utc_now()
    if receipt_id is None:
        stamp = now.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
        receipt_id = f"{stamp}-{_slug(title)}"
    path = Path(intake_dir) / f"{receipt_id}.json"
    if path.exists():
        raise FileExistsError(f"intake receipt already exists: {receipt_id}")
    payload = {
        "receipt_id": receipt_id,
        "title": title.strip(),
        "request": request.strip(),
        "source": source,
        "status": "RECEIVED",
        "received_at": now,
        "submitted_at": None,
        "job_id": None,
        "notified_at": None,
    }
    _atomic_write(path, payload)
    return payload


def load(receipt_id, *, intake_dir=DEFAULT_INTAKE_DIR):
    path = Path(intake_dir) / f"{receipt_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def update(receipt_id, *, status, job_id=None, intake_dir=DEFAULT_INTAKE_DIR):
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    payload = load(receipt_id, intake_dir=intake_dir)
    payload["status"] = status
    if status == "SUBMITTED":
        if not job_id:
            raise ValueError("job_id is required for SUBMITTED")
        payload["job_id"] = job_id
        payload["submitted_at"] = utc_now()
    _atomic_write(Path(intake_dir) / f"{receipt_id}.json", payload)
    return payload

def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m core.intake")
    p.add_argument("--intake-dir", default=str(DEFAULT_INTAKE_DIR))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("receive")
    r.add_argument("--title", required=True)
    r.add_argument("--request", required=True)
    r.add_argument("--receipt-id")
    r.add_argument("--source", default="chatgpt")
    u = sub.add_parser("update")
    u.add_argument("--receipt-id", required=True)
    u.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    u.add_argument("--job-id")
    s = sub.add_parser("show")
    s.add_argument("--receipt-id", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    intake_dir = Path(args.intake_dir)
    if args.cmd == "receive":
        payload = receive(args.title, args.request, intake_dir=intake_dir,
                          receipt_id=args.receipt_id, source=args.source)
    elif args.cmd == "update":
        payload = update(args.receipt_id, status=args.status, job_id=args.job_id,
                         intake_dir=intake_dir)
    else:
        payload = load(args.receipt_id, intake_dir=intake_dir)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
