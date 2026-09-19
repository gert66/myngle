import os
import unittest
from unittest import mock

from hubspot_population_audit.hubspot_client import (
    LookupCache,
    ReadOnlyHubSpotClient,
    WriteOperationBlocked,
    is_read_path,
)

TOKEN_ENV_VAR = "HUBSPOT_ACCESS_TOKEN"


class IsReadPathTests(unittest.TestCase):
    def test_search_paths_are_read(self):
        self.assertTrue(is_read_path("/crm/v3/objects/companies/search"))

    def test_batch_read_paths_are_read(self):
        self.assertTrue(is_read_path("/crm/v3/objects/contacts/batch/read"))

    def test_v4_associations_batch_read_paths_are_read(self):
        self.assertTrue(is_read_path("/crm/v4/associations/companies/contacts/batch/read"))
        self.assertTrue(is_read_path("/crm/v4/associations/companies/deals/batch/read"))
        self.assertTrue(is_read_path("/crm/v4/associations/contacts/deals/batch/read"))

    def test_batch_create_is_not_read(self):
        self.assertFalse(is_read_path("/crm/v3/objects/companies/batch/create"))

    def test_batch_update_is_not_read(self):
        self.assertFalse(is_read_path("/crm/v3/objects/companies/batch/update"))

    def test_merge_is_not_read(self):
        self.assertFalse(is_read_path("/crm/v3/objects/companies/merge"))

    def test_batch_archive_is_not_read(self):
        self.assertFalse(is_read_path("/crm/v3/objects/companies/batch/archive"))

    def test_bare_object_path_is_not_read(self):
        self.assertFalse(is_read_path("/crm/v3/objects/companies"))


class ReadOnlyHubSpotClientTests(unittest.TestCase):
    def setUp(self):
        os.environ[TOKEN_ENV_VAR] = "fake-token-for-tests"
        self.addCleanup(os.environ.pop, TOKEN_ENV_VAR, None)
        self.client = ReadOnlyHubSpotClient(token_env_var=TOKEN_ENV_VAR, max_retries=0)

    def test_raises_without_token(self):
        os.environ.pop(TOKEN_ENV_VAR, None)
        with self.assertRaises(RuntimeError):
            ReadOnlyHubSpotClient(token_env_var=TOKEN_ENV_VAR)

    @mock.patch("requests.get")
    def test_get_performs_a_real_request(self, mock_get):
        mock_get.return_value = mock.Mock(status_code=200, json=lambda: {"results": []})
        result = self.client.get("/crm/v3/objects/companies", {"limit": 1})
        self.assertEqual(result, {"results": []})
        mock_get.assert_called_once()

    @mock.patch("requests.post")
    def test_search_is_allowed_and_performs_a_request(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, json=lambda: {"total": 42, "results": []})
        result = self.client.search("companies", {"limit": 1})
        self.assertEqual(result["total"], 42)
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_batch_read_is_allowed_and_performs_a_request(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, json=lambda: {"results": []})
        self.client.batch_read("contacts", {"inputs": [{"id": "1"}]})
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_associations_batch_read_is_allowed_and_performs_a_request(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, json=lambda: {"results": []})
        result = self.client.associations_batch_read("companies", "contacts", ["1", "2"])
        self.assertEqual(result, {"results": []})
        mock_post.assert_called_once()
        called_url = mock_post.call_args[0][0]
        self.assertTrue(called_url.endswith("/crm/v4/associations/companies/contacts/batch/read"))

    @mock.patch("requests.post")
    def test_403_response_is_not_retried(self, mock_post):
        import requests

        mock_response = mock.Mock(status_code=403)
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mock_post.return_value = mock_response
        client = ReadOnlyHubSpotClient(token_env_var=TOKEN_ENV_VAR, max_retries=3)
        with self.assertRaises(requests.HTTPError) as ctx:
            client.batch_read("contacts", {"inputs": [{"id": "1"}]})
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(ctx.exception.response.status_code, 403)

    @mock.patch("requests.post")
    def test_post_to_non_read_path_raises_before_any_request(self, mock_post):
        with self.assertRaises(WriteOperationBlocked):
            self.client.post("/crm/v3/objects/companies/batch/create", {})
        mock_post.assert_not_called()

    @mock.patch("requests.post")
    def test_post_merge_path_raises_before_any_request(self, mock_post):
        with self.assertRaises(WriteOperationBlocked):
            self.client.post("/crm/v3/objects/companies/merge", {})
        mock_post.assert_not_called()

    @mock.patch("requests.put")
    def test_put_raises_before_any_request(self, mock_put):
        with self.assertRaises(WriteOperationBlocked):
            self.client.put("/crm/v3/objects/companies/1")
        mock_put.assert_not_called()

    @mock.patch("requests.patch")
    def test_patch_raises_before_any_request(self, mock_patch):
        with self.assertRaises(WriteOperationBlocked):
            self.client.patch("/crm/v3/objects/companies/1")
        mock_patch.assert_not_called()

    @mock.patch("requests.delete")
    def test_delete_raises_before_any_request(self, mock_delete):
        with self.assertRaises(WriteOperationBlocked):
            self.client.delete("/crm/v3/objects/companies/1")
        mock_delete.assert_not_called()

    def test_no_write_shaped_http_verb_reachable_from_source(self):
        import inspect

        source = inspect.getsource(ReadOnlyHubSpotClient)
        for forbidden in ("requests.put", "requests.patch", "requests.delete"):
            self.assertNotIn(forbidden, source)


class LookupCacheTests(unittest.TestCase):
    def test_cache_round_trips_and_persists_to_disk(self):
        import shutil
        import tempfile

        tmp_dir = tempfile.mkdtemp()
        try:
            cache = LookupCache(tmp_dir)
            self.assertFalse(cache.has("k"))
            cache.set("k", {"a": 1})
            self.assertTrue(cache.has("k"))
            self.assertEqual(cache.get("k"), {"a": 1})

            reloaded = LookupCache(tmp_dir)
            self.assertEqual(reloaded.get("k"), {"a": 1})
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cache_never_stores_the_auth_token(self):
        import shutil
        import tempfile

        os.environ[TOKEN_ENV_VAR] = "super-secret-token"
        self.addCleanup(os.environ.pop, TOKEN_ENV_VAR, None)
        tmp_dir = tempfile.mkdtemp()
        try:
            cache = LookupCache(tmp_dir)
            client = ReadOnlyHubSpotClient(token_env_var=TOKEN_ENV_VAR, cache=cache, max_retries=0)
            with mock.patch("requests.get") as mock_get:
                mock_get.return_value = mock.Mock(status_code=200, json=lambda: {"results": []})
                client.get("/crm/v3/objects/companies", {"limit": 1})
            with open(cache.path, "r", encoding="utf-8") as fh:
                contents = fh.read()
            self.assertNotIn("super-secret-token", contents)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
