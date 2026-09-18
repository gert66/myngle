"""Streaming, read-only loader over an immutable HubSpot extraction snapshot.

On-disk layout, verified read-only against the live snapshot at
``/home/myngle/hubspot-audit-live-20260918-r2`` (see ``README.md`` under
"Snapshot layout" for the full evidence trail):

    <snapshot_dir>/
        raw/
            companies.jsonl        one JSON envelope per line:
                                    {"record": {"id": ..., "properties": {...}},
                                     "extracted_at": ..., "page_index": ...}
                                    -- properties carry ONLY the default set
                                    (createdate, domain, hs_lastmodifieddate,
                                    hs_object_id, name); no associations, no
                                    hs_object_source/lifecyclestage/owner.
            contacts.jsonl          same envelope shape; default properties
                                    only (createdate, email, firstname,
                                    lastname, hs_object_id, lastmodifieddate).
            deals.jsonl             OPTIONAL and absent in the live snapshot
                                    (the deals probe failed and
                                    properties_deals returned 403).
            <activity>.jsonl       calls/meetings/emails/notes/tasks, same
                                    shape, optional -- not present in the
                                    live snapshot either.
            owners.jsonl           {"record": {...}, "extracted_at": ...}
            properties_<object>.json   {"properties": [...], "extracted_at": ...}
            _checkpoint.json       per-object pagination/completeness metadata;
                                    the live snapshot's companies/contacts
                                    extractions both report "resumed": true,
                                    so duplicate envelopes are possible and
                                    callers must dedupe by id (see
                                    ``reconcile.py`` / ``cohorts.py``).
        portal_totals.json         OPTIONAL: {"companies": <int>, "contacts": <int>, ...}
                                    an independently recorded HubSpot-reported
                                    total, used as the reconciliation
                                    baseline. Absent in the live snapshot for
                                    every object type -- treated as an
                                    explicit gap, never guessed.
        run_status.json            OPTIONAL; in the live snapshot this file
                                    exists but carries no "portal_totals" key.

Every iteration method streams the underlying JSONL file line by line and
never loads a whole object type into memory at once.
"""
from __future__ import annotations

import json
import os
from typing import Iterator, Optional

POPULATION_OBJECT_TYPES = ("companies", "contacts", "deals")

_PORTAL_TOTALS_CANDIDATES = (
    "portal_totals.json",
    os.path.join("raw", "portal_totals.json"),
)


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str) -> Iterator[dict]:
    """Stream a JSONL file one line at a time. Yields nothing if the file is absent."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


class Snapshot:
    """Read-only view over an immutable extraction snapshot directory.

    Never writes to, moves, or deletes anything under ``root``.
    """

    def __init__(self, root: str):
        self.root = root
        self.raw_dir = os.path.join(root, "raw")

    # -- discovery -----------------------------------------------------
    def discover_object_types(self) -> list:
        """Return the population object types actually present in this snapshot,
        preserving the canonical order for the ones we know about."""
        present = []
        for object_type in POPULATION_OBJECT_TYPES:
            if os.path.exists(os.path.join(self.raw_dir, f"{object_type}.jsonl")):
                present.append(object_type)
        return present

    # -- raw record access ----------------------------------------------
    def raw_path(self, object_type: str) -> str:
        return os.path.join(self.raw_dir, f"{object_type}.jsonl")

    def iter_raw_envelopes(self, object_type: str) -> Iterator[dict]:
        return read_jsonl(self.raw_path(object_type))

    def iter_records(self, object_type: str) -> Iterator[dict]:
        """Yield the normalized ``record`` dict (id + properties) for each
        envelope, streaming -- never materializing the full object list."""
        for envelope in self.iter_raw_envelopes(object_type):
            record = envelope.get("record", envelope)
            yield record

    def count_raw_records(self, object_type: str) -> int:
        count = 0
        for _ in self.iter_raw_envelopes(object_type):
            count += 1
        return count

    # -- metadata ---------------------------------------------------------
    def checkpoint(self) -> dict:
        return _read_json(os.path.join(self.raw_dir, "_checkpoint.json")) or {}

    def properties(self, object_type: str) -> list:
        payload = _read_json(os.path.join(self.raw_dir, f"properties_{object_type}.json")) or {}
        return payload.get("properties", [])

    def portal_totals(self) -> tuple:
        """Return ``(totals: dict, source: Optional[str])``.

        Looks for an independently recorded HubSpot portal total in a small,
        explicit set of candidate locations. Returns an empty dict (and
        ``source=None``) if none is found -- this is a legitimate, expected
        outcome for a snapshot that never captured one, and callers must
        treat that as an explicit gap, not silently substitute the raw
        record count (which would make reconciliation circular).
        """
        for relative in _PORTAL_TOTALS_CANDIDATES:
            path = os.path.join(self.root, relative)
            payload = _read_json(path)
            if payload:
                return payload, relative
        run_status = _read_json(os.path.join(self.root, "run_status.json"))
        if run_status and isinstance(run_status.get("portal_totals"), dict):
            return run_status["portal_totals"], "run_status.json:portal_totals"
        return {}, None
