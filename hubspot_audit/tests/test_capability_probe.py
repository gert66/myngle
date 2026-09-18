import os
import unittest

from hubspot_audit.capability_probe import capability_matrix_to_dict, run_capability_probe
from hubspot_audit.hubspot_client import FixtureHubSpotClient
from hubspot_audit.models import CapabilityStatus

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


class CapabilityProbeTests(unittest.TestCase):
    def test_all_fixture_objects_report_available(self):
        client = FixtureHubSpotClient(FIXTURES_DIR)
        matrix = run_capability_probe(client)
        for name in ("companies", "contacts", "deals", "calls", "meetings", "emails", "notes", "tasks", "owners"):
            self.assertEqual(matrix[name].status, CapabilityStatus.AVAILABLE, name)

    def test_every_gap_has_a_downstream_impact_string(self):
        client = FixtureHubSpotClient(FIXTURES_DIR)
        matrix = run_capability_probe(client)
        for record in matrix.values():
            self.assertTrue(record.downstream_impact)

    def test_to_dict_never_includes_a_token_field(self):
        client = FixtureHubSpotClient(FIXTURES_DIR)
        matrix = run_capability_probe(client)
        as_dict = capability_matrix_to_dict(matrix)
        serialized = str(as_dict).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("bearer", serialized)


if __name__ == "__main__":
    unittest.main()
