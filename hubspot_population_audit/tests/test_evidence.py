import json
import os
import shutil
import tempfile
import unittest

from hubspot_population_audit import cohorts, evidence
from hubspot_population_audit.cli import run_population_audit
from hubspot_population_audit.fixtures.cohort_snapshot_builder import build_cohort_snapshot
from hubspot_population_audit.fixtures.fake_client import FakeAssociationError, FakeReadOnlyClient
from hubspot_population_audit.hubspot_client import is_read_path
from hubspot_population_audit.snapshot import Snapshot

FIXTURE_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "sample_snapshot"
)


class SamplingPlanTests(unittest.TestCase):
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

    def test_wave_stratum_is_sampled_not_full_fetch(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=1)
        wave_strata = [s for s in plan["companies"]["strata"] if s["kind"] == "wave"]
        self.assertEqual(len(wave_strata), 1)
        stratum = wave_strata[0]
        self.assertEqual(stratum["population_size"], self.facts["wave_company_count"])
        self.assertEqual(stratum["method"], "uniform_random_sample")
        self.assertEqual(stratum["sample_size"], evidence.DEFAULT_SAMPLE_SIZE)
        self.assertIn("95%", stratum["uncertainty_note"])

    def test_small_by_month_stratum_is_full_fetch(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=1)
        by_month = {s["key"]: s for s in plan["companies"]["strata"] if s["kind"] == "by_month"}
        stratum = by_month["2022-03"]
        self.assertEqual(stratum["method"], "full_fetch")
        self.assertEqual(stratum["sample_size"], stratum["population_size"])
        self.assertIn("no sampling uncertainty", stratum["uncertainty_note"])

    def test_seed_stability(self):
        plan_a = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=42)
        plan_b = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=42)
        self.assertEqual(plan_a["companies"]["unique_ids"], plan_b["companies"]["unique_ids"])

    def test_different_seed_changes_the_sample(self):
        plan_a = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=1)
        plan_b = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=2)
        wave_a = next(s for s in plan_a["companies"]["strata"] if s["kind"] == "wave")
        wave_b = next(s for s in plan_b["companies"]["strata"] if s["kind"] == "wave")
        self.assertNotEqual(wave_a["sample_ids"], wave_b["sample_ids"])

    def test_ids_deduplicated_across_overlapping_strata(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=1)
        strata = plan["companies"]["strata"]
        total_stratum_samples = sum(s["sample_size"] for s in strata)
        # The Sep-2023 wave stratum and the "2023-09" by_month stratum overlap heavily,
        # so the deduplicated union must be strictly smaller than the naive sum.
        self.assertLess(plan["companies"]["total_unique_ids"], total_stratum_samples)
        all_sample_ids = {rid for s in strata for rid in s["sample_ids"]}
        self.assertEqual(set(plan["companies"]["unique_ids"]), all_sample_ids)

    def test_recent_activity_stratum_present(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.output_dir, seed=1)
        kinds = {s["kind"] for s in plan["companies"]["strata"]}
        self.assertIn("recent_activity", kinds)

    def test_empty_stratum_has_explicit_note(self):
        # Pinned, unconditional regression: an empty stratum's margin note must
        # always say so, regardless of what the fixture happens to produce.
        self.assertIn("Empty stratum", evidence._margin_note(0, 0, "full_fetch"))

        # Fixture-based check, kept in addition: force an empty recent_activity
        # window (far in the past relative to the fixture).
        plan = evidence.build_sampling_plan(
            self.cohort_analysis, self.output_dir, seed=1, recent_activity_days=0
        )
        recent = next(s for s in plan["companies"]["strata"] if s["kind"] == "recent_activity")
        if recent["population_size"] == 0:
            self.assertIn("Empty stratum", recent["uncertainty_note"])


class LookupCacheResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.cache_dir = os.path.join(self.tmp_dir, "cache", "evidence")

    def test_second_run_performs_zero_api_calls(self):
        client1 = FakeReadOnlyClient()
        cache1 = evidence.ObjectCache(self.cache_dir, "companies")
        rate_limiter = evidence.RateLimiter(0)
        counters1: dict = {}
        result1 = evidence.fetch_object_records(
            client1, "companies", ["1", "2", "3", "4"], ["hs_object_source"], cache1, rate_limiter, counters1, None
        )
        self.assertEqual(len(result1), 4)
        self.assertGreater(len(client1.calls), 0)

        client2 = FakeReadOnlyClient()
        cache2 = evidence.ObjectCache(self.cache_dir, "companies")  # fresh instance, same disk path
        counters2: dict = {}
        result2 = evidence.fetch_object_records(
            client2, "companies", ["1", "2", "3", "4"], ["hs_object_source"], cache2, rate_limiter, counters2, None
        )
        self.assertEqual(len(result2), 4)
        self.assertEqual(client2.calls, [])
        self.assertEqual(counters2["cache_hits"], 4)

    def test_association_cache_resumes_too(self):
        client1 = FakeReadOnlyClient(deny_deals=False)
        cache1 = evidence.AssociationCache(self.cache_dir, "companies", "contacts")
        rate_limiter = evidence.RateLimiter(0)
        counters1: dict = {}
        evidence.fetch_associations(
            client1, "companies", "contacts", ["1", "2", "3", "4"], cache1, rate_limiter, counters1, None, []
        )
        self.assertGreater(len(client1.calls), 0)

        client2 = FakeReadOnlyClient(deny_deals=False)
        cache2 = evidence.AssociationCache(self.cache_dir, "companies", "contacts")
        counters2: dict = {}
        evidence.fetch_associations(
            client2, "companies", "contacts", ["1", "2", "3", "4"], cache2, rate_limiter, counters2, None, []
        )
        self.assertEqual(client2.calls, [])


class _EchoBatchClient:
    """Minimal fake client (no canned-fixture files needed) that echoes back
    every requested id as found, and records the size of every batch it was
    called with -- used to exercise --max-lookups batch-trimming generically,
    independent of the small sample_snapshot fixture's id space."""

    def __init__(self):
        self.calls: list = []
        self.batch_sizes: list = []

    def batch_read(self, object_type: str, body: dict) -> dict:
        ids = [item["id"] for item in body.get("inputs", [])]
        self.calls.append(("POST", f"/crm/v3/objects/{object_type}/batch/read"))
        self.batch_sizes.append(len(ids))
        return {"results": [{"id": rid, "properties": {}} for rid in ids]}


class _AlwaysDenyAssociationClient:
    """Fake client whose association endpoint always raises a 403, used to
    prove ``fetch_associations`` stops after the first 403 for a pair
    instead of re-hitting the endpoint once per 100-id batch."""

    def __init__(self):
        self.calls: list = []

    def associations_batch_read(self, from_object_type: str, to_object_type: str, ids) -> dict:
        self.calls.append(("POST", f"/crm/v4/associations/{from_object_type}/{to_object_type}/batch/read"))
        raise FakeAssociationError(403)


class _PartialBatchClient:
    """Fake client that only returns a subset of requested ids, simulating
    HubSpot records deleted/merged since the snapshot was taken."""

    def __init__(self, missing_ids):
        self.calls: list = []
        self.missing_ids = set(missing_ids)

    def batch_read(self, object_type: str, body: dict) -> dict:
        ids = [item["id"] for item in body.get("inputs", [])]
        self.calls.append(("POST", f"/crm/v3/objects/{object_type}/batch/read"))
        return {
            "results": [
                {"id": rid, "properties": {"hs_object_source": "CRM_UI"}}
                for rid in ids
                if rid not in self.missing_ids
            ]
        }


class MaxLookupsBatchTrimmingTests(unittest.TestCase):
    def test_max_lookups_trims_the_final_batch_to_the_remaining_cap(self):
        client = _EchoBatchClient()
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        cache = evidence.ObjectCache(os.path.join(tmp_dir, "cache", "evidence"), "companies")
        rate_limiter = evidence.RateLimiter(0)
        counters: dict = {}
        ids = [str(i) for i in range(250)]
        result = evidence.fetch_object_records(
            client, "companies", ids, ["hs_object_source"], cache, rate_limiter, counters, 150
        )
        self.assertEqual(client.batch_sizes, [100, 50])
        self.assertEqual(counters["api_calls"], 2)
        self.assertEqual(len(result), 150)


class NotFoundCacheMarkerTests(unittest.TestCase):
    def test_ids_missing_from_the_response_are_cached_as_not_found_and_not_refetched(self):
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        cache_dir = os.path.join(tmp_dir, "cache", "evidence")
        ids = ["1", "2", "3"]

        client1 = _PartialBatchClient(missing_ids={"2"})
        cache1 = evidence.ObjectCache(cache_dir, "companies")
        rate_limiter = evidence.RateLimiter(0)
        counters1: dict = {}
        result1 = evidence.fetch_object_records(
            client1, "companies", ids, ["hs_object_source"], cache1, rate_limiter, counters1, None
        )
        self.assertEqual(len(result1), 3)
        self.assertTrue(result1["2"]["not_found"])
        self.assertEqual(result1["2"]["properties"], {})

        # Resume: the same ids (including the not_found one) must never be refetched.
        client2 = _PartialBatchClient(missing_ids={"2"})
        cache2 = evidence.ObjectCache(cache_dir, "companies")
        counters2: dict = {}
        result2 = evidence.fetch_object_records(
            client2, "companies", ids, ["hs_object_source"], cache2, rate_limiter, counters2, None
        )
        self.assertEqual(client2.calls, [])
        self.assertEqual(counters2["cache_hits"], 3)
        self.assertTrue(result2["2"]["not_found"])


class Association403StopsAfterFirstBatchTests(unittest.TestCase):
    def test_only_one_call_and_one_gap_for_a_denied_pair_across_multiple_batches(self):
        client = _AlwaysDenyAssociationClient()
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        cache = evidence.AssociationCache(os.path.join(tmp_dir, "cache", "evidence"), "companies", "deals")
        rate_limiter = evidence.RateLimiter(0)
        counters: dict = {}
        gaps: list = []
        ids = [str(i) for i in range(250)]  # would span 3 batches of 100/100/50 if not stopped early
        evidence.fetch_associations(client, "companies", "deals", ids, cache, rate_limiter, counters, None, gaps)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(gaps), 1)
        self.assertIn("companies->deals", gaps[0])
        self.assertIn("403", gaps[0])


class ReadOnlyInvariantTests(unittest.TestCase):
    def test_all_recorded_calls_are_allowlisted_read_paths(self):
        client = FakeReadOnlyClient(deny_deals=False)
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        cohort_analysis = cohorts.build_cohort_analysis(snapshot, ["companies", "contacts"], tmp_dir)
        result = evidence.run_evidence(snapshot, cohort_analysis, tmp_dir, client=client)
        self.assertEqual(result["evidence"]["status"], "completed")
        self.assertGreater(len(client.calls), 0)
        for method, path in client.calls:
            self.assertEqual(method, "POST")
            self.assertTrue(is_read_path(path), path)

    def test_fake_client_write_methods_raise(self):
        client = FakeReadOnlyClient()
        with self.assertRaises(AssertionError):
            client.put()
        with self.assertRaises(AssertionError):
            client.patch()
        with self.assertRaises(AssertionError):
            client.delete()


class DealsAssociation403Tests(unittest.TestCase):
    def test_403_on_companies_to_deals_is_recorded_as_a_gap_and_run_continues(self):
        client = FakeReadOnlyClient(deny_deals=True)
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        cohort_analysis = cohorts.build_cohort_analysis(snapshot, ["companies", "contacts"], tmp_dir)
        result = evidence.run_evidence(snapshot, cohort_analysis, tmp_dir, client=client)
        # A denied association pair makes the run's evidence incomplete for
        # that pair, so the overall status must reflect that -- not "completed".
        self.assertEqual(result["evidence"]["status"], "partial")
        joined = " ".join(result["gaps"])
        self.assertIn("companies->deals", joined)
        self.assertIn("403", joined)
        self.assertTrue(any("companies_deals" in item for item in result["evidence"]["uncertainty"]))
        contacts_summaries = result["evidence"]["strata_summaries"]["contacts"]
        self.assertTrue(
            any(
                s["fetched_count"] and s["association_presence"]["has_deals_rate"] is not None
                for s in contacts_summaries
            )
        )


class ObjectBatchRead403Tests(unittest.TestCase):
    def test_403_on_contacts_batch_read_is_a_gap_marks_inaccessible_and_run_is_partial(self):
        client = FakeReadOnlyClient(deny_deals=False, deny_object_type="contacts")
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)

        result = run_population_audit(snapshot_dir=FIXTURE_SNAPSHOT, output_dir=tmp_dir, client=client)
        self.assertTrue(os.path.exists(os.path.join(tmp_dir, "evidence.json")))
        with open(os.path.join(tmp_dir, "evidence.json")) as fh:
            ev = json.load(fh)

        with open(os.path.join(tmp_dir, "gaps.json")) as fh:
            gaps = json.load(fh)
        joined = " ".join(gaps)
        self.assertIn("contacts", joined)
        self.assertIn("403", joined)
        self.assertTrue(any("contacts" in item for item in ev["inaccessible"]))

        contacts_calls = [c for c in client.calls if c == ("POST", "/crm/v3/objects/contacts/batch/read")]
        self.assertEqual(len(contacts_calls), 1)

        self.assertEqual(ev["status"], "partial")
        self.assertTrue(result["report_path"])  # the run must still complete, not raise


class CapAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.snapshot = Snapshot(FIXTURE_SNAPSHOT)
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot, ["companies", "contacts"], self.tmp_dir
        )

    def test_cache_hits_do_not_consume_the_max_lookups_budget(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.tmp_dir)
        cache_dir = os.path.join(self.tmp_dir, "cache", "evidence")
        cache = evidence.ObjectCache(cache_dir, "companies")
        cache.put_many({"id": rid, "properties": {}} for rid in plan["companies"]["unique_ids"])

        client = FakeReadOnlyClient(deny_deals=False)
        evidence.run_evidence(self.snapshot, self.cohort_analysis, self.tmp_dir, client=client, max_lookups=5)

        contacts_calls = [c for c in client.calls if c == ("POST", "/crm/v3/objects/contacts/batch/read")]
        assoc_calls = [
            c for c in client.calls if c == ("POST", "/crm/v4/associations/companies/contacts/batch/read")
        ]
        self.assertTrue(contacts_calls, "contacts batch-read must still be attempted despite the companies cache hits")
        self.assertTrue(assoc_calls, "companies->contacts association lookup must still be attempted")

    def test_second_run_new_fetched_counters_are_zero_while_resolved_counters_equal_plan_size(self):
        plan = evidence.build_sampling_plan(self.cohort_analysis, self.tmp_dir)

        client1 = FakeReadOnlyClient(deny_deals=False)
        result1 = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=client1, max_lookups=None
        )
        counters1 = result1["counters"]
        for object_type in ("companies", "contacts"):
            self.assertEqual(counters1[f"ids_fetched.{object_type}"], plan[object_type]["total_unique_ids"])

        client2 = FakeReadOnlyClient(deny_deals=False)
        result2 = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=client2, max_lookups=None
        )
        counters2 = result2["counters"]
        self.assertEqual(client2.calls, [])
        for object_type in ("companies", "contacts"):
            self.assertEqual(counters2.get(f"ids_new_fetched.{object_type}", 0), 0)
            self.assertEqual(counters2[f"ids_fetched.{object_type}"], plan[object_type]["total_unique_ids"])
        for from_type, to_type in evidence.ASSOCIATION_PAIRS:
            pair_key = f"{from_type}_{to_type}"
            self.assertEqual(counters2.get(f"associations_new_fetched.{pair_key}", 0), 0)


class LookupBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.snapshot = Snapshot(FIXTURE_SNAPSHOT)
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot, ["companies", "contacts"], self.tmp_dir
        )

    def test_default_none_covers_the_full_plan(self):
        client = FakeReadOnlyClient(deny_deals=False)
        result = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=client, max_lookups=None
        )
        budget = result["evidence"]["lookup_budget"]
        self.assertIsNone(budget["requested"])
        self.assertEqual(budget["effective"], budget["plan_total"])
        self.assertFalse(budget["truncated"])
        self.assertEqual(result["evidence"]["status"], "completed")
        for object_type in ("companies", "contacts"):
            summaries = result["evidence"]["strata_summaries"][object_type]
            self.assertTrue(
                all(s["fetched_count"] == s["sample_size"] for s in summaries if s["sample_size"])
            )

    def test_explicit_smaller_cap_is_truncated_with_a_next_actions_hint(self):
        client = FakeReadOnlyClient(deny_deals=False)
        result = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=client, max_lookups=1
        )
        budget = result["evidence"]["lookup_budget"]
        self.assertTrue(budget["truncated"])
        self.assertEqual(budget["requested"], 1)
        self.assertEqual(budget["effective"], 1)
        self.assertGreater(budget["plan_total"], 1)
        self.assertEqual(result["evidence"]["status"], "partial")
        self.assertTrue(any(str(budget["plan_total"]) in action for action in result["next_actions"]))


class OfflineRunTests(unittest.TestCase):
    def test_offline_writes_full_plan_without_any_client_call(self):
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        cohort_analysis = cohorts.build_cohort_analysis(snapshot, ["companies", "contacts"], tmp_dir)
        result = evidence.run_evidence(snapshot, cohort_analysis, tmp_dir, offline=True)
        self.assertEqual(result["evidence"]["status"], "skipped_offline")
        self.assertEqual(result["evidence"]["strata_summaries"], {})
        self.assertIn("companies", result["evidence"]["sampling_plan"])
        self.assertTrue(any("offline" in g.lower() for g in result["gaps"]))
        self.assertTrue(result["next_actions"])

    def test_no_token_present_also_skips_offline(self):
        self.assertNotIn(evidence.DEFAULT_TOKEN_ENV, os.environ)
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        cohort_analysis = cohorts.build_cohort_analysis(snapshot, ["companies", "contacts"], tmp_dir)
        result = evidence.run_evidence(snapshot, cohort_analysis, tmp_dir)
        self.assertEqual(result["evidence"]["status"], "skipped_offline")


class MaxLookupsTests(unittest.TestCase):
    def test_max_lookups_zero_performs_no_fetches(self):
        client = FakeReadOnlyClient(deny_deals=False)
        tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        snapshot = Snapshot(FIXTURE_SNAPSHOT)
        cohort_analysis = cohorts.build_cohort_analysis(snapshot, ["companies", "contacts"], tmp_dir)
        result = evidence.run_evidence(snapshot, cohort_analysis, tmp_dir, client=client, max_lookups=0)
        counters = result["counters"]
        total_fetched = sum(v for k, v in counters.items() if k.startswith("ids_fetched."))
        total_assoc = sum(v for k, v in counters.items() if k.startswith("associations_fetched."))
        self.assertEqual(total_fetched, 0)
        self.assertEqual(total_assoc, 0)
        self.assertEqual(counters["api_calls"], 0)


class StrataSummaryRecencyAndAssociationTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.snapshot = Snapshot(FIXTURE_SNAPSHOT)
        self.cohort_analysis = cohorts.build_cohort_analysis(
            self.snapshot, ["companies", "contacts"], self.tmp_dir
        )

    def test_notes_recency_buckets_present_when_property_available(self):
        result = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=FakeReadOnlyClient(deny_deals=False)
        )
        summaries = result["evidence"]["strata_summaries"]["companies"]
        populated = [s for s in summaries if s["fetched_count"]]
        self.assertTrue(populated)
        for summary in populated:
            recency_by_field = summary["activity_recency_by_field"]
            self.assertIn("notes_last_updated", recency_by_field)
            self.assertIn("notes_last_contacted", recency_by_field)
            self.assertNotEqual(recency_by_field["notes_last_updated"], "unavailable")
            self.assertEqual(set(recency_by_field["notes_last_updated"]), set(evidence.RECENCY_BUCKETS))

    def test_email_recency_buckets_present_for_contacts(self):
        result = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=FakeReadOnlyClient(deny_deals=False)
        )
        summaries = result["evidence"]["strata_summaries"]["contacts"]
        populated = [s for s in summaries if s["fetched_count"]]
        self.assertTrue(populated)
        for summary in populated:
            recency_by_field = summary["activity_recency_by_field"]
            self.assertNotEqual(recency_by_field["hs_email_last_send_date"], "unavailable")
            self.assertNotEqual(recency_by_field["hs_email_last_open_date"], "unavailable")

    def test_field_marked_unavailable_when_not_in_properties_requested(self):
        summary = evidence.aggregate_stratum(
            "companies",
            {
                "stratum_id": "companies:by_month:2099-01",
                "kind": "by_month",
                "key": "2099-01",
                "sample_ids": ["1"],
                "population_size": 1,
                "sample_size": 1,
                "method": "full_fetch",
                "uncertainty_note": "n/a",
            },
            {"1": {"id": "1", "properties": {"notes_last_updated": "2026-09-01T00:00:00Z"}}},
            {},
            {},
            evidence._parse_iso("2026-09-18T00:00:00Z"),
            [],
            properties_requested=["createdate"],  # notes_last_updated deliberately absent
        )
        self.assertEqual(summary["activity_recency_by_field"]["notes_last_updated"], "unavailable")

    def test_association_type_labels_summarized_per_stratum(self):
        result = evidence.run_evidence(
            self.snapshot, self.cohort_analysis, self.tmp_dir, client=FakeReadOnlyClient(deny_deals=False)
        )
        summaries = result["evidence"]["strata_summaries"]["companies"]
        populated = [s for s in summaries if s["fetched_count"]]
        self.assertTrue(populated)
        for summary in populated:
            assoc_types = summary["association_types"]
            self.assertIn("companies_contacts", assoc_types)
            self.assertIn("label_counts", assoc_types["companies_contacts"])
            self.assertIn("ids_with_any", assoc_types["companies_contacts"])


class RateLimiterTests(unittest.TestCase):
    def test_zero_rps_never_waits(self):
        limiter = evidence.RateLimiter(0)
        limiter.wait()
        limiter.wait()
        self.assertEqual(limiter.waits, 0)

    def test_positive_rps_can_record_a_wait(self):
        limiter = evidence.RateLimiter(1000000)  # tiny interval, still exercises the code path
        limiter.wait()
        limiter.wait()
        self.assertGreaterEqual(limiter.waits, 0)


class CliEndToEndWithFakeClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_full_cli_run_with_injected_fake_client_produces_all_artifacts(self):
        result = run_population_audit(
            snapshot_dir=FIXTURE_SNAPSHOT,
            output_dir=self.tmp_dir,
            client=FakeReadOnlyClient(deny_deals=True),
        )
        self.assertTrue(os.path.exists(result["report_path"]))
        for name in (
            "progress.json",
            "population_map.json",
            "cohort_analysis.json",
            "evidence.json",
            "gaps.json",
            "next_actions.json",
        ):
            self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, name)), name)

        with open(os.path.join(self.tmp_dir, "evidence.json")) as fh:
            ev = json.load(fh)
        # deny_deals=True denies the companies->deals association pair, so
        # the run's evidence is incomplete for that pair -- "partial", not
        # "completed".
        self.assertEqual(ev["status"], "partial")
        self.assertTrue(ev["strata_summaries"]["companies"])
        self.assertTrue(ev["strata_summaries"]["contacts"])
        self.assertFalse(ev["lookup_budget"]["truncated"])

        with open(os.path.join(self.tmp_dir, "progress.json")) as fh:
            progress = json.load(fh)
        phases = {p["name"]: p["status"] for p in progress["phases"]}
        self.assertEqual(phases["evidence"], "partial")
        self.assertGreater(progress["counters"]["evidence.api_calls"], 0)

        with open(os.path.join(self.tmp_dir, "gaps.json")) as fh:
            gaps = json.load(fh)
        self.assertIn("companies->deals", " ".join(gaps))

        for name in ("companies_evidence.csv.gz", "contacts_evidence.csv.gz"):
            self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "work", name)))

        with open(os.path.join(self.tmp_dir, "reports", "index.html")) as fh:
            report_html = fh.read()
        self.assertIn("Targeted evidence", report_html)

    def test_snapshot_directory_is_never_modified_with_live_lookups(self):
        before = {
            os.path.join(root, f)
            for root, _, files in os.walk(FIXTURE_SNAPSHOT)
            for f in files
        }
        before_mtimes = {p: os.path.getmtime(p) for p in before}
        run_population_audit(
            snapshot_dir=FIXTURE_SNAPSHOT,
            output_dir=self.tmp_dir,
            client=FakeReadOnlyClient(deny_deals=True),
        )
        after = {
            os.path.join(root, f)
            for root, _, files in os.walk(FIXTURE_SNAPSHOT)
            for f in files
        }
        self.assertEqual(before, after)
        for p in after:
            self.assertEqual(before_mtimes[p], os.path.getmtime(p))


if __name__ == "__main__":
    unittest.main()
