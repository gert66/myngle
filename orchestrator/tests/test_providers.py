import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.claude_runner import ClaudeResult  # noqa: E402
from core.providers import (  # noqa: E402
    DEFAULT_WORKER_PROVIDER, WORKER_PROVIDERS, ProviderError, build_runner,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"


def fake_claude_bin(mode="success"):
    return [sys.executable, str(FAKE), "--fake-mode", mode]


CALL_KWARGS = dict(
    cwd=".", json_schema={"type": "object"}, permission_mode="plan", disallowed_tools=[],
)


class BuildRunnerDispatchTests(unittest.TestCase):
    """core.providers.build_runner in isolation: the runner Machine calls."""

    def test_default_and_explicit_claude_use_the_real_claude_runner(self):
        run = build_runner(claude_bin=fake_claude_bin("success"))
        for provider in (None, "claude"):
            result = run("worker", "do it", worker_provider=provider, **CALL_KWARGS)
            self.assertIsInstance(result, ClaudeResult)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                result.parsed_json["structured_output"], {"status": "COMPLETED", "summary": "fake done"}
            )

    def test_brain_and_reviewer_always_use_claude_even_if_codex_is_requested(self):
        # codex_bin deliberately does not exist: if brain/reviewer were ever
        # routed to it, this test would see the codex failure instead.
        run = build_runner(claude_bin=fake_claude_bin("success"), codex_bin="definitely-not-a-real-binary-xyz")
        for role in ("brain", "reviewer"):
            result = run(role, "do it", worker_provider="codex", **CALL_KWARGS)
            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(result.error)

    def test_missing_codex_binary_fails_closed_with_a_distinct_clear_error(self):
        run = build_runner(claude_bin=fake_claude_bin("success"), codex_bin="definitely-not-a-real-binary-xyz")
        result = run("worker", "do it", worker_provider="codex", **CALL_KWARGS)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("codex binary not found", result.error)
        self.assertNotIn("not implemented", result.error)

    def test_build_runner_rejects_empty_codex_bin(self):
        with self.assertRaises(ProviderError):
            build_runner(codex_bin="")

    def test_default_worker_provider_is_auto(self):
        self.assertEqual(DEFAULT_WORKER_PROVIDER, "auto")
        self.assertEqual(set(WORKER_PROVIDERS), {"auto", "claude", "gemini", "codex"})


    def test_auto_normal_routes_to_gemini(self):
        gemini = ClaudeResult(argv=["gemini-api"], cwd=".", exit_code=0, stdout="{}", stderr="",
                              duration_seconds=0.1, timed_out=False, structured=True, schema_requested=True,
                              parsed_json={"type":"result","subtype":"success","is_error":False,
                                           "structured_output":{"status":"COMPLETED","summary":"gemini"}},
                              prompt_chars=5)
        gemini.provider = "gemini"
        gemini.service_tier = "flex"
        with mock.patch("core.providers.run_gemini", return_value=gemini) as rg:
            run = build_runner(claude_bin=fake_claude_bin("success"))
            result = run("worker", "do it", worker_provider="auto", worker_complexity="normal",
                         worker_mode="read", allowed_paths=["*"], **CALL_KWARGS)
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(result.provider_route, "auto_normal_gemini")
        self.assertEqual(rg.call_count, 1)

    def test_auto_high_routes_directly_to_claude_sonnet(self):
        with mock.patch("core.providers.run_gemini") as rg:
            run = build_runner(claude_bin=fake_claude_bin("success"))
            result = run("worker", "do it", worker_provider="auto", worker_complexity="high", **CALL_KWARGS)
        self.assertEqual(result.provider, "claude")
        self.assertIn("--model", result.argv)
        self.assertEqual(result.argv[result.argv.index("--model") + 1], "sonnet")
        self.assertEqual(result.provider_route, "auto_high_claude_sonnet")
        rg.assert_not_called()

    def test_reviewer_escalation_routes_next_worker_to_claude_sonnet(self):
        with mock.patch("core.providers.run_gemini") as rg:
            run = build_runner(claude_bin=fake_claude_bin("success"))
            result = run("worker", "repair it", worker_provider="auto", worker_complexity="low",
                         worker_escalated=True, **CALL_KWARGS)
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.argv[result.argv.index("--model") + 1], "sonnet")
        self.assertEqual(result.provider_route, "auto_reviewer_escalation_claude_sonnet")
        rg.assert_not_called()

    def test_auto_gemini_failure_falls_back_to_claude_sonnet(self):
        failed = ClaudeResult(argv=["gemini-api"], cwd=".", exit_code=1, stdout="", stderr="HTTP 500",
                              duration_seconds=0.1, timed_out=False, structured=True, schema_requested=True,
                              parsed_json=None, prompt_chars=5, error="HTTP 500")
        failed.provider = "gemini"
        failed.fallback_events = [{"from":"flex","to":"standard","reason":"HTTP 500"}]
        failed.provider_attempts = [{"provider":"gemini","model":"gemini-3.8-flash","service_tier":"flex"}]
        failed.retries = 1
        failed.gemini_usage = {"input_tokens": 10, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 10}
        with mock.patch("core.providers.run_gemini", return_value=failed):
            run = build_runner(claude_bin=fake_claude_bin("success"))
            result = run("worker", "do it", worker_provider="auto", worker_complexity="low", **CALL_KWARGS)
        self.assertEqual(result.provider, "claude")
        self.assertEqual(result.argv[result.argv.index("--model") + 1], "sonnet")
        self.assertEqual(result.retries, 2)
        self.assertEqual(result.fallback_events[-1]["to"], "claude-sonnet")
        self.assertEqual(result.gemini_usage_before_fallback["total_tokens"], 10)

    def test_quota_route_is_attached_to_result_and_router_is_exposed(self):
        class Router:
            def __init__(self):
                self.activities = []
            def select(self, role, requested_model=None):
                return {
                    "account": "gert66", "bucket": "all", "model": requested_model,
                    "claude_bin": fake_claude_bin("success"),
                }
            def record_activity(self, route):
                self.activities.append(route)
        router = Router()
        run = build_runner(claude_bin=fake_claude_bin("success"), account_router=router)
        result = run("worker", "do it", worker_provider="claude", model="sonnet", **CALL_KWARGS)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.quota_route, {"account": "gert66", "bucket": "all", "model": "sonnet"})
        self.assertIs(run.quota_router, router)
        self.assertEqual(router.activities[0]["account"], "gert66")


if __name__ == "__main__":
    unittest.main()
