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


class TestHqCrawl4aiFallbackNoFirecrawlPages(unittest.TestCase):
    def test_escalation_reinterprets_serper_only_when_firecrawl_has_no_pages(self):
        from unittest.mock import patch
        from lead_output_schema import LeadInput
        from lead_prioritizer_core import prioritize_single_lead

        risky = HQDetectionResult(
            ai_hq_classification="domestic", ai_hq_confidence="High",
            ai_parent_hq_country="Ireland", hq_detected_country="Ireland",
            hq_structure_type="domestic", foreign_hq_simple=False,
            hq_evidence_url="https://example.com/about-us",
        )
        corrected = HQDetectionResult(
            ai_hq_classification="foreign_parent", ai_hq_confidence="Medium",
            ai_parent_hq_country="Ireland", hq_detected_country="Ireland",
            hq_structure_type="foreign_parent", foreign_hq_simple=True,
            hq_evidence_url="https://example.com/contact-us",
        )
        with (
            patch("lead_prioritizer_core.call_serper_for_hq", return_value={}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages_crawl4ai",
                  return_value={"used": True, "pages": [{"url": "https://example.com", "text": "x"}]}),
            patch("lead_prioritizer_core.collect_own_domain_hq_pages",
                  return_value={"used": False, "pages": []}),
            patch("lead_prioritizer_core.interpret_hq_with_ai", side_effect=[risky, corrected]) as p_ai,
        ):
            result = prioritize_single_lead(
                LeadInput(company_name="Example", domain="example.com", input_country="Switzerland"),
                serper_api_key="s", anthropic_api_key="a", firecrawl_api_key="f",
                hq_crawl_provider="crawl4ai_with_firecrawl_fallback",
                crawl4ai_runner="/fake/runner",
            )

        self.assertEqual(2, p_ai.call_count)
        self.assertEqual("Yes", result.hq_firecrawl_fallback_used)
        self.assertIn("firecrawl_no_usable_pages_serper_only", result.hq_firecrawl_fallback_reason)
        self.assertTrue(result.foreign_hq_simple)


class TestHqCountryConsistencyGuard(unittest.TestCase):
    def test_domestic_with_foreign_parent_country_is_adjudicated_as_foreign(self):
        import json
        from unittest.mock import patch
        from lead_output_schema import LeadInput
        from lead_hq_ai_interpreter import interpret_hq_with_ai

        raw = json.dumps({
            "classification": "domestic",
            "confidence": "High",
            "parent_company": "Example Group",
            "parent_hq_country": "Ireland",
            "parent_hq_city": "Dublin",
            "evidence_url": "https://example.ch/about",
            "evidence_urls": ["https://example.ch/about"],
            "evidence_quote": "Group headquarters are in Dublin.",
            "reason": "The parent headquarters are in Dublin, Ireland.",
            "industry": "",
            "sub_industry": "",
        })
        serper = {"organic": [{"link": "https://example.ch/about", "title": "About", "snippet": "HQ Dublin"}]}
        with patch("lead_hq_ai_interpreter._call_anthropic_hq", return_value=(raw, {})):
            hq = interpret_hq_with_ai(
                lead_input=LeadInput(company_name="Example", domain="example.ch", input_country="Switzerland"),
                domain_root="example", query="example headquarters", serper_payload=serper,
                anthropic_api_key="fake", crawled_pages=[],
            )

        self.assertEqual("domestic", hq.ai_hq_classification)
        self.assertEqual("foreign_parent", hq.hq_structure_type)
        self.assertTrue(hq.foreign_hq_simple)
        self.assertIn("country_consistency_override", hq.hq_reason)
