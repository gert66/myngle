import csv
import gzip
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from hubspot_population_audit import cohorts
from hubspot_population_audit.cli import main
from hubspot_population_audit.fixtures.cohort_snapshot_builder import (
    MISSING_CREATEDATE_COUNT,
    SPIKE_DAY,
    UNPARSABLE_CREATEDATE_COUNT,
    WAVE_COMPANY_BURST_MINUTE_END,
    WAVE_COMPANY_BURST_MINUTE_START,
    WAVE_COMPANY_COUNT,
    WAVE_CONTACT_COUNT,
    WAVE_END,
    WAVE_PRESENCE_STRIDE,
    WAVE_START,
    WAVE_TOUCHED_TAIL,
    build_cohort_snapshot,
)
from hubspot_population_audit.snapshot import Snapshot

UNKNOWN_CREATEDATE_COUNT = MISSING_CREATEDATE_COUNT + UNPARSABLE_CREATEDATE_COUNT
PER_DAY_COMPANY = WAVE_COMPANY_COUNT // 2
PER_DAY_CONTACT = WAVE_CONTACT_COUNT // 2
COMPANY_PRESENT_PER_DAY = (PER_DAY_COMPANY - 1) // WAVE_PRESENCE_STRIDE + 1
CONTACT_PRESENT_PER_DAY = (PER_DAY_CONTACT - 1) // WAVE_PRESENCE_STRIDE + 1
COMPANY_BURST_PER_DAY = PER_DAY_COMPANY - 10
CONTACT_BURST_PER_DAY = PER_DAY_CONTACT - 10


class CohortAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot_root = tempfile.mkdtemp()
        cls.facts = build_cohort_snapshot(cls.snapshot_root)
        cls.snapshot = Snapshot(cls.snapshot_root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.snapshot_root, ignore_errors=True)

    def setUp(self):
        self.output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.output_dir, ignore_errors=True)

    def _analyze(self, object_type, **kwargs):
        return cohorts.analyze_object_type(self.snapshot, object_type, self.output_dir, **kwargs)

    def test_by_day_week_month_totals_match_unique_count_including_unknown(self):
        for object_type, expected_unique in (
            ("companies", self.facts["unique_companies"]),
            ("contacts", self.facts["unique_contacts"]),
        ):
            with self.subTest(object_type=object_type):
                analysis = self._analyze(object_type)
                self.assertEqual(analysis["unique_id_count"], expected_unique)
                self.assertEqual(sum(analysis["by_day"].values()), expected_unique)
                self.assertEqual(sum(analysis["by_week"].values()), expected_unique)
                self.assertEqual(sum(analysis["by_month"].values()), expected_unique)
                self.assertIn("unknown", analysis["by_day"])
                self.assertIn("unknown", analysis["by_week"])
                self.assertIn("unknown", analysis["by_month"])
                self.assertEqual(analysis["by_day"]["unknown"], UNKNOWN_CREATEDATE_COUNT)
                self.assertEqual(analysis["unknown_createdate_count"], UNKNOWN_CREATEDATE_COUNT)

    def test_duplicate_envelope_counted_once(self):
        analysis = self._analyze("companies")
        self.assertEqual(analysis["raw_record_count"], self.facts["raw_company_envelopes"])
        self.assertEqual(analysis["unique_id_count"], self.facts["unique_companies"])
        self.assertLess(analysis["unique_id_count"], analysis["raw_record_count"])

    def test_wave_detected_for_companies_with_overridden_thresholds(self):
        analysis = self._analyze("companies", wave_abs_min=50, wave_factor=8.0, burst_min_records=50)
        self.assertEqual(len(analysis["waves"]), 1)
        wave = analysis["waves"][0]
        self.assertEqual(wave["start"], WAVE_START.isoformat())
        self.assertEqual(wave["end"], WAVE_END.isoformat())
        self.assertEqual(wave["total_records"], WAVE_COMPANY_COUNT)
        self.assertAlmostEqual(wave["share_of_population"], WAVE_COMPANY_COUNT / self.facts["unique_companies"])
        expected_present = COMPANY_PRESENT_PER_DAY * 2
        expected_untouched = (PER_DAY_COMPANY - WAVE_TOUCHED_TAIL) * 2
        self.assertAlmostEqual(wave["presence_rate"], expected_present / WAVE_COMPANY_COUNT)
        self.assertAlmostEqual(wave["untouched_since_creation_rate"], expected_untouched / WAVE_COMPANY_COUNT)
        self.assertAlmostEqual(wave["name_present_rate"], 1.0)
        self.assertEqual(wave["presence_field"], "domain")
        self.assertEqual(set(wave["alternative_explanations"]), set(cohorts.ALTERNATIVE_EXPLANATIONS))
        self.assertGreater(wave["share_in_timestamp_bursts"], 0.9)

    def test_wave_detected_for_contacts_with_overridden_thresholds(self):
        analysis = self._analyze("contacts", wave_abs_min=50, wave_factor=8.0, burst_min_records=50)
        self.assertEqual(len(analysis["waves"]), 1)
        wave = analysis["waves"][0]
        self.assertEqual(wave["total_records"], WAVE_CONTACT_COUNT)
        expected_present = CONTACT_PRESENT_PER_DAY * 2
        expected_untouched = (PER_DAY_CONTACT - WAVE_TOUCHED_TAIL) * 2
        self.assertAlmostEqual(wave["presence_rate"], expected_present / WAVE_CONTACT_COUNT)
        self.assertAlmostEqual(wave["untouched_since_creation_rate"], expected_untouched / WAVE_CONTACT_COUNT)
        self.assertEqual(wave["presence_field"], "email")

    def test_moderate_spike_is_not_flagged(self):
        analysis = self._analyze("companies", wave_abs_min=50, wave_factor=8.0)
        for wave in analysis["waves"]:
            start = date.fromisoformat(wave["start"])
            end = date.fromisoformat(wave["end"])
            self.assertFalse(start <= SPIKE_DAY <= end)

    def test_default_thresholds_suppress_the_wave(self):
        # DEFAULT_WAVE_ABS_MIN (500) is well above the 150/day wave in this fixture.
        analysis = self._analyze("companies")
        self.assertEqual(analysis["waves"], [])

    def test_raising_abs_min_suppresses_a_previously_detected_wave(self):
        detected = self._analyze("companies", wave_abs_min=50, wave_factor=8.0)
        self.assertEqual(len(detected["waves"]), 1)
        suppressed = self._analyze("companies", wave_abs_min=1000, wave_factor=8.0)
        self.assertEqual(suppressed["waves"], [])

    def test_timestamp_bursts_are_found(self):
        analysis = self._analyze("companies", burst_min_records=50)
        minutes = {row["minute"]: row["count"] for row in analysis["timestamp_bursts"]["minutes"]}
        self.assertEqual(minutes.get(WAVE_COMPANY_BURST_MINUTE_START), COMPANY_BURST_PER_DAY)
        self.assertEqual(minutes.get(WAVE_COMPANY_BURST_MINUTE_END), COMPANY_BURST_PER_DAY)
        self.assertEqual(analysis["timestamp_bursts"]["total_records_in_bursts"], COMPANY_BURST_PER_DAY * 2)

    def test_sep_2023_focus_reports_the_wave(self):
        analysis = self._analyze("companies", wave_abs_min=50, wave_factor=8.0)
        focus = analysis["sep_2023_focus"]
        self.assertTrue(focus["detected_as_wave"])
        self.assertGreaterEqual(focus["count"], WAVE_COMPANY_COUNT)
        self.assertEqual(focus["by_day"]["2023-09-12"] + focus["by_day"]["2023-09-13"], WAVE_COMPANY_COUNT)

    def test_month_profile_rates_for_a_clean_baseline_month(self):
        analysis = self._analyze("companies")
        expected_count = 0
        d = date(2022, 3, 1)
        while d <= date(2022, 3, 31):
            expected_count += (d.toordinal() % 3) + 1
            d += timedelta(days=1)
        profile = analysis["month_profiles"]["2022-03"]
        self.assertEqual(profile["count"], expected_count)
        self.assertAlmostEqual(profile["presence_rate"], 1.0)
        self.assertAlmostEqual(profile["name_present_rate"], 1.0)
        self.assertAlmostEqual(profile["untouched_since_creation_rate"], 1.0)

    def test_feature_table_row_count_and_wave_id(self):
        analysis = self._analyze("companies", wave_abs_min=50, wave_factor=8.0)
        wave_id = analysis["waves"][0]["wave_id"]
        feature_path = os.path.join(self.output_dir, "work", "companies_features.csv.gz")
        self.assertTrue(os.path.exists(feature_path))
        with gzip.open(feature_path, "rt", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), self.facts["unique_companies"])
        wave_rows = [row for row in rows if row["id"].startswith("wc")]
        self.assertEqual(len(wave_rows), WAVE_COMPANY_COUNT)
        for row in wave_rows:
            self.assertEqual(row["wave_id"], wave_id)
        baseline_row = next(row for row in rows if row["id"] == "bc1")
        self.assertEqual(baseline_row["wave_id"], "")

    def test_unsupported_object_type_raises(self):
        with self.assertRaises(ValueError):
            self._analyze("deals")


class CliCohortEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot_root = tempfile.mkdtemp()
        cls.facts = build_cohort_snapshot(cls.snapshot_root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.snapshot_root, ignore_errors=True)

    def setUp(self):
        self.output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.output_dir, ignore_errors=True)

    def test_cli_run_produces_completed_cohort_analysis(self):
        exit_code = main(["run", "--snapshot", self.snapshot_root, "--output-dir", self.output_dir])
        self.assertEqual(exit_code, 0)

        with open(os.path.join(self.output_dir, "cohort_analysis.json")) as fh:
            cohort_analysis = json.load(fh)
        self.assertEqual(cohort_analysis["status"], "completed")
        self.assertIn("observed_facts", cohort_analysis["labels"])
        self.assertIn("inferred", cohort_analysis["labels"])
        self.assertIn("companies", cohort_analysis)
        self.assertIn("contacts", cohort_analysis)
        self.assertEqual(cohort_analysis["companies"]["unique_id_count"], self.facts["unique_companies"])

        with open(os.path.join(self.output_dir, "progress.json")) as fh:
            progress = json.load(fh)
        phases = {p["name"]: p for p in progress["phases"]}
        self.assertEqual(phases["cohort_analysis"]["status"], "completed")
        self.assertIn("records_scanned.companies", progress["counters"])
        self.assertIn("records_scanned.contacts", progress["counters"])
        self.assertEqual(progress["counters"]["records_scanned.companies"], self.facts["raw_company_envelopes"])

        with open(os.path.join(self.output_dir, "reports", "index.html")) as fh:
            report_html = fh.read()
        self.assertIn("Creation cohorts", report_html)
        self.assertIn("Bulk-import wave candidates", report_html)
        self.assertIn("Sep 2023 focus", report_html)

        for feature_name in ("companies_features.csv.gz", "contacts_features.csv.gz"):
            self.assertTrue(os.path.exists(os.path.join(self.output_dir, "work", feature_name)))

    def test_gaps_json_states_deals_and_default_property_limitations(self):
        main(["run", "--snapshot", self.snapshot_root, "--output-dir", self.output_dir])
        with open(os.path.join(self.output_dir, "gaps.json")) as fh:
            gaps = json.load(fh)
        joined = " ".join(gaps)
        self.assertIn("deals", joined.lower())
        self.assertIn("403", joined)
        self.assertIn("portal total", joined.lower())
        self.assertIn("default properties", joined.lower())


if __name__ == "__main__":
    unittest.main()
