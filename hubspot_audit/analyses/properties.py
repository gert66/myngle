"""Property inventory and fill rate: unused/near-unused fields, Salesforce
``*_sf`` fields, migration/Apollo candidates, overlapping/conflicting fields.

Fill rate is computed by streaming the normalized object once and counting,
per property name, how many records have a non-empty value in
``source_properties`` -- properties are read from the raw property
definitions file for the full inventory (including fields with zero fill).
"""
from __future__ import annotations

import json
import os
from collections import Counter

from ..normalization import iter_normalized

LEGACY_NAME_HINTS = ("_sf", "salesforce", "sfdc")
MIGRATION_NAME_HINTS = ("apollo", "import", "legacy", "migrated")


def analyze_properties(normalized_dir: str, raw_dir: str, thresholds) -> dict:
    result = {}
    for object_type in ("companies", "contacts", "deals"):
        props_path = os.path.join(raw_dir, f"properties_{object_type}.json")
        definitions = []
        if os.path.exists(props_path):
            with open(props_path, "r", encoding="utf-8") as fh:
                definitions = json.load(fh).get("properties", [])

        fill_counts = Counter()
        total = 0
        for record in iter_normalized(normalized_dir, object_type):
            total += 1
            for name, value in (record.get("source_properties") or {}).items():
                if value not in (None, ""):
                    fill_counts[name] += 1

        rows = []
        for definition in definitions:
            name = definition.get("name", "")
            filled = fill_counts.get(name, 0)
            fill_rate = (filled / total) if total else 0.0
            is_legacy = any(hint in name.lower() for hint in LEGACY_NAME_HINTS)
            is_migration_candidate = any(hint in name.lower() for hint in MIGRATION_NAME_HINTS)
            rows.append(
                {
                    "name": name,
                    "label": definition.get("label"),
                    "group": definition.get("groupName"),
                    "field_type": definition.get("type"),
                    "filled_count": filled,
                    "total_count": total,
                    "fill_rate": round(fill_rate, 4),
                    "is_legacy_salesforce_field": is_legacy,
                    "is_migration_candidate": is_migration_candidate,
                }
            )

        unused = [r for r in rows if r["fill_rate"] <= thresholds.unused_fill_rate]
        near_unused = [
            r for r in rows if thresholds.unused_fill_rate < r["fill_rate"] <= thresholds.near_unused_fill_rate
        ]
        legacy = [r for r in rows if r["is_legacy_salesforce_field"]]
        migration_candidates = [r for r in rows if r["is_migration_candidate"]]

        result[object_type] = {
            "metrics": {
                "total_properties": len(rows),
                "unused_properties": len(unused),
                "near_unused_properties": len(near_unused),
                "legacy_salesforce_properties": len(legacy),
                "migration_candidate_properties": len(migration_candidates),
            },
            "signals": {
                "unused": unused[:200],
                "near_unused": near_unused[:200],
                "legacy_salesforce": legacy[:200],
                "migration_candidates": migration_candidates[:200],
            },
        }
    return result
