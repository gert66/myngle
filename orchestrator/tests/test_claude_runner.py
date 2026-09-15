import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import claude_runner as runner  # noqa: E402
from core.claude_runner import (  # noqa: E402
    EXIT_NOT_FOUND, EXIT_TIMEOUT, ClaudeResult, ClaudeRunnerError, build_argv,
    parse_top_level_json, run_claude,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"
SESSION = "123e4567-e89b-42d3-a456-426614174000"


def fake_bin(mode=None):
    """claude_bin list for the fake; mode None leaves it to FAKE_CLAUDE_MODE."""
    argv = [sys.executable, str(FAKE)]
    if mode is not None:
        argv += ["--fake-mode", mode]
    return argv


class BuildArgvTests(unittest.TestCase):
    def test_structured_default(self):
        argv = build_argv("/bin/claude")
        self.assertEqual(argv, ["/bin/claude", "--print", "--output-format", "json"])

    def test_unstructured_uses_text(self):
        argv = build_argv("/bin/claude", structured=False)
        self.assertEqual(argv[1:], ["--print", "--output-format", "text"])

    def test_bin_as_list(self):
        argv = build_argv(["python3", "fake.py", "--fake-mode", "x"])
        self.assertEqual(argv[:4], ["python3", "fake.py", "--fake-mode", "x"])
        self.assertIn("--print", argv)

    def test_json_schema_dict_is_compact_and_sorted(self):
        argv = build_argv("c", json_schema={"type": "object", "properties": {"b": {}, "a": {}}})
        i = argv.index("--json-schema")
        self.assertEqual(argv[i + 1], '{"properties":{"a":{},"b":{}},"type":"object"}')

    def test_json_schema_string_passthrough(self):
        argv = build_argv("c", json_schema='{"type": "object"}')
        self.assertEqual(argv[argv.index("--json-schema") + 1], '{"type":"object"}')

    def test_json_schema_wire_compatibility_strips_unsupported_top_level_keywords(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "properties": {"x": {"oneOf": [{"type": "string"}, {"type": "null"}]}},
            "allOf": [{"required": ["x"]}], "oneOf": [{"type": "object"}], "anyOf": [{"type": "object"}],
        }
        argv = build_argv("c", json_schema=schema)
        wire = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertNotIn("$schema", wire)
        self.assertNotIn("allOf", wire)
        self.assertNotIn("oneOf", wire)
        self.assertNotIn("anyOf", wire)
        self.assertIn("oneOf", wire["properties"]["x"])  # nested combinators remain intact
        self.assertIn("$schema", schema)  # canonical input is never mutated

    def test_json_schema_invalid(self):
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", json_schema="{nope")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", json_schema=42)
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", structured=False, json_schema={"type": "object"})

    def test_session_id(self):
        argv = build_argv("c", session_id=SESSION)
        self.assertEqual(argv[argv.index("--session-id") + 1], SESSION)
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", session_id="not-a-uuid")

    def test_permission_mode(self):
        argv = build_argv("c", permission_mode="acceptEdits")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(build_argv("c", permission_mode="plan")[-1], "plan")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", permission_mode="default")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", permission_mode="yolo")

    def test_tool_flags_one_per_tool(self):
        argv = build_argv("c", allowed_tools=["Read", "Bash(git status)"], disallowed_tools=["Write"])
        self.assertEqual(argv.count("--allowedTools"), 2)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read")
        self.assertIn("Bash(git status)", argv)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Write")

    def test_tool_flags_reject_bad_values(self):
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", allowed_tools="Read")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", disallowed_tools=["", "Write"])

    def test_append_system_prompt_file(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "stable.md"
            prefix.write_text("stable prefix")
            argv = build_argv("c", append_system_prompt_file=str(prefix))
            self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], str(prefix))
            with self.assertRaises(ClaudeRunnerError):
                build_argv("c", append_system_prompt_file="   ")

    def test_model_effort_fallback_and_budget(self):
        argv = build_argv("c", model="claude-sonnet-5", effort="high", fallback_model="opus", max_budget_usd=2.5)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "opus")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.5")
        self.assertEqual(build_argv("c", max_budget_usd=3)[-1], "3")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", max_budget_usd=0)
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", max_budget_usd="2")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", model="  ")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", effort="ultra")
        with self.assertRaises(ClaudeRunnerError):
            build_argv("c", fallback_model="  ")

    def test_all_flags_together_in_order(self):
        argv = build_argv(
            "c", json_schema={"type": "object"}, session_id=SESSION,
            permission_mode="plan", allowed_tools=["Read"], disallowed_tools=["Bash"],
            model="m", effort="medium", fallback_model="fallback", max_budget_usd=1,
        )
        self.assertEqual(argv, [
            "c", "--print", "--output-format", "json",
            "--json-schema", '{"type":"object"}',
            "--session-id", SESSION,
            "--permission-mode", "plan",
            "--allowedTools", "Read",
            "--disallowedTools", "Bash",
            "--model", "m",
            "--effort", "medium",
            "--fallback-model", "fallback",
            "--max-budget-usd", "1",
        ])

    def test_prompt_never_in_argv(self):
        argv = build_argv("c", json_schema={"type": "object"})
        self.assertTrue(all(isinstance(a, str) for a in argv))
        self.assertNotIn("-p", argv)


class ParseTopLevelJsonTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(parse_top_level_json('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_top_level_json("[1]"), [1])
        self.assertIsNone(parse_top_level_json("{nope"))
        self.assertIsNone(parse_top_level_json(""))
        self.assertIsNone(parse_top_level_json('{"a":1} trailing'))
        self.assertIsNone(parse_top_level_json(None))


class RunClaudeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_structured(self):
        prompt = "hello fake claude é"
        prefix = Path(self.cwd) / "stable-prefix.md"
        prefix.write_text("stable prefix")
        res = run_claude(
            prompt, cwd=self.cwd, timeout=10, claude_bin=fake_bin("success"),
            json_schema={"type": "object"}, session_id=SESSION,
            permission_mode="dontAsk", allowed_tools=["Read"], model="m", effort="low",
            fallback_model="fallback", max_budget_usd=1,
            append_system_prompt_file=str(prefix),
        )
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.timed_out)
        self.assertTrue(res.structured)
        self.assertTrue(res.schema_requested)
        self.assertIsNone(res.error)
        self.assertEqual(res.cwd, str(Path(self.cwd)))
        self.assertGreaterEqual(res.duration_seconds, 0)
        self.assertIsInstance(res.parsed_json, dict)
        self.assertEqual(res.parsed_json["structured_output"]["status"], "COMPLETED")
        # The prompt went over stdin and the flags reached the child.
        self.assertEqual(res.parsed_json["prompt_chars"], len(prompt))
        self.assertEqual(res.prompt_chars, len(prompt))
        seen = res.parsed_json["argv"]
        for flag in ("--print", "--output-format", "--json-schema", "--session-id",
                     "--permission-mode", "--allowedTools", "--model", "--effort",
                     "--fallback-model", "--max-budget-usd", "--append-system-prompt-file"):
            self.assertIn(flag, seen)
        self.assertEqual(seen[seen.index("--output-format") + 1], "json")
        self.assertEqual(seen[seen.index("--session-id") + 1], SESSION)
        self.assertNotIn(prompt, seen)

    def test_mode_from_environment(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "auth"}):
            res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin())
        self.assertEqual(res.exit_code, 1)
        self.assertIn("Not logged in", res.stderr)
        self.assertIsNone(res.parsed_json)

    def test_unstructured_text(self):
        res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin("success_text"),
                         structured=False)
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.structured)
        self.assertEqual(res.stdout.strip(), "plain text answer")
        self.assertIsNone(res.parsed_json)

    def test_invalid_json_is_captured_not_parsed(self):
        res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin("invalid_json"))
        self.assertEqual(res.exit_code, 0)
        self.assertIsNone(res.parsed_json)
        self.assertTrue(res.stdout.startswith("{not json"))

    def test_nonzero_exit_captured(self):
        res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin("transient"))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("ECONNRESET", res.stderr)
        self.assertFalse(res.timed_out)

    def test_timeout_graceful_terminate(self):
        t0 = time.monotonic()
        res = run_claude("p", cwd=self.cwd, timeout=0.5, claude_bin=fake_bin("timeout"),
                         kill_grace_seconds=2)
        elapsed = time.monotonic() - t0
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, EXIT_TIMEOUT)
        self.assertIn("timed out", res.error)
        self.assertLess(elapsed, 5, "terminate should stop the child well before its 30s sleep")
        self.assertIn("partial output", res.stdout)
        self.assertGreaterEqual(res.duration_seconds, 0.5)

    def test_timeout_kills_stubborn_child(self):
        t0 = time.monotonic()
        res = run_claude("p", cwd=self.cwd, timeout=0.5, claude_bin=fake_bin("timeout_stubborn"),
                         kill_grace_seconds=0.5)
        elapsed = time.monotonic() - t0
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, EXIT_TIMEOUT)
        self.assertLess(elapsed, 5, "SIGKILL must follow when SIGTERM is ignored")

    def test_missing_binary_returns_result(self):
        res = run_claude("p", cwd=self.cwd, timeout=5,
                         claude_bin=str(Path(self.cwd) / "does-not-exist"))
        self.assertEqual(res.exit_code, EXIT_NOT_FOUND)
        self.assertIn("not found", res.error)
        self.assertFalse(res.timed_out)
        self.assertIsNone(res.parsed_json)

    def test_invalid_arguments_raise(self):
        with self.assertRaises(ClaudeRunnerError):
            run_claude("", cwd=self.cwd, claude_bin=fake_bin())
        with self.assertRaises(ClaudeRunnerError):
            run_claude("p", cwd=str(Path(self.cwd) / "missing-dir"), claude_bin=fake_bin())
        with self.assertRaises(ClaudeRunnerError):
            run_claude("p", cwd=self.cwd, timeout=0, claude_bin=fake_bin())
        with self.assertRaises(ClaudeRunnerError):
            run_claude("p", cwd=self.cwd, kill_grace_seconds=-1, claude_bin=fake_bin())
        with self.assertRaises(ClaudeRunnerError):
            run_claude("p", cwd=self.cwd, claude_bin=fake_bin(),
                       append_system_prompt_file=str(Path(self.cwd) / "missing-prefix.md"))

    def test_popen_called_with_argv_list_and_no_shell(self):
        real_popen = subprocess.Popen
        calls = []

        def spy(argv, **kwargs):
            calls.append((argv, kwargs))
            return real_popen(argv, **kwargs)

        with mock.patch.object(runner.subprocess, "Popen", side_effect=spy):
            run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin("success"))
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertNotIn("env", kwargs)
        self.assertTrue(kwargs.get("start_new_session"))

    def test_result_does_not_leak_environment(self):
        secret = "sk-ant-TESTSECRET-9f8e7d6c"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret, "FAKE_CLAUDE_MODE": "success"}):
            res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin())
        dumped = json.dumps(res.to_dict())
        self.assertNotIn(secret, dumped)
        self.assertNotIn("ANTHROPIC_API_KEY", dumped)

    def test_to_dict_roundtrips_json(self):
        res = run_claude("p", cwd=self.cwd, timeout=10, claude_bin=fake_bin("success"))
        d = json.loads(json.dumps(res.to_dict()))
        self.assertEqual(set(d), {
            "argv", "cwd", "exit_code", "stdout", "stderr", "duration_seconds", "timed_out",
            "structured", "schema_requested", "parsed_json", "prompt_chars", "error",
        })
        self.assertIsInstance(ClaudeResult(**d), ClaudeResult)


if __name__ == "__main__":
    unittest.main()
