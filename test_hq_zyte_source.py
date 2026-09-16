import unittest
from unittest.mock import patch

from lead_output_schema import HQDetectionResult, LeadInput
from lead_hq_zyte_source import collect_own_domain_hq_pages_zyte
from lead_prioritizer_core import prioritize_single_lead


class TestZyteSmartDiscovery(unittest.TestCase):
    def test_discovers_linked_group_domain_from_homepage(self):
        home = {
            "ok": True, "status": 200, "url": "https://example.ch",
            "text": "Swiss subsidiary", "links": [
                ("https://group.example.com/about", "About our Group"),
                ("https://facebook.com/example", "Facebook"),
            ],
        }
        group = {
            "ok": True, "status": 200, "url": "https://group.example.com/about",
            "text": "Group headquarters Austria", "links": [],
        }
        with patch("lead_hq_zyte_source._fetch", side_effect=[home, group]):
            result = collect_own_domain_hq_pages_zyte("example.ch", "key", max_pages=2)
        self.assertTrue(result["used"])
        self.assertEqual(2, len(result["pages"]))
        self.assertEqual("linked_group_domain", result["pages"][1]["source_kind"])
        self.assertEqual("https://group.example.com/about", result["pages"][1]["url"])

    def test_static_paths_are_fallback_when_homepage_has_no_relevant_links(self):
        home = {"ok": True, "status": 200, "url": "https://example.ch", "text": "Home", "links": []}
        about = {"ok": True, "status": 200, "url": "https://example.ch/about", "text": "About", "links": []}
        with patch("lead_hq_zyte_source._fetch", side_effect=[home, about]):
            result = collect_own_domain_hq_pages_zyte("example.ch", "key", max_pages=2)
        self.assertEqual(2, len(result["pages"]))
        self.assertEqual("https://example.ch/about", result["pages"][1]["url"])


class TestZyteCoreSafety(unittest.TestCase):
    def test_unlinked_external_foreign_parent_escalates_to_firecrawl(self):
        risky = HQDetectionResult(
            ai_hq_classification="foreign_parent", ai_hq_confidence="High",
            ai_parent_hq_country="Japan", hq_detected_country="Japan",
            hq_structure_type="foreign_parent", foreign_hq_simple=True,
            hq_evidence_url="https://unrelated.jp/about",
            hq_evidence_domain_mismatch_warning="Yes",
        )
        safe = HQDetectionResult(
            ai_hq_classification="unclear", ai_hq_confidence="Low",
            hq_structure_type=None, foreign_hq_simple=None,
            needs_manual_review=True, hq_evidence_url="https://example.ch",
        )
        with (
            patch("lead_prioritizer_core.call_serper_for_hq", return_value={}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages_zyte", return_value={
                "used": True, "pages": [{"url": "https://example.ch", "text": "local club"}],
            }),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages", return_value={"used": True, "pages": []}),
            patch("lead_prioritizer_core.interpret_hq_with_ai", side_effect=[risky, safe]) as ai,
        ):
            result = prioritize_single_lead(
                LeadInput(company_name="Example", domain="example.ch", input_country="Switzerland"),
                serper_api_key="s", anthropic_api_key="a", firecrawl_api_key="f",
                zyte_api_key="z", hq_crawl_provider="zyte_with_firecrawl_fallback",
            )
        self.assertEqual(2, ai.call_count)
        self.assertEqual("zyte", result.hq_crawl_provider_primary)
        self.assertEqual("Yes", result.hq_firecrawl_fallback_used)
        self.assertIn("foreign_parent_external_evidence_not_corroborated_by_zyte", result.hq_firecrawl_fallback_reason)
        self.assertTrue(result.needs_manual_review)

    def test_linked_group_evidence_does_not_escalate(self):
        good = HQDetectionResult(
            ai_hq_classification="foreign_parent", ai_hq_confidence="High",
            ai_parent_hq_country="Austria", hq_detected_country="Austria",
            hq_structure_type="foreign_parent", foreign_hq_simple=True,
            hq_evidence_url="https://group.at/about",
            hq_evidence_domain_mismatch_warning="Yes",
            sig_foreign_hq_score_for_next_scoring=3.0,
        )
        with (
            patch("lead_prioritizer_core.call_serper_for_hq", return_value={}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages_zyte", return_value={
                "used": True, "pages": [
                    {"url": "https://example.ch", "text": "subsidiary"},
                    {"url": "https://group.at/about", "text": "parent group"},
                ],
            }),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages") as fc,
            patch("lead_prioritizer_core.interpret_hq_with_ai", return_value=good) as ai,
        ):
            result = prioritize_single_lead(
                LeadInput(company_name="Example", domain="example.ch", input_country="Switzerland"),
                serper_api_key="s", anthropic_api_key="a", firecrawl_api_key="f",
                zyte_api_key="z", hq_crawl_provider="zyte_with_firecrawl_fallback",
            )
        self.assertEqual(1, ai.call_count)
        fc.assert_not_called()
        self.assertEqual("No", result.hq_firecrawl_fallback_used)
        self.assertEqual(3.0, result.sig_foreign_hq_score_for_next_scoring)


if __name__ == "__main__":
    unittest.main()
