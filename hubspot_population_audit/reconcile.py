"""Independent reconciliation of recorded portal totals against the raw
extraction, per object type.

This module never trusts a single number: it recomputes the raw record
count and the unique-ID count directly from the streamed snapshot, and
only calls an object type "reconciled" when there is an independently
recorded portal total *and* it matches the unique-ID count exactly. When
no independent portal total was recorded, the object type is reported as
unreconciled with an explicit note -- never silently treated as passing.
"""
from __future__ import annotations

from typing import Optional

from .snapshot import Snapshot


def reconcile_object(snapshot: Snapshot, object_type: str) -> dict:
    seen = set()
    raw_record_count = 0
    duplicate_count = 0
    for record in snapshot.iter_records(object_type):
        raw_record_count += 1
        record_id = record.get("id")
        if record_id in seen:
            duplicate_count += 1
        else:
            seen.add(record_id)
    unique_id_count = len(seen)

    portal_totals, totals_source = snapshot.portal_totals()
    recorded_portal_total: Optional[int] = portal_totals.get(object_type)
    portal_total_recorded = recorded_portal_total is not None

    delta: Optional[int] = None
    notes = []

    if portal_total_recorded:
        delta = recorded_portal_total - unique_id_count
        reconciled = delta == 0
        if not reconciled:
            notes.append(
                f"Recorded portal total ({recorded_portal_total}) differs from unique "
                f"raw IDs ({unique_id_count}) by {delta}; investigate pagination "
                "completeness, trailing writes during extraction, or a stale total."
            )
    else:
        reconciled = False
        notes.append(
            f"No independently recorded portal total was found for '{object_type}' in "
            "this snapshot's metadata (checked portal_totals.json and "
            "run_status.json). Reconciliation is conservatively marked "
            "unreconciled rather than assumed to pass."
        )

    if duplicate_count:
        notes.append(
            f"{duplicate_count} duplicate ID occurrence(s) found within the raw "
            f"extraction for '{object_type}'."
        )

    return {
        "object_type": object_type,
        "recorded_portal_total": recorded_portal_total,
        "portal_total_source": totals_source if portal_total_recorded else None,
        "raw_record_count": raw_record_count,
        "unique_id_count": unique_id_count,
        "duplicate_count": duplicate_count,
        "delta": delta,
        "reconciled": reconciled,
        "notes": notes,
    }


def reconcile_all(snapshot: Snapshot, object_types) -> dict:
    return {object_type: reconcile_object(snapshot, object_type) for object_type in object_types}
