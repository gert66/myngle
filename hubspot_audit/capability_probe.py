"""Read-only capability probe.

Determines, for each object type, whether the configured client can read it,
and records the downstream analysis impact of any gap. Never logs the
HubSpot token; the probe only ever calls ``client.capability_check`` /
``client.get_properties``, both of which are read-only by construction (see
hubspot_client.py).
"""
from __future__ import annotations

from .hubspot_client import ACTIVITY_TYPES, HubSpotReadClient, OBJECT_TYPES
from .models import CapabilityRecord, CapabilityStatus

DOWNSTREAM_IMPACT = {
    "companies": "Company duplicate/quality analyses cannot run.",
    "contacts": "Contact duplicate/orphan analyses cannot run.",
    "deals": "Deal/pipeline health analyses cannot run.",
    "calls": "Call activity volume/recency analyses skipped for this object.",
    "meetings": "Meeting activity volume/recency analyses skipped for this object.",
    "emails": "Email activity volume/recency analyses skipped for this object.",
    "notes": "Note logging-consistency analyses skipped for this object.",
    "tasks": "Overdue/unassigned task analyses skipped for this object.",
    "owners": "Ownership/usage attribution cannot be computed; owner_id will be shown raw.",
    "properties": "Property fill-rate and legacy-field analyses cannot run for this object.",
}


def run_capability_probe(client: HubSpotReadClient) -> dict:
    """Returns {object_name: CapabilityRecord}."""
    results: dict = {}

    for obj in OBJECT_TYPES + ACTIVITY_TYPES + ("owners",):
        try:
            ok, detail = client.capability_check(obj) if obj != "owners" else (True, "n/a")
            if obj == "owners":
                try:
                    owners = client.get_owners()
                    ok = True
                    detail = f"{len(owners)} owners read"
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    detail = f"{type(exc).__name__}: {exc}"
            status = CapabilityStatus.AVAILABLE if ok else CapabilityStatus.ENDPOINT_UNAVAILABLE
        except Exception as exc:  # noqa: BLE001
            status = CapabilityStatus.ERROR
            detail = f"{type(exc).__name__}: {exc}"
        results[obj] = CapabilityRecord(
            object_name=obj,
            status=status,
            detail=detail,
            downstream_impact=DOWNSTREAM_IMPACT.get(obj, "Unknown downstream impact."),
        )

    for obj in OBJECT_TYPES:
        try:
            props = client.get_properties(obj)
            status = CapabilityStatus.AVAILABLE if props else CapabilityStatus.PARTIALLY_AVAILABLE
            detail = f"{len(props)} property definitions read"
        except Exception as exc:  # noqa: BLE001
            status = CapabilityStatus.ERROR
            detail = f"{type(exc).__name__}: {exc}"
        results[f"properties_{obj}"] = CapabilityRecord(
            object_name=f"properties_{obj}",
            status=status,
            detail=detail,
            downstream_impact=DOWNSTREAM_IMPACT["properties"],
        )

    return results


def capability_matrix_to_dict(matrix: dict) -> dict:
    return {name: record.to_dict() for name, record in matrix.items()}
