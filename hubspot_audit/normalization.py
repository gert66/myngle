"""Normalize raw HubSpot v3-shaped records into the dataclasses in models.py.

Input: raw JSONL envelopes ``{"record": {...}, "extracted_at": ...}`` where
``record`` follows the HubSpot v3 CRM object shape
(``{"id", "properties": {...}, "associations": {...}}``). Output: normalized
JSONL, one dataclass-as-dict per line, streamed so large object sets never
sit fully in memory.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterable, Optional

from .extraction import read_jsonl
from .models import Activity, Company, Contact, Deal

_DOMAIN_STRIP_RE = re.compile(r"^(https?://)?(www\.)?", re.IGNORECASE)


def normalize_domain(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip().lower()
    value = _DOMAIN_STRIP_RE.sub("", value)
    value = value.rstrip("/")
    return value or None


def normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return raw.strip().lower() or None


def _associated_ids(record: dict, assoc_key: str) -> list:
    assoc = record.get("associations", {}).get(assoc_key, {})
    results = assoc.get("results", []) if isinstance(assoc, dict) else []
    return [str(r.get("id")) for r in results if r.get("id") is not None]


def normalize_company(record: dict) -> Company:
    props = record.get("properties", {}) or {}
    domain = props.get("domain")
    return Company(
        id=str(record.get("id")),
        name=props.get("name"),
        domain=domain,
        normalized_domain=normalize_domain(domain),
        createdate=props.get("createdate"),
        hs_lastmodifieddate=props.get("hs_lastmodifieddate"),
        owner_id=props.get("hubspot_owner_id"),
        parent_company_id=props.get("parent_company_id") or props.get("associatedcompanyid"),
        lifecycle_stage=props.get("lifecyclestage"),
        source_properties=props,
    )


def normalize_contact(record: dict) -> Contact:
    props = record.get("properties", {}) or {}
    email = props.get("email")
    return Contact(
        id=str(record.get("id")),
        email=email,
        normalized_email=normalize_email(email),
        firstname=props.get("firstname"),
        lastname=props.get("lastname"),
        createdate=props.get("createdate"),
        hs_lastmodifieddate=props.get("hs_lastmodifieddate"),
        owner_id=props.get("hubspot_owner_id"),
        company_ids=_associated_ids(record, "companies"),
        lifecycle_stage=props.get("lifecyclestage"),
        source_properties=props,
    )


def normalize_deal(record: dict) -> Deal:
    props = record.get("properties", {}) or {}
    amount_raw = props.get("amount")
    try:
        amount = float(amount_raw) if amount_raw not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    return Deal(
        id=str(record.get("id")),
        dealname=props.get("dealname"),
        pipeline=props.get("pipeline"),
        dealstage=props.get("dealstage"),
        is_closed=str(props.get("hs_is_closed", "false")).lower() == "true",
        amount=amount,
        createdate=props.get("createdate"),
        hs_lastmodifieddate=props.get("hs_lastmodifieddate"),
        closedate=props.get("closedate"),
        owner_id=props.get("hubspot_owner_id"),
        company_ids=_associated_ids(record, "companies"),
        contact_ids=_associated_ids(record, "contacts"),
        source_properties=props,
    )


def normalize_activity(record: dict, kind: str) -> Activity:
    props = record.get("properties", {}) or {}
    timestamp = (
        props.get("hs_timestamp")
        or props.get("hs_meeting_start_time")
        or props.get("hs_call_timestamp")
        or props.get("createdate")
    )
    overdue = None
    if kind == "task":
        status = (props.get("hs_task_status") or "").upper()
        due = props.get("hs_task_due_date") or props.get("hs_timestamp")
        overdue = status not in ("COMPLETED",) and bool(due)
    return Activity(
        id=str(record.get("id")),
        kind=kind,
        timestamp=timestamp,
        owner_id=props.get("hubspot_owner_id"),
        deal_ids=_associated_ids(record, "deals"),
        contact_ids=_associated_ids(record, "contacts"),
        company_ids=_associated_ids(record, "companies"),
        completed=(props.get("hs_task_status") == "COMPLETED") if kind == "task" else None,
        overdue=overdue,
        source_properties=props,
    )


_NORMALIZERS = {
    "companies": ("company", normalize_company),
    "contacts": ("contact", normalize_contact),
    "deals": ("deal", normalize_deal),
}
_ACTIVITY_KINDS = {
    "calls": "call",
    "meetings": "meeting",
    "emails": "email",
    "notes": "note",
    "tasks": "task",
}


def normalize_all(raw_dir: str, normalized_dir: str) -> dict:
    os.makedirs(normalized_dir, exist_ok=True)
    summary = {}

    for object_type, (_, normalizer) in _NORMALIZERS.items():
        raw_path = os.path.join(raw_dir, f"{object_type}.jsonl")
        out_path = os.path.join(normalized_dir, f"{object_type}.jsonl")
        count = 0
        with open(out_path, "w", encoding="utf-8") as out_fh:
            for envelope in read_jsonl(raw_path):
                normalized = normalizer(envelope["record"])
                out_fh.write(json.dumps(normalized.__dict__) + "\n")
                count += 1
        summary[object_type] = count

    for object_type, kind in _ACTIVITY_KINDS.items():
        raw_path = os.path.join(raw_dir, f"{object_type}.jsonl")
        out_path = os.path.join(normalized_dir, f"{object_type}.jsonl")
        count = 0
        with open(out_path, "w", encoding="utf-8") as out_fh:
            for envelope in read_jsonl(raw_path):
                normalized = normalize_activity(envelope["record"], kind)
                out_fh.write(json.dumps(normalized.__dict__) + "\n")
                count += 1
        summary[object_type] = count

    return summary


def iter_normalized(normalized_dir: str, object_type: str) -> Iterable[dict]:
    path = os.path.join(normalized_dir, f"{object_type}.jsonl")
    return read_jsonl(path)
