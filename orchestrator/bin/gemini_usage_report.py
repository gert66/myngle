#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
LEDGER = ORCH_ROOT / "logs" / "gemini_usage.jsonl"


def parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_rows(path):
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


def summarize(rows):
    return {
        "records": len(rows),
        "completed": sum(r.get("outcome") == "completed" for r in rows),
        "escalated": sum(str(r.get("outcome") or "").startswith("escalated_to_") for r in rows),
        "failed": sum(r.get("outcome") == "gemini_failed" for r in rows),
        "flex_attempts": sum("flex" in (r.get("tiers_attempted") or []) or r.get("service_tier") == "flex" for r in rows),
        "standard_attempts": sum("standard" in (r.get("tiers_attempted") or []) or r.get("service_tier") == "standard" for r in rows),
        "fallbacks": sum(len(r.get("fallback_events") or []) for r in rows),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "thinking_tokens": sum(int(r.get("thinking_tokens") or 0) for r in rows),
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in rows),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Summarize Gemini Worker usage")
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = load_rows(args.ledger)
    if args.days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        rows = [r for r in rows if (parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    totals = summarize(rows)
    if args.json:
        print(json.dumps({"totals": totals, "recent": rows[:args.limit]}, indent=2, sort_keys=True))
        return 0
    print(f"Gemini usage, last {args.days} day(s)")
    print(f"Records: {totals['records']} | completed: {totals['completed']} | escalated: {totals['escalated']} | failed: {totals['failed']}")
    print(f"Flex attempts: {totals['flex_attempts']} | Standard attempts: {totals['standard_attempts']} | fallbacks: {totals['fallbacks']}")
    print(f"Tokens: input {totals['input_tokens']:,} | thinking {totals['thinking_tokens']:,} | output {totals['output_tokens']:,} | total {totals['total_tokens']:,}")
    print()
    print("Recent Gemini work:")
    for r in rows[:args.limit]:
        print(
            f"- {r.get('ts') or '?'} | {r.get('job_id') or '?'} | {r.get('step_id') or '?'} | "
            f"{r.get('role') or '?'} | {r.get('service_tier') or '?'} | {r.get('outcome') or '?'} | "
            f"tokens={int(r.get('total_tokens') or 0):,}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
