import os
import shutil
import tempfile
import unittest

from hubspot_audit.report.html_report import generate_html_report

REQUIRED_SECTION_ANCHORS = [
    "overview", "coverage", "kpis", "companies", "contacts", "deals", "activities",
    "associations", "properties", "historical", "ownership", "crossobject", "gaps",
    "principles", "roadmap", "appendix",
]


def _minimal_context():
    return {
        "run_id": "r1",
        "generated_at": "2026-01-01T00:00:00Z",
        "mode": "fixture",
        "metrics": {
            "companies": {"total_companies": 10, "missing_domain": 3, "missing_name": 0,
                           "duplicate_domain_groups": 1, "stale_companies": 2, "creation_burst_days": 0,
                           "orphan_parent_references": 0},
            "contacts": {"total_contacts": 20, "missing_email": 5, "missing_company_association": 2,
                          "multi_company_contacts": 1, "stale_contacts": 0},
            "deals": {"total_deals": 5, "missing_company_association": 1, "missing_contact_association": 1,
                       "missing_owner": 0, "open_deals": 2, "stale_open_deals": 1, "pipelines_in_use": 1},
            "activities": {"volume_by_kind": {"calls": 3}, "last_30_days_by_kind": {"calls": 1},
                            "unassigned_by_kind": {"calls": 0}, "overdue_tasks": 1},
            "properties": {"companies": {"total_properties": 5, "unused_properties": 1,
                                          "near_unused_properties": 1, "legacy_salesforce_properties": 1,
                                          "migration_candidate_properties": 0}},
            "historical_imports": {"companies": {"creation_burst_days": 0, "migration_cohort_candidates": 0,
                                                  "distinct_source_values": 2}},
            "ownership": {"ownership_by_object": {"companies": []}},
            "cross_object": {"open_deals_without_recent_activity": 1},
        },
        "capability_matrix": {"companies": {"object_name": "companies", "status": "available",
                                             "detail": "ok", "downstream_impact": "none"}},
        "findings": [
            {"finding_id": "f1", "title": "Test finding", "category": "companies", "status": "confirmed",
             "confidence": 0.9, "analysis_version": "1.0.0", "evidence": [], "counter_evidence": [],
             "hypothesis": None},
        ],
        "investigations": [],
        "cost_summary": {"ai_cost_estimate_eur": None, "ai_budget_eur": 5.0, "pct_budget_consumed": None},
        "gaps": [],
        "company_signals": {"duplicate_domains": []},
        "contact_signals": {"duplicate_emails": []},
        "pipeline_signals": {"stuck_pipeline_stages": []},
    }


class HtmlReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.output_path = os.path.join(self.tmp_dir, "reports", "index.html")

    def test_report_contains_every_required_section(self):
        generate_html_report(_minimal_context(), self.output_path)
        with open(self.output_path, encoding="utf-8") as fh:
            html = fh.read()
        for anchor in REQUIRED_SECTION_ANCHORS:
            self.assertIn(f'id="{anchor}"', html, f"missing section: {anchor}")

    def test_report_is_well_formed_html(self):
        generate_html_report(_minimal_context(), self.output_path)
        with open(self.output_path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.strip().endswith("</html>"))

    def test_report_escapes_hostile_input(self):
        context = _minimal_context()
        context["findings"][0]["title"] = "<script>alert(1)</script>"
        generate_html_report(context, self.output_path)
        with open(self.output_path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unpriced_ai_cost_shown_as_unknown_not_zero(self):
        generate_html_report(_minimal_context(), self.output_path)
        with open(self.output_path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("onbekend", html)


if __name__ == "__main__":
    unittest.main()
