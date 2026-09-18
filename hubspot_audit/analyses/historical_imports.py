"""Historical import detection: creation-date bursts, source clusters,
duplicate bursts, migration cohorts.

This module only ever proposes *candidates* for a migration cohort -- it
never asserts one is a real Salesforce/Apollo-era import. Confirmation is
left to the AI interpretation layer (ambiguous, needs evidence) or to a human
via ``needs_human_context``, per the MISSION constraint that AI/deterministic
code must not promote a hypothesis to a confirmed finding without evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..normalization import iter_normalized
from .companies import _parse_date

SOURCE_PROPERTY_CANDIDATES = (
    "hs_analytics_source",
    "hs_object_source",
    "hs_object_source_label",
    "leadsource",
    "source",
)


def _find_source_value(source_properties: dict) -> str | None:
    for name in SOURCE_PROPERTY_CANDIDATES:
        value = source_properties.get(name)
        if value:
            return f"{name}={value}"
    return None


def analyze_historical_imports(normalized_dir: str, thresholds) -> dict:
    result = {}
    for object_type in ("companies", "contacts", "deals"):
        creation_days = defaultdict(int)
        source_counts = Counter()
        day_source_counts = defaultdict(Counter)

        for record in iter_normalized(normalized_dir, object_type):
            created = _parse_date(record.get("createdate"))
            source_value = _find_source_value(record.get("source_properties") or {})
            if created:
                day = created.date().isoformat()
                creation_days[day] += 1
                if source_value:
                    day_source_counts[day][source_value] += 1
            if source_value:
                source_counts[source_value] += 1

        bursts = [
            {"date": day, "count": count}
            for day, count in creation_days.items()
            if count >= thresholds.creation_burst_min_records
        ]
        bursts.sort(key=lambda r: -r["count"])

        cohort_candidates = []
        for burst in bursts[:50]:
            day = burst["date"]
            sources_that_day = day_source_counts.get(day, Counter())
            if sources_that_day:
                dominant_source, dominant_count = sources_that_day.most_common(1)[0]
                if dominant_count / burst["count"] >= 0.6:
                    cohort_candidates.append(
                        {
                            "date": day,
                            "record_count": burst["count"],
                            "dominant_source": dominant_source,
                            "dominant_source_share": round(dominant_count / burst["count"], 2),
                        }
                    )

        result[object_type] = {
            "metrics": {
                "creation_burst_days": len(bursts),
                "migration_cohort_candidates": len(cohort_candidates),
                "distinct_source_values": len(source_counts),
            },
            "signals": {
                "creation_bursts": bursts[:50],
                "migration_cohort_candidates": cohort_candidates,
                "top_source_values": source_counts.most_common(20),
            },
        }
    return result
