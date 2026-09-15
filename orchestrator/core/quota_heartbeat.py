"""Minimal quota-window heartbeat scheduler.

The 24-hour quota clock is deterministic, but heartbeats are opt-in.  When
``clock.heartbeat_enabled`` is false this module performs no Claude calls.
A small state file makes each anchor idempotent even when invoked every minute.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import classify as classify_mod
from core.claude_runner import run_claude
from core.quota_clock import daily_anchor_utc, due_anchor, due_seed_anchor, seed_anchor_utc
from core.quota_router import DEFAULT_QUOTA_STATE, QuotaRouter

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEARTBEAT_STATE = ORCH_ROOT / "config" / "quota_heartbeat_state.json"
DEFAULT_HEARTBEAT_LOG = ORCH_ROOT / "logs" / "quota-heartbeat.jsonl"
HEARTBEAT_PROMPT = "Reply with exactly OK and nothing else."


def _parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load(path, default):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def due_accounts(config, heartbeat_state, now):
    clock = config.get("clock") or {}
    if not clock.get("heartbeat_enabled", False):
        return []
    timezone_name = clock.get("timezone", "Europe/Amsterdam")
    period = float(clock.get("period_hours", 5))
    grace = int(clock.get("grace_minutes", 3))
    last = (heartbeat_state or {}).get("last_anchor") or {}
    due = []
    for name, account_clock in (clock.get("accounts") or {}).items():
        local_time = account_clock.get("daily_anchor_local")
        seed_local = account_clock.get("seed_local")
        if not (local_time or seed_local) or name not in (config.get("accounts") or {}):
            continue
        account = config["accounts"][name]
        all_bucket = (account.get("buckets") or {}).get("all", {})
        weekly_remaining = all_bucket.get("remaining", account.get("weekly_remaining"))
        weekly_reset = _parse_ts(all_bucket.get("reset_at", account.get("weekly_reset_at")))
        if weekly_remaining is not None and float(weekly_remaining) <= 0 and weekly_reset and weekly_reset > now:
            continue
        if seed_local:
            anchor = due_seed_anchor(now, seed_local=seed_local, timezone_name=timezone_name,
                                     period_hours=period, grace_minutes=grace)
        else:
            anchor = due_anchor(now, local_time=local_time, timezone_name=timezone_name,
                                period_hours=period, grace_minutes=grace)
        if anchor is None or last.get(name) == _iso(anchor):
            continue
        last_used = _parse_ts(account.get("last_used_at"))
        if last_used is not None and anchor <= last_used < anchor + timedelta(minutes=grace):
            continue
        if seed_local and account_clock.get("external_seed", False):
            if anchor == seed_anchor_utc(seed_local, timezone_name=timezone_name):
                continue
        elif account_clock.get("external_daily_anchor", False):
            daily = daily_anchor_utc(now, local_time=local_time, timezone_name=timezone_name)
            if anchor == daily:
                continue
        due.append({"account": name, "anchor": anchor, "period_hours": period})
    return due


def _append_log(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def run_once(*, config_path=DEFAULT_QUOTA_STATE, heartbeat_state_path=DEFAULT_HEARTBEAT_STATE,
             log_path=DEFAULT_HEARTBEAT_LOG, now=None, runner=run_claude, dry_run=False):
    now = now or datetime.now(timezone.utc)
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    hb_state = _load(heartbeat_state_path, {"last_anchor": {}})
    due = due_accounts(config, hb_state, now)
    if dry_run:
        return {"enabled": bool((config.get("clock") or {}).get("heartbeat_enabled")),
                "due": [x["account"] for x in due], "calls": 0}
    results = []
    for item in due:
        name = item["account"]
        account = config["accounts"][name]
        result = runner(
            HEARTBEAT_PROMPT, cwd=str(ORCH_ROOT), timeout=120, claude_bin=account["claude_bin"],
            structured=False, permission_mode="plan",
            disallowed_tools=["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"],
        )
        cls = classify_mod.classify(result)
        record = {"ts": _iso(now), "account": name, "anchor": _iso(item["anchor"]),
                  "classification": cls.category, "exit_code": getattr(result, "exit_code", None)}
        if cls.category == classify_mod.SUCCESS:
            hb_state.setdefault("last_anchor", {})[name] = _iso(item["anchor"])
            account["session_reset_at"] = _iso(item["anchor"] + timedelta(hours=item["period_hours"]))
            account["session_remaining"] = max(float(account.get("session_remaining", 0)), 1.0)
            account.pop("cooldown_until", None)
            config["updated_at"] = _iso(now)
            _atomic_json(heartbeat_state_path, hb_state)
            _atomic_json(config_path, config)
        elif cls.category == classify_mod.RATE_LIMIT:
            QuotaRouter(config_path, clock=lambda: now).mark_rate_limited({"account": name, "bucket": "all"}, now=now)
        _append_log(log_path, record)
        results.append(record)
    return {"enabled": bool((config.get("clock") or {}).get("heartbeat_enabled")),
            "due": [x["account"] for x in due], "calls": len(results), "results": results}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.quota_heartbeat")
    p.add_argument("--config", default=str(DEFAULT_QUOTA_STATE))
    p.add_argument("--state", default=str(DEFAULT_HEARTBEAT_STATE))
    p.add_argument("--log", default=str(DEFAULT_HEARTBEAT_LOG))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    print(json.dumps(run_once(config_path=args.config, heartbeat_state_path=args.state,
                              log_path=args.log, dry_run=args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
