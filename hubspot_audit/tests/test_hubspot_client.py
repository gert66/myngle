import os
import unittest

from hubspot_audit.hubspot_client import FixtureHubSpotClient, LiveHubSpotReadClient
from hubspot_audit.config import AuditConfig

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


class FixtureHubSpotClientTests(unittest.TestCase):
    def setUp(self):
        self.client = FixtureHubSpotClient(FIXTURES_DIR)

    def test_pagination_covers_all_records_without_overlap(self):
        seen_ids = set()
        cursor = None
        pages = 0
        while True:
            page = self.client.get_page("companies", cursor, page_size=17)
            for record in page.records:
                self.assertNotIn(record["id"], seen_ids)
                seen_ids.add(record["id"])
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break
            self.assertLess(pages, 100)  # guard against infinite loop bugs
        self.assertEqual(len(seen_ids), 120)

    def test_capability_check_true_for_known_object(self):
        ok, _ = self.client.capability_check("companies")
        self.assertTrue(ok)

    def test_capability_check_false_for_unknown_object(self):
        ok, _ = self.client.capability_check("not_a_real_object")
        self.assertFalse(ok)

    def test_get_properties_reads_definitions(self):
        props = self.client.get_properties("companies")
        names = {p["name"] for p in props}
        self.assertIn("createdate", names)
        self.assertIn("companies_sf_id", names)


class LiveHubSpotReadClientTests(unittest.TestCase):
    def test_raises_without_token(self):
        os.environ.pop("HUBSPOT_ACCESS_TOKEN", None)
        config = AuditConfig.build("t", "/tmp/whatever", mode="live")
        with self.assertRaises(RuntimeError):
            LiveHubSpotReadClient(config)

    def test_only_exposes_read_shaped_methods(self):
        # Non-negotiable safety property: no write-shaped method exists at all.
        forbidden = {"create", "update", "delete", "merge", "archive", "post", "put", "patch"}
        methods = {name.lower() for name in dir(LiveHubSpotReadClient)}
        self.assertFalse(forbidden & methods)


if __name__ == "__main__":
    unittest.main()
