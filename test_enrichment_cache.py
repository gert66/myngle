"""Tests for the shared, GCS-backed per-country enrichment cache.

No real network/gcloud calls: ``resolve_gcs_upload_tool``/``upload_file``/
``subprocess.run`` are mocked throughout. Covers key normalization, TTL
expiry, force_refresh, and graceful degradation on a failing
download/upload (cache must always be an optimization, never a hard
dependency).
"""

from __future__ import annotations

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
        assert ec.serper_ttl_days("hq") == 90
        assert ec.serper_ttl_days("international_profile") == 30
        assert ec.serper_ttl_days("unknown_signal") == 30
        assert ec.serper_ttl_days("HQ") == 90

    def test_firecrawl_ttl_constant(self):
        assert ec.FIRECRAWL_TTL_DAYS == 120


# ---------------------------------------------------------------------------
# load_cache_index — graceful degradation
# ---------------------------------------------------------------------------

class TestLoadCacheIndex:
    def test_blank_bucket_or_country_returns_empty(self):
        assert ec.load_cache_index("", "italy") == {}
        assert ec.load_cache_index("bucket", "") == {}

    def test_no_gcloud_tool_returns_empty(self):
        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=None):
            assert ec.load_cache_index("bucket", "italy") == {}

    def test_failing_download_returns_empty_dict_without_crash(self):
        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]):
            with patch("subprocess.run", side_effect=OSError("boom")):
                assert ec.load_cache_index("bucket", "italy") == {}

    def test_nonzero_returncode_returns_empty(self):
        class _Proc:
            returncode = 1
        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]):
            with patch("subprocess.run", return_value=_Proc()):
                assert ec.load_cache_index("bucket", "italy") == {}

    def test_successful_download_parses_json(self, tmp_path):
        payload = {"serper|acme.com|hq": {"fetched_at": "2026-01-01T00:00:00+00:00",
                                           "response": {"organic": []}}}

        def _fake_run(cmd, capture_output, text, timeout):
            # cmd[-1] is the local destination path the function generated.
            local_path = cmd[-1]
            from pathlib import Path
            import json
            Path(local_path).write_text(json.dumps(payload), encoding="utf-8")

            class _Proc:
                returncode = 0
            return _Proc()

        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]):
            with patch("subprocess.run", side_effect=_fake_run):
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

        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=["gcloud", "storage", "cp"]):
            with patch("subprocess.run", side_effect=_fake_run):
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
        with patch("enrichment_cache.resolve_gcs_upload_tool", return_value=None):
            result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result["success"] is False
        assert "gcloud" in result["error"].lower() or "gsutil" in result["error"].lower()

    def test_failing_upload_returns_result_dict_no_crash(self):
        with patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]):
            with patch("enrichment_cache.upload_file",
                       return_value={"success": False, "error": "network unreachable"}):
                result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result == {"success": False, "error": "network unreachable"}

    def test_successful_upload_delegates_to_upload_file(self):
        captured = {}

        def _fake_upload(tool_cmd, local_path, destination):
            captured["destination"] = destination
            captured["local_path"] = local_path
            return {"success": True}

        with patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]):
            with patch("enrichment_cache.upload_file", side_effect=_fake_upload):
                result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result == {"success": True}
        assert captured["destination"] == \
            "gs://bucket/_enrichment_cache/italy_cache_index.json"

    def test_unexpected_exception_returns_failure_dict(self):
        with patch("enrichment_cache.resolve_gcs_upload_tool",
                   return_value=["gcloud", "storage", "cp"]):
            with patch("enrichment_cache.upload_file", side_effect=RuntimeError("boom")):
                result = ec.save_cache_index("bucket", "italy", {"a": 1})
        assert result["success"] is False
        assert "boom" in result["error"]
