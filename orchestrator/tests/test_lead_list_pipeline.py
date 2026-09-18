import tempfile
import unittest
from pathlib import Path

import pandas as pd

from lead_list_intake import analyze_lead_list
from lead_list_pipeline import build_dry_run_report, persist_intake_artifacts


class LeadListPipelineTests(unittest.TestCase):
    def test_confirmed_browser_mapping_wins(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "korea.csv"
            pd.DataFrame([
                {"Person": "Jane", "Employer": "Acme", "Mail": "jane@acme.kr", "ANSWER": "+82 10 1234 5678", "My Notes": "called"},
            ]).to_csv(path, index=False)
            plan = {
                "header_row": 0,
                "mapping": [
                    {"index": 0, "header": "Person", "field": "contact_name"},
                    {"index": 1, "header": "Employer", "field": "company"},
                    {"index": 2, "header": "Mail", "field": "email"},
                    {"index": 3, "header": "ANSWER", "field": "phone"},
                    {"index": 4, "header": "My Notes", "field": "ignore"},
                ],
            }
            result = analyze_lead_list(path, default_country="South Korea", assigned_caller="Carla", import_plan=plan)
            self.assertEqual(result.report["mapping_source"], "confirmed_by_user")
            self.assertEqual(result.mapping["company_name"], "Employer")
            self.assertEqual(result.mapping["direct_phone"], "ANSWER")
            self.assertNotIn("My Notes", result.mapping.values())

    def test_dry_run_has_no_external_writes_and_persists_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "leads.csv"
            pd.DataFrame([
                {"Company name": "Acme", "Work email": "jane@acme.kr", "Contact name": "Jane"},
                {"Company name": "Beta", "Work email": "john@beta.kr", "Contact name": "John"},
            ]).to_csv(path, index=False)
            result = analyze_lead_list(path, default_country="South Korea", assigned_caller="Carla")
            plan = {"options": {"enrich": True, "publish": True, "hubspot_sync": True}}
            pipeline = build_dry_run_report(result, plan)
            self.assertEqual(pipeline["mode"], "dry_run")
            self.assertEqual(pipeline["writes_performed"], 0)
            self.assertEqual(pipeline["external_write_calls"], 0)
            self.assertEqual(pipeline["supplier_calls"], 0)
            stages = {row["key"]: row for row in pipeline["stages"]}
            self.assertEqual(stages["protection"]["status"], "simulated")
            self.assertEqual(stages["enrichment"]["status"], "simulated")
            self.assertEqual(stages["cockpit_publish"]["status"], "simulated")
            self.assertEqual(stages["hubspot_sync"]["status"], "skipped")

            out = persist_intake_artifacts("list-1", result, pipeline, Path(td) / "artifacts")
            self.assertTrue((out / "normalized_rows.jsonl").is_file())
            self.assertTrue((out / "companies.jsonl").is_file())
            self.assertTrue((out / "pipeline_report.json").is_file())


if __name__ == "__main__":
    unittest.main()


def test_multi_country_worksets_are_partitioned(tmp_path):
    path = tmp_path / "multi.csv"
    pd.DataFrame([
        {"Company name": "Acme", "Country": "South Korea", "Contact name": "Jane"},
        {"Company name": "Beta", "Country": "Japan", "Contact name": "John"},
        {"Company name": "Gamma", "Country": "", "Contact name": "Gina"},
    ]).to_csv(path, index=False)
    result = analyze_lead_list(
        path, default_country="South Korea", assigned_caller="Carla")
    pipeline = build_dry_run_report(result, {"options": {"publish": True}})
    worksets = {w["slug"]: w for w in pipeline["country_worksets"]}
    assert worksets["south-korea"]["source_rows"] == 2
    assert worksets["japan"]["source_rows"] == 1
    assert worksets["south-korea"]["target_prefix"] == "south-korea/current"
    assert pipeline["external_write_calls"] == 0

    out = persist_intake_artifacts("multi", result, pipeline, tmp_path / "artifacts")
    assert (out / "countries" / "south-korea" / "normalized_rows.jsonl").is_file()
    assert (out / "countries" / "japan" / "normalized_rows.jsonl").is_file()
