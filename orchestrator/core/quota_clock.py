"""Deterministic 24-hour quota anchor clock.

This module does not call Claude. It computes desired five-hour anchor moments
for each account from one daily local-time anchor. Actual quota observations
remain authoritative and can override routing; this clock only prevents idle
periods from letting the desired cadence drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _hhmm(value: str):
    hh, mm = value.split(":", 1)
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid anchor time: {value!r}")
    return h, m


def daily_anchor_utc(now: datetime, *, local_time: str, timezone_name: str):
    """Return today's configured local anchor as an aware UTC datetime."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz)
    hh, mm = _hhmm(local_time)
    local = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def surrounding_anchors(now: datetime, *, local_time: str, timezone_name: str,
                        period_hours: float = 5.0, count: int = 8):
    """Return anchors around now, spanning prior/current and upcoming cycles."""
    if period_hours <= 0:
        raise ValueError("period_hours must be > 0")
    base = daily_anchor_utc(now, local_time=local_time, timezone_name=timezone_name)
    period = timedelta(hours=period_hours)
    while base > now:
        base -= period
    while base + period <= now:
        base += period
    start = base - period
    return [start + i * period for i in range(count)]


def next_anchor(now: datetime, *, local_time: str, timezone_name: str,
                period_hours: float = 5.0):
    """Return the first configured anchor strictly after now."""
    for item in surrounding_anchors(now, local_time=local_time,
                                    timezone_name=timezone_name,
                                    period_hours=period_hours):
        if item > now:
            return item
    raise RuntimeError("unable to compute next anchor")


def due_anchor(now: datetime, *, local_time: str, timezone_name: str,
               period_hours: float = 5.0, grace_minutes: int = 3):
    """Return an anchor if now lies in its post-anchor grace window, else None."""
    grace = timedelta(minutes=grace_minutes)
    for item in surrounding_anchors(now, local_time=local_time,
                                    timezone_name=timezone_name,
                                    period_hours=period_hours):
        if item <= now < item + grace:
            return item
    return None


def seed_anchor_utc(seed_local: str, *, timezone_name: str):
    """Convert one local wall-clock seed (YYYY-MM-DDTHH:MM[:SS]) to UTC."""
    local = datetime.fromisoformat(seed_local)
    if local.tzinfo is not None:
        return local.astimezone(timezone.utc)
    return local.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


def surrounding_seed_anchors(now: datetime, *, seed_local: str, timezone_name: str,
                             period_hours: float = 5.0, count: int = 8):
    """Continuous elapsed-time anchors from one seed; never re-anchors each day."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if period_hours <= 0:
        raise ValueError("period_hours must be > 0")
    seed = seed_anchor_utc(seed_local, timezone_name=timezone_name)
    period = timedelta(hours=period_hours)
    if now < seed:
        return [seed + i * period for i in range(count)]
    n = int((now - seed) // period)
    base = seed + n * period
    start = max(seed, base - period)
    return [start + i * period for i in range(count)]


def next_seed_anchor(now: datetime, *, seed_local: str, timezone_name: str,
                     period_hours: float = 5.0):
    seed = seed_anchor_utc(seed_local, timezone_name=timezone_name)
    if now < seed:
        return seed
    period = timedelta(hours=period_hours)
    n = int((now - seed) // period) + 1
    return seed + n * period


def due_seed_anchor(now: datetime, *, seed_local: str, timezone_name: str,
                    period_hours: float = 5.0, grace_minutes: int = 3):
    seed = seed_anchor_utc(seed_local, timezone_name=timezone_name)
    if now < seed:
        return None
    period = timedelta(hours=period_hours)
    n = int((now - seed) // period)
    anchor = seed + n * period
    grace = timedelta(minutes=grace_minutes)
    return anchor if anchor <= now < anchor + grace else None
