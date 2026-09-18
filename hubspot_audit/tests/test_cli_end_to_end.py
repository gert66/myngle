import json
import os
import shutil
import tempfile
import unittest

from hubspot_audit.config import AuditConfig
from hubspot_audit.runner import run_audit

REQUIRED_TOP_LEVEL_FILES = [
    "run_status.json", "capability_matrix.json", "gaps.json", "findings.json",
    "evidence.json", "investigations.json", "next_actions.json", "control_center.json",
]
REQUIRED_SUBDIRS = ["raw", "normalized", "analyses", "investigations", "evidence", "reports", "logs"]


class EndToEndFixtureRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.config = AuditConfig.build(run_id="e2e-test", output_dir=self.tmp_dir, mode="fixture")

    def test_run_produces_every_required_artifact(self):
        run_audit(self.config)
        for name in REQUIRED_TOP_LEVEL_FILES:
            path = os.path.join(self.tmp_dir, name)
            self.assertTrue(os.path.exists(path), f"missing artifact: {name}")
        for name in REQUIRED_SUBDIRS:
            path = os.path.join(self.tmp_dir, name)
            self.assertTrue(os.path.isdir(path), f"missing directory: {name}")
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "reports", "index.html")))

    def test_run_works_with_ai_disabled_by_default(self):
        self.assertFalse(self.config.ai.enabled)
        run_audit(self.config)
        with open(os.path.join(self.tmp_dir, "control_center.json")) as fh:
            control_center = json.load(fh)
        self.assertEqual(control_center["status"], "done")
        self.assertEqual(control_center["overall_progress_pct"], 100.0)

    def test_findings_and_investigations_are_non_empty_for_the_fixture_dataset(self):
        run_audit(self.config)
        with open(os.path.join(self.tmp_dir, "findings.json")) as fh:
            findings = json.load(fh)
        with open(os.path.join(self.tmp_dir, "investigations.json")) as fh:
            investigations = json.load(fh)
        self.assertGreater(len(findings), 0)
        self.assertGreater(len(investigations), 0)

    def test_no_write_shaped_hubspot_calls_are_possible_in_fixture_mode(self):
        # FixtureHubSpotClient has no network access at all -- this is a structural
        # guarantee, not just a runtime observation.
        from hubspot_audit.hubspot_client import FixtureHubSpotClient
        import inspect

        source = inspect.getsource(FixtureHubSpotClient)
        for forbidden in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
