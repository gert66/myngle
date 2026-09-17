#!/usr/bin/env python3
"""Safe feedback poller with duplicate linking and research-only Lusha coverage checks."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.feedback_autopilot import (
    DEFAULT_API_URL,
    ingest_row,
    list_cases,
    load_case,
    reconcile_case,
    save_case,
    submit_case,
)

TOKEN_FILE = Path(os.getenv("FEEDBACK_AUTOPILOT_TOKEN_FILE", str(ROOT / "secrets" / "feedback_autopilot_token")))
URL_FILE = Path(os.getenv("FEEDBACK_AUTOPILOT_URL_FILE", str(ROOT / "config" / "feedback_autopilot_url.txt")))
SUPPRESSIONS_FILE = Path(os.getenv("FEEDBACK_SUPPRESSIONS_FILE", str(ROOT / "state" / "feedback_autopilot_suppressions.json")))


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def issue_key(text: str) -> str | None:
    t = _norm(text)
    if any(x in t for x in ("log call", "could not log a call", "could not log the call", "cannot log a call", "can't log a call")):
        return "log_call_failed"
    if "lusha" in t and any(x in t for x in ("phone", "number", "contact", "disconnect", "less efficient", "less phone")):
        return "lusha_contact_coverage"
    return None


def fetch_rows(url: str, token: str):
    req = request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Autopilot/1.1",
        "x-feedback-autopilot-token": token,
    })
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))["feedback"]


def load_suppressions() -> dict:
    try:
        return json.loads(SUPPRESSIONS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_suppressions(data: dict) -> None:
    SUPPRESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUPPRESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(SUPPRESSIONS_FILE)


def post_resolution(feedback_id: str, expected_updated_at: str | None, note: str, url: str, token: str) -> dict:
    payload = json.dumps({
        "id": feedback_id,
        "resolution_note": note,
        "expected_updated_at": expected_updated_at,
    }).encode("utf-8")
    req = request.Request(url, data=payload, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Sales-Cockpit-Feedback-Autopilot/1.1",
        "x-feedback-autopilot-token": token,
    })
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--no-submit", action="store_true")
    args = ap.parse_args()

    token = TOKEN_FILE.read_text(encoding="utf-8").strip() if not args.fixture else "fixture"
    url = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else DEFAULT_API_URL
    rows = json.loads(Path(args.fixture).read_text(encoding="utf-8")) if args.fixture else fetch_rows(url, token)
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""))

    canonical: dict[str, str] = {}
    for row in rows:
        key = issue_key(str(row.get("comment") or ""))
        if key == "log_call_failed" and key not in canonical:
            canonical[key] = str(row["id"])

    suppressions = load_suppressions()
    created = 0
    linked = 0
    for row in rows:
        fid = str(row["id"])
        key = issue_key(str(row.get("comment") or ""))
        case, is_new = ingest_row(row)
        created += int(is_new)

        if key == "lusha_contact_coverage" and not case.get("job_id"):
            case["kind"] = "data_quality"
            case["risk"] = "AMBER"
            case["issue_key"] = key
            if case.get("status") not in {"resolved", "informational"}:
                case["status"] = "queued_research"
            save_case(case)

        if key == "log_call_failed":
            case["issue_key"] = key
            canonical_id = canonical[key]
            if fid != canonical_id:
                case["canonical_feedback_id"] = canonical_id
                case["status"] = "linked_duplicate"
                save_case(case)
                suppressions[fid] = {
                    "canonical_feedback_id": canonical_id,
                    "source_updated_at": row.get("updated_at"),
                }
                linked += 1
                continue
            save_case(case)

        if case.get("status") in {"queued", "queued_research"} and not args.no_submit:
            submit_case(case)

    save_suppressions(suppressions)

    for case in list_cases():
        if not case.get("canonical_feedback_id"):
            reconcile_case(case)

    if not args.fixture:
        for duplicate_id, meta in list(suppressions.items()):
            try:
                canonical_case = load_case(str(meta["canonical_feedback_id"]))
                duplicate_case = load_case(duplicate_id)
            except FileNotFoundError:
                continue
            if canonical_case.get("status") != "resolved":
                continue
            note = str(canonical_case.get("resolution_note") or "").strip()
            if not note:
                continue
            result = post_resolution(duplicate_id, meta.get("source_updated_at"), note, url, token)
            if result.get("status") == "resolved":
                duplicate_case["status"] = "resolved"
                duplicate_case["resolution_note"] = note
                duplicate_case["resolved_at"] = (result.get("row") or {}).get("resolved_at")
                save_case(duplicate_case)
                suppressions.pop(duplicate_id, None)
        save_suppressions(suppressions)

    print(json.dumps({
        "fetched": len(rows),
        "created": created,
        "linked_duplicates": linked,
        "cases": len(list_cases()),
        "submit_enabled": not args.no_submit,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
