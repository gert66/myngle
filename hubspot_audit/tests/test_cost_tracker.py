import unittest

from hubspot_audit.config import AIConfig, ModelPricing
from hubspot_audit.cost_tracker import CostTracker


class CostTrackerTests(unittest.TestCase):
    def test_ai_disabled_blocks_every_call(self):
        config = AIConfig(enabled=False)
        tracker = CostTracker(config)
        ok, reason = tracker.can_make_call()
        self.assertFalse(ok)
        self.assertIn("disabled", reason)

    def test_unpriced_model_reports_unknown_cost(self):
        config = AIConfig(enabled=True, budget_eur=10.0, pricing={"m": ModelPricing()})
        tracker = CostTracker(config)
        call = tracker.record_call("m", 1000, 500, is_strong_model=False, purpose="test")
        self.assertIsNone(call.estimated_cost_eur)
        self.assertIsNone(tracker.cumulative_cost_eur())

    def test_priced_model_computes_expected_cost(self):
        pricing = {"m": ModelPricing(input_price_per_million=2.0, output_price_per_million=10.0)}
        config = AIConfig(enabled=True, budget_eur=10.0, pricing=pricing)
        tracker = CostTracker(config)
        call = tracker.record_call("m", 1_000_000, 1_000_000, is_strong_model=False, purpose="test")
        self.assertAlmostEqual(call.estimated_cost_eur, 12.0)

    def test_max_ai_calls_enforced(self):
        config = AIConfig(enabled=True, budget_eur=1000.0, max_ai_calls=1, pricing={"m": ModelPricing(0.0, 0.0)})
        tracker = CostTracker(config)
        self.assertTrue(tracker.can_make_call()[0])
        tracker.record_call("m", 10, 10, False, "x")
        self.assertFalse(tracker.can_make_call()[0])

    def test_budget_exhaustion_blocks_further_calls(self):
        pricing = {"m": ModelPricing(input_price_per_million=1_000_000.0, output_price_per_million=0.0)}
        config = AIConfig(enabled=True, budget_eur=0.5, pricing=pricing)
        tracker = CostTracker(config)
        tracker.record_call("m", 1, 0, False, "x")  # costs 1.0 EUR, already over budget
        ok, reason = tracker.can_make_call()
        self.assertFalse(ok)
        self.assertIn("BUDGET", reason)

    def test_strong_model_escalation_cap(self):
        config = AIConfig(enabled=True, budget_eur=1000.0, max_strong_model_escalations=1, pricing={"m": ModelPricing(0.0, 0.0)})
        tracker = CostTracker(config)
        self.assertTrue(tracker.can_make_call(is_strong_model=True)[0])
        tracker.record_call("m", 1, 1, True, "x")
        self.assertFalse(tracker.can_make_call(is_strong_model=True)[0])
        # cheap-model calls remain unaffected by the strong-model cap
        self.assertTrue(tracker.can_make_call(is_strong_model=False)[0])

    def test_budget_summary_shape(self):
        config = AIConfig(enabled=False, budget_eur=5.0)
        summary = CostTracker(config).budget_summary()
        for key in (
            "provider", "standard_model", "bulk_model", "strong_model", "ai_enabled", "num_calls",
            "num_strong_model_escalations", "total_input_tokens", "total_output_tokens",
            "ai_cost_estimate_eur", "ai_budget_eur", "pct_budget_consumed", "remaining_budget_eur",
        ):
            self.assertIn(key, summary)


if __name__ == "__main__":
    unittest.main()
