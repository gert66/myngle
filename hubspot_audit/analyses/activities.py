"""Activity checks: volume, recency, owner patterns, overdue/unassigned
tasks, inconsistent logging across calls/meetings/emails/notes/tasks.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from ..normalization import iter_normalized
from .companies import _parse_date

ACTIVITY_KINDS = ("calls", "meetings", "emails", "notes", "tasks")


def analyze_activities(normalized_dir: str, thresholds) -> dict:
    volume_by_kind = Counter()
    owner_activity_counts = defaultdict(Counter)
    unassigned_by_kind = Counter()
    overdue_tasks = []
    most_recent_by_deal = {}
    now = datetime.now(timezone.utc)
    last_30d_by_kind = Counter()

    for kind in ACTIVITY_KINDS:
        for activity in iter_normalized(normalized_dir, kind):
            volume_by_kind[kind] += 1
            owner = activity.get("owner_id")
            if not owner:
                unassigned_by_kind[kind] += 1
            else:
                owner_activity_counts[owner][kind] += 1

            ts = _parse_date(activity.get("timestamp"))
            if ts and (now - ts).days <= 30:
                last_30d_by_kind[kind] += 1

            if kind == "tasks" and activity.get("overdue"):
                overdue_tasks.append(activity["id"])

            if ts:
                for deal_id in activity.get("deal_ids") or []:
                    prev = most_recent_by_deal.get(deal_id)
                    if prev is None or ts > prev:
                        most_recent_by_deal[deal_id] = ts

    return {
        "metrics": {
            "volume_by_kind": dict(volume_by_kind),
            "last_30_days_by_kind": dict(last_30d_by_kind),
            "unassigned_by_kind": dict(unassigned_by_kind),
            "overdue_tasks": len(overdue_tasks),
            "owners_with_activity": len(owner_activity_counts),
        },
        "signals": {
            "overdue_task_ids": overdue_tasks[:200],
        },
        "most_recent_activity_by_deal": most_recent_by_deal,  # consumed by cross_object.py
    }
