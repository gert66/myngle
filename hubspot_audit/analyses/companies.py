"""Company deterministic checks: duplicate domains/names, missing fields,
stale records, creation clusters, parent-child inconsistencies.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from ..normalization import iter_normalized


def _parse_date(value):
    if not value:
        return None
    try:
        # HubSpot dates are millis-since-epoch strings or ISO8601
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None


def analyze_companies(normalized_dir: str, thresholds) -> dict:
    total = 0
    missing_domain = 0
    missing_name = 0
    domain_groups = defaultdict(list)
    name_groups = defaultdict(list)
    stale_ids = []
    creation_days = defaultdict(int)
    parent_ids_seen = set()
    child_to_parent = {}
    now = datetime.now(timezone.utc)

    for company in iter_normalized(normalized_dir, "companies"):
        total += 1
        cid = company["id"]
        if not company.get("domain"):
            missing_domain += 1
        else:
            domain_groups[company["normalized_domain"]].append(cid)
        name = (company.get("name") or "").strip().lower()
        if not name:
            missing_name += 1
        else:
            name_groups[name].append(cid)

        modified = _parse_date(company.get("hs_lastmodifieddate"))
        if modified and (now - modified).days > thresholds.stale_company_days:
            stale_ids.append(cid)

        created = _parse_date(company.get("createdate"))
        if created:
            creation_days[created.date().isoformat()] += 1

        parent_id = company.get("parent_company_id")
        if parent_id:
            child_to_parent[cid] = parent_id
            parent_ids_seen.add(parent_id)

    duplicate_domain_groups = {d: ids for d, ids in domain_groups.items() if len(ids) > 1}
    duplicate_name_groups = {n: ids for n, ids in name_groups.items() if len(ids) > 1}

    all_ids = set()
    for ids in domain_groups.values():
        all_ids.update(ids)
    for ids in name_groups.values():
        all_ids.update(ids)
    known_ids = all_ids
    orphan_parent_refs = [cid for cid, pid in child_to_parent.items() if pid not in known_ids and pid != cid]

    creation_bursts = [
        {"date": day, "count": count}
        for day, count in creation_days.items()
        if count >= thresholds.creation_burst_min_records
    ]

    return {
        "metrics": {
            "total_companies": total,
            "missing_domain": missing_domain,
            "missing_name": missing_name,
            "duplicate_domain_groups": len(duplicate_domain_groups),
            "duplicate_domain_records": sum(len(v) for v in duplicate_domain_groups.values()),
            "duplicate_name_groups": len(duplicate_name_groups),
            "stale_companies": len(stale_ids),
            "orphan_parent_references": len(orphan_parent_refs),
            "creation_burst_days": len(creation_bursts),
        },
        "signals": {
            "duplicate_domains": [
                {"key": d, "ids": ids, "count": len(ids)} for d, ids in list(duplicate_domain_groups.items())[:200]
            ],
            "duplicate_names": [
                {"key": n, "ids": ids, "count": len(ids)} for n, ids in list(duplicate_name_groups.items())[:200]
            ],
            "stale_company_ids": stale_ids[:200],
            "orphan_parent_reference_ids": orphan_parent_refs[:200],
            "creation_bursts": sorted(creation_bursts, key=lambda x: -x["count"])[:50],
        },
    }
