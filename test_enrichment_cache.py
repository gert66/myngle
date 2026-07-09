"""Tests for the shared, GCS-backed per-country enrichment cache.

No real network/gcloud/google-cloud-storage calls: ``_storage_client``,
``resolve_gcs_upload_tool``/``upload_file``/``subprocess.run`` are mocked
throughout. Covers key normalization, TTL expiry, force_refresh, the Python
client transport, the gcloud/gsutil CLI fallback transport, and graceful
degradation when both fail (cache must always be an optimization, never a
hard dependency).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import enrichment_cache as ec


# ---------------------------------------------------------------------------
# _cache_key normalization
# ---------------------------------------------------------------------------

class TestCacheKeyNormalization:
    def test_serper_key_shape(self):
        assert ec._cache_key("serper", "acme.com", "hq") == "serper|acme.com|hq"

    def test_serper_key_strips_scheme_and_www_but_keeps_tld(self):
        assert ec._cache_key("serper", "https://www.Acme.COM/", "hq") == "serper|acme.com|hq"

    def test_serper_key_distinguishes_different_tlds(self):
        key_com = ec._cache_key("serper", "acme.com", "hq")
        key_nl = ec._cache_key("serper", "acme.nl", "hq")
        assert key_com != key_nl

    def test_serper_key_lowercases_signal_type(self):
        assert ec._cache_key("serper", "acme.com", "HQ") == "serper|acme.com|hq"

    def test_firecrawl_key_shape(self):
        assert ec._cache_key("firecrawl", "https://acme.com/about") == \
            "firecrawl|https://acme.com/about"

    def test_firecrawl_key_lowercases_and_strips_trailing_slash(self):
        assert ec._cache_key("firecrawl", "HTTPS://ACME.com/About/") == \
            "firecrawl|https://acme.com/about"

    def test_firecrawl_key_keeps_full_url_distinct_paths(self):
        k1 = ec._cache_key("firecrawl", "https://acme.com/about")
        k2 = ec._cache_key("firecrawl", "https://acme.com/careers")
        assert k1 != k2

    def test_unknown_source_never_raises(self):
        assert ec._cache_key("other", "a", "b") == "other|a|b"
        assert ec._cache_key("other") == "other"


# ---------------------------------------------------------------------------
# get_cached / put_cached — TTL expiry, force_refresh, malformed entries
# ---------------------------------------------------------------------------

class TestGetPutCached:
    def test_put_then_get_round_trips(self):
        index: dict = {}
        ec.put_cached(index, "serper", "acme.com", "hq", response={"organic": []})
        result = ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90)
        assert result == {"organic": []}

    def test_get_missing_key_returns_none(self):
        assert ec.get_cached({}, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_get_respects_ttl_expiry(self):
        index = {
            "serper|acme.com|hq": {
                "fetched_at": (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(),
                "response": {"organic": []},
            }
        }
        assert ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_get_within_ttl_returns_response(self):
        index = {
            "serper|acme.com|hq": {
                "fetched_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "response": {"organic": []},
            }
        }
        assert ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90) == {"organic": []}

    def test_force_refresh_ignores_fresh_entry(self):
        index = {}
        ec.put_cached(index, "serper", "acme.com", "hq", response={"organic": []})
        assert ec.get_cached(
            index, "serper", "acme.com", "hq", ttl_days=90, force_refresh=True) is None

    def test_malformed_entry_treated_as_miss(self):
        index = {"serper|acme.com|hq": "not-a-dict"}
        assert ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_missing_fetched_at_treated_as_miss(self):
        index = {"serper|acme.com|hq": {"response": {}}}
        assert ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_unparsable_fetched_at_treated_as_miss(self):
        index = {"serper|acme.com|hq": {"fetched_at": "not-a-date", "response": {}}}
        assert ec.get_cached(index, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_get_on_non_dict_index_returns_none(self):
        assert ec.get_cached(None, "serper", "acme.com", "hq", ttl_days=90) is None

    def test_put_on_non_dict_index_is_noop(self):
        # Must not raise.
        ec.put_cached(None, "serper", "acme.com", "hq", response={})

    def test_serper_ttl_days_helper(self):
        assert ec.serper_ttl_days("hq") == 120
        assert ec.serper_ttl_days("international_profile") == 120
        assert ec.serper_ttl_days("unknown_signal") == 120
        assert ec.serper_ttl_days("HQ") == 120

    def test_firecrawl_ttl_constant(self):
        assert ec.FIRECRAWL_TTL_DAYS == 120


# ---------------------------------------------------------------------------
# load_cache_index / save_cache_index — Python google-cloud-storage client
# path (this is what actually works inside a Cloud Run Job, which has ADC
# via its service account but no gcloud/gsutil CLI installed at all).
# ---------------------------------------------------------------------------

class _FakeBlob:
    """Generation-aware fake: mirrors the tiny slice of google-cloud-storage's
    Blob that enrichment_cache uses, including ``if_generation_match``
    semantics (0 = must not exist) so the compare-and-swap save is testable.
    Like the real client, a blob returned by ``get_blob`` snapshots its
    ``generation`` at fetch time (``frozen_generation``) while the store
    itself can move on — that's exactly the stale-read window CAS closes."""

    def __init__(self, store: dict, generations: dict, name: str,
                 frozen_generation=None):
        self._store = store
        self._generations = generations
        self.name = name
        self._frozen_generation = frozen_generation

    @property
    def generation(self):
        if self._frozen_generation is not None:
            return self._frozen_generation
        return self._generations.get(self.name)

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_text(self, if_generation_match=None) -> str:
        if if_generation_match is not None and \
                self._generations.get(self.name, 0) != if_generation_match:
            from google.api_core import exceptions as gcs_exceptions
            raise gcs_exceptions.PreconditionFailed("generation mismatch")
        return self._store[self.name]

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if if_generation_match is not None and \
                self._generations.get(self.name, 0) != if_generation_match:
            from google.api_core import exceptions as gcs_exceptions
            raise gcs_exceptions.PreconditionFailed("generation mismatch")
        self._store[self.name] = data
        self._generations[self.name] = self._generations.get(self.name, 0) + 1


class _FakeBucket:
    def __init__(self, store: dict, generations: dict):
        self._store = store
        self._generations = generations

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, self._generations, name)

    def get_blob(self, name: str):
        if name not in self._store:
            return None
        return _FakeBlob(self._store, self._generations, name,
                         frozen_generation=self._generations.get(name, 0))


class _FakeClient:
    def __init__(self, store: dict, generations: dict = None):
        self._store = store
        self._generations = generations if generations is not None else \
            {name: 1 for name in store}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store, self._generations)


class TestLoadCacheIndexViaClient:
    def test_blank_bucket_or_country_returns_empty_before_touching_client(self):
        with patch("enrichment_cache._storage_client") as m_client:
            assert ec.load_cache_index("", "italy") == {}
            assert ec.load_cache_index("bucket", "") == {}
        m_client.assert_not_called()

    def test_missing_blob_returns_empty_without_falling_back_to_cli(self):
        with patch("enrichment_cache._storage_client", return_value=_FakeClient({})), \
             patch("enrichment_cache.resolve_gcs_upload_tool") as m_cli:
            assert ec.load_cache_index("bucket", "italy") == {}
        m_cli.assert_not_called()

    def test_existing_blob_parses_json(self):
        payload = {"serper|acme.com|hq": {"fetched_at": "2026-01-01T00:00:00+00:00",
                                           "response": {"organic": []}}}
        store = {"_enrichment_cache/italy_cache_index.json": json.dumps(payload)}
        with patch("enrichment_cache._storage_client", return_value=_FakeClient(store)):
            assert ec.load_cache_index("bucket", "italy") == payload

    def test_invalid_json_returns_empty(self):
        store = {"_enrichment_cache/italy_cache_index.json": "not json{{{"}
        with patch("enrichment_cache._storage_client", return_value=_FakeClient(store)):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_client_unavailable_falls_back_to_cli(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli either")):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_client_raising_falls_back_to_cli(self):
        class _BrokenClient:
            def bucket(self, name):
                raise RuntimeError("no ADC")

        with patch("enrichment_cache._storage_client", return_value=_BrokenClient()), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli either")):
            assert ec.load_cache_index("bucket", "italy") == {}


class TestSaveCacheIndexViaClient:
    def test_uploads_via_client_when_available(self):
        store: dict = {}
        with patch("enrichment_cache._storage_client", return_value=_FakeClient(store)), \
             patch("enrichment_cache.resolve_gcs_upload_tool") as m_cli:
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result["success"] is True
        assert json.loads(store["_enrichment_cache/italy_cache_index.json"]) == {"a": 1}
        m_cli.assert_not_called()  # client worked -- CLI fallback never touched

    def test_client_raising_falls_back_to_cli(self):
        class _BrokenClient:
            def bucket(self, name):
                raise RuntimeError("no ADC")

        with patch("enrichment_cache._storage_client", return_value=_BrokenClient()), \
             patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli download")), \
             patch("enrichment_cache.upload_file", return_value={"success": True}) as m_upload:
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result == {"success": True}
        m_upload.assert_called_once()

    def test_save_merges_with_remote_instead_of_overwriting(self):
        # The lost-write scenario of parallel Cloud Run tasks: another task
        # already saved its entries; our save must ADD ours, not erase theirs.
        theirs = {"serper|other.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {"o": 1}}}
        ours = {"serper|acme.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {"a": 1}}}
        store = {"_enrichment_cache/italy_cache_index.json": json.dumps(theirs)}
        with patch("enrichment_cache._storage_client", return_value=_FakeClient(store)):
            result = ec.save_cache_index("bucket", "italy", ours)
        assert result["success"] is True
        saved = json.loads(store["_enrichment_cache/italy_cache_index.json"])
        assert set(saved) == {"serper|other.com|hq", "serper|acme.com|hq"}

    def test_save_retries_on_generation_mismatch_and_succeeds(self):
        # A concurrent task writes right after our read: the first attempt's
        # frozen generation is stale, the precondition fails, the retry
        # re-reads (picking up the other task's entries) and succeeds.
        blob_name = "_enrichment_cache/italy_cache_index.json"
        store = {blob_name: json.dumps({})}
        generations = {blob_name: 1}
        client = _FakeClient(store, generations)
        real_get_blob = _FakeBucket.get_blob
        state = {"raced": False}

        def _racing_get_blob(bucket_self, name):
            blob = real_get_blob(bucket_self, name)  # snapshots the generation
            if not state["raced"]:
                state["raced"] = True
                # Another task's save lands after our read, before our write.
                store[name] = json.dumps({"serper|other.com|hq": {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "response": {}}})
                generations[name] += 1
            return blob

        ours = {"serper|acme.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {}}}
        with patch("enrichment_cache._storage_client", return_value=client), \
             patch.object(_FakeBucket, "get_blob", _racing_get_blob), \
             patch("enrichment_cache.time.sleep") as m_sleep:
            result = ec.save_cache_index("bucket", "italy", ours)
        assert result["success"] is True
        m_sleep.assert_called()  # the stale first attempt really happened
        saved = json.loads(store[blob_name])
        # Both the concurrent writer's entry and ours survived the retry.
        assert set(saved) == {"serper|other.com|hq", "serper|acme.com|hq"}

    def test_save_prunes_entries_older_than_max_ttl(self):
        stale = {"serper|old.com|hq": {
            "fetched_at": (datetime.now(timezone.utc)
                           - timedelta(days=ec.max_ttl_days() + 1)).isoformat(),
            "response": {}}}
        fresh = {"serper|acme.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {}}}
        store = {"_enrichment_cache/italy_cache_index.json": json.dumps(stale)}
        with patch("enrichment_cache._storage_client", return_value=_FakeClient(store)):
            result = ec.save_cache_index("bucket", "italy", fresh)
        assert result["success"] is True
        saved = json.loads(store["_enrichment_cache/italy_cache_index.json"])
        assert set(saved) == {"serper|acme.com|hq"}


class TestMergeCacheIndexes:
    def test_union_of_disjoint_keys(self):
        a = {"k1": {"fetched_at": "2026-01-01T00:00:00+00:00", "response": {}}}
        b = {"k2": {"fetched_at": "2026-01-02T00:00:00+00:00", "response": {}}}
        assert set(ec.merge_cache_indexes(a, b)) == {"k1", "k2"}

    def test_newest_fetched_at_wins_on_collision(self):
        older = {"k": {"fetched_at": "2026-01-01T00:00:00+00:00", "response": {"v": "old"}}}
        newer = {"k": {"fetched_at": "2026-06-01T00:00:00+00:00", "response": {"v": "new"}}}
        assert ec.merge_cache_indexes(older, newer)["k"]["response"] == {"v": "new"}
        assert ec.merge_cache_indexes(newer, older)["k"]["response"] == {"v": "new"}

    def test_valid_timestamp_beats_malformed_entry(self):
        malformed = {"k": "not-a-dict"}
        valid = {"k": {"fetched_at": "2026-01-01T00:00:00+00:00", "response": {}}}
        assert ec.merge_cache_indexes(malformed, valid)["k"] == valid["k"]
        assert ec.merge_cache_indexes(valid, malformed)["k"] == valid["k"]

    def test_never_mutates_arguments_and_tolerates_non_dicts(self):
        base = {"k1": {"fetched_at": "2026-01-01T00:00:00+00:00", "response": {}}}
        base_copy = dict(base)
        ec.merge_cache_indexes(base, {"k2": {}})
        assert base == base_copy
        assert ec.merge_cache_indexes(None, base) == base
        assert ec.merge_cache_indexes(base, None) == base


# ---------------------------------------------------------------------------
# load_cache_index / save_cache_index — gcloud/gsutil CLI fallback path
# (local machine with CLI credentials but no ADC configured; also what a
# Cloud Run Job falls back to if google-cloud-storage is ever unusable).
# ---------------------------------------------------------------------------

class TestLoadCacheIndex:
    def test_blank_bucket_or_country_returns_empty(self):
        assert ec.load_cache_index("", "italy") == {}
        assert ec.load_cache_index("bucket", "") == {}

    def test_no_gcloud_tool_returns_empty(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=None):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_failing_download_returns_empty_dict_without_crash(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("boom")):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_nonzero_returncode_returns_empty(self):
        class _Proc:
            returncode = 1
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", return_value=_Proc()):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_successful_download_parses_json(self, tmp_path):
        payload = {"serper|acme.com|hq": {"fetched_at": "2026-01-01T00:00:00+00:00",
                                           "response": {"organic": []}}}

        def _fake_run(cmd, capture_output, text, timeout):
            # cmd[-1] is the local destination path the function generated.
            local_path = cmd[-1]
            from pathlib import Path
            Path(local_path).write_text(json.dumps(payload), encoding="utf-8")

            class _Proc:
                returncode = 0
            return _Proc()

        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=_fake_run):
            result = ec.load_cache_index("bucket", "italy")
        assert result == payload

    def test_invalid_json_returns_empty(self):
        def _fake_run(cmd, capture_output, text, timeout):
            local_path = cmd[-1]
            from pathlib import Path
            Path(local_path).write_text("not json{{{", encoding="utf-8")

            class _Proc:
                returncode = 0
            return _Proc()

        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=_fake_run):
            assert ec.load_cache_index("bucket", "italy") == {}


# ---------------------------------------------------------------------------
# save_cache_index — graceful degradation
# ---------------------------------------------------------------------------

class TestSaveCacheIndex:
    def test_blank_bucket_or_country_fails_without_crash(self):
        result = ec.save_cache_index("", "italy", {})
        assert result["success"] is False
        result = ec.save_cache_index("bucket", "", {})
        assert result["success"] is False

    def test_non_dict_index_fails_without_crash(self):
        result = ec.save_cache_index("bucket", "italy", "not-a-dict")
        assert result["success"] is False

    def test_no_gcloud_tool_fails_with_clear_error(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool", return_value=None):
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result["success"] is False
        assert "gcloud" in result["error"].lower() or "gsutil" in result["error"].lower()

    def test_failing_upload_returns_result_dict_no_crash(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli download")), \
             patch("enrichment_cache.upload_file",
                   return_value={"success": False, "error": "network unreachable"}):
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result == {"success": False, "error": "network unreachable"}

    def test_successful_upload_delegates_to_upload_file(self):
        captured = {}

        def _fake_upload(tool_cmd, local_path, destination):
            captured["destination"] = destination
            captured["local_path"] = local_path
            return {"success": True}

        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli download")), \
             patch("enrichment_cache.upload_file", side_effect=_fake_upload):
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result == {"success": True}
        assert captured["destination"] == \
            "gs://bucket/_enrichment_cache/italy_cache_index.json"

    def test_cli_save_merges_with_remote_index_first(self):
        # Same lost-write protection as the client path, best-effort: the
        # remote index is downloaded and merged in before the CLI upload.
        theirs = {"serper|other.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {}}}

        def _fake_download(cmd, capture_output, text, timeout):
            from pathlib import Path
            Path(cmd[-1]).write_text(json.dumps(theirs), encoding="utf-8")

            class _Proc:
                returncode = 0
            return _Proc()

        uploaded = {}

        def _fake_upload(tool_cmd, local_path, destination):
            from pathlib import Path
            uploaded.update(json.loads(Path(local_path).read_text(encoding="utf-8")))
            return {"success": True}

        ours = {"serper|acme.com|hq": {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": {}}}
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=_fake_download), \
             patch("enrichment_cache.upload_file", side_effect=_fake_upload):
            result = ec.save_cache_index("bucket", "italy", ours)
        assert result == {"success": True}
        assert set(uploaded) == {"serper|other.com|hq", "serper|acme.com|hq"}

    def test_unexpected_exception_returns_failure_dict(self):
        with patch("enrichment_cache._storage_client", return_value=None), \
             patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]), \
             patch("subprocess.run", side_effect=OSError("no cli download")), \
             patch("enrichment_cache.upload_file", side_effect=RuntimeError("boom")):
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result["success"] is False
        assert "boom" in result["error"]
