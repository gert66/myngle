import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hubspot_audit.analyses.companies import analyze_companies
from hubspot_audit.analyses.contacts import analyze_contacts
from hubspot_audit.analyses.cross_object import open_deals_without_recent_activity, ownership_usage
from hubspot_audit.analyses.deals import analyze_deals
from hubspot_audit.analyses.activities import analyze_activities
from hubspot_audit.config import Thresholds


def _write_jsonl(directory, name, rows):
    with open(os.path.join(directory, f"{name}.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _millis(dt):
    return str(int(dt.timestamp() * 1000))


class AnalysesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.thresholds = Thresholds()
        for kind in ("calls", "meetings", "emails", "notes", "tasks"):
            _write_jsonl(self.dir, kind, [])

    def test_company_duplicate_domain_detection(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"id": "1", "name": "A", "domain": "acme.com", "normalized_domain": "acme.com",
             "createdate": _millis(now), "hs_lastmodifieddate": _millis(now), "owner_id": "1",
             "parent_company_id": None, "lifecycle_stage": None, "source_properties": {}},
            {"id": "2", "name": "A Inc", "domain": "acme.com", "normalized_domain": "acme.com",
             "createdate": _millis(now), "hs_lastmodifieddate": _millis(now), "owner_id": "1",
             "parent_company_id": None, "lifecycle_stage": None, "source_properties": {}},
            {"id": "3", "name": "B", "domain": None, "normalized_domain": None,
             "createdate": _millis(now), "hs_lastmodifieddate": _millis(now - timedelta(days=1000)),
             "owner_id": None, "parent_company_id": None, "lifecycle_stage": None, "source_properties": {}},
        ]
        _write_jsonl(self.dir, "companies", rows)
        result = analyze_companies(self.dir, self.thresholds)
        self.assertEqual(result["metrics"]["total_companies"], 3)
        self.assertEqual(result["metrics"]["missing_domain"], 1)
        self.assertEqual(result["metrics"]["duplicate_domain_groups"], 1)
        self.assertEqual(result["metrics"]["stale_companies"], 1)
        self.assertEqual(set(result["signals"]["duplicate_domains"][0]["ids"]), {"1", "2"})

    def test_contact_duplicate_email_and_missing_company(self):
        rows = [
            {"id": "1", "email": "a@b.com", "normalized_email": "a@b.com", "firstname": "A", "lastname": "One",
             "createdate": None, "hs_lastmodifieddate": None, "owner_id": None, "company_ids": [],
             "lifecycle_stage": None, "source_properties": {}},
            {"id": "2", "email": "A@B.com", "normalized_email": "a@b.com", "firstname": "A2", "lastname": "Two",
             "createdate": None, "hs_lastmodifieddate": None, "owner_id": None, "company_ids": ["10"],
             "lifecycle_stage": None, "source_properties": {}},
            {"id": "3", "email": None, "normalized_email": None, "firstname": "C", "lastname": "Three",
             "createdate": None, "hs_lastmodifieddate": None, "owner_id": None, "company_ids": ["10", "11"],
             "lifecycle_stage": None, "source_properties": {}},
        ]
        _write_jsonl(self.dir, "contacts", rows)
        result = analyze_contacts(self.dir, self.thresholds)
        self.assertEqual(result["metrics"]["missing_email"], 1)
        self.assertEqual(result["metrics"]["missing_company_association"], 1)
        self.assertEqual(result["metrics"]["duplicate_email_groups"], 1)
        self.assertEqual(result["metrics"]["multi_company_contacts"], 1)

    def test_deal_missing_associations_and_bad_dates(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"id": "1", "dealname": "D1", "pipeline": "default", "dealstage": "won", "is_closed": True,
             "amount": 100, "createdate": _millis(now), "hs_lastmodifieddate": _millis(now),
             "closedate": _millis(now - timedelta(days=10)), "owner_id": "1", "company_ids": [], "contact_ids": [],
             "source_properties": {}},
            {"id": "2", "dealname": "D2", "pipeline": "default", "dealstage": "open", "is_closed": False,
             "amount": 200, "createdate": _millis(now - timedelta(days=200)),
             "hs_lastmodifieddate": _millis(now - timedelta(days=200)), "closedate": None, "owner_id": None,
             "company_ids": ["1"], "contact_ids": ["1"], "source_properties": {}},
        ]
        _write_jsonl(self.dir, "deals", rows)
        result = analyze_deals(self.dir, self.thresholds)
        self.assertEqual(result["metrics"]["missing_company_association"], 1)
        self.assertEqual(result["metrics"]["missing_owner"], 1)
        self.assertEqual(result["metrics"]["suspicious_dates"], 1)
        self.assertEqual(result["metrics"]["stale_open_deals"], 1)
        self.assertEqual(result["open_deal_ids"], ["2"])

    def test_open_deal_without_recent_activity_cross_check(self):
        deals_result = {"open_deal_ids": ["2"]}
        activities_result = {"most_recent_activity_by_deal": {}}
        result = open_deals_without_recent_activity(deals_result, activities_result, self.thresholds)
        self.assertEqual(result["metrics"]["open_deals_without_recent_activity"], 1)

    def test_ownership_usage_counts_by_owner(self):
        _write_jsonl(self.dir, "companies", [
            {"id": "1", "owner_id": "1", "name": "A", "domain": None, "normalized_domain": None,
             "createdate": None, "hs_lastmodifieddate": None, "parent_company_id": None,
             "lifecycle_stage": None, "source_properties": {}},
        ])
        _write_jsonl(self.dir, "contacts", [])
        _write_jsonl(self.dir, "deals", [])
        owners = [{"id": "1", "firstName": "Anna", "lastName": "de Vries"}]
        result = ownership_usage(self.dir, owners)
        self.assertEqual(result["signals"]["ownership_by_object"]["companies"][0]["owner_name"], "Anna de Vries")


if __name__ == "__main__":
    unittest.main()
