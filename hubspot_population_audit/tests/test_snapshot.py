import os
import unittest

from hubspot_population_audit.snapshot import Snapshot

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(FIXTURE_SNAPSHOT)

    def test_discovers_all_present_object_types(self):
        self.assertEqual(self.snapshot.discover_object_types(), ["companies", "contacts", "deals"])

    def test_iter_records_streams_raw_records_including_duplicates(self):
        ids = [record["id"] for record in self.snapshot.iter_records("companies")]
        self.assertEqual(ids, ["1", "2", "3", "4", "1"])

    def test_count_raw_records_matches_line_count(self):
        self.assertEqual(self.snapshot.count_raw_records("companies"), 5)
        self.assertEqual(self.snapshot.count_raw_records("contacts"), 4)
        self.assertEqual(self.snapshot.count_raw_records("deals"), 3)

    def test_count_raw_records_is_zero_for_missing_object_type(self):
        self.assertEqual(self.snapshot.count_raw_records("emails"), 0)

    def test_portal_totals_found_at_snapshot_root(self):
        totals, source = self.snapshot.portal_totals()
        self.assertEqual(totals, {"companies": 5, "contacts": 4})
        self.assertEqual(source, "portal_totals.json")

    def test_properties_reads_definitions(self):
        props = self.snapshot.properties("companies")
        names = {p["name"] for p in props}
        self.assertIn("domain", names)

    def test_checkpoint_reads_pagination_metadata(self):
        checkpoint = self.snapshot.checkpoint()
        self.assertTrue(checkpoint["companies"]["complete"])
        self.assertEqual(checkpoint["contacts"]["record_count"], 4)


class SnapshotWithoutPortalTotalsTests(unittest.TestCase):
    def test_portal_totals_absent_returns_empty_dict_and_none_source(self):
        import shutil
        import tempfile

        tmp_dir = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(FIXTURE_SNAPSHOT, "raw"), os.path.join(tmp_dir, "raw"))
            snapshot = Snapshot(tmp_dir)
            totals, source = snapshot.portal_totals()
            self.assertEqual(totals, {})
            self.assertIsNone(source)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
