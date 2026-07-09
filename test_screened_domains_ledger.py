"""Tests for screened_domains_ledger.py: the shared, GCS-backed ledger of
settled "definitely not foreign-HQ" domains.

No real network/gcloud/google-cloud-storage calls: ``_storage_client``,
``resolve_gcs_upload_tool``/``upload_file``/``subprocess.run`` are mocked
throughout, same pattern as test_enrichment_cache.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import screened_domains_ledger as sdl


# ---------------------------------------------------------------------------
# normalize_domain
# ---------------------------------------------------------------------------

class TestNormalizeDomain:
    def test_strips_scheme_and_www_but_keeps_tld(self):
        assert sdl.normalize_domain("https://www.Acme.COM/") == "acme.com"

    def test_distinguishes_different_tlds(self):
        assert sdl.normalize_domain("acme.com") != sdl.normalize_domain("acme.nl")

    def test_blank_returns_empty_string(self):
        assert sdl.normalize_domain("") == ""
        assert sdl.normalize_domain(None) == ""


# ---------------------------------------------------------------------------
# is_clearly_domestic / build_ledger_updates
# ---------------------------------------------------------------------------

class TestIsClearlyDomestic:
    def test_score_zero_no_flags_is_domestic(self):
        assert sdl.is_clearly_domestic({"sig_foreign_hq_score_for_next_scoring": 0.0}) is True

    def test_score_three_is_not_domestic(self):
        assert sdl.is_clearly_domestic({"sig_foreign_hq_score_for_next_scoring": 3.0}) is False

    def test_ambiguous_score_is_not_domestic(self):
        assert sdl.is_clearly_domestic({"sig_foreign_hq_score_for_next_scoring": 1.0}) is False
        assert sdl.is_clearly_domestic({"sig_foreign_hq_score_for_next_scoring": 2.0}) is False

    def test_missing_score_is_not_domestic(self):
        assert sdl.is_clearly_domestic({}) is False

    def test_manual_review_flag_blocks_settled_domestic(self):
        row = {"sig_foreign_hq_score_for_next_scoring": 0.0, "needs_manual_review": True}
        assert sdl.is_clearly_domestic(row) is False

    def test_suppressed_flag_blocks_settled_domestic(self):
        row = {"sig_foreign_hq_score_for_next_scoring": 0.0,
               "hq_positive_score_suppressed_for_review": "Yes"}
        assert sdl.is_clearly_domestic(row) is False

    def test_c5_domestic_confirmed_stays_domestic(self):
        row = {"sig_foreign_hq_score_for_next_scoring": 0.0,
               "c5_adjudication": "domestic_confirmed"}
        assert sdl.is_clearly_domestic(row) is True

    def test_c5_unclear_blocks_settled_domestic(self):
        row = {"sig_foreign_hq_score_for_next_scoring": 0.0, "c5_adjudication": "unclear"}
        assert sdl.is_clearly_domestic(row) is False

    def test_no_c5_at_all_is_fine(self):
        # The common case when C5 is off entirely -- blank c5_adjudication
        # must not be treated as "ambiguous C5 outcome".
        row = {"sig_foreign_hq_score_for_next_scoring": 0.0, "c5_adjudication": ""}
        assert sdl.is_clearly_domestic(row) is True


class TestBuildLedgerUpdates:
    def test_only_clearly_domestic_rows_included(self):
        rows = [
            {"domain": "domestic.com", "sig_foreign_hq_score_for_next_scoring": 0.0},
            {"domain": "foreign.com", "sig_foreign_hq_score_for_next_scoring": 3.0},
            {"domain": "ambiguous.com", "sig_foreign_hq_score_for_next_scoring": 1.0},
        ]
        updates = sdl.build_ledger_updates(rows)
        assert set(updates) == {"domestic.com"}
        assert updates["domestic.com"]["confirmed_foreign_hq"] is False
        assert "screened_at" in updates["domestic.com"]

    def test_missing_domain_excluded(self):
        rows = [{"sig_foreign_hq_score_for_next_scoring": 0.0}]
        assert sdl.build_ledger_updates(rows) == {}

    def test_domain_normalized(self):
        rows = [{"domain": "HTTPS://WWW.Domestic.com/",
                  "sig_foreign_hq_score_for_next_scoring": 0.0}]
        updates = sdl.build_ledger_updates(rows)
        assert set(updates) == {"domestic.com"}

    def test_non_dict_rows_never_raise(self):
        assert sdl.build_ledger_updates([None, "not-a-dict", 42]) == {}

    def test_empty_rows_returns_empty(self):
        assert sdl.build_ledger_updates([]) == {}


class TestKnownDomesticDomains:
    def test_only_confirmed_false_entries_included(self):
        ledger = {
            "domestic.com": {"confirmed_foreign_hq": False},
            "weird.com": {"confirmed_foreign_hq": True},
        }
        assert sdl.known_domestic_domains(ledger) == {"domestic.com"}

    def test_non_dict_ledger_returns_empty_set(self):
        assert sdl.known_domestic_domains(None) == set()
        assert sdl.known_domestic_domains("not-a-dict") == set()

    def test_malformed_entries_ignored(self):
        ledger = {"a.com": "not-a-dict", "b.com": {"confirmed_foreign_hq": False}}
        assert sdl.known_domestic_domains(ledger) == {"b.com"}


class TestMergeLedgers:
    def test_union_of_disjoint_domains(self):
        a = {"a.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        b = {"b.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-02T00:00:00+00:00"}}
        assert set(sdl.merge_ledgers(a, b)) == {"a.com", "b.com"}

    def test_newest_screened_at_wins_on_collision(self):
        older = {"a.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        newer = {"a.com": {"confirmed_foreign_hq": False, "screened_at": "2026-06-01T00:00:00+00:00"}}
        assert sdl.merge_ledgers(older, newer)["a.com"]["screened_at"] == "2026-06-01T00:00:00+00:00"
        assert sdl.merge_ledgers(newer, older)["a.com"]["screened_at"] == "2026-06-01T00:00:00+00:00"

    def test_never_mutates_arguments(self):
        base = {"a.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        base_copy = dict(base)
        sdl.merge_ledgers(base, {"b.com": {}})
        assert base == base_copy

    def test_tolerates_non_dicts(self):
        base = {"a.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        assert sdl.merge_ledgers(None, base) == base
        assert sdl.merge_ledgers(base, None) == base


# ---------------------------------------------------------------------------
# GCS transport -- Python client path (fakes mirror test_enrichment_cache.py)
# ---------------------------------------------------------------------------

class _FakeBlob:
    def __init__(self, store: dict, generations: dict, name: str, frozen_generation=None):
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
        self._generations = generations if generations is not None else {name: 1 for name in store}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store, self._generations)


class TestLoadLedger:
    def test_blank_bucket_or_country_returns_empty(self):
        assert sdl.load_ledger("", "italy") == {}
        assert sdl.load_ledger("bucket", "") == {}

    def test_missing_blob_returns_empty(self):
        with patch("screened_domains_ledger._storage_client", return_value=_FakeClient({})):
            assert sdl.load_ledger("bucket", "italy") == {}

    def test_existing_blob_parses_json(self):
        payload = {"acme.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        store = {"_screened_domains/italy_screened_domains.json": json.dumps(payload)}
        with patch("screened_domains_ledger._storage_client", return_value=_FakeClient(store)):
            assert sdl.load_ledger("bucket", "italy") == payload

    def test_client_unavailable_falls_back_to_cli(self):
        with patch("screened_domains_ledger._storage_client", return_value=None), \
             patch("screened_domains_ledger.resolve_gcs_upload_tool", return_value=None):
            assert sdl.load_ledger("bucket", "italy") == {}


class TestSaveLedger:
    def test_blank_bucket_or_country_fails_without_crash(self):
        assert sdl.save_ledger("", "italy", {})["success"] is False
        assert sdl.save_ledger("bucket", "", {})["success"] is False

    def test_non_dict_updates_fails_without_crash(self):
        result = sdl.save_ledger("bucket", "italy", "not-a-dict")
        assert result["success"] is False

    def test_empty_updates_is_a_noop_success(self):
        with patch("screened_domains_ledger._storage_client") as m_client:
            result = sdl.save_ledger("bucket", "italy", {})
        assert result["success"] is True
        m_client.assert_not_called()

    def test_uploads_via_client_when_available(self):
        store: dict = {}
        updates = {"acme.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        with patch("screened_domains_ledger._storage_client", return_value=_FakeClient(store)):
            result = sdl.save_ledger("bucket", "italy", updates)
        assert result["success"] is True
        assert json.loads(store["_screened_domains/italy_screened_domains.json"]) == updates

    def test_save_merges_with_remote_instead_of_overwriting(self):
        theirs = {"other.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-01T00:00:00+00:00"}}
        ours = {"acme.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-02T00:00:00+00:00"}}
        store = {"_screened_domains/italy_screened_domains.json": json.dumps(theirs)}
        with patch("screened_domains_ledger._storage_client", return_value=_FakeClient(store)):
            result = sdl.save_ledger("bucket", "italy", ours)
        assert result["success"] is True
        saved = json.loads(store["_screened_domains/italy_screened_domains.json"])
        assert set(saved) == {"other.com", "acme.com"}

    def test_save_retries_on_generation_mismatch_and_succeeds(self):
        blob_name = "_screened_domains/italy_screened_domains.json"
        store = {blob_name: json.dumps({})}
        generations = {blob_name: 1}
        client = _FakeClient(store, generations)
        real_get_blob = _FakeBucket.get_blob
        state = {"raced": False}

        def _racing_get_blob(bucket_self, name):
            blob = real_get_blob(bucket_self, name)
            if not state["raced"]:
                state["raced"] = True
                store[name] = json.dumps(
                    {"other.com": {"confirmed_foreign_hq": False,
                                    "screened_at": "2026-01-01T00:00:00+00:00"}})
                generations[name] += 1
            return blob

        ours = {"acme.com": {"confirmed_foreign_hq": False, "screened_at": "2026-01-02T00:00:00+00:00"}}
        with patch("screened_domains_ledger._storage_client", return_value=client), \
             patch.object(_FakeBucket, "get_blob", _racing_get_blob), \
             patch("screened_domains_ledger.time.sleep") as m_sleep:
            result = sdl.save_ledger("bucket", "italy", ours)
        assert result["success"] is True
        m_sleep.assert_called()
        saved = json.loads(store[blob_name])
        assert set(saved) == {"other.com", "acme.com"}

    def test_no_gcloud_tool_fails_with_clear_error(self):
        with patch("screened_domains_ledger._storage_client", return_value=None), \
             patch("screened_domains_ledger.resolve_gcs_upload_tool", return_value=None):
            result = sdl.save_ledger("bucket", "italy", {"a.com": {"confirmed_foreign_hq": False}})
        assert result["success"] is False
