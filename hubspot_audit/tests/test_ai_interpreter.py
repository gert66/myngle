import json
import unittest
from types import SimpleNamespace

from hubspot_audit.ai_interpreter import AnthropicAIInterpreter, NullAIInterpreter, build_interpreter
from hubspot_audit.config import AIConfig, ModelPricing
from hubspot_audit.models import FindingStatus


class NullAIInterpreterTests(unittest.TestCase):
    def test_disabled_config_gives_null_interpreter(self):
        interpreter = build_interpreter(AIConfig(enabled=False))
        self.assertIsInstance(interpreter, NullAIInterpreter)

    def test_null_interpreter_always_returns_none(self):
        interpreter = NullAIInterpreter(AIConfig(enabled=False))
        result = interpreter.interpret_cluster("f1", "question?", [{"a": 1}])
        self.assertIsNone(result)

    def test_null_interpreter_budget_summary_shows_disabled(self):
        interpreter = NullAIInterpreter(AIConfig(enabled=False))
        summary = interpreter.budget_summary()
        self.assertFalse(summary["ai_enabled"])
        self.assertEqual(summary["num_calls"], 0)


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeAnthropicClient:
    def __init__(self, response_json: dict):
        self._response_json = response_json
        self.calls = []

        class _Messages:
            def create(inner_self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=100, output_tokens=50),
                    content=[_FakeTextBlock(json.dumps(self._response_json))],
                )

        self.messages = _Messages()


class AnthropicAIInterpreterTests(unittest.TestCase):
    def test_budget_gate_short_circuits_before_calling_the_model(self):
        interpreter = AnthropicAIInterpreter(AIConfig(enabled=False))
        result = interpreter.interpret_cluster("f1", "question?", [{"a": 1}])
        self.assertEqual(result.status, FindingStatus.NEEDS_HUMAN_CONTEXT.value)
        self.assertIn("disabled", result.evidence_summary.lower())

    def test_ai_cannot_self_promote_to_confirmed(self):
        config = AIConfig(enabled=True, budget_eur=1000.0, pricing={"claude-haiku-4-5-20251001": ModelPricing(0.0, 0.0)})
        interpreter = AnthropicAIInterpreter(config)
        interpreter._client = _FakeAnthropicClient(
            {
                "hypothesis": "these are duplicates",
                "evidence_summary": "same domain",
                "counter_evidence": "",
                "confidence": 0.9,
                "status": "confirmed",
                "recommended_next_investigation": None,
                "potential_remediation": "merge candidates",
                "reasoning_summary": "High confidence based on exact domain match.",
            }
        )
        result = interpreter.interpret_cluster("f1", "are these duplicates?", [{"key": "acme.com", "count": 3}])
        self.assertNotEqual(result.status, FindingStatus.CONFIRMED.value)
        self.assertEqual(result.status, FindingStatus.INVESTIGATING.value)

    def test_candidate_payload_is_capped(self):
        config = AIConfig(enabled=True, budget_eur=1000.0, pricing={"claude-haiku-4-5-20251001": ModelPricing(0.0, 0.0)})
        interpreter = AnthropicAIInterpreter(config)
        fake_client = _FakeAnthropicClient(
            {
                "hypothesis": "h", "evidence_summary": "e", "counter_evidence": "",
                "confidence": 0.1, "status": "inconclusive", "recommended_next_investigation": None,
                "potential_remediation": None, "reasoning_summary": "short",
            }
        )
        interpreter._client = fake_client
        big_candidate_list = [{"i": i} for i in range(1000)]
        interpreter.interpret_cluster("f1", "q?", big_candidate_list)
        sent_payload = json.loads(fake_client.calls[0]["messages"][0]["content"])
        self.assertLessEqual(len(sent_payload["candidates"]), 25)

    def test_non_json_response_is_treated_as_inconclusive_not_raised(self):
        config = AIConfig(enabled=True, budget_eur=1000.0, pricing={"claude-haiku-4-5-20251001": ModelPricing(0.0, 0.0)})
        interpreter = AnthropicAIInterpreter(config)

        class _BadClient(_FakeAnthropicClient):
            def __init__(self):
                class _Messages:
                    def create(inner_self, **kwargs):
                        return SimpleNamespace(
                            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                            content=[_FakeTextBlock("not json")],
                        )

                self.messages = _Messages()

        interpreter._client = _BadClient()
        result = interpreter.interpret_cluster("f1", "q?", [])
        self.assertEqual(result.status, FindingStatus.INCONCLUSIVE.value)


if __name__ == "__main__":
    unittest.main()
