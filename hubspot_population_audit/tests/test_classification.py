import csv
import gzip
import json
import os
import shutil
import tempfile
import unittest

from hubspot_population_audit import classification, cohorts
from hubspot_population_audit.cli import main, run_population_audit
from hubspot_population_audit.fixtures.cohort_snapshot_builder import build_cohort_snapshot
from hubspot_population_audit.reconcile import reconcile_all
from hubspot_population_audit.snapshot import Snapshot

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)

ALL_BUCKETS = set(classification.ALL_BUCKETS)


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row))
            fh.write("\n")


def _envelope(record, extracted_at="2026-09-18T00:00:00Z", page_index=0):
    return {"record": record, "extracted_at": extracted_at, "page_index": page_index}


class ClassifyWaveUnitTests(unittest.TestCase):
    """Direct, pure-function tests of the wave 2x2 decision table -- no
    snapshot or CLI plumbing needed, matching this package's precedent of
    unit-testing private helpers directly (e.g. evidence._margin_note)."""

    def _wave(self, presence_rate, untouched_rate, field="domain"):
        return {
            "wave_id": "wave-1",
            "start": "2023-09-12",
            "end": "2023-09-13",
            "total_records": 300,
            "share_of_population": 0.1,
            "presence_field": field,
            "presence_rate": presence_rate,
            "untouched_since_creation_rate": untouched_rate,
            "alternative_explanations": list(cohorts.ALTERNATIVE_EXPLANATIONS),
        }

    def test_low_presence_high_untouched_is_historical_import_without_evidence(self):
        result = classification._classify_wave(self._wave(0.05, 0.95), None)
        self.assertEqual(result["bucket"], "historical_import")
        self.assertEqual(result["confidence"], "low_no_evidence_sample")
        self.assertTrue(any("genuine campaign" in r for r in result["rationale"]))

    def test_low_presence_high_untouched_with_evidence_is_sample_calibrated(self):
        stratum = {
            "fetched_count": 150,
            "uncertainty_note": "n=150",
            "source_distribution": {"IMPORT": 140},
            "lifecyclestage_distribution": {"unknown": 145},
            "association_presence": {"has_contacts_rate": 0.02},
        }
        result = classification._classify_wave(self._wave(0.05, 0.95), stratum)
        self.assertEqual(result["bucket"], "historical_import")
        self.assertEqual(result["confidence"], "medium_sample_calibrated")

    def test_low_presence_high_untouched_with_enrichment_source_hint(self):
        stratum = {
            "fetched_count": 150,
            "uncertainty_note": "n=150",
            "source_distribution": {"BULK_ENRICHMENT_TOOL": 140},
            "lifecyclestage_distribution": {"unknown": 145},
            "association_presence": {"has_contacts_rate": 0.02},
        }
        result = classification._classify_wave(self._wave(0.05, 0.95), stratum)
        self.assertEqual(result["bucket"], "enrichment_or_bulk")

    def test_high_presence_high_untouched_is_enrichment_or_bulk(self):
        result = classification._classify_wave(self._wave(0.9, 0.9), None)
        self.assertEqual(result["bucket"], "enrichment_or_bulk")

    def test_high_presence_low_untouched_is_active_other(self):
        result = classification._classify_wave(self._wave(0.9, 0.1), None)
        self.assertEqual(result["bucket"], "active_other")
        self.assertEqual(result["confidence"], "medium")
        self.assertTrue(any("genuine bulk onboarding" in r for r in result["rationale"]))

    def test_mixed_signal_is_uncertain(self):
        result = classification._classify_wave(self._wave(0.5, 0.4), None)
        self.assertEqual(result["bucket"], "uncertain")
        self.assertEqual(result["confidence"], "mixed_signal")

    def test_alternative_explanations_are_recorded_in_rationale(self):
        wave = self._wave(0.05, 0.95)
        result = classification._classify_wave(wave, None)
        joined = " ".join(result["rationale"])
        for explanation in cohorts.ALTERNATIVE_EXPLANATIONS:
            self.assertIn(explanation, joined)


class SyntheticWaveSnapshotTests(unittest.TestCase):
    """Uses the same synthetic bulk-import-wave snapshot builder as
    test_cohorts.py (lowered thresholds so the wave is actually detected)
    to exercise the full classify_object_type pipeline end to end, offline
    (no evidence sample), and prove the invariant that bucket counts sum
    exactly to reconcile.py's unique_id_count."""

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
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot,
            ["companies", "contacts"],
            self.output_dir,
            wave_abs_min=50,
            wave_factor=8.0,
            burst_min_records=50,
        )
        self.reconciliation = reconcile_all(self.snapshot, ["companies", "contacts"])
        self.offline_evidence = {"status": "skipped_offline", "strata_summaries": {}}

    def test_bucket_counts_sum_to_reconciled_total(self):
        for object_type in ("companies", "contacts"):
            with self.subTest(object_type=object_type):
                result = classification.classify_object_type(
                    self.snapshot, object_type, self.cohort_analysis, self.offline_evidence, self.output_dir
                )
                total = sum(result["bucket_counts"].values())
                self.assertEqual(total, self.reconciliation[object_type]["unique_id_count"])
                self.assertEqual(result["unique_id_count"], self.reconciliation[object_type]["unique_id_count"])

    def test_every_bucket_key_present_even_if_zero(self):
        result = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.offline_evidence, self.output_dir
        )
        self.assertEqual(set(result["bucket_counts"].keys()), ALL_BUCKETS)

    def test_wave_records_land_in_the_wave_calibrated_bucket(self):
        result = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.offline_evidence, self.output_dir
        )
        wave_id = self.cohort_analysis["companies"]["waves"][0]["wave_id"]
        expected_bucket = result["wave_rationale"][wave_id]["bucket"]
        # The synthetic wave has low domain presence and high untouched rate
        # (see cohort_snapshot_builder.py), so it must not land in an
        # "active" bucket.
        self.assertIn(expected_bucket, ("historical_import", "enrichment_or_bulk", "uncertain"))
        self.assertEqual(result["bucket_counts"][expected_bucket] >= self.facts["wave_company_count"] - 1, True)

    def test_classification_feature_csv_has_one_row_per_unique_record(self):
        result = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.offline_evidence, self.output_dir
        )
        path = os.path.join(self.output_dir, result["classification_feature_table"])
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), self.facts["unique_companies"])
        for row in rows:
            self.assertIn(row["bucket"], ALL_BUCKETS)

    def test_determinism_two_runs_produce_identical_bucket_counts(self):
        result_a = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.offline_evidence, self.output_dir
        )
        result_b = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.offline_evidence, self.output_dir
        )
        self.assertEqual(result_a["bucket_counts"], result_b["bucket_counts"])
        self.assertEqual(result_a["rule_counts"], result_b["rule_counts"])
        self.assertEqual(result_a["wave_rationale"], result_b["wave_rationale"])


class ExplicitLifecycleAndSourceRuleTests(unittest.TestCase):
    """A small, hand-built snapshot (outside any detected wave) exercising
    the highest-priority record-level rules directly, so each bucket has at
    least one unambiguous, clearly-triggering fixture record."""

    def setUp(self):
        self.snapshot_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.snapshot_root, ignore_errors=True)
        self.output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.output_dir, ignore_errors=True)

        companies = [
            {
                "id": "cust-1",
                "properties": {
                    "name": "Acme BV",
                    "domain": "acme.example",
                    "createdate": "2022-01-05T10:00:00Z",
                    "hs_lastmodifieddate": "2026-09-01T10:00:00Z",
                    "lifecyclestage": "customer",
                },
            },
            {
                "id": "prospect-1",
                "properties": {
                    "name": "Prospect NV",
                    "domain": "prospect.example",
                    "createdate": "2022-02-05T10:00:00Z",
                    "hs_lastmodifieddate": "2026-08-01T10:00:00Z",
                    "lifecyclestage": "opportunity",
                },
            },
            {
                "id": "import-1",
                "properties": {
                    "name": "Bulk Import Ltd",
                    "domain": "import.example",
                    "createdate": "2022-03-05T10:00:00Z",
                    "hs_lastmodifieddate": "2022-03-05T10:00:05Z",
                    "hs_object_source": "IMPORT",
                },
            },
            {
                "id": "legacy-1",
                "properties": {
                    "name": "Legacy Holdings",
                    "domain": "legacy.example",
                    "createdate": "2018-01-05T10:00:00Z",
                    "hs_lastmodifieddate": "2018-01-05T10:00:05Z",
                },
            },
            {
                "id": "uncertain-1",
                "properties": {
                    "name": "No Signal Co",
                    "createdate": "2020-05-01T10:00:00Z",
                },
            },
            # Same-domain regression: two independent companies sharing a
            # domain, neither an import/legacy record -- must classify
            # purely on their own recency/identity signals, not on the
            # domain match.
            {
                "id": "shared-domain-1",
                "properties": {
                    "name": "Shared Domain Co A",
                    "domain": "shared-group.example",
                    "createdate": "2022-04-01T10:00:00Z",
                    "hs_lastmodifieddate": "2026-09-05T10:00:00Z",
                    "lifecyclestage": "customer",
                },
            },
            {
                "id": "shared-domain-2",
                "properties": {
                    "name": "Shared Domain Co B",
                    "domain": "shared-group.example",
                    "createdate": "2022-04-02T10:00:00Z",
                    "hs_lastmodifieddate": "2026-08-15T10:00:00Z",
                    "lifecyclestage": "opportunity",
                },
            },
        ]
        _write_jsonl(
            os.path.join(self.snapshot_root, "raw", "companies.jsonl"),
            [_envelope(c) for c in companies],
        )
        _write_jsonl(os.path.join(self.snapshot_root, "raw", "contacts.jsonl"), [])

        self.snapshot = Snapshot(self.snapshot_root)
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot, ["companies", "contacts"], self.output_dir
        )
        self.evidence = {"status": "skipped_offline", "strata_summaries": {}}
        self.result = classification.classify_object_type(
            self.snapshot, "companies", self.cohort_analysis, self.evidence, self.output_dir
        )
        self.by_id = self._load_rows()

    def _load_rows(self):
        path = os.path.join(self.output_dir, self.result["classification_feature_table"])
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            return {row["id"]: row for row in csv.DictReader(fh)}

    def test_explicit_customer_lifecycle_wins(self):
        self.assertEqual(self.by_id["cust-1"]["bucket"], "operational_customer")
        self.assertEqual(self.by_id["cust-1"]["rule_id"], "explicit_customer_lifecycle")
        self.assertEqual(self.by_id["cust-1"]["confidence"], "high")

    def test_explicit_prospect_lifecycle_wins(self):
        self.assertEqual(self.by_id["prospect-1"]["bucket"], "operational_prospect")
        self.assertEqual(self.by_id["prospect-1"]["rule_id"], "explicit_prospect_lifecycle")

    def test_explicit_bulk_source_with_no_lifecycle_is_historical_import(self):
        self.assertEqual(self.by_id["import-1"]["bucket"], "historical_import")
        self.assertEqual(self.by_id["import-1"]["rule_id"], "explicit_bulk_source_no_lifecycle")

    def test_stale_untouched_identity_is_legacy_candidate(self):
        self.assertEqual(self.by_id["legacy-1"]["bucket"], "legacy_or_obsolete_candidate")

    def test_no_identity_no_recency_lands_in_uncertain(self):
        self.assertEqual(self.by_id["uncertain-1"]["bucket"], "uncertain")
        self.assertEqual(self.by_id["uncertain-1"]["confidence"], "insufficient_signal")

    def test_same_domain_companies_are_not_forced_into_the_same_bucket_or_historical(self):
        # Regression test: sharing a domain must never, by itself, push a
        # company toward historical_import/legacy -- each is classified
        # purely on its own lifecycle/recency signal.
        self.assertEqual(self.by_id["shared-domain-1"]["bucket"], "operational_customer")
        self.assertEqual(self.by_id["shared-domain-2"]["bucket"], "operational_prospect")
        for record_id in ("shared-domain-1", "shared-domain-2"):
            self.assertNotIn(self.by_id[record_id]["bucket"], ("historical_import", "legacy_or_obsolete_candidate"))

    def test_bucket_counts_sum_to_all_companies(self):
        self.assertEqual(sum(self.result["bucket_counts"].values()), len(self.by_id))


class MonthCalibrationTests(unittest.TestCase):
    def test_dominant_customer_lifecycle_calibrates_the_bucket(self):
        stratum = {
            "fetched_count": 100,
            "uncertainty_note": "n=100",
            "lifecyclestage_distribution": {"customer": 80, "unknown": 20},
        }
        calibration = classification._classify_month_calibration(stratum)
        self.assertEqual(calibration["bucket"], "operational_customer")

    def test_no_dominant_stage_returns_none(self):
        stratum = {
            "fetched_count": 100,
            "uncertainty_note": "n=100",
            "lifecyclestage_distribution": {"customer": 20, "lead": 20, "unknown": 60},
        }
        self.assertIsNone(classification._classify_month_calibration(stratum))

    def test_empty_stratum_returns_none(self):
        self.assertIsNone(classification._classify_month_calibration(None))
        self.assertIsNone(classification._classify_month_calibration({"fetched_count": 0}))


class BuildPopulationMapTests(unittest.TestCase):
    def setUp(self):
        self.snapshot_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.snapshot_root, ignore_errors=True)
        self.facts = build_cohort_snapshot(self.snapshot_root)
        self.snapshot = Snapshot(self.snapshot_root)
        self.output_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.output_dir, ignore_errors=True)
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot, ["companies", "contacts"], self.output_dir, wave_abs_min=50, wave_factor=8.0,
        )
        self.reconciliation = reconcile_all(self.snapshot, ["companies", "contacts"])
        self.evidence = {"status": "skipped_offline", "strata_summaries": {}}

    def test_reconciliation_crosscheck_matches_for_every_object_type(self):
        built = classification.build_population_map(
            self.snapshot, self.cohort_analysis, self.evidence, self.reconciliation,
            ["companies", "contacts"], self.output_dir,
        )
        population_map = built["population_map"]
        for object_type in ("companies", "contacts"):
            crosscheck = population_map["reconciliation_crosscheck"][object_type]
            self.assertTrue(crosscheck["matches"])
            self.assertEqual(crosscheck["classified_total"], crosscheck["reconciled_total"])
            self.assertEqual(
                sum(population_map["inferred_classification"][object_type]["bucket_counts"].values()),
                crosscheck["reconciled_total"],
            )

    def test_schema_has_required_top_level_sections(self):
        built = classification.build_population_map(
            self.snapshot, self.cohort_analysis, self.evidence, self.reconciliation,
            ["companies", "contacts"], self.output_dir,
        )
        population_map = built["population_map"]
        for key in (
            "schema_version", "status", "buckets", "reconciliation", "reconciliation_crosscheck",
            "unclassified", "observed_facts", "inferred_classification", "uncertainty", "inaccessible",
        ):
            self.assertIn(key, population_map)
        self.assertEqual(population_map["status"], "completed")
        self.assertEqual(set(population_map["buckets"]), ALL_BUCKETS)
        for object_type in ("companies", "contacts"):
            entry = population_map["inferred_classification"][object_type]
            for key in (
                "bucket_counts", "bucket_percentages", "rule_counts", "bucket_rule_counts",
                "wave_rationale", "examples", "classification_feature_table",
            ):
                self.assertIn(key, entry)

    def test_offline_evidence_produces_a_gap_about_missing_calibration(self):
        built = classification.build_population_map(
            self.snapshot, self.cohort_analysis, self.evidence, self.reconciliation,
            ["companies", "contacts"], self.output_dir,
        )
        self.assertTrue(any("skipped_offline" in g for g in built["gaps"]))

    def test_low_confidence_waves_produce_an_explicit_gap(self):
        built = classification.build_population_map(
            self.snapshot, self.cohort_analysis, self.evidence, self.reconciliation,
            ["companies", "contacts"], self.output_dir,
        )
        joined = " ".join(built["gaps"])
        self.assertIn("low_no_evidence_sample", joined)


class DeterministicCliRunTests(unittest.TestCase):
    """End-to-end determinism: running the CLI twice against the same
    fixture snapshot (fresh, independent output dirs, same default seed)
    must produce identical population_map.json bucket counts."""

    def test_two_cli_runs_produce_identical_population_map_bucket_counts(self):
        out_a = tempfile.mkdtemp()
        out_b = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out_a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, out_b, ignore_errors=True)

        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", out_a])
        main(["run", "--snapshot", FIXTURE_SNAPSHOT, "--output-dir", out_b])

        with open(os.path.join(out_a, "population_map.json")) as fh:
            map_a = json.load(fh)
        with open(os.path.join(out_b, "population_map.json")) as fh:
            map_b = json.load(fh)

        self.assertEqual(
            map_a["inferred_classification"]["companies"]["bucket_counts"],
            map_b["inferred_classification"]["companies"]["bucket_counts"],
        )
        self.assertEqual(
            map_a["inferred_classification"]["contacts"]["bucket_counts"],
            map_b["inferred_classification"]["contacts"]["bucket_counts"],
        )


class CliPopulationMapIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_population_map_sums_match_reconciliation_for_the_fixture(self):
        result = run_population_audit(snapshot_dir=FIXTURE_SNAPSHOT, output_dir=self.tmp_dir)
        population_map = result["population_map"]
        for object_type in ("companies", "contacts"):
            reconciled_total = population_map["reconciliation"][object_type]["unique_id_count"]
            classified_total = sum(
                population_map["inferred_classification"][object_type]["bucket_counts"].values()
            )
            self.assertEqual(classified_total, reconciled_total)

    def test_no_write_capable_client_calls_are_reachable_from_classification(self):
        # classification.py never imports hubspot_client at all -- it takes
        # already-computed snapshot/cohort/evidence data only.
        classification_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "classification.py"
        )
        with open(classification_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("hubspot_client", source)
        self.assertNotIn("requests.", source)

    def test_progress_json_exposes_classification_counters(self):
        run_population_audit(snapshot_dir=FIXTURE_SNAPSHOT, output_dir=self.tmp_dir)
        with open(os.path.join(self.tmp_dir, "progress.json")) as fh:
            progress = json.load(fh)
        phases = {p["name"]: p["status"] for p in progress["phases"]}
        self.assertEqual(phases["classification"], "completed")
        counters = progress["counters"]
        self.assertTrue(any(k.startswith("classification.records_classified.") for k in counters))
        self.assertTrue(any(k.startswith("classification.bucket_counts.") for k in counters))

    def test_gaps_and_next_actions_reflect_classification(self):
        run_population_audit(snapshot_dir=FIXTURE_SNAPSHOT, output_dir=self.tmp_dir)
        with open(os.path.join(self.tmp_dir, "gaps.json")) as fh:
            gaps = json.load(fh)
        with open(os.path.join(self.tmp_dir, "next_actions.json")) as fh:
            next_actions = json.load(fh)
        self.assertTrue(any("skipped_offline" in g and "calibrat" in g for g in gaps))
        self.assertTrue(any("lifecycle/source/association samples" in a for a in next_actions))

    def test_html_report_contains_population_reconciliation_section(self):
        run_population_audit(snapshot_dir=FIXTURE_SNAPSHOT, output_dir=self.tmp_dir)
        with open(os.path.join(self.tmp_dir, "reports", "index.html")) as fh:
            html = fh.read()
        self.assertIn("Population reconciliation by bucket", html)
        self.assertIn("operational_customer", html)
        self.assertIn("uncertain", html)


if __name__ == "__main__":
    unittest.main()
