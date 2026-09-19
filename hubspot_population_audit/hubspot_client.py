"""Strictly read-only HubSpot client for targeted classification-evidence
lookups (associations, activity recency, etc.).

Reuses the retry/auth pattern from ``hubspot_audit.hubspot_client.LiveHubSpotReadClient``.
The safety property added here is stronger than "no write method exists":
even the generic ``post`` entry point validates the request path against an
explicit allowlist of read-shaped endpoints (``/search`` and ``/batch/read``)
*before* any HTTP request is attempted, and ``put``/``patch``/``delete`` are
defined only to raise immediately. There is no code path in this module that
can reach ``requests.put``, ``requests.patch``, ``requests.delete``, or a
non-allowlisted ``requests.post``.

The token is read once from an environment variable and is never logged,
printed, or written to any output artifact -- only the *name* of the
environment variable is ever stored.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

_READ_POST_PATH_RE = re.compile(r"(/search$|/batch/read(/|$))")


class WriteOperationBlocked(RuntimeError):
    """Raised when code attempts a HubSpot write-shaped call. No HTTP request
    is made before this is raised -- see ``_assert_read_path`` and the
    ``put``/``patch``/``delete`` guards below."""


def is_read_path(path: str) -> bool:
    """True for GET-equivalent POST paths only: search and batch/read.

    Deliberately an allowlist, not a blocklist, so a new write-shaped
    endpoint added to HubSpot in the future is blocked by default rather
    than silently permitted.
    """
    return bool(_READ_POST_PATH_RE.search(path))


def _assert_read_path(path: str) -> None:
    if not is_read_path(path):
        raise WriteOperationBlocked(
            f"Refusing POST to non-read path {path!r}; only paths ending in "
            "'/search' or containing '/batch/read' are permitted."
        )


class LookupCache:
    """Simple on-disk cache/checkpoint for targeted lookups, so repeated runs
    against the same snapshot don't re-issue the same read. Stores only
    request keys and response payloads -- never tokens or headers."""

    def __init__(self, output_dir: str, filename: str = "lookup_cache.json"):
        self.path = os.path.join(output_dir, filename)
        self._data: dict = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                try:
                    self._data = json.load(fh)
                except json.JSONDecodeError:
                    self._data = {}

    def get(self, key: str):
        entry = self._data.get(key)
        return entry["value"] if entry is not None else None

    def has(self, key: str) -> bool:
        return key in self._data

    def set(self, key: str, value) -> None:
        self._data[key] = {"value": value}
        self._flush()

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp_path, self.path)

    def __len__(self) -> int:
        return len(self._data)


class ReadOnlyHubSpotClient:
    """Read-only live HubSpot client: GET anywhere, POST only to /search or
    /batch/read endpoints. Every other verb is blocked before any request
    is attempted.
    """

    def __init__(
        self,
        base_url: str = "https://api.hubapi.com",
        token_env_var: str = "HUBSPOT_ACCESS_TOKEN",
        timeout_seconds: int = 30,
        max_retries: int = 3,
        cache: Optional[LookupCache] = None,
    ):
        self.base_url = base_url
        self.token_env_var = token_env_var
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.cache = cache
        token = os.environ.get(token_env_var)
        if not token:
            raise RuntimeError(
                f"No live token in env var {token_env_var}; targeted live "
                "lookups are not required to build, test, or fixture-run "
                "this package."
            )
        self._token = token  # in-memory only, never logged

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _request_with_retry(self, method_name: str, path: str, **kwargs) -> dict:
        import requests  # local import: optional dependency, live lookups only

        method = getattr(requests, method_name)
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            try:
                resp = method(url, headers=self._headers(), timeout=self.timeout_seconds, **kwargs)
            except requests.RequestException:
                if attempt < self.max_retries:
                    time.sleep(max(0.25, min(2 ** attempt, 8)))
                    continue
                raise
            # Only 429 (rate limit) and 5xx (transient server errors) are
            # retried. Any other 4xx (e.g. 401/403 for a missing scope) is
            # not retryable -- raise immediately, on the first attempt, so a
            # denied endpoint costs exactly one request rather than
            # ``max_retries`` pointless ones with backoff.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 8)
                except ValueError:
                    delay = min(2 ** attempt, 8)
                time.sleep(max(0.25, delay))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("HubSpot request failed without a response")

    # -- allowed read operations -----------------------------------------
    def get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET is always read-shaped; no path validation needed."""
        cache_key = f"GET {path} {json.dumps(params or {}, sort_keys=True)}"
        if self.cache is not None and self.cache.has(cache_key):
            return self.cache.get(cache_key)
        result = self._request_with_retry("get", path, params=params or {})
        if self.cache is not None:
            self.cache.set(cache_key, result)
        return result

    def post(self, path: str, json_body: Optional[dict] = None) -> dict:
        """POST is only permitted to /search or /batch/read paths. The path
        is validated *before* any request is constructed or sent."""
        _assert_read_path(path)
        cache_key = f"POST {path} {json.dumps(json_body or {}, sort_keys=True)}"
        if self.cache is not None and self.cache.has(cache_key):
            return self.cache.get(cache_key)
        result = self._request_with_retry("post", path, json=json_body or {})
        if self.cache is not None:
            self.cache.set(cache_key, result)
        return result

    def search(self, object_type: str, body: dict) -> dict:
        return self.post(f"/crm/v3/objects/{object_type}/search", body)

    def batch_read(self, object_type: str, body: dict) -> dict:
        return self.post(f"/crm/v3/objects/{object_type}/batch/read", body)

    def associations_batch_read(self, from_object_type: str, to_object_type: str, ids) -> dict:
        """POST /crm/v4/associations/{from}/{to}/batch/read -- a read-shaped
        endpoint (path ends in '/batch/read', so it is covered by the same
        allowlist as ``batch_read``); no separate validation is needed."""
        body = {"inputs": [{"id": record_id} for record_id in ids]}
        return self.post(f"/crm/v4/associations/{from_object_type}/{to_object_type}/batch/read", body)

    # -- explicitly blocked write-shaped operations -----------------------
    def put(self, *args, **kwargs):
        raise WriteOperationBlocked("PUT is never permitted by ReadOnlyHubSpotClient.")

    def patch(self, *args, **kwargs):
        raise WriteOperationBlocked("PATCH is never permitted by ReadOnlyHubSpotClient.")

    def delete(self, *args, **kwargs):
        raise WriteOperationBlocked("DELETE is never permitted by ReadOnlyHubSpotClient.")
