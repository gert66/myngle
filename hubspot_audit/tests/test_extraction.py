import json
import os
import shutil
import tempfile
import unittest

from hubspot_audit.config import AuditConfig
from hubspot_audit.hubspot_client import build_client
from hubspot_audit.extraction import extract_object, read_jsonl

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


class ExtractionResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.config = AuditConfig.build("t", self.tmp_dir, mode="fixture", fixtures_dir=FIXTURES_DIR)
        self.client = build_client(self.config)
        self.raw_dir = os.path.join(self.tmp_dir, "raw")

    def test_partial_then_resume_yields_every_record_once(self):
        first = extract_object(self.client, "companies", self.raw_dir, page_size=20, max_pages=2)
        self.assertFalse(first["complete"])
        self.assertEqual(first["record_count"], 40)

        second = extract_object(self.client, "companies", self.raw_dir, page_size=20)
        self.assertTrue(second["resumed"])
        self.assertTrue(second["complete"])
        self.assertEqual(second["record_count"], 120)

        records = list(read_jsonl(os.path.join(self.raw_dir, "companies.jsonl")))
        ids = [r["record"]["id"] for r in records]
        self.assertEqual(len(ids), 120)
        self.assertEqual(len(set(ids)), 120)  # no duplicates from the resume

    def test_checkpoint_file_tracks_progress(self):
        extract_object(self.client, "companies", self.raw_dir, page_size=50, max_pages=1)
        checkpoint_path = os.path.join(self.raw_dir, "_checkpoint.json")
        with open(checkpoint_path) as fh:
            checkpoint = json.load(fh)
        self.assertIn("companies", checkpoint)
        self.assertFalse(checkpoint["companies"]["complete"])
        self.assertEqual(checkpoint["companies"]["record_count"], 50)

    def test_every_envelope_has_pagination_and_timestamp_metadata(self):
        extract_object(self.client, "companies", self.raw_dir, page_size=50)
        for envelope in read_jsonl(os.path.join(self.raw_dir, "companies.jsonl")):
            self.assertIn("extracted_at", envelope)
            self.assertIn("page_index", envelope)


if __name__ == "__main__":
    unittest.main()
