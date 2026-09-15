import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gemini_runner import (  # noqa: E402
    FLEX_TIER, STANDARD_TIER, GeminiHTTPError, load_secrets, run_gemini,
)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout


def make_repo(root):
    repo = Path(root) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "work")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def response(worker, patch="", *, tier="flex", thinking=7):
    envelope = json.dumps({"worker": worker, "patch": patch})
    return {
        "service_tier": tier,
        "model": "gemini-3.8-flash",
        "usage": {
            "total_input_tokens": 11,
            "total_output_tokens": 5,
            "total_thought_tokens": thinking,
            "total_tokens": 16 + thinking,
        },
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": envelope}]}],
    }


def worker(step="step-1", status="COMPLETED"):
    return {
        "schema_version": 1,
        "step_id": step,
        "status": status,
        "summary": "done",
        "files_changed": [],
        "tests_run": [],
        "tests_passed": None,
        "blockers": [],
    }


class GeminiRunnerTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.repo = make_repo(self.root)
        self.schema = {"type": "object"}

    def test_flex_success_records_thinking_and_total_tokens(self):
        seen = []
        def call(prompt, tier):
            seen.append(tier)
            return response(worker(), tier=tier, thinking=13)
        result = run_gemini("inspect README", cwd=self.repo, json_schema=self.schema,
                            mode="read", allowed_paths=["*"], call_fn=call)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(seen, [FLEX_TIER])
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(result.service_tier, FLEX_TIER)
        self.assertEqual(result.gemini_usage["thinking_tokens"], 13)
        self.assertEqual(result.gemini_usage["total_tokens"], 29)
        self.assertEqual(result.parsed_json["usage"]["thinking_tokens"], 13)

    def test_429_500_503_flex_falls_back_to_standard(self):
        for code in (429, 500, 503):
            with self.subTest(code=code):
                seen = []
                def call(prompt, tier):
                    seen.append(tier)
                    if tier == FLEX_TIER:
                        raise GeminiHTTPError(code, "synthetic")
                    return response(worker(), tier=tier)
                result = run_gemini("inspect README", cwd=self.repo, json_schema=self.schema,
                                    mode="read", allowed_paths=["*"], call_fn=call)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(seen, [FLEX_TIER, STANDARD_TIER])
                self.assertEqual(result.service_tier, STANDARD_TIER)
                self.assertEqual(result.retries, 1)
                self.assertEqual(result.fallback_events[0]["reason"], f"HTTP {code}")

    def test_non_fallback_http_error_stays_on_flex(self):
        seen = []
        def call(prompt, tier):
            seen.append(tier)
            raise GeminiHTTPError(400, "bad request")
        result = run_gemini("inspect README", cwd=self.repo, json_schema=self.schema,
                            mode="read", allowed_paths=["*"], call_fn=call)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(seen, [FLEX_TIER])
        self.assertEqual(result.retries, 0)

    def test_write_patch_is_scope_checked_and_applied(self):
        patch = """diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"""
        def call(prompt, tier):
            return response(worker(), patch=patch, tier=tier)
        result = run_gemini("change VALUE", cwd=self.repo, json_schema=self.schema,
                            mode="write", allowed_paths=["src"], call_fn=call)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual((self.repo / "src" / "app.py").read_text(), "VALUE = 2\n")
        self.assertEqual(result.parsed_json["structured_output"]["files_changed"], ["src/app.py"])
        self.assertEqual(result.parsed_json["structured_output"]["tests_run"], [])
        self.assertIsNone(result.parsed_json["structured_output"]["tests_passed"])

    def test_out_of_scope_patch_is_rejected_without_mutation(self):
        patch = """diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hello\n+changed\n"""
        def call(prompt, tier):
            return response(worker(), patch=patch, tier=tier)
        result = run_gemini("change README", cwd=self.repo, json_schema=self.schema,
                            mode="write", allowed_paths=["src"], call_fn=call)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual((self.repo / "README.md").read_text(), "hello\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_read_mode_rejects_patch(self):
        patch = """diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hello\n+changed\n"""
        result = run_gemini("review only", cwd=self.repo, json_schema=self.schema,
                            mode="read", allowed_paths=["*"], call_fn=lambda p, t: response(worker(), patch=patch))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual((self.repo / "README.md").read_text(), "hello\n")

    def test_secret_loader_strips_matching_quotes(self):
        secret = self.root / "secrets.env"
        secret.write_text('GEMINI_API_KEY="abc123"\n', encoding="utf-8")
        env = {}
        load_secrets(secret, env)
        self.assertEqual(env["GEMINI_API_KEY"], "abc123")


if __name__ == "__main__":
    unittest.main()
