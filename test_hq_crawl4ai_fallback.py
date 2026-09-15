import unittest

from lead_output_schema import HQDetectionResult
from lead_prioritizer_core import _hq_firecrawl_fallback_reasons


class TestHqCrawl4aiFallback(unittest.TestCase):
    def reasons(self, **kwargs):
        used = kwargs.pop("crawl4ai_used", True)
        hq = HQDetectionResult(**kwargs)
        return _hq_firecrawl_fallback_reasons(
            hq, input_country="Switzerland", crawl4ai_used=used,
        )

    def test_domestic_parent_country_mismatch_escalates(self):
        reasons = self.reasons(
            ai_hq_classification="domestic",
            ai_hq_confidence="High",
            ai_parent_hq_country="Ireland",
            hq_evidence_url="https://example.com/about",
        )
        self.assertIn("domestic_parent_country_mismatch", reasons)

    def test_domestic_contact_page_escalates(self):
        reasons = self.reasons(
            ai_hq_classification="domestic",
            ai_hq_confidence="High",
            ai_parent_hq_country="Switzerland",
            hq_evidence_url="https://example.com/contact-us/",
        )
        self.assertIn("domestic_contact_page_evidence", reasons)

    def test_foreign_parent_without_own_site_non_high_escalates(self):
        reasons = self.reasons(
            crawl4ai_used=False,
            ai_hq_classification="foreign_parent",
            ai_hq_confidence="Medium",
            ai_parent_hq_country="United States",
            hq_evidence_url="https://external.example/company",
        )
        self.assertIn("foreign_parent_without_own_site_non_high_confidence", reasons)

    def test_high_confidence_foreign_without_own_site_does_not_escalate(self):
        reasons = self.reasons(
            crawl4ai_used=False,
            ai_hq_classification="foreign_parent",
            ai_hq_confidence="High",
            ai_parent_hq_country="United States",
            hq_evidence_url="https://external.example/company",
        )
        self.assertNotIn("foreign_parent_without_own_site_non_high_confidence", reasons)

    def test_clean_domestic_result_does_not_escalate(self):
        reasons = self.reasons(
            ai_hq_classification="domestic",
            ai_hq_confidence="High",
            ai_parent_hq_country="Switzerland",
            hq_evidence_url="https://example.ch/about",
        )
        self.assertEqual([], reasons)


if __name__ == "__main__":
    unittest.main()


class TestHqCrawl4aiFallbackIntegration(unittest.TestCase):
    def test_hybrid_core_reinterprets_with_firecrawl_on_trigger(self):
        from unittest.mock import patch
        from lead_output_schema import LeadInput
        from lead_prioritizer_core import prioritize_single_lead

        c4_hq = HQDetectionResult(
            ai_hq_classification="domestic", ai_hq_confidence="High",
            ai_parent_hq_country="Switzerland", hq_detected_country="Switzerland",
            hq_structure_type="domestic", foreign_hq_simple=False,
            hq_evidence_url="https://example.ch/contact-us/",
        )
        fc_hq = HQDetectionResult(
            ai_hq_classification="foreign_parent", ai_hq_confidence="High",
            ai_parent_hq_country="United States", hq_detected_country="United States",
            hq_structure_type="foreign_parent", foreign_hq_simple=True,
            hq_evidence_url="https://example.ch/about-us",
        )
        with (
            patch("lead_prioritizer_core.call_serper_for_hq", return_value={}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages_crawl4ai",
                  return_value={"used": True, "pages": [{"url": "https://example.ch", "text": "x"}]}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages",
                  return_value={"used": True, "pages": [{"url": "https://example.ch/about-us", "text": "y"}]}),
            patch("lead_prioritizer_core.interpret_hq_with_ai", side_effect=[c4_hq, fc_hq]) as p_ai,
        ):
            result = prioritize_single_lead(
                LeadInput(company_name="Example", domain="example.ch", input_country="Switzerland"),
                serper_api_key="s", anthropic_api_key="a", firecrawl_api_key="f",
                hq_crawl_provider="crawl4ai_with_firecrawl_fallback",
                crawl4ai_runner="/fake/runner",
            )

        self.assertEqual(2, p_ai.call_count)
        self.assertEqual("crawl4ai", result.hq_crawl_provider_primary)
        self.assertEqual("Yes", result.hq_firecrawl_fallback_used)
        self.assertIn("domestic_contact_page_evidence", result.hq_firecrawl_fallback_reason)
        self.assertTrue(result.foreign_hq_simple)
