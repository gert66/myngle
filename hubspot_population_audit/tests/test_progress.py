import json
import os
import tempfile
import unittest

from hubspot_population_audit.progress import ProgressWriter


class ProgressWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp_dir, ignore_errors=True)
        self.path = os.path.join(self.tmp_dir, "progress.json")

    def test_initial_write_has_required_top_level_fields(self):
        ProgressWriter(self.path)
        with open(self.path) as fh:
            state = json.load(fh)
        for field in ("schema_version", "job", "phase", "phases", "counters", "started_at", "updated_at", "last_error"):
            self.assertIn(field, state)

    def test_no_leftover_tmp_file_after_writes(self):
        writer = ProgressWriter(self.path)
        writer.set_phase("running")
        writer.set_subprocess_status("snapshot_load", "completed")
        writer.set_counter("records_loaded.companies", 5)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertTrue(os.path.exists(self.path))

    def test_subprocess_status_is_queryable_by_name(self):
        writer = ProgressWriter(self.path)
        writer.set_subprocess_status("reconciliation", "running")
        writer.set_subprocess_status("reconciliation", "completed")
        with open(self.path) as fh:
            state = json.load(fh)
        names = {p["name"]: p["status"] for p in state["phases"]}
        self.assertEqual(names["reconciliation"], "completed")
        self.assertEqual(len(state["phases"]), 1)  # updated in place, not duplicated

    def test_counters_accumulate(self):
        writer = ProgressWriter(self.path)
        writer.increment_counter("cache_hits")
        writer.increment_counter("cache_hits", by=2)
        with open(self.path) as fh:
            state = json.load(fh)
        self.assertEqual(state["counters"]["cache_hits"], 3)

    def test_last_error_defaults_to_none_and_can_be_set(self):
        writer = ProgressWriter(self.path)
        with open(self.path) as fh:
            state = json.load(fh)
        self.assertIsNone(state["last_error"])
        writer.set_error("boom")
        with open(self.path) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
