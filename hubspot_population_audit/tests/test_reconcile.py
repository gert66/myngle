import os
import unittest

from hubspot_population_audit.reconcile import reconcile_all, reconcile_object
from hubspot_population_audit.snapshot import Snapshot

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(FIXTURE_SNAPSHOT)

    def test_companies_mismatch_case_is_flagged_unreconciled(self):
        result = reconcile_object(self.snapshot, "companies")
        self.assertEqual(result["recorded_portal_total"], 5)
        self.assertEqual(result["raw_record_count"], 5)
        self.assertEqual(result["unique_id_count"], 4)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["delta"], 1)
        self.assertFalse(result["reconciled"])
        self.assertTrue(any("differs" in note for note in result["notes"]))
        self.assertTrue(any("duplicate" in note for note in result["notes"]))

    def test_contacts_exact_match_is_reconciled(self):
        result = reconcile_object(self.snapshot, "contacts")
        self.assertEqual(result["recorded_portal_total"], 4)
        self.assertEqual(result["raw_record_count"], 4)
        self.assertEqual(result["unique_id_count"], 4)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["delta"], 0)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["notes"], [])

    def test_deals_without_recorded_portal_total_is_unreconciled_not_assumed_passing(self):
        result = reconcile_object(self.snapshot, "deals")
        self.assertIsNone(result["recorded_portal_total"])
        self.assertEqual(result["raw_record_count"], 3)
        self.assertEqual(result["unique_id_count"], 3)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertIsNone(result["delta"])
        self.assertFalse(result["reconciled"])
        self.assertTrue(any("No independently recorded portal total" in note for note in result["notes"]))

    def test_reconcile_all_covers_every_discovered_object_type(self):
        object_types = self.snapshot.discover_object_types()
        results = reconcile_all(self.snapshot, object_types)
        self.assertEqual(set(results.keys()), {"companies", "contacts", "deals"})


if __name__ == "__main__":
    unittest.main()
