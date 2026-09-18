import json
import os
import shutil
import tempfile
import unittest

from hubspot_population_audit.cli import main

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)

REQUIRED_ARTIFACTS = [
    "progress.json",
    "population_map.json",
    "cohort_analysis.json",
    "evidence.json",
    "gaps.json",
    "next_actions.json",
]


class CliHelpTests(unittest.TestCase):
    def test_top_level_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_run_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["run", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_missing_command_is_a_usage_error_not_a_crash(self):
        with self.assertRaises(SystemExit) as ctx:
            main([])
        self.assertNotEqual(ctx.exception.code, 0)


class CliEndToEndFixtureRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_run_produces_every_required_artifact(self):
        exit_code = main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        self.assertEqual(exit_code, 0)
        for name in REQUIRED_ARTIFACTS:
            path = os.path.join(self.tmp_dir, name)
            self.assertTrue(os.path.exists(path), f"missing artifact: {name}")
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "reports", "index.html")))

    def test_progress_json_reaches_a_terminal_phase(self):
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        with open(os.path.join(self.tmp_dir, "progress.json")) as fh:
            progress = json.load(fh)
        self.assertEqual(progress["phase"], "completed")
        phase_names = {p["name"] for p in progress["phases"]}
        self.assertIn("snapshot_load", phase_names)
        self.assertIn("reconciliation", phase_names)
        self.assertIn("records_loaded.companies", progress["counters"])

    def test_population_map_reconciliation_matches_fixture_expectations(self):
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        with open(os.path.join(self.tmp_dir, "population_map.json")) as fh:
            population_map = json.load(fh)
        self.assertEqual(population_map["status"], "not_yet_implemented")
        reconciliation = population_map["reconciliation"]
        self.assertTrue(reconciliation["contacts"]["reconciled"])
        self.assertFalse(reconciliation["companies"]["reconciled"])
        self.assertFalse(reconciliation["deals"]["reconciled"])

    def test_cohort_analysis_is_completed_not_a_placeholder(self):
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        with open(os.path.join(self.tmp_dir, "cohort_analysis.json")) as fh:
            cohort_analysis = json.load(fh)
        self.assertEqual(cohort_analysis["status"], "completed")
        self.assertIn("companies", cohort_analysis)
        self.assertIn("contacts", cohort_analysis)

    def test_other_analyses_remain_explicit_placeholders(self):
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        with open(os.path.join(self.tmp_dir, "population_map.json")) as fh:
            population_map = json.load(fh)
        self.assertEqual(population_map["status"], "not_yet_implemented")
        with open(os.path.join(self.tmp_dir, "progress.json")) as fh:
            progress = json.load(fh)
        phases = {p["name"]: p["status"] for p in progress["phases"]}
        self.assertEqual(phases["cohort_analysis"], "completed")
        self.assertEqual(phases["association_evidence"], "not_yet_implemented")
        self.assertEqual(phases["activity_evidence"], "not_yet_implemented")
        self.assertEqual(phases["classification"], "not_yet_implemented")
        self.assertEqual(phases["live_lookups"], "not_yet_implemented")

    def test_html_report_separates_required_sections(self):
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        with open(os.path.join(self.tmp_dir, "reports", "index.html")) as fh:
            html = fh.read()
        for label in ("Observed facts", "Inferred classifications", "Uncertainty", "Inaccessible data"):
            self.assertIn(label, html)

    def test_snapshot_directory_is_never_modified(self):
        before = {
            os.path.join(root, f)
            for root, _, files in os.walk(FIXTURE_SNAPSHOT)
            for f in files
        }
        before_mtimes = {p: os.path.getmtime(p) for p in before}
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", self.tmp_dir])
        after = {
            os.path.join(root, f)
            for root, _, files in os.walk(FIXTURE_SNAPSHOT)
            for f in files
        }
        self.assertEqual(before, after)
        for p in after:
            self.assertEqual(before_mtimes[p], os.path.getmtime(p))


class NoWriteEndpointSourceScanTests(unittest.TestCase):
    def test_package_source_never_calls_write_shaped_requests_functions(self):
        package_dir = os.path.dirname(os.path.dirname(__file__))
        forbidden = ("requests.put(", "requests.patch(", "requests.delete(")
        for filename in os.listdir(package_dir):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(package_dir, filename)
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            for pattern in forbidden:
                self.assertNotIn(pattern, source, f"{pattern} found in {filename}")


if __name__ == "__main__":
    unittest.main()
