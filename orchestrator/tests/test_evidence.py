import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import evidence  # noqa: E402
from core.evidence import (  # noqa: E402
    ERROR, EXIT_NOT_RUN, EXIT_TIMEOUT, FAIL, HARNESS_ERROR, PASS, TIMEOUT, BatchEvidence,
    EvidenceError, collect_evidence, run_test_command, truncate_text,
)

PY = sys.executable
WORK = "phase3/batch3"


def py(code):
    """argv list running a small Python snippet (never a shell string)."""
    return [PY, "-c", code]


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout


def make_repo(root, branch=WORK):
    repo = Path(root) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('app')\n")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


# ---------------------------------------------------------------------------
# truncate_text
# ---------------------------------------------------------------------------

class TruncateTextTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(truncate_text("abc", 10), ("abc", False, 3))
        self.assertEqual(truncate_text("abc", 3), ("abc", False, 3))
        self.assertEqual(truncate_text("", 0), ("", False, 0))

    def test_cuts_at_limit(self):
        self.assertEqual(truncate_text("abcdef", 4), ("abcd", True, 6))
        self.assertEqual(truncate_text("abcdef", 0), ("", True, 6))

    def test_multibyte_boundary_not_split(self):
        text = "aé"  # 'é' is 2 bytes; cutting at 2 would split it
        cut, truncated, total = truncate_text(text, 2)
        self.assertEqual((cut, truncated, total), ("a", True, 3))
        self.assertEqual(truncate_text(text, 3), (text, False, 3))

    def test_deterministic(self):
        text = "x" * 1000 + "é" * 100
        first = truncate_text(text, 1050)
        for _ in range(5):
            self.assertEqual(truncate_text(text, 1050), first)
        self.assertEqual(len(first[0].encode("utf-8")), 1050)

    def test_invalid_limit(self):
        for bad in (-1, 1.5, "10", True, None):
            with self.assertRaises(EvidenceError, msg=repr(bad)):
                truncate_text("x", bad)


# ---------------------------------------------------------------------------
# run_test_command
# ---------------------------------------------------------------------------

class RunTestCommandTests(TempDirTestCase):
    def test_pass(self):
        res = run_test_command(py("import sys; print('ok'); sys.exit(0)"), self.root, timeout=30)
        self.assertEqual(res.status, PASS)
        self.assertTrue(res.passed)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.stdout, "ok\n")
        self.assertEqual(res.stderr, "")
        self.assertIsNone(res.error)
        self.assertGreaterEqual(res.duration_seconds, 0)
        self.assertEqual(res.timeout_seconds, 30)
        self.assertEqual(res.cwd, str(self.root))

    def test_fail(self):
        res = run_test_command(
            py("import sys; print('FAILED 1 test'); print('boom', file=sys.stderr); sys.exit(3)"),
            self.root, timeout=30,
        )
        self.assertEqual(res.status, FAIL)
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, 3)
        self.assertEqual(res.stdout, "FAILED 1 test\n")
        self.assertEqual(res.stderr, "boom\n")

    def test_worker_claim_in_output_does_not_make_it_pass(self):
        res = run_test_command(py("print('ALL TESTS PASSED'); raise SystemExit(1)"), self.root, timeout=30)
        self.assertEqual(res.status, FAIL)
        self.assertIn("ALL TESTS PASSED", res.stdout)

    def test_timeout(self):
        started = time.monotonic()
        res = run_test_command(
            py("import sys, time; print('partial', flush=True); time.sleep(30)"),
            self.root, timeout=0.5, kill_grace_seconds=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(res.status, TIMEOUT)
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, EXIT_TIMEOUT)
        self.assertIn("timed out after 0.5", res.error)
        self.assertLess(elapsed, 10)   # the process was actually stopped
        self.assertGreaterEqual(res.duration_seconds, 0.5)
        self.assertEqual(res.stdout, "partial\n")  # output before the stop is kept

    def test_timeout_stops_child_processes(self):
        # Parent spawns a grandchild sleeper; both must be gone after the timeout.
        code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        res = run_test_command(py(code), self.root, timeout=0.5, kill_grace_seconds=2)
        self.assertEqual(res.status, TIMEOUT)
        grandchild = int(res.stdout.strip())
        time.sleep(0.2)
        alive = Path(f"/proc/{grandchild}").exists()
        if alive:
            state = (Path(f"/proc/{grandchild}") / "status").read_text()
            alive = "zombie" not in state.lower()
        self.assertFalse(alive, "grandchild survived the timeout")

    def test_error_missing_binary(self):
        res = run_test_command(["/nonexistent/binary-xyz", "--version"], self.root, timeout=5)
        self.assertEqual(res.status, ERROR)
        self.assertEqual(res.exit_code, EXIT_NOT_RUN)
        self.assertIn("cannot start", res.error)
        self.assertEqual(res.stdout, "")

    def test_error_bad_cwd(self):
        res = run_test_command(py("pass"), self.root / "missing", timeout=5)
        self.assertEqual(res.status, ERROR)
        self.assertEqual(res.exit_code, EXIT_NOT_RUN)

    def test_error_not_executable(self):
        script = self.root / "notexec.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)
        res = run_test_command([str(script)], self.root, timeout=5)
        self.assertEqual(res.status, ERROR)
        self.assertIn("not executable", res.error)

    def test_rejects_shell_string_and_bad_argv(self):
        with self.assertRaises(EvidenceError):
            run_test_command("python3 -m unittest", self.root)
        with self.assertRaises(EvidenceError):
            run_test_command([], self.root)
        with self.assertRaises(EvidenceError):
            run_test_command([PY, ""], self.root)
        with self.assertRaises(EvidenceError):
            run_test_command([PY, 1], self.root)

    def test_shell_metacharacters_are_literal(self):
        # If a shell were involved this would echo nothing; argv passes it verbatim.
        res = run_test_command(py("import sys; print(sys.argv[1])") + ["$HOME; echo pwned"], self.root, timeout=10)
        self.assertEqual(res.status, PASS)
        self.assertEqual(res.stdout, "$HOME; echo pwned\n")

    def test_invalid_timeout(self):
        for bad in (0, -1, "5", True, None):
            with self.assertRaises(EvidenceError, msg=repr(bad)):
                run_test_command(py("pass"), self.root, timeout=bad)

    def test_output_truncation(self):
        res = run_test_command(
            py("import sys; sys.stdout.write('o' * 100); sys.stderr.write('e' * 100)"),
            self.root, timeout=10, output_max_bytes=10,
        )
        self.assertEqual(res.status, PASS)
        self.assertEqual(res.stdout, "o" * 10)
        self.assertEqual(res.stderr, "e" * 10)
        self.assertTrue(res.stdout_truncated)
        self.assertTrue(res.stderr_truncated)

    def test_to_dict_is_json_serialisable(self):
        res = run_test_command(py("pass"), self.root, timeout=10)
        data = json.loads(json.dumps(res.to_dict()))
        self.assertEqual(data["status"], PASS)
        self.assertEqual(data["argv"], py("pass"))
        self.assertEqual(set(data), {
            "argv", "cwd", "status", "exit_code", "stdout", "stderr", "duration_seconds",
            "timeout_seconds", "stdout_truncated", "stderr_truncated", "error",
        })


# ---------------------------------------------------------------------------
# collect_evidence
# ---------------------------------------------------------------------------

class CollectEvidenceTests(TempDirTestCase):
    def test_captures_git_facts(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("print('v2')\n")   # modified
        (repo / "src" / "new.py").write_text("NEW_CONTENT = 1\n")  # untracked
        ev = collect_evidence(repo)

        self.assertEqual(ev.repo, str(repo))
        self.assertEqual(ev.branch, WORK)
        self.assertEqual(ev.head_sha, git(repo, "rev-parse", "HEAD").strip())
        self.assertEqual(ev.status_porcelain, git(repo, "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertIn(" M src/app.py", ev.status_porcelain)
        self.assertIn("?? src/new.py", ev.status_porcelain)
        self.assertEqual([(c.path, c.status) for c in ev.changed_paths], [("src/app.py", " M"), ("src/new.py", "??")])
        # Diff covers tracked modifications and untracked files.
        self.assertIn("-print('app')", ev.diff)
        self.assertIn("+print('v2')", ev.diff)
        self.assertIn("+NEW_CONTENT = 1", ev.diff)
        self.assertFalse(ev.diff_truncated)
        self.assertEqual(ev.diff_total_bytes, len(ev.diff.encode("utf-8")))
        self.assertEqual(ev.diff_max_bytes, evidence.DEFAULT_DIFF_MAX_BYTES)
        self.assertEqual(ev.git_errors, [])
        self.assertEqual(ev.tests, [])
        self.assertEqual(ev.tests_summary, "NONE")
        self.assertFalse(ev.tests_passed)
        self.assertTrue(ev.collected_at.endswith("Z") or "+" in ev.collected_at)

    def test_clean_repo(self):
        repo = make_repo(self.root)
        ev = collect_evidence(repo)
        self.assertEqual(ev.status_porcelain, "")
        self.assertEqual(ev.changed_paths, [])
        self.assertEqual(ev.diff, "")
        self.assertEqual(ev.diff_total_bytes, 0)

    def test_staged_changes_are_in_diff(self):
        repo = make_repo(self.root)
        (repo / "README.md").write_text("staged\n")
        git(repo, "add", "--", "README.md")
        ev = collect_evidence(repo)
        self.assertIn("+staged", ev.diff)
        self.assertEqual(ev.changed_paths[0].status, "M ")

    def test_runs_test_commands_and_classifies(self):
        repo = make_repo(self.root)
        ev = collect_evidence(repo, test_commands=[
            py("import sys; sys.exit(0)"),
            py("import sys; sys.exit(2)"),
            py("import time; time.sleep(30)"),
            ["/nonexistent/binary-xyz"],
        ], test_timeout=0.5)
        self.assertEqual([t.status for t in ev.tests], [PASS, FAIL, TIMEOUT, ERROR])
        self.assertEqual(ev.tests_summary, HARNESS_ERROR)
        self.assertFalse(ev.tests_passed)
        self.assertTrue(all(t.cwd == str(repo) for t in ev.tests))

    def test_all_pass(self):
        repo = make_repo(self.root)
        ev = collect_evidence(repo, test_commands=[py("pass"), py("print(1)")], test_timeout=30)
        self.assertEqual(ev.tests_summary, PASS)
        self.assertTrue(ev.tests_passed)

    def test_test_cwd_override(self):
        repo = make_repo(self.root)
        other = self.root / "elsewhere"
        other.mkdir()
        ev = collect_evidence(repo, test_commands=[py("import os; print(os.getcwd())")], test_cwd=other, test_timeout=30)
        self.assertEqual(Path(ev.tests[0].stdout.strip()).resolve(), other.resolve())

    def test_diff_truncation_is_deterministic(self):
        repo = make_repo(self.root)
        (repo / "big.txt").write_text("".join(f"line {i} é\n" for i in range(500)))
        full = collect_evidence(repo)
        self.assertFalse(full.diff_truncated)
        total = full.diff_total_bytes
        self.assertGreater(total, 100)

        cut1 = collect_evidence(repo, diff_max_bytes=100)
        cut2 = collect_evidence(repo, diff_max_bytes=100)
        self.assertTrue(cut1.diff_truncated)
        self.assertEqual(cut1.diff, cut2.diff)
        self.assertEqual(cut1.diff, full.diff[: len(cut1.diff)])       # a prefix, nothing inserted
        self.assertLessEqual(len(cut1.diff.encode("utf-8")), 100)
        self.assertGreaterEqual(len(cut1.diff.encode("utf-8")), 98)    # only a split char may be dropped
        self.assertEqual(cut1.diff_total_bytes, total)                 # true size still reported
        self.assertEqual(cut1.diff_max_bytes, 100)

        exact = collect_evidence(repo, diff_max_bytes=total)
        self.assertFalse(exact.diff_truncated)
        self.assertEqual(exact.diff, full.diff)
        zero = collect_evidence(repo, diff_max_bytes=0)
        self.assertEqual(zero.diff, "")
        self.assertTrue(zero.diff_truncated)

    def test_invalid_arguments(self):
        repo = make_repo(self.root)
        with self.assertRaises(EvidenceError):
            collect_evidence(self.root / "missing")
        with self.assertRaises(EvidenceError):
            collect_evidence(repo, diff_max_bytes=-1)
        with self.assertRaises(EvidenceError):
            collect_evidence(repo, diff_max_bytes="100")
        with self.assertRaises(EvidenceError):
            collect_evidence(repo, test_commands=["python3 -m unittest"])
        with self.assertRaises(EvidenceError):
            collect_evidence(repo, test_commands=[[]])

    def test_non_repo_yields_git_errors_not_exception(self):
        plain = self.root / "plain"
        plain.mkdir()
        ev = collect_evidence(plain, test_commands=[py("pass")], test_timeout=30)
        self.assertIsNone(ev.head_sha)
        self.assertIsNone(ev.branch)
        self.assertEqual(ev.status_porcelain, "")
        self.assertEqual(ev.changed_paths, [])
        self.assertTrue(ev.git_errors)
        self.assertEqual(ev.tests[0].status, PASS)  # tests still run and are reported honestly

    def test_evidence_is_read_only(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        head = git(repo, "rev-parse", "HEAD")
        status = git(repo, "status", "--porcelain")
        collect_evidence(repo, test_commands=[py("pass")], test_timeout=30)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(repo, "status", "--porcelain"), status)

    def test_to_dict_is_json_serialisable(self):
        repo = make_repo(self.root)
        (repo / "new.txt").write_text("n\n")
        ev = collect_evidence(repo, test_commands=[py("import sys; sys.exit(1)")], test_timeout=30)
        data = json.loads(json.dumps(ev.to_dict()))
        self.assertEqual(data["branch"], WORK)
        self.assertEqual(data["head_sha"], ev.head_sha)
        self.assertEqual(data["changed_paths"], [{"path": "new.txt", "status": "??", "orig_path": None}])
        self.assertIn("+n", data["diff"])
        self.assertEqual(data["tests"][0]["status"], FAIL)
        self.assertEqual(data["tests_summary"], FAIL)
        self.assertFalse(data["tests_passed"])
        self.assertEqual(data["git_errors"], [])

    def test_summary_properties_on_plain_dataclass(self):
        ev = BatchEvidence(
            repo="r", collected_at="t", head_sha=None, branch=None, status_porcelain="",
            changed_paths=[], diff="", diff_truncated=False, diff_total_bytes=0, diff_max_bytes=1,
        )
        self.assertEqual(ev.tests_summary, "NONE")
        self.assertFalse(ev.tests_passed)


class ModuleGuaranteesTests(unittest.TestCase):
    def test_no_shell(self):
        source = Path(evidence.__file__).read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        self.assertEqual(evidence.TEST_STATUSES, (PASS, FAIL, TIMEOUT, ERROR))


if __name__ == "__main__":
    unittest.main()
