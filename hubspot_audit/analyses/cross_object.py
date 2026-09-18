"""Cross-object checks that need output from more than one single-object
analysis: open deals without recent activity, ownership/usage attribution.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from ..normalization import iter_normalized
from .companies import _parse_date


def open_deals_without_recent_activity(deals_result: dict, activities_result: dict, thresholds) -> dict:
    most_recent = activities_result["most_recent_activity_by_deal"]
    now = datetime.now(timezone.utc)
    no_activity_ids = []
    for deal_id in deals_result["open_deal_ids"]:
        last = most_recent.get(deal_id)
        if last is None or (now - last).days > thresholds.stale_open_deal_days:
            no_activity_ids.append(deal_id)
    return {
        "metrics": {"open_deals_without_recent_activity": len(no_activity_ids)},
        "signals": {"open_deal_ids_without_recent_activity": no_activity_ids[:200]},
    }


def ownership_usage(normalized_dir: str, owners: list) -> dict:
    owner_names = {str(o.get("id")): f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() for o in owners}
    counts = {"companies": Counter(), "contacts": Counter(), "deals": Counter()}
    for object_type in counts:
        for record in iter_normalized(normalized_dir, object_type):
            owner_id = record.get("owner_id") or "(unassigned)"
            counts[object_type][owner_id] += 1

    by_object = {}
    for object_type, counter in counts.items():
        by_object[object_type] = [
            {"owner_id": owner_id, "owner_name": owner_names.get(owner_id, "(unknown owner)"), "count": count}
            for owner_id, count in counter.most_common(50)
        ]

    return {
        "metrics": {
            "known_owners": len(owners),
            "distinct_owners_with_companies": len([k for k in counts["companies"] if k != "(unassigned)"]),
            "distinct_owners_with_contacts": len([k for k in counts["contacts"] if k != "(unassigned)"]),
            "distinct_owners_with_deals": len([k for k in counts["deals"] if k != "(unassigned)"]),
        },
        "signals": {"ownership_by_object": by_object},
    }
