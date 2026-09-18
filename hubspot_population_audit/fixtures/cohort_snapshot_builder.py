"""Deterministic builder for the synthetic cohort-analysis test snapshot.

Checked in as a small generator script rather than a static fixture
directory, to keep the repository small: ``test_cohorts.py`` calls
``build_cohort_snapshot(tmp_dir)`` to materialize the snapshot into a
temporary directory at test-setup time. Running this file directly writes
the snapshot to ``./cohort_snapshot`` next to this script, for manual
inspection.

Layout matches the verified live-snapshot facts (see ``README.md``):
``raw/companies.jsonl`` / ``raw/contacts.jsonl`` with envelopes
``{"record": {"id", "properties"}, "extracted_at", "page_index"}`` and only
default properties -- no ``deals.jsonl``, no ``portal_totals.json``.

Shape of the synthetic population (all dates UTC):

* A steady baseline of 1-3 records/day from 2022-01-01 through 2024-06-30
  (``BASELINE_START``/``BASELINE_END``), with real, varied domains/emails.
* A bulk-import wave on 2023-09-12 and 2023-09-13 (``WAVE_START``/
  ``WAVE_END``): ``WAVE_COMPANY_COUNT`` companies / ``WAVE_CONTACT_COUNT``
  contacts, mostly created at one exact minute per day (a timestamp burst),
  almost all missing a domain/email, and untouched since creation.
* A moderate, genuine-looking spike on ``SPIKE_DAY`` (2023-11-15,
  ``SPIKE_COUNT`` records) with varied domains/emails and touched
  last-modified dates -- must NOT be flagged as a wave.
* A handful of records with missing or unparsable createdate (the
  "unknown" cohort) and a handful missing their last-modified property.
* One duplicated envelope per object type (same id, re-extracted), to
  exercise dedup.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

BASELINE_START = date(2022, 1, 1)
BASELINE_END = date(2024, 6, 30)

WAVE_START = date(2023, 9, 12)
WAVE_END = date(2023, 9, 13)
WAVE_COMPANY_COUNT = 300
WAVE_CONTACT_COUNT = 400
WAVE_COMPANY_BURST_MINUTE_START = "2023-09-12T08:00"
WAVE_COMPANY_BURST_MINUTE_END = "2023-09-13T09:00"
WAVE_CONTACT_BURST_MINUTE_START = "2023-09-12T08:05"
WAVE_CONTACT_BURST_MINUTE_END = "2023-09-13T09:05"
WAVE_PRESENCE_STRIDE = 10  # every Nth wave record has a domain/email
WAVE_TOUCHED_TAIL = 5  # last N wave records per day have lastmodified != createdate

SPIKE_DAY = date(2023, 11, 15)
SPIKE_COUNT = 12

MISSING_CREATEDATE_COUNT = 5
UNPARSABLE_CREATEDATE_COUNT = 3
MISSING_LASTMODIFIED_COUNT = 4


def _iso(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"


def _write_jsonl(path: str, envelopes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for envelope in envelopes:
            fh.write(json.dumps(envelope))
            fh.write("\n")


def _envelope(record: dict, extracted_at: str, page_index: int) -> dict:
    return {"record": record, "extracted_at": extracted_at, "page_index": page_index}


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def build_cohort_snapshot(root: str) -> dict:
    """Write the deterministic synthetic snapshot into ``root`` (a raw/
    directory with companies.jsonl and contacts.jsonl, no deals.jsonl, no
    portal_totals.json -- matching the verified live-snapshot layout).

    Returns a dict of facts about what was written, for tests to sanity
    check against (independent of the cohorts.py logic under test).
    """
    extracted_at = "2024-07-01T00:00:00Z"

    companies = []
    contacts = []
    company_seq = 0
    contact_seq = 0

    for day in _daterange(BASELINE_START, BASELINE_END):
        if day in (WAVE_START, WAVE_END):
            # The wave block below generates this day's full population; no
            # separate baseline contribution, so wave totals are exact.
            continue
        if day == SPIKE_DAY:
            n = SPIKE_COUNT
        else:
            n = (day.toordinal() % 3) + 1
        for i in range(n):
            company_seq += 1
            contact_seq += 1
            created = _iso(day, 9, i % 60)
            companies.append(
                {
                    "id": f"bc{company_seq}",
                    "properties": {
                        "name": f"Baseline Co {company_seq}",
                        "domain": f"baseline{company_seq % 37}.example",
                        "createdate": created,
                        "hs_lastmodifieddate": created,
                        "hs_object_id": f"bc{company_seq}",
                    },
                }
            )
            contacts.append(
                {
                    "id": f"bp{contact_seq}",
                    "properties": {
                        "firstname": f"First{contact_seq}",
                        "lastname": f"Last{contact_seq}",
                        "email": f"user{contact_seq % 37}@baseline{contact_seq % 37}.example",
                        "createdate": created,
                        "lastmodifieddate": created,
                        "hs_object_id": f"bp{contact_seq}",
                    },
                }
            )

    wave_days = [WAVE_START, WAVE_END]
    company_burst_minutes = [WAVE_COMPANY_BURST_MINUTE_START, WAVE_COMPANY_BURST_MINUTE_END]
    contact_burst_minutes = [WAVE_CONTACT_BURST_MINUTE_START, WAVE_CONTACT_BURST_MINUTE_END]
    per_day_company = WAVE_COMPANY_COUNT // 2
    per_day_contact = WAVE_CONTACT_COUNT // 2

    for day_idx, day in enumerate(wave_days):
        burst_minute = company_burst_minutes[day_idx]
        burst_hour, burst_min = (int(x) for x in burst_minute.split("T")[1].split(":"))
        for i in range(per_day_company):
            company_seq += 1
            idx_in_day = i
            has_domain = idx_in_day % WAVE_PRESENCE_STRIDE == 0
            in_burst = idx_in_day < per_day_company - 10
            if in_burst:
                created = f"{day.isoformat()}T{burst_hour:02d}:{burst_min:02d}:00Z"
            else:
                created = _iso(day, 7, idx_in_day % 60)
            touched = idx_in_day >= per_day_company - WAVE_TOUCHED_TAIL
            modified = _iso(day + timedelta(days=1), 12, 0) if touched else created
            companies.append(
                {
                    "id": f"wc{company_seq}",
                    "properties": {
                        "name": f"Bulk Import Co {company_seq}",
                        "domain": "bulk-import.example" if has_domain else None,
                        "createdate": created,
                        "hs_lastmodifieddate": modified,
                        "hs_object_id": f"wc{company_seq}",
                    },
                }
            )

        burst_minute_c = contact_burst_minutes[day_idx]
        burst_hour_c, burst_min_c = (int(x) for x in burst_minute_c.split("T")[1].split(":"))
        for i in range(per_day_contact):
            contact_seq += 1
            idx_in_day = i
            has_email = idx_in_day % WAVE_PRESENCE_STRIDE == 0
            in_burst = idx_in_day < per_day_contact - 10
            if in_burst:
                created = f"{day.isoformat()}T{burst_hour_c:02d}:{burst_min_c:02d}:00Z"
            else:
                created = _iso(day, 7, idx_in_day % 60)
            touched = idx_in_day >= per_day_contact - WAVE_TOUCHED_TAIL
            modified = _iso(day + timedelta(days=1), 12, 0) if touched else created
            contacts.append(
                {
                    "id": f"wp{contact_seq}",
                    "properties": {
                        "firstname": "Bulk",
                        "lastname": f"Import{contact_seq}",
                        "email": f"bulk{contact_seq}@bulk-import.example" if has_email else None,
                        "createdate": created,
                        "lastmodifieddate": modified,
                        "hs_object_id": f"wp{contact_seq}",
                    },
                }
            )

    for i in range(MISSING_CREATEDATE_COUNT):
        companies.append(
            {
                "id": f"ec_missing_{i}",
                "properties": {
                    "name": f"Edge Missing Createdate {i}",
                    "domain": f"edge{i}.example",
                    "hs_object_id": f"ec_missing_{i}",
                },
            }
        )
        contacts.append(
            {
                "id": f"ep_missing_{i}",
                "properties": {
                    "firstname": "Edge",
                    "lastname": f"Missing{i}",
                    "email": f"edge{i}@edge.example",
                    "hs_object_id": f"ep_missing_{i}",
                },
            }
        )
    for i in range(UNPARSABLE_CREATEDATE_COUNT):
        companies.append(
            {
                "id": f"ec_bad_{i}",
                "properties": {
                    "name": f"Edge Bad Createdate {i}",
                    "domain": f"edgebad{i}.example",
                    "createdate": "not-a-date",
                    "hs_object_id": f"ec_bad_{i}",
                },
            }
        )
        contacts.append(
            {
                "id": f"ep_bad_{i}",
                "properties": {
                    "firstname": "Edge",
                    "lastname": f"Bad{i}",
                    "email": f"edgebad{i}@edge.example",
                    "createdate": "not-a-date",
                    "hs_object_id": f"ep_bad_{i}",
                },
            }
        )
    for i in range(MISSING_LASTMODIFIED_COUNT):
        created = _iso(date(2023, 5, 1), 10, i)
        companies.append(
            {
                "id": f"ec_nomod_{i}",
                "properties": {
                    "name": f"Edge No Lastmod {i}",
                    "domain": f"edgenomod{i}.example",
                    "createdate": created,
                    "hs_object_id": f"ec_nomod_{i}",
                },
            }
        )
        contacts.append(
            {
                "id": f"ep_nomod_{i}",
                "properties": {
                    "firstname": "Edge",
                    "lastname": f"NoMod{i}",
                    "email": f"edgenomod{i}@edge.example",
                    "createdate": created,
                    "hs_object_id": f"ep_nomod_{i}",
                },
            }
        )

    company_envelopes = [_envelope(r, extracted_at, 0) for r in companies]
    contact_envelopes = [_envelope(r, extracted_at, 0) for r in contacts]
    # Duplicate the very first envelope of each object type (a resumed
    # extraction re-emitting an already-seen page), to exercise dedup.
    company_envelopes.append(_envelope(companies[0], extracted_at, 1))
    contact_envelopes.append(_envelope(contacts[0], extracted_at, 1))

    raw_dir = os.path.join(root, "raw")
    _write_jsonl(os.path.join(raw_dir, "companies.jsonl"), company_envelopes)
    _write_jsonl(os.path.join(raw_dir, "contacts.jsonl"), contact_envelopes)

    return {
        "unique_companies": len(companies),
        "unique_contacts": len(contacts),
        "raw_company_envelopes": len(company_envelopes),
        "raw_contact_envelopes": len(contact_envelopes),
        "wave_company_count": WAVE_COMPANY_COUNT,
        "wave_contact_count": WAVE_CONTACT_COUNT,
        "spike_count": SPIKE_COUNT,
        "spike_day": SPIKE_DAY.isoformat(),
        "wave_start": WAVE_START.isoformat(),
        "wave_end": WAVE_END.isoformat(),
        "unknown_createdate_count": MISSING_CREATEDATE_COUNT + UNPARSABLE_CREATEDATE_COUNT,
    }


if __name__ == "__main__":
    target = os.path.join(os.path.dirname(__file__), "cohort_snapshot")
    facts = build_cohort_snapshot(target)
    print(json.dumps(facts, indent=2))
