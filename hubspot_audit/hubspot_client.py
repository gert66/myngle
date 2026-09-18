"""Read-only HubSpot access.

Every concrete client below exposes only GET-shaped operations. There is no
create/update/delete/merge/archive method anywhere in this module, and
``LiveHubSpotReadClient`` only ever issues HTTP GET requests. This is the one
place in the package that is allowed to know about HTTP or file-fixture
details; everything downstream talks to the small interface defined here.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Iterator, Optional

OBJECT_TYPES = ("companies", "contacts", "deals")
ACTIVITY_TYPES = ("calls", "meetings", "emails", "notes", "tasks")


@dataclass
class Page:
    records: list
    next_cursor: Optional[str]
    fetched_at: str


class HubSpotReadClient:
    """Interface every audit code path programs against."""

    def get_page(self, object_type: str, cursor: Optional[str], page_size: int) -> Page:
        raise NotImplementedError

    def get_owners(self) -> list:
        raise NotImplementedError

    def get_properties(self, object_type: str) -> list:
        raise NotImplementedError

    def get_activity_page(self, activity_type: str, cursor: Optional[str], page_size: int) -> Page:
        raise NotImplementedError

    def capability_check(self, object_type: str) -> tuple:
        """Return (ok: bool, detail: str) for a lightweight read probe."""
        raise NotImplementedError


class FixtureHubSpotClient(HubSpotReadClient):
    """Dry-run / fixture-mode client. Reads local JSONL/JSON files only.

    Never touches the network. This is the client used by default for build
    and test purposes, and the one the CLI uses unless ``--mode live`` is
    passed explicitly together with a configured token.
    """

    def __init__(self, fixtures_dir: str):
        self.fixtures_dir = fixtures_dir
        self._cache: dict = {}

    def _load_jsonl(self, name: str) -> list:
        if name in self._cache:
            return self._cache[name]
        path = os.path.join(self.fixtures_dir, f"{name}.jsonl")
        records = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        self._cache[name] = records
        return records

    def get_page(self, object_type: str, cursor: Optional[str], page_size: int) -> Page:
        from .models import now_iso

        records = self._load_jsonl(object_type)
        start = int(cursor) if cursor else 0
        end = start + page_size
        chunk = records[start:end]
        next_cursor = str(end) if end < len(records) else None
        return Page(records=chunk, next_cursor=next_cursor, fetched_at=now_iso())

    def get_activity_page(self, activity_type: str, cursor: Optional[str], page_size: int) -> Page:
        return self.get_page(activity_type, cursor, page_size)

    def get_owners(self) -> list:
        return self._load_jsonl("owners")

    def get_properties(self, object_type: str) -> list:
        path = os.path.join(self.fixtures_dir, f"properties_{object_type}.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def capability_check(self, object_type: str) -> tuple:
        path_jsonl = os.path.join(self.fixtures_dir, f"{object_type}.jsonl")
        if os.path.exists(path_jsonl):
            return True, "fixture file present"
        return False, "no fixture file for this object"


class LiveHubSpotReadClient(HubSpotReadClient):
    """Live, read-only HubSpot client.

    Documented as a later execution prerequisite: this class is fully
    implemented so the interface is real, but nothing in this build invokes
    it, and no live token is required to build or test the package. To run a
    live audit later, set the environment variable named by
    ``config.hubspot.token_env_var`` (default ``HUBSPOT_ACCESS_TOKEN``) to a
    HubSpot private-app token scoped to read-only CRM scopes, and pass
    ``mode="live"`` to the runner.

    Only ``requests.get`` is ever called. The token is read once from the
    environment and passed as a bearer header; it is never logged, printed,
    or written to any output artifact.
    """

    _ENDPOINTS = {
        "companies": "/crm/v3/objects/companies",
        "contacts": "/crm/v3/objects/contacts",
        "deals": "/crm/v3/objects/deals",
        "calls": "/crm/v3/objects/calls",
        "meetings": "/crm/v3/objects/meetings",
        "emails": "/crm/v3/objects/emails",
        "notes": "/crm/v3/objects/notes",
        "tasks": "/crm/v3/objects/tasks",
        "owners": "/crm/v3/owners",
    }

    def __init__(self, config):
        self.config = config
        token = os.environ.get(config.hubspot.token_env_var)
        if not token:
            raise RuntimeError(
                f"Live mode requested but env var {config.hubspot.token_env_var} is not set. "
                "This is expected during build/test; live mode is a later execution prerequisite."
            )
        self._token = token  # kept only in-memory, never logged

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict) -> dict:
        import requests  # local import: optional dependency for live mode only

        url = f"{self.config.hubspot.base_url}{path}"
        last_response = None
        for attempt in range(self.config.hubspot.max_retries + 1):
            resp = requests.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self.config.hubspot.timeout_seconds,
            )
            last_response = resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                resp.raise_for_status()
                return resp.json()
            if attempt < self.config.hubspot.max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 8)
                except ValueError:
                    delay = min(2 ** attempt, 8)
                time.sleep(max(0.25, delay))
        last_response.raise_for_status()
        return last_response.json()

    def get_page(self, object_type: str, cursor: Optional[str], page_size: int) -> Page:
        from .models import now_iso

        path = self._ENDPOINTS[object_type]
        params = {"limit": page_size}
        if cursor:
            params["after"] = cursor
        data = self._get(path, params)
        next_cursor = data.get("paging", {}).get("next", {}).get("after")
        return Page(records=data.get("results", []), next_cursor=next_cursor, fetched_at=now_iso())

    def get_activity_page(self, activity_type: str, cursor: Optional[str], page_size: int) -> Page:
        return self.get_page(activity_type, cursor, page_size)

    def get_owners(self) -> list:
        data = self._get(self._ENDPOINTS["owners"], {})
        return data.get("results", [])

    def get_properties(self, object_type: str) -> list:
        data = self._get(f"/crm/v3/properties/{object_type}", {})
        return data.get("results", [])

    def capability_check(self, object_type: str) -> tuple:
        try:
            self.get_page(object_type, None, 1)
            return True, "read probe succeeded"
        except Exception as exc:  # noqa: BLE001 - surfaced as a capability gap, not raised
            return False, f"{type(exc).__name__}: probe failed"


def build_client(config) -> HubSpotReadClient:
    if config.mode == "live":
        return LiveHubSpotReadClient(config)
    fixtures_dir = config.fixtures_dir or os.path.join(os.path.dirname(__file__), "fixtures")
    return FixtureHubSpotClient(fixtures_dir)
