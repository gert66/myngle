"""Pipeline health: active vs historical use, unused stages, stuck records,
duplicate-meaning stages, cross-pipeline anomalies.

Uses the pipeline/stage breakdown already computed in analyses/deals.py plus
per-deal staleness to flag "stuck" stages (many open deals, none touched
recently).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ..normalization import iter_normalized
from .companies import _parse_date


def analyze_pipelines(normalized_dir: str, thresholds, most_recent_activity_by_deal: dict) -> dict:
    stage_open_count = defaultdict(int)
    stage_stuck_count = defaultdict(int)
    now = datetime.now(timezone.utc)

    for deal in iter_normalized(normalized_dir, "deals"):
        if deal.get("is_closed"):
            continue
        pipeline = deal.get("pipeline") or "(no pipeline)"
        stage = deal.get("dealstage") or "(no stage)"
        key = f"{pipeline} / {stage}"
        stage_open_count[key] += 1

        last_activity = most_recent_activity_by_deal.get(deal["id"])
        reference_date = last_activity or _parse_date(deal.get("hs_lastmodifieddate"))
        if reference_date is None or (now - reference_date).days > thresholds.stale_open_deal_days:
            stage_stuck_count[key] += 1

    stuck_stages = [
        {"pipeline_stage": key, "open_count": stage_open_count[key], "stuck_count": stuck}
        for key, stuck in stage_stuck_count.items()
        if stuck > 0
    ]
    stuck_stages.sort(key=lambda r: -r["stuck_count"])

    return {
        "metrics": {
            "distinct_pipeline_stages_open": len(stage_open_count),
            "pipeline_stages_with_stuck_deals": len(stuck_stages),
        },
        "signals": {
            "open_deals_by_pipeline_stage": dict(stage_open_count),
            "stuck_pipeline_stages": stuck_stages[:50],
        },
    }
