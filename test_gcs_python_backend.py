"""Tests for gcs_python_backend — the google-cloud-storage fallback used on
hosts without the gcloud/gsutil CLI (Streamlit Community Cloud).

The google-cloud-storage library is never imported here: everything runs
against a fake client, so the suite passes on machines without the package
installed. Integration tests at the bottom prove rescore_from_gcs routes
through this backend when no CLI tool is on PATH.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import gcs_python_backend
from gcs_python_backend import (
    download_file,
    download_files_batch,
    list_country_folders,
    list_files,
    upload_file,
)
from rescore_from_gcs import (
    CURRENT_MANIFEST_FILENAME,
    LIST_FILENAME,
)


# ---------------------------------------------------------------------------
# Fake google-cloud-storage client
# ---------------------------------------------------------------------------


class _FakeBlobItem:
    def __init__(self, name: str):
        self.name = name


class _FakeListing:
    """Mimics the HTTPIterator list_blobs returns: iterable blob items plus
    a .prefixes set (the 'folders' collapsed by the delimiter)."""

    def __init__(self, items, prefixes):
        self._items = items
        self.prefixes = prefixes

    def __iter__(self):
        return iter(self._items)


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name
        self.cache_control = None

    def download_to_filename(self, path):
        if self.name not in self._store:
            raise FileNotFoundError(f"gs blob not found: {self.name}")
        Path(path).write_bytes(self._store[self.name])

    def upload_from_filename(self, path, content_type=None):
        self._store[self.name] = Path(path).read_bytes()
        self._store.setdefault("__meta__", {})[self.name] = {
            "cache_control": self.cache_control, "content_type": content_type,
        }


class _FakeBucket:
    def __init__(self, store: dict):
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


class FakeClient:
    """In-memory single-project fake: ``stores[bucket_name] = {blob_name: bytes}``."""

    def __init__(self, stores: dict):
        self._stores = stores

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._stores.setdefault(name, {}))

    def list_blobs(self, bucket, prefix=None, delimiter=None):
        store = self._stores.get(bucket, {})
        prefix = prefix or ""
        items, prefixes = [], set()
        for name in sorted(store):
            if name == "__meta__" or not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            if delimiter and delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                items.append(_FakeBlobItem(name))
        return _FakeListing(items, prefixes)


def _patched_client(fake: "FakeClient | None"):
    return patch("gcs_python_backend.get_client", return_value=fake)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListCountryFolders:
    def test_returns_top_level_prefixes_only(self):
        fake = FakeClient({"bucket-a": {
            "brazil/current/companies.list.json": b"[]",
            "italy/current/companies.list.json": b"[]",
            "countries.index.json": b"{}",
        }})
        with _patched_client(fake):
            assert list_country_folders("bucket-a") == ["brazil", "italy"]

    def test_no_client_returns_empty(self):
        with _patched_client(None):
            assert list_country_folders("bucket-a") == []

    def test_listing_error_returns_empty(self):
        class Boom:
            def list_blobs(self, *a, **k):
                raise RuntimeError("network down")
        with _patched_client(Boom()):
            assert list_country_folders("bucket-a") == []


class TestListFiles:
    def test_returns_direct_children_basenames_only(self):
        fake = FakeClient({"bucket-a": {
            "brazil/current/companies.list.json": b"[]",
            "brazil/current/company-details-000.json": b"{}",
            "brazil/current/export_manifest.json": b"{}",
            "brazil/runs/run1/companies.list.json": b"[]",
        }})
        with _patched_client(fake):
            assert list_files("bucket-a", "brazil/current") == [
                "companies.list.json", "company-details-000.json",
                "export_manifest.json",
            ]

    def test_trailing_slash_in_prefix_is_normalized(self):
        fake = FakeClient({"bucket-a": {"brazil/current/f.json": b"{}"}})
        with _patched_client(fake):
            assert list_files("bucket-a", "brazil/current/") == ["f.json"]

    def test_no_client_returns_empty(self):
        with _patched_client(None):
            assert list_files("bucket-a", "brazil/current") == []


# ---------------------------------------------------------------------------
# Download / upload result shapes
# ---------------------------------------------------------------------------


class TestDownloadFile:
    def test_success_writes_file(self, tmp_path):
        fake = FakeClient({"bucket-a": {"brazil/current/f.json": b'{"x": 1}'}})
        local = tmp_path / "f.json"
        with _patched_client(fake):
            result = download_file("bucket-a", "brazil/current/f.json", str(local))
        assert result["success"] is True
        assert result["source"] == "gs://bucket-a/brazil/current/f.json"
        assert json.loads(local.read_text()) == {"x": 1}

    def test_missing_blob_fails_without_raising(self, tmp_path):
        fake = FakeClient({"bucket-a": {}})
        with _patched_client(fake):
            result = download_file("bucket-a", "brazil/current/nope.json",
                                   str(tmp_path / "nope.json"))
        assert result["success"] is False
        assert "nope.json" in result["error"]

    def test_no_client_fails_with_clear_error(self, tmp_path):
        with _patched_client(None):
            result = download_file("bucket-a", "b/f.json", str(tmp_path / "f.json"))
        assert result["success"] is False
        assert "credentials" in result["error"].lower() or "client" in result["error"].lower()


class TestDownloadFilesBatch:
    def test_downloads_each_blob_under_its_basename(self, tmp_path):
        fake = FakeClient({"bucket-a": {
            "brazil/current/a.json": b"1", "brazil/current/b.json": b"2",
        }})
        with _patched_client(fake):
            result = download_files_batch(
                "bucket-a", ["brazil/current/a.json", "brazil/current/b.json"], tmp_path)
        assert result["success"] is True
        assert (tmp_path / "a.json").read_bytes() == b"1"
        assert (tmp_path / "b.json").read_bytes() == b"2"

    def test_empty_sources_is_a_no_op(self, tmp_path):
        with _patched_client(None):
            assert download_files_batch("bucket-a", [], tmp_path)["success"] is True

    def test_first_failure_stops_and_reports(self, tmp_path):
        fake = FakeClient({"bucket-a": {"brazil/current/a.json": b"1"}})
        with _patched_client(fake):
            result = download_files_batch(
                "bucket-a", ["brazil/current/missing.json", "brazil/current/a.json"],
                tmp_path)
        assert result["success"] is False
        assert "missing.json" in result["error"]


class TestUploadFile:
    def test_missing_local_file_fails_before_client(self, tmp_path):
        with _patched_client(None):
            result = upload_file("bucket-a", str(tmp_path / "nope.json"), "b/f.json")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_success_stores_blob_and_cache_control(self, tmp_path):
        store: dict = {}
        fake = FakeClient({"bucket-a": store})
        local = tmp_path / "companies.list.json"
        local.write_text("[]")
        with _patched_client(fake):
            result = upload_file(
                "bucket-a", str(local), "brazil/current/companies.list.json",
                cache_control="no-cache, max-age=0, must-revalidate")
        assert result["success"] is True
        assert result["destination"] == "gs://bucket-a/brazil/current/companies.list.json"
        assert store["brazil/current/companies.list.json"] == b"[]"
        meta = store["__meta__"]["brazil/current/companies.list.json"]
        assert meta["cache_control"] == "no-cache, max-age=0, must-revalidate"
        assert meta["content_type"] == "application/json"

    def test_no_client_fails_with_clear_error(self, tmp_path):
        local = tmp_path / "f.json"
        local.write_text("{}")
        with _patched_client(None):
            result = upload_file("bucket-a", str(local), "b/f.json")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


class TestServiceAccountInfo:
    def test_env_var_json_is_parsed(self, monkeypatch):
        monkeypatch.setenv(
            gcs_python_backend.SERVICE_ACCOUNT_ENV_VAR,
            '{"type": "service_account", "project_id": "p1"}')
        info = gcs_python_backend._service_account_info()
        assert info == {"type": "service_account", "project_id": "p1"}

    def test_invalid_env_var_json_returns_none(self, monkeypatch):
        monkeypatch.setenv(gcs_python_backend.SERVICE_ACCOUNT_ENV_VAR, "not-json")
        assert gcs_python_backend._service_account_info() is None

    def test_nothing_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv(gcs_python_backend.SERVICE_ACCOUNT_ENV_VAR, raising=False)
        assert gcs_python_backend._service_account_info() is None


# ---------------------------------------------------------------------------
# Integration: rescore_from_gcs routes through this backend when no CLI tool
# is on PATH — the Streamlit Community Cloud situation.
# ---------------------------------------------------------------------------


def _fixture_stores() -> dict:
    detail_bucket = {
        "company-a": {"company_id": "company-a", "commercial_fit_score": 7.0,
                      "commercial_tier": "🥇 Hot", "assigned_cold_caller": "Vanessa",
                      "assigned_cold_caller_rank": 1},
        "company-b": {"company_id": "company-b", "commercial_fit_score": 5.0,
                      "commercial_tier": "🥉 Cool", "assigned_cold_caller": "Vanessa",
                      "assigned_cold_caller_rank": 2},
    }
    list_items = [
        {"company_id": cid, "commercial_fit_score": d["commercial_fit_score"],
         "commercial_tier": d["commercial_tier"],
         "assigned_cold_caller": d["assigned_cold_caller"],
         "assigned_cold_caller_rank": d["assigned_cold_caller_rank"]}
        for cid, d in detail_bucket.items()
    ]
    manifest = {"generated_at": "2026-07-01T00:00:00Z", "export_country": "Brazil"}
    prefix = "brazil/current"
    return {"bucket-a": {
        f"{prefix}/{LIST_FILENAME}": json.dumps(list_items).encode(),
        f"{prefix}/company-details-000.json": json.dumps(detail_bucket).encode(),
        f"{prefix}/{CURRENT_MANIFEST_FILENAME}": json.dumps(manifest).encode(),
    }}


def _no_cli():
    return patch("rescore_from_gcs.resolve_gcs_tool", return_value=None)


class TestRescoreFromGcsFallback:
    def test_list_country_folders_falls_back(self):
        from rescore_from_gcs import list_country_folders as rescore_list
        with _no_cli(), _patched_client(FakeClient(_fixture_stores())):
            assert rescore_list("bucket-a") == ["brazil"]

    def test_list_current_files_falls_back(self):
        from rescore_from_gcs import list_current_files
        with _no_cli(), _patched_client(FakeClient(_fixture_stores())):
            files = list_current_files("bucket-a", "brazil")
        assert set(files) == {
            LIST_FILENAME, "company-details-000.json", CURRENT_MANIFEST_FILENAME}

    def test_download_current_run_falls_back(self, tmp_path):
        from rescore_from_gcs import download_current_run
        with _no_cli(), _patched_client(FakeClient(_fixture_stores())):
            current = download_current_run("bucket-a", "brazil", tmp_path / "work")
        assert current["manifest"]["export_country"] == "Brazil"
        assert len(current["list_items"]) == 2
        assert set(current["detail_files"]["company-details-000.json"]) == {
            "company-a", "company-b"}

    def test_upload_run_falls_back(self, tmp_path):
        from rescore_from_gcs import upload_rescored_run
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / LIST_FILENAME).write_text("[]")
        (out_dir / CURRENT_MANIFEST_FILENAME).write_text("{}")
        stores = _fixture_stores()
        with _no_cli(), _patched_client(FakeClient(stores)):
            results = upload_rescored_run(out_dir, "bucket-a", "brazil", "run1")
        assert all(r["success"] for r in results)
        assert f"brazil/runs/run1/{LIST_FILENAME}" in stores["bucket-a"]
        assert {r["destination"] for r in results} == {
            f"gs://bucket-a/brazil/runs/run1/{LIST_FILENAME}",
            f"gs://bucket-a/brazil/runs/run1/{CURRENT_MANIFEST_FILENAME}",
        }

    def test_promote_run_to_current_falls_back_and_stamps_manifest(self, tmp_path):
        from rescore_from_gcs import CURRENT_CACHE_CONTROL, promote_run_to_current
        stores = _fixture_stores()
        run_prefix = "brazil/runs/2026-07-13_reallocate"
        store = stores["bucket-a"]
        store[f"{run_prefix}/{LIST_FILENAME}"] = json.dumps(
            [{"company_id": "company-a", "assigned_cold_caller": "Ernie"}]).encode()
        store[f"{run_prefix}/{CURRENT_MANIFEST_FILENAME}"] = json.dumps(
            {"run_folder": "2026-07-13_reallocate"}).encode()

        with _no_cli(), _patched_client(FakeClient(stores)):
            result = promote_run_to_current(
                "bucket-a", "brazil", "2026-07-13_reallocate")

        assert all(r["success"] for r in result["results"])
        promoted_list = json.loads(store[f"brazil/current/{LIST_FILENAME}"])
        assert promoted_list[0]["assigned_cold_caller"] == "Ernie"
        promoted_manifest = json.loads(
            store[f"brazil/current/{CURRENT_MANIFEST_FILENAME}"])
        assert promoted_manifest["promoted_to_current"] is True
        assert promoted_manifest["promoted_from_run_folder"] == "2026-07-13_reallocate"
        # current/ objects must carry the no-cache header via this path too.
        meta = store["__meta__"][f"brazil/current/{LIST_FILENAME}"]
        assert meta["cache_control"] == CURRENT_CACHE_CONTROL

    def test_no_backend_available_raises_clear_error(self, tmp_path):
        from rescore_from_gcs import download_current_run
        with _no_cli(), _patched_client(None):
            with pytest.raises(RuntimeError, match="gcp_service_account"):
                download_current_run("bucket-a", "brazil", tmp_path / "work")
