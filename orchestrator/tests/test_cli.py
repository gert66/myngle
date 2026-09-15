import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cli import build_parser, main


class CliHardeningTests(unittest.TestCase):
    def test_extend_scope_command_parses_repeated_paths(self):
        args = build_parser().parse_args([
            "extend-scope", "--job-id", "j1",
            "--allow", "src/a.ts", "--allow", "src/b.ts",
            "--answer", "approved",
        ])
        self.assertEqual(args.command, "extend-scope")
        self.assertEqual(args.allow, ["src/a.ts", "src/b.ts"])

    def test_submit_parses_role_policy_json(self):
        args = build_parser().parse_args([
            "submit", "--job-id", "j1", "--repo", "gert66/myngle", "--repo-path", "/tmp/repo",
            "--branch", "work", "--mode", "write", "--goal", "g",
            "--role-policy-json", '{"brain":{"model":"sonnet","effort":"low"}}',
        ])
        self.assertEqual(args.role_policy_json, '{"brain":{"model":"sonnet","effort":"low"}}')

    def test_submit_parses_no_start(self):
        args = build_parser().parse_args([
            "submit", "--job-id", "j1", "--repo", "gert66/myngle", "--repo-path", "/tmp/repo",
            "--branch", "work", "--mode", "write", "--goal", "g", "--no-start",
        ])
        self.assertTrue(args.no_start)

    def test_submit_autostarts_user_service(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            jobs = Path(td) / "jobs"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("core.cli.start_job_service", return_value="orchestrator@j1.service") as start:
                rc = main(["--jobs-dir", str(jobs), "submit", "--job-id", "j1",
                           "--repo", "gert66/myngle", "--repo-path", str(repo), "--branch", "work",
                           "--mode", "write", "--goal", "g"], out=out, err=err)
            self.assertEqual(rc, 0)
            start.assert_called_once_with("j1")
            self.assertTrue((jobs / "j1" / "state.json").is_file())
            self.assertIn("started orchestrator@j1.service", out.getvalue())

    def test_submit_persists_claude_affinity_and_quota_state(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            jobs = Path(td) / "jobs"
            rc = main(["--jobs-dir", str(jobs), "submit", "--job-id", "j-affinity",
                       "--repo", "gert66/myngle", "--repo-path", str(repo), "--branch", "work",
                       "--mode", "write", "--goal", "g", "--no-start",
                       "--claude-bin", "/home/myngle/bin/claude-gert66", "--quota-routing"],
                      out=io.StringIO(), err=io.StringIO())
            self.assertEqual(rc, 0)
            state = json.loads((jobs / "j-affinity" / "state.json").read_text())
            cfg = state["runtime"]["config"]
            self.assertEqual(cfg["claude_bin"], "/home/myngle/bin/claude-gert66")
            self.assertTrue(cfg["quota_routing"])

    def test_submit_persists_not_before_in_utc(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            jobs = Path(td) / "jobs"
            rc = main(["--jobs-dir", str(jobs), "submit", "--job-id", "j-scheduled",
                       "--repo", "gert66/myngle", "--repo-path", str(repo), "--branch", "work",
                       "--mode", "write", "--goal", "g", "--no-start",
                       "--not-before", "2026-09-15T03:00:00+02:00"],
                      out=io.StringIO(), err=io.StringIO())
            self.assertEqual(rc, 0)
            state = json.loads((jobs / "j-scheduled" / "state.json").read_text())
            self.assertEqual(state["runtime"]["config"]["not_before"], "2026-09-15T01:00:00Z")

    def test_submit_no_start_skips_user_service(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            jobs = Path(td) / "jobs"
            with mock.patch("core.cli.start_job_service") as start:
                rc = main(["--jobs-dir", str(jobs), "submit", "--job-id", "j2",
                           "--repo", "gert66/myngle", "--repo-path", str(repo), "--branch", "work",
                           "--mode", "write", "--goal", "g", "--no-start"],
                          out=io.StringIO(), err=io.StringIO())
            self.assertEqual(rc, 0)
            start.assert_not_called()

    def test_set_config_command_parses_tests_timeout_and_caps(self):
        args = build_parser().parse_args([
            "set-config", "--job-id", "j1",
            "--test", "./node_modules/.bin/tsc --noEmit",
            "--test", "python3 -m unittest",
            "--test-timeout", "45", "--max-claude-calls", "20",
        ])
        self.assertEqual(args.command, "set-config")
        self.assertEqual(args.test, ["./node_modules/.bin/tsc --noEmit", "python3 -m unittest"])
        self.assertEqual(args.test_timeout, 45)
        self.assertEqual(args.max_claude_calls, 20)

    def test_complete_command_parses_reason(self):
        args = build_parser().parse_args([
            "complete", "--job-id", "j1", "--reason", "bounded result accepted",
        ])
        self.assertEqual(args.command, "complete")
        self.assertEqual(args.reason, "bounded result accepted")


if __name__ == "__main__":
    unittest.main()
