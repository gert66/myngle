#!/usr/bin/env python3
"""Poll Sales Cockpit feedback and advance durable feedback cases."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.feedback_autopilot import DEFAULT_API_URL, ingest_row, list_cases, reconcile_case, submit_case

TOKEN_FILE = ROOT / "secrets" / "feedback_autopilot_token"
URL_FILE = ROOT / "config" / "feedback_autopilot_url.txt"

def fetch_rows(url: str, token: str):
    req=request.Request(url, headers={"Accept":"application/json","User-Agent":"Sales-Cockpit-Feedback-Autopilot/1.0","x-feedback-autopilot-token":token})
    with request.urlopen(req, timeout=20) as r: return json.loads(r.read().decode())["feedback"]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--fixture"); ap.add_argument("--no-submit",action="store_true"); args=ap.parse_args()
    if args.fixture:
        rows=json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    else:
        token=TOKEN_FILE.read_text(encoding="utf-8").strip()
        url=URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else DEFAULT_API_URL
        rows=fetch_rows(url,token)
    created=0
    for row in rows:
        case,is_new=ingest_row(row); created+=int(is_new)
        if case.get("status") in {"queued", "queued_research"} and not args.no_submit: submit_case(case)
    for case in list_cases(): reconcile_case(case)
    print(json.dumps({"fetched":len(rows),"created":created,"cases":len(list_cases())},sort_keys=True))
    return 0
if __name__=="__main__": raise SystemExit(main())
