"""Generates the small, synthetic fixture dataset used by fixture/dry-run
mode and the test suite. Deterministic (fixed seed) so committed fixture
files are reproducible. Re-run with ``python -m hubspot_audit.fixtures.generate_fixtures``
after changing this file.

Proportions loosely mirror the provisional baseline mentioned in the audit
brief (roughly a third of companies missing a domain, roughly 40% of
contacts missing an email, a small fraction of deals missing associations)
scaled down to a size that is fast to extract/normalize/analyze in tests.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone

random.seed(20260101)

HERE = os.path.dirname(__file__)

OWNER_IDS = [str(i) for i in range(1, 6)]
OWNER_NAMES = [("Anna", "de Vries"), ("Bram", "Jansen"), ("Chiara", "Rossi"), ("Devon", "Lee"), ("Eva", "Kowalski")]


def _millis(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def _rand_date(days_back_min: int, days_back_max: int) -> datetime:
    days = random.randint(days_back_min, days_back_max)
    return datetime.now(timezone.utc) - timedelta(days=days)


def write_jsonl(name: str, records: list) -> None:
    path = os.path.join(HERE, f"{name}.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def gen_owners() -> list:
    return [
        {"id": oid, "firstName": first, "lastName": last, "email": f"{first.lower()}.{last.lower()}@myngle.com"}
        for oid, (first, last) in zip(OWNER_IDS, OWNER_NAMES)
    ]


def gen_companies(n: int) -> list:
    records = []
    domains = [f"company{i}.example.com" for i in range(1, int(n * 0.7))]
    # force some duplicate-domain groups
    duplicate_domains = random.sample(domains, k=8)

    burst_day = _rand_date(400, 400)

    for i in range(n):
        cid = str(1000 + i)
        has_domain = random.random() > 0.35  # ~35% missing domain, mirrors the provisional baseline shape
        if has_domain:
            domain = random.choice(duplicate_domains) if random.random() < 0.15 else random.choice(domains)
        else:
            domain = None
        is_burst = i < 15  # simulate a migration-style creation burst
        created = burst_day if is_burst else _rand_date(1, 900)
        props = {
            "name": f"Company {i}" if random.random() > 0.02 else "",
            "domain": domain,
            "createdate": _millis(created),
            "hs_lastmodifieddate": _millis(_rand_date(0, 500)),
            "hubspot_owner_id": random.choice(OWNER_IDS) if random.random() > 0.1 else None,
            "lifecyclestage": random.choice(["lead", "customer", "opportunity", None]),
            "hs_analytics_source": "OFFLINE" if is_burst else random.choice(["ORGANIC_SEARCH", "DIRECT_TRAFFIC", "OFFLINE"]),
        }
        associations = {}
        if i > 30 and random.random() < 0.05:
            associations["parent_company"] = {"results": [{"id": "999999"}]}  # orphan parent reference
            props["parent_company_id"] = "999999"
        records.append({"id": cid, "properties": props, "associations": associations})
    return records


def gen_contacts(n: int, company_ids: list) -> list:
    records = []
    emails = [f"person{i}@example.com" for i in range(1, int(n * 0.8))]
    duplicate_emails = random.sample(emails, k=10)

    for i in range(n):
        cid = str(2000 + i)
        has_email = random.random() > 0.40
        email = (random.choice(duplicate_emails) if random.random() < 0.12 else random.choice(emails)) if has_email else None
        props = {
            "email": email,
            "firstname": f"First{i}",
            "lastname": f"Last{i % 50}",
            "createdate": _millis(_rand_date(1, 900)),
            "hs_lastmodifieddate": _millis(_rand_date(0, 500)),
            "hubspot_owner_id": random.choice(OWNER_IDS) if random.random() > 0.1 else None,
            "lifecyclestage": random.choice(["lead", "customer", "subscriber", None]),
        }
        associations = {}
        if random.random() > 0.26:  # ~26% missing company association
            num_companies = 2 if random.random() < 0.05 else 1
            chosen = random.sample(company_ids, k=min(num_companies, len(company_ids)))
            associations["companies"] = {"results": [{"id": c} for c in chosen]}
        records.append({"id": cid, "properties": props, "associations": associations})
    return records


def gen_deals(n: int, company_ids: list, contact_ids: list) -> list:
    records = []
    pipelines = {
        "default": ["appointmentscheduled", "qualifiedtobuy", "presentationscheduled", "closedwon", "closedlost"],
        "enterprise": ["discovery", "proposal", "negotiation", "closedwon", "closedlost"],
    }
    for i in range(n):
        did = str(3000 + i)
        pipeline = random.choice(list(pipelines))
        stage = random.choice(pipelines[pipeline])
        is_closed = stage in ("closedwon", "closedlost")
        created = _rand_date(1, 600)
        closedate = _rand_date(0, 400) if random.random() > 0.5 else created - timedelta(days=5)
        props = {
            "dealname": f"Deal {i}",
            "pipeline": pipeline,
            "dealstage": stage,
            "hs_is_closed": "true" if is_closed else "false",
            "amount": round(random.uniform(500, 50000), 2) if random.random() > 0.1 else None,
            "createdate": _millis(created),
            "hs_lastmodifieddate": _millis(_rand_date(0, 200) if not is_closed and random.random() < 0.3 else _rand_date(120, 500)),
            "closedate": _millis(closedate) if is_closed else None,
            "hubspot_owner_id": random.choice(OWNER_IDS) if random.random() > 0.03 else None,
        }
        associations = {}
        if random.random() > 0.016:  # ~1.6% missing company, mirrors the provisional baseline shape
            associations["companies"] = {"results": [{"id": random.choice(company_ids)}]}
        if random.random() > 0.73:  # ~27% missing contact, close to the provisional baseline shape
            associations["contacts"] = {"results": [{"id": random.choice(contact_ids)}]}
        records.append({"id": did, "properties": props, "associations": associations})
    return records


def gen_activities(kind: str, n: int, deal_ids: list, contact_ids: list) -> list:
    records = []
    for i in range(n):
        aid = str(hash((kind, i)) % 10_000_000)
        ts = _rand_date(0, 400)
        props = {
            "hs_timestamp": _millis(ts),
            "hubspot_owner_id": random.choice(OWNER_IDS) if random.random() > 0.15 else None,
        }
        if kind == "tasks":
            props["hs_task_status"] = random.choice(["COMPLETED", "NOT_STARTED", "WAITING"])
            props["hs_task_due_date"] = _millis(_rand_date(-30, 60))
        associations = {}
        if random.random() > 0.4:
            associations["deals"] = {"results": [{"id": random.choice(deal_ids)}]}
        if random.random() > 0.5:
            associations["contacts"] = {"results": [{"id": random.choice(contact_ids)}]}
        records.append({"id": aid, "properties": props, "associations": associations})
    return records


def gen_properties(object_type: str, extra_names: list) -> list:
    base = [
        {"name": "createdate", "label": "Create Date", "type": "datetime", "groupName": "core"},
        {"name": "hs_lastmodifieddate", "label": "Last Modified", "type": "datetime", "groupName": "core"},
        {"name": "hubspot_owner_id", "label": "Owner", "type": "enumeration", "groupName": "core"},
    ]
    legacy = [
        {"name": f"{object_type}_sf_id", "label": "Salesforce ID", "type": "string", "groupName": "legacy"},
        {"name": f"{object_type}_sf_sync_status", "label": "Salesforce Sync Status", "type": "string", "groupName": "legacy"},
        {"name": "apollo_enriched", "label": "Apollo Enriched", "type": "bool", "groupName": "legacy"},
    ]
    return base + legacy + [{"name": n, "label": n, "type": "string", "groupName": "custom"} for n in extra_names]


def main() -> None:
    owners = gen_owners()
    write_jsonl("owners", owners)

    companies = gen_companies(120)
    write_jsonl("companies", companies)
    company_ids = [c["id"] for c in companies]

    contacts = gen_contacts(220, company_ids)
    write_jsonl("contacts", contacts)
    contact_ids = [c["id"] for c in contacts]

    deals = gen_deals(70, company_ids, contact_ids)
    write_jsonl("deals", deals)
    deal_ids = [d["id"] for d in deals]

    write_jsonl("calls", gen_activities("calls", 90, deal_ids, contact_ids))
    write_jsonl("meetings", gen_activities("meetings", 40, deal_ids, contact_ids))
    write_jsonl("emails", gen_activities("emails", 150, deal_ids, contact_ids))
    write_jsonl("notes", gen_activities("notes", 60, deal_ids, contact_ids))
    write_jsonl("tasks", gen_activities("tasks", 55, deal_ids, contact_ids))

    for object_type, extra in (
        ("companies", ["industry", "employee_range", "unused_custom_field_1"]),
        ("contacts", ["job_title", "unused_custom_field_2"]),
        ("deals", ["forecast_category", "unused_custom_field_3"]),
    ):
        props = gen_properties(object_type, extra)
        with open(os.path.join(HERE, f"properties_{object_type}.json"), "w", encoding="utf-8") as fh:
            json.dump(props, fh, indent=2)

    print("Fixtures written to", HERE)


if __name__ == "__main__":
    main()
