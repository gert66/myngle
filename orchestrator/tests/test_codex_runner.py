import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.codex_runner import run_codex


class CodexRunnerTests(unittest.TestCase):
    def fake_codex(self, root):
        path = Path(root) / "codex-fake"
        path.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            out = args[args.index("--output-last-message") + 1]
            prompt = sys.stdin.read()
            with open(out, "w") as f:
                json.dump({"schema_version": 1, "step_id": "s", "status": "COMPLETED",
                           "summary": "fake codex", "files_changed": [], "tests_run": [],
                           "tests_passed": None, "blockers": []}, f)
            print("fake codex ok")
        '''))
        path.chmod(0o755)
        return str(path)

    def test_success_reads_structured_last_message_and_closes_stdin(self):
        with tempfile.TemporaryDirectory() as td:
            binary = self.fake_codex(td)
            schema = {"type": "object"}
            result = run_codex("do work", cwd=td, json_schema=schema, codex_bin=binary, timeout=5)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.parsed_json["structured_output"]["status"], "COMPLETED")
            self.assertIn("fake codex ok", result.stdout)
            self.assertIn("exec", result.argv)
            self.assertIn("workspace-write", result.argv)
            self.assertIn("--ephemeral", result.argv)
            self.assertEqual(result.provider, "codex")

    def test_stable_prompt_is_prepended(self):
        with tempfile.TemporaryDirectory() as td:
            binary = self.fake_codex(td)
            prefix = Path(td) / "prefix.txt"; prefix.write_text("RULES")
            result = run_codex("TASK", cwd=td, json_schema={"type": "object"}, codex_bin=binary,
                               append_system_prompt_file=prefix, timeout=5)
            self.assertEqual(result.prompt_chars, len("RULES\n\nTASK"))

    def test_missing_binary_is_clear_runner_error(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_codex("x", cwd=td, json_schema={"type": "object"},
                               codex_bin="/definitely/missing/codex", timeout=1)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("codex binary not found", result.error)


if __name__ == "__main__":
    unittest.main()
