import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.claude_runner import EXIT_TIMEOUT, ClaudeResult, run_claude  # noqa: E402
from core.classify import (  # noqa: E402
    AUTH, BACKOFF_SECONDS, BUDGET, CLASSES, CONFIG, INVALID_OUTPUT, RATE_LIMIT, RETRYABLE, SUCCESS,
    TASK_ERROR, TIMEOUT, TRANSIENT, Classification, classify, match_text, retry_metadata,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"


def make_result(exit_code=0, stdout="", stderr="", timed_out=False, structured=True,
                schema_requested=False, error=None, parsed=None):
    """Build a ClaudeResult by hand. parsed=dict serialises into stdout."""
    if parsed is not None:
        stdout = json.dumps(parsed)
    try:
        parsed_json = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        parsed_json = None
    return ClaudeResult(
        argv=["claude", "--print"], cwd="/tmp", exit_code=exit_code, stdout=stdout,
        stderr=stderr, duration_seconds=0.1, timed_out=timed_out, structured=structured,
        schema_requested=schema_requested, parsed_json=parsed_json, prompt_chars=1, error=error,
    )


def claude_json(subtype="success", is_error=False, result="ok", **extra):
    obj = {"type": "result", "subtype": subtype, "is_error": is_error, "result": result}
    obj.update(extra)
    return obj


class RetryMetadataTests(unittest.TestCase):
    def test_all_classes_have_metadata(self):
        self.assertEqual(set(CLASSES), set(BACKOFF_SECONDS))
        self.assertEqual(len(CLASSES), 9)

    def test_backoff_table(self):
        self.assertEqual(BACKOFF_SECONDS[RATE_LIMIT], 300)
        self.assertEqual(BACKOFF_SECONDS[TRANSIENT], 30)
        self.assertEqual(BACKOFF_SECONDS[TIMEOUT], 30)
        self.assertEqual(BACKOFF_SECONDS[INVALID_OUTPUT], 0)
        for cls in (SUCCESS, AUTH, BUDGET, CONFIG, TASK_ERROR):
            self.assertEqual(BACKOFF_SECONDS[cls], 0)

    def test_retryable_set(self):
        self.assertEqual(RETRYABLE, {RATE_LIMIT, TRANSIENT, TIMEOUT, INVALID_OUTPUT})
        self.assertEqual(retry_metadata(RATE_LIMIT), (True, 300))
        self.assertEqual(retry_metadata(AUTH), (False, 0))
        with self.assertRaises(ValueError):
            retry_metadata("NOPE")

    def test_classification_carries_metadata_and_serialises(self):
        c = classify(make_result(timed_out=True, exit_code=EXIT_TIMEOUT))
        self.assertIsInstance(c, Classification)
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TIMEOUT, True, 30))
        d = json.loads(json.dumps(c.to_dict()))
        self.assertEqual(d["category"], TIMEOUT)


class TextPatternTests(unittest.TestCase):
    def test_rate_limit_patterns_case_insensitive(self):
        for text in ("Rate Limit exceeded", "rate_limit_error", "usage limit reached",
                     "You've hit your limit", "HTTP 429", "API is OVERLOADED", "error 529",
                     "Limit resets at 3pm", "Too Many Requests"):
            hit = match_text(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], RATE_LIMIT, text)

    def test_auth_patterns_case_insensitive(self):
        for text in ("status 401", "Unauthorized", "UNAUTHORISED request", "Not logged in",
                     "Authentication required", "authentication_error", "Invalid API key",
                     "invalid x-api-key", "please run /login"):
            hit = match_text(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], AUTH, text)

    def test_budget_patterns_case_insensitive(self):
        for text in ("Reached max budget", "MAX_BUDGET_USD exceeded", "Budget exhausted",
                     "cost limit reached", "spending limit hit"):
            hit = match_text(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], BUDGET, text)

    def test_no_match(self):
        self.assertIsNone(match_text("ECONNRESET socket hang up"))
        self.assertIsNone(match_text(""))
        self.assertIsNone(match_text(None))
        # Word boundaries: 4290 is not 429, 4010 is not 401.
        self.assertIsNone(match_text("job 4290 done, 4010 rows"))


class ClassifyUnitTests(unittest.TestCase):
    def test_timeout_beats_everything(self):
        c = classify(make_result(timed_out=True, exit_code=EXIT_TIMEOUT, stderr="429 rate limit"))
        self.assertEqual(c.category, TIMEOUT)
        self.assertTrue(c.retryable)

    def test_rate_limit_from_json_result_text(self):
        parsed = claude_json("error_during_execution", True,
                             "You've hit your usage limit · resets at 7pm")
        c = classify(make_result(exit_code=1, parsed=parsed))
        self.assertEqual(c.category, RATE_LIMIT)
        self.assertEqual((c.retryable, c.backoff_seconds), (True, 300))
        self.assertIsNotNone(c.matched_pattern)

    def test_rate_limit_from_stderr(self):
        c = classify(make_result(exit_code=1, stderr="API Error: 529 overloaded_error"))
        self.assertEqual(c.category, RATE_LIMIT)

    def test_cli_configuration_error_is_non_retryable(self):
        c = classify(make_result(exit_code=1, stderr="Error: --json-schema is not a valid JSON Schema"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (CONFIG, False, 0))
        c = classify(make_result(exit_code=1, stderr="unknown option --effort"))
        self.assertEqual(c.category, CONFIG)

    def test_auth(self):
        c = classify(make_result(exit_code=1, stderr="Error: Not logged in"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (AUTH, False, 0))
        c = classify(make_result(exit_code=1, stdout="401 Unauthorized"))
        self.assertEqual(c.category, AUTH)

    def test_budget_from_subtype(self):
        parsed = claude_json("error_max_budget_usd", True, "stopped")
        c = classify(make_result(exit_code=1, parsed=parsed))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (BUDGET, False, 0))

    def test_budget_from_text(self):
        c = classify(make_result(exit_code=1, stderr="Cost limit of $2 exceeded"))
        self.assertEqual(c.category, BUDGET)

    def test_unknown_nonzero_is_transient(self):
        c = classify(make_result(exit_code=1, stderr="ECONNRESET"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TRANSIENT, True, 30))
        c = classify(make_result(exit_code=137))
        self.assertEqual(c.category, TRANSIENT)

    def test_runner_error_is_transient(self):
        c = classify(make_result(exit_code=127, stderr="claude binary not found: x",
                                 error="claude binary not found: x"))
        self.assertEqual(c.category, TRANSIENT)
        self.assertIn("runner error", c.reason)

    def test_task_error_from_json(self):
        parsed = claude_json("error_max_turns", True, "Reached max turns")
        c = classify(make_result(exit_code=1, parsed=parsed))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TASK_ERROR, False, 0))

    def test_task_error_is_error_with_exit_zero(self):
        parsed = claude_json("error_during_execution", True, "tool crashed")
        c = classify(make_result(exit_code=0, parsed=parsed))
        self.assertEqual(c.category, TASK_ERROR)

    def test_is_error_flag_alone(self):
        parsed = claude_json("success", True, "something went wrong")
        c = classify(make_result(exit_code=1, parsed=parsed))
        self.assertEqual(c.category, TASK_ERROR)

    def test_success_structured_without_schema(self):
        parsed = claude_json(result="done")
        c = classify(make_result(exit_code=0, parsed=parsed))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (SUCCESS, False, 0))
        self.assertEqual(c.payload, parsed)

    def test_success_structured_with_schema(self):
        parsed = claude_json(structured_output={"status": "COMPLETED"})
        c = classify(make_result(exit_code=0, parsed=parsed, schema_requested=True))
        self.assertEqual(c.category, SUCCESS)
        self.assertEqual(c.payload, {"status": "COMPLETED"})

    def test_success_unstructured(self):
        c = classify(make_result(exit_code=0, stdout="plain answer", structured=False))
        self.assertEqual(c.category, SUCCESS)
        self.assertIsNone(c.payload)

    def test_success_text_mentioning_429_is_not_rate_limited(self):
        parsed = claude_json(result="Handled HTTP 429 and 401 in the retry helper; budget ok")
        c = classify(make_result(exit_code=0, parsed=parsed))
        self.assertEqual(c.category, SUCCESS)

    def test_invalid_output_not_json(self):
        c = classify(make_result(exit_code=0, stdout="{not json"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (INVALID_OUTPUT, True, 0))

    def test_invalid_output_empty_stdout(self):
        c = classify(make_result(exit_code=0, stdout=""))
        self.assertEqual(c.category, INVALID_OUTPUT)

    def test_invalid_output_not_an_object(self):
        c = classify(make_result(exit_code=0, stdout="[1, 2]"))
        self.assertEqual(c.category, INVALID_OUTPUT)

    def test_invalid_output_missing_structured_payload(self):
        c = classify(make_result(exit_code=0, parsed=claude_json(), schema_requested=True))
        self.assertEqual(c.category, INVALID_OUTPUT)
        self.assertIn("structured_output", c.reason)

    def test_invalid_output_structured_payload_not_object(self):
        parsed = claude_json(structured_output="just a string")
        c = classify(make_result(exit_code=0, parsed=parsed, schema_requested=True))
        self.assertEqual(c.category, INVALID_OUTPUT)
        parsed = claude_json(structured_output=None)
        c = classify(make_result(exit_code=0, parsed=parsed, schema_requested=True))
        self.assertEqual(c.category, INVALID_OUTPUT)

    def test_accepts_dict_form(self):
        res = make_result(exit_code=1, stderr="rate limit")
        self.assertEqual(classify(res.to_dict()).category, RATE_LIMIT)

    def test_reason_does_not_copy_output(self):
        blob = "SECRET-VALUE-XYZ " * 5
        c = classify(make_result(exit_code=1, stderr=blob + " rate limit"))
        self.assertNotIn("SECRET-VALUE-XYZ", c.reason)
        self.assertNotIn("SECRET-VALUE-XYZ", json.dumps(c.to_dict()))


class ClassifyWithFakeClaudeTests(unittest.TestCase):
    """End to end: run the fake CLI through the runner, then classify."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_mode(self, mode, **kwargs):
        kwargs.setdefault("timeout", 10)
        return run_claude("prompt", cwd=self.cwd,
                          claude_bin=[sys.executable, str(FAKE), "--fake-mode", mode], **kwargs)

    def test_success(self):
        c = classify(self.run_mode("success", json_schema={"type": "object"}))
        self.assertEqual(c.category, SUCCESS)
        self.assertEqual(c.payload["status"], "COMPLETED")

    def test_success_via_env(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "success"}):
            res = run_claude("prompt", cwd=self.cwd, timeout=10,
                             claude_bin=[sys.executable, str(FAKE)])
        self.assertEqual(classify(res).category, SUCCESS)

    def test_rate_limit(self):
        c = classify(self.run_mode("rate_limit"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (RATE_LIMIT, True, 300))
        c = classify(self.run_mode("rate_limit_stderr"))
        self.assertEqual(c.category, RATE_LIMIT)

    def test_auth(self):
        c = classify(self.run_mode("auth"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (AUTH, False, 0))

    def test_transient(self):
        c = classify(self.run_mode("transient"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TRANSIENT, True, 30))

    def test_timeout(self):
        c = classify(self.run_mode("timeout", timeout=0.5, kill_grace_seconds=2))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TIMEOUT, True, 30))

    def test_invalid_json(self):
        c = classify(self.run_mode("invalid_json"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (INVALID_OUTPUT, True, 0))

    def test_missing_structured_with_schema(self):
        c = classify(self.run_mode("missing_structured", json_schema={"type": "object"}))
        self.assertEqual(c.category, INVALID_OUTPUT)
        # Without a schema the top-level object is the payload and that is fine.
        c = classify(self.run_mode("missing_structured"))
        self.assertEqual(c.category, SUCCESS)

    def test_budget(self):
        c = classify(self.run_mode("budget"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (BUDGET, False, 0))

    def test_task_error(self):
        c = classify(self.run_mode("task_error"))
        self.assertEqual((c.category, c.retryable, c.backoff_seconds), (TASK_ERROR, False, 0))

    def test_unstructured_text(self):
        c = classify(self.run_mode("success_text", structured=False))
        self.assertEqual(c.category, SUCCESS)


if __name__ == "__main__":
    unittest.main()
