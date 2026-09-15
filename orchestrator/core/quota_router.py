"""Quota-aware Claude account routing.

Selects an account/model bucket using remaining capacity, time to reset,
plan capacity, role fit and cooldowns. State is external JSON so usage
snapshots can be updated without changing jobs or code.
"""
from __future__ import annotations

import fcntl
import json
import os
import argparse
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.quota_clock import next_anchor, next_seed_anchor

DEFAULT_QUOTA_STATE = Path("/home/myngle/orchestrator/config/claude_quota.json")
DEFAULT_FABLE_ROLES = ("brain", "reviewer")


def _dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _hours_until(value, now):
    target = _dt(value)
    if target is None:
        return None
    return max((target - now).total_seconds() / 3600.0, 1.0 / 60.0)


def _bucket_for_model(model):
    return "fable" if model and "fable" in str(model).lower() else "all"


def _rollover(remaining, reset_at, period_hours, now):
    """Treat an elapsed reset as a fresh full bucket and roll its next reset forward."""
    target = _dt(reset_at)
    if target is None:
        return remaining, reset_at
    if target > now:
        return remaining, reset_at
    period = timedelta(hours=float(period_hours))
    while target <= now:
        target += period
    return 1.0, target.isoformat().replace("+00:00", "Z")



def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ready_at(account, bucket, now, policy):
    """Earliest instant this account/bucket can be considered usable again."""
    cooldown = _dt(account.get("cooldown_until"))

    session_remaining = float(account.get("session_remaining", 1.0))
    session_reset = _dt(account.get("session_reset_at"))
    if session_reset and session_reset <= now:
        session_remaining = 1.0
    session_ready = now if session_remaining > 0 else session_reset

    bucket_state = (account.get("buckets") or {}).get(bucket, {})
    weekly_remaining = bucket_state.get("remaining", account.get("weekly_remaining"))
    weekly_remaining = float(weekly_remaining) if weekly_remaining is not None else None
    weekly_reset = _dt(bucket_state.get("reset_at", account.get("weekly_reset_at")))
    if weekly_reset and weekly_reset <= now:
        weekly_remaining = 1.0
    weekly_ready = now if weekly_remaining is not None and weekly_remaining > 0 else weekly_reset

    if session_ready is None or weekly_ready is None:
        return None
    return max(now, cooldown or now, session_ready, weekly_ready)

def _pressure(remaining, reset_at, plan_factor, now):
    hours = _hours_until(reset_at, now)
    if remaining is None or hours is None or remaining <= 0:
        return 0.0
    return (float(remaining) * float(plan_factor)) / hours


def _candidate(account_name, account, bucket, role, model, now, policy):
    cooldown = _dt(account.get("cooldown_until"))
    if cooldown and cooldown > now:
        return None
    plan = float(account.get("plan_factor", 1.0))
    session_remaining, session_reset = _rollover(
        float(account.get("session_remaining", 1.0)), account.get("session_reset_at"),
        policy.get("session_period_hours", 5), now)
    if session_remaining <= 0:
        return None
    bucket_state = (account.get("buckets") or {}).get(bucket, {})
    weekly_remaining, weekly_reset = _rollover(
        bucket_state.get("remaining", account.get("weekly_remaining")),
        bucket_state.get("reset_at", account.get("weekly_reset_at")),
        policy.get("weekly_period_hours", 168), now)
    if weekly_remaining is None or float(weekly_remaining) <= 0:
        return None
    wp = _pressure(float(weekly_remaining), weekly_reset, plan, now)
    sp = _pressure(session_remaining, session_reset, plan, now)
    if wp <= 0 or sp <= 0:
        return None
    score = (wp ** 0.65) * (sp ** 0.35)
    if float(weekly_remaining) < float(policy.get("weekly_reserve", 0.15)):
        score *= 0.10
    if session_remaining < float(policy.get("session_reserve", 0.10)):
        score *= 0.20
    role_fit = ((account.get("role_fit") or {}).get(role, 1.0))
    score *= float(role_fit)
    return {
        "account": account_name, "bucket": bucket, "model": model,
        "claude_bin": account["claude_bin"], "score": score,
        "weekly_remaining": float(weekly_remaining),
        "session_remaining": session_remaining, "plan_factor": plan,
    }


def rank_routes(state, *, role, requested_model=None, now=None):
    now = now or datetime.now(timezone.utc)
    policy = state.get("policy") or {}
    fable_roles = tuple(policy.get("fable_eligible_roles", DEFAULT_FABLE_ROLES))
    routes = []
    for name, account in (state.get("accounts") or {}).items():
        if requested_model:
            bucket = _bucket_for_model(requested_model)
            c = _candidate(name, account, bucket, role, requested_model, now, policy)
            if c:
                routes.append(c)
            continue
        c = _candidate(name, account, "all", role, None, now, policy)
        if c:
            routes.append(c)
        if role in fable_roles and "fable" in (account.get("buckets") or {}):
            c = _candidate(name, account, "fable", role, "fable", now, policy)
            if c:
                routes.append(c)
    return sorted(routes, key=lambda item: item["score"], reverse=True)


class QuotaRouter:
    def __init__(self, path=DEFAULT_QUOTA_STATE, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _mutate(self, callback):
        """Serialize shared quota-state writes across concurrent jobs."""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.load()
            result = callback(state)
            tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            return result

    def select(self, role, requested_model=None):
        ranked = rank_routes(self.load(), role=role, requested_model=requested_model, now=self.clock())
        return ranked[0] if ranked else None

    def rank(self, role, requested_model=None):
        return rank_routes(self.load(), role=role, requested_model=requested_model, now=self.clock())

    def record_activity(self, route, now=None):
        """Persist that a routed account was actually contacted by Claude CLI."""
        if not route or not route.get("account"):
            return None
        now = now or self.clock()
        def mutate(state):
            account = (state.get("accounts") or {}).get(route["account"])
            if not account:
                return None
            account["last_used_at"] = _iso(now)
            state["updated_at"] = _iso(now)
            return _iso(now)
        return self._mutate(mutate)

    def mark_rate_limited(self, route, now=None):
        """Persist a session cooldown for the account that actually hit a limit."""
        if not route or not route.get("account"):
            return None
        now = now or self.clock()
        def mutate(state):
            account = (state.get("accounts") or {}).get(route["account"])
            if not account:
                return None
            reset = _dt(account.get("session_reset_at"))
            if reset is None or reset <= now:
                clock_cfg = state.get("clock") or {}
                account_clock = (clock_cfg.get("accounts") or {}).get(route["account"], {})
                local_time = account_clock.get("daily_anchor_local")
                seed_local = account_clock.get("seed_local")
                timezone_name = clock_cfg.get("timezone")
                period = float(clock_cfg.get("period_hours", (state.get("policy") or {}).get("session_period_hours", 5)))
                if seed_local and timezone_name:
                    reset = next_seed_anchor(now, seed_local=seed_local, timezone_name=timezone_name, period_hours=period)
                elif local_time and timezone_name:
                    reset = next_anchor(now, local_time=local_time, timezone_name=timezone_name, period_hours=period)
                else:
                    reset = now + timedelta(hours=period)
            account["session_remaining"] = 0.0
            account["session_reset_at"] = _iso(reset)
            account["cooldown_until"] = _iso(reset)
            account["last_used_at"] = _iso(now)
            state["updated_at"] = _iso(now)
            return _iso(reset)
        return self._mutate(mutate)

    def next_available_at(self, role, requested_model=None, now=None):
        """Earliest time any eligible Claude account/model bucket can be used."""
        now = now or self.clock()
        state = self.load()
        policy = state.get("policy") or {}
        fable_roles = tuple(policy.get("fable_eligible_roles", DEFAULT_FABLE_ROLES))
        times = []
        for account in (state.get("accounts") or {}).values():
            if requested_model:
                buckets = [_bucket_for_model(requested_model)]
            else:
                buckets = ["all"]
                if role in fable_roles and "fable" in (account.get("buckets") or {}):
                    buckets.append("fable")
            for bucket in buckets:
                ready = _ready_at(account, bucket, now, policy)
                if ready is not None:
                    times.append(ready)
        return _iso(min(times)) if times else None


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m core.quota_router")
    p.add_argument("--state", default=str(DEFAULT_QUOTA_STATE))
    p.add_argument("--role", choices=("brain", "worker", "reviewer"), default="worker")
    p.add_argument("--model")
    args = p.parse_args(argv)
    router = QuotaRouter(args.state)
    ranked = router.rank(args.role, args.model)
    if not ranked:
        print("NO_ROUTE")
        return 2
    for i, route in enumerate(ranked, 1):
        print(f"{i}. {route['account']}/{route['bucket']} model={route['model'] or 'default'} "
              f"score={route['score']:.6f} week={route['weekly_remaining']:.0%} "
              f"session={route['session_remaining']:.0%} plan={route['plan_factor']:g}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
