"""Deal deterministic checks: missing company/contact/owner, stage/pipeline
use, stale open deals, suspicious dates/statuses.

"No next action" is recorded as a data gap here (it needs the activities
analysis to cross-reference) and finished in cross_object.py.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from ..normalization import iter_normalized
from .companies import _parse_date


def analyze_deals(normalized_dir: str, thresholds) -> dict:
    total = 0
    missing_company = 0
    missing_contact = 0
    missing_owner = 0
    stale_open_ids = []
    suspicious_date_ids = []
    pipeline_stage_counts = defaultdict(Counter)
    now = datetime.now(timezone.utc)
    open_deal_ids = []

    for deal in iter_normalized(normalized_dir, "deals"):
        total += 1
        did = deal["id"]
        if not deal.get("company_ids"):
            missing_company += 1
        if not deal.get("contact_ids"):
            missing_contact += 1
        if not deal.get("owner_id"):
            missing_owner += 1

        pipeline = deal.get("pipeline") or "(no pipeline)"
        stage = deal.get("dealstage") or "(no stage)"
        pipeline_stage_counts[pipeline][stage] += 1

        created = _parse_date(deal.get("createdate"))
        closed = _parse_date(deal.get("closedate"))
        if created and closed and closed < created:
            suspicious_date_ids.append(did)

        if not deal.get("is_closed"):
            open_deal_ids.append(did)
            modified = _parse_date(deal.get("hs_lastmodifieddate")) or created
            if modified and (now - modified).days > thresholds.stale_open_deal_days:
                stale_open_ids.append(did)

    return {
        "metrics": {
            "total_deals": total,
            "missing_company_association": missing_company,
            "missing_contact_association": missing_contact,
            "missing_owner": missing_owner,
            "open_deals": len(open_deal_ids),
            "stale_open_deals": len(stale_open_ids),
            "suspicious_dates": len(suspicious_date_ids),
            "pipelines_in_use": len(pipeline_stage_counts),
        },
        "signals": {
            "stale_open_deal_ids": stale_open_ids[:200],
            "suspicious_date_ids": suspicious_date_ids[:200],
            "pipeline_stage_breakdown": {
                pipeline: dict(stages) for pipeline, stages in pipeline_stage_counts.items()
            },
        },
        "open_deal_ids": open_deal_ids,  # consumed by cross_object.py, not written raw to reports
    }
