"""Contact deterministic checks: duplicate emails/persons, missing data,
orphan contacts, multi-company anomalies, stale contacts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ..normalization import iter_normalized
from .companies import _parse_date


def analyze_contacts(normalized_dir: str, thresholds) -> dict:
    total = 0
    missing_email = 0
    missing_company_association = 0
    email_groups = defaultdict(list)
    person_groups = defaultdict(list)  # (firstname, lastname) fallback dedupe
    stale_ids = []
    multi_company_ids = []
    now = datetime.now(timezone.utc)

    for contact in iter_normalized(normalized_dir, "contacts"):
        total += 1
        cid = contact["id"]
        if not contact.get("email"):
            missing_email += 1
        else:
            email_groups[contact["normalized_email"]].append(cid)

        company_ids = contact.get("company_ids") or []
        if not company_ids:
            missing_company_association += 1
        elif len(company_ids) > 1:
            multi_company_ids.append(cid)

        first = (contact.get("firstname") or "").strip().lower()
        last = (contact.get("lastname") or "").strip().lower()
        if first and last and not contact.get("email"):
            person_groups[(first, last)].append(cid)

        modified = _parse_date(contact.get("hs_lastmodifieddate"))
        if modified and (now - modified).days > thresholds.stale_contact_days:
            stale_ids.append(cid)

    duplicate_email_groups = {e: ids for e, ids in email_groups.items() if len(ids) > 1}
    duplicate_person_groups = {p: ids for p, ids in person_groups.items() if len(ids) > 1}

    return {
        "metrics": {
            "total_contacts": total,
            "missing_email": missing_email,
            "missing_company_association": missing_company_association,
            "duplicate_email_groups": len(duplicate_email_groups),
            "duplicate_email_records": sum(len(v) for v in duplicate_email_groups.values()),
            "duplicate_person_groups_no_email": len(duplicate_person_groups),
            "stale_contacts": len(stale_ids),
            "multi_company_contacts": len(multi_company_ids),
        },
        "signals": {
            "duplicate_emails": [
                {"key": e, "ids": ids, "count": len(ids)} for e, ids in list(duplicate_email_groups.items())[:200]
            ],
            "duplicate_persons_no_email": [
                {"key": f"{p[0]} {p[1]}", "ids": ids, "count": len(ids)}
                for p, ids in list(duplicate_person_groups.items())[:200]
            ],
            "stale_contact_ids": stale_ids[:200],
            "multi_company_contact_ids": multi_company_ids[:200],
        },
    }
