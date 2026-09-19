"""A ``ReadOnlyHubSpotClient``-shaped test double for ``evidence.py``
tests. Never performs network I/O -- ``batch_read``/``associations_batch_read``
are served from the canned JSON fixtures in ``evidence_responses/``,
filtered down to whatever ids were actually requested (mimicking the real
API). Every call is recorded in ``self.calls`` so tests can assert only
GET-equivalent, allowlisted paths were used.
"""
from __future__ import annotations

import json
import os

_RESPONSES_DIR = os.path.join(os.path.dirname(__file__), "evidence_responses")


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeAssociationError(Exception):
    """Raised by ``FakeReadOnlyClient.associations_batch_read`` to simulate
    a 403/404 from a live HubSpot association endpoint (e.g. deals not
    granted in the private-app scope)."""

    def __init__(self, status_code: int):
        super().__init__(f"fake HTTP {status_code}")
        self.response = _FakeResponse(status_code)


class FakeBatchReadError(Exception):
    """Raised by ``FakeReadOnlyClient.batch_read`` to simulate a 401/403
    from a live HubSpot object batch-read endpoint (e.g. a scope missing
    for a given object type)."""

    def __init__(self, status_code: int):
        super().__init__(f"fake HTTP {status_code}")
        self.response = _FakeResponse(status_code)


def _load(name: str) -> dict:
    with open(os.path.join(_RESPONSES_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class FakeReadOnlyClient:
    """Records every ``(method, path)`` it was called with, and serves
    canned batch-read / v4-association-batch-read responses. By default
    ``companies -> deals`` association lookups raise a simulated 403, to
    exercise the "deals endpoint inaccessible" gap-handling path; pass
    ``deny_deals=False`` to exercise the happy path instead.
    """

    def __init__(self, *, deny_deals: bool = True, deny_object_type: str = None, deny_object_status: int = 403):
        self.calls: list = []
        self.deny_deals = deny_deals
        self.deny_object_type = deny_object_type
        self.deny_object_status = deny_object_status
        self._batch_responses = {
            "companies": _load("companies_batch_read.json"),
            "contacts": _load("contacts_batch_read.json"),
        }
        self._association_responses = {
            ("companies", "contacts"): _load("associations_companies_contacts.json"),
            ("companies", "deals"): _load("associations_companies_deals.json"),
            ("contacts", "deals"): _load("associations_contacts_deals.json"),
        }

    def batch_read(self, object_type: str, body: dict) -> dict:
        self.calls.append(("POST", f"/crm/v3/objects/{object_type}/batch/read"))
        if self.deny_object_type == object_type:
            raise FakeBatchReadError(self.deny_object_status)
        requested_ids = {item["id"] for item in body.get("inputs", [])}
        canned = self._batch_responses.get(object_type, {"results": []})
        results = [r for r in canned.get("results", []) if r.get("id") in requested_ids]
        return {"results": results}

    def associations_batch_read(self, from_object_type: str, to_object_type: str, ids) -> dict:
        path = f"/crm/v4/associations/{from_object_type}/{to_object_type}/batch/read"
        self.calls.append(("POST", path))
        if self.deny_deals and to_object_type == "deals" and from_object_type == "companies":
            raise FakeAssociationError(403)
        requested_ids = set(ids)
        canned = self._association_responses.get((from_object_type, to_object_type), {"results": []})
        results = [r for r in canned.get("results", []) if (r.get("from") or {}).get("id") in requested_ids]
        return {"results": results}

    # -- write-shaped methods must never be reachable from evidence.py --
    def put(self, *args, **kwargs):
        raise AssertionError("FakeReadOnlyClient.put must never be called")

    def patch(self, *args, **kwargs):
        raise AssertionError("FakeReadOnlyClient.patch must never be called")

    def delete(self, *args, **kwargs):
        raise AssertionError("FakeReadOnlyClient.delete must never be called")
