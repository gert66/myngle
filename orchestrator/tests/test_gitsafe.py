import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gitsafe  # noqa: E402
from core.gitsafe import (  # noqa: E402
    ChangedPath, GitSafeError, changed_paths, check_head, normalize_pattern,
    normalize_remote_url, path_matches, preflight, run_git, staging_plan,
    validate_scope,
)

ORIGIN = "git@github.com:myngle/myngle.git"
WORK = "phase3/batch3"


def git(repo, *args):
    """Run git in a temporary test repository (argv list, never a shell)."""
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout


def make_repo(root, branch=WORK, origin=ORIGIN):
    """Fresh repository with local identity, one commit and an origin remote."""
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
    if origin is not None:
        git(repo, "remote", "add", "origin", origin)
    return repo


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def check(self, result, name):
        found = [c for c in result.checks if c.name == name]
        self.assertEqual(len(found), 1, f"check {name!r} missing in {result.to_dict()}")
        return found[0]


# ---------------------------------------------------------------------------
# run_git / normalize_remote_url
# ---------------------------------------------------------------------------

class RunGitTests(RepoTestCase):
    def test_rejects_shell_string(self):
        with self.assertRaises(GitSafeError):
            run_git(self.root, "status --porcelain")
        with self.assertRaises(GitSafeError):
            run_git(self.root, ["status", 42])

    def test_missing_directory_is_error_not_exception(self):
        res = run_git(self.root / "nope", ["status"])
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, -1)
        self.assertIn("cannot run", res.error)

    def test_failed_command_is_not_ok(self):
        res = run_git(self.root, ["rev-parse", "--show-toplevel"])
        self.assertFalse(res.ok)
        self.assertNotEqual(res.exit_code, 0)
        self.assertIsNone(res.error)
        self.assertEqual(res.argv[0], "git")

    def test_success_and_to_dict(self):
        repo = make_repo(self.root)
        res = run_git(repo, ["rev-parse", "--is-inside-work-tree"])
        self.assertTrue(res.ok)
        self.assertEqual(res.stdout.strip(), "true")
        self.assertEqual(set(res.to_dict()), {"argv", "exit_code", "stdout", "stderr", "error"})

    def test_normalize_remote_url(self):
        self.assertEqual(normalize_remote_url(" git@x:a/b.git \n"), "git@x:a/b")
        self.assertEqual(normalize_remote_url("https://x/a/b"), "https://x/a/b")
        self.assertEqual(normalize_remote_url("https://x/a/b.git.git"), "https://x/a/b.git")
        with self.assertRaises(GitSafeError):
            normalize_remote_url(None)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

class PreflightTests(RepoTestCase):
    def test_expected_work_branch_accepted(self):
        repo = make_repo(self.root)
        res = preflight(repo, ORIGIN, WORK)
        self.assertTrue(res.ok, res.to_dict())
        self.assertEqual(res.failures, [])
        self.assertEqual(res.branch, WORK)
        self.assertEqual(res.origin_url, ORIGIN)
        self.assertEqual(res.head_sha, git(repo, "rev-parse", "HEAD").strip())
        self.assertEqual(len(res.head_sha), 40)
        names = [c.name for c in res.checks]
        self.assertEqual(names, [
            "repo_exists", "is_git_repo", "origin_url", "branch",
            "protected_branch", "clean_tree", "head_sha",
        ])
        json.dumps(res.to_dict())

    def test_origin_trailing_git_normalized_both_ways(self):
        repo = make_repo(self.root, origin="https://github.com/myngle/myngle")
        self.assertTrue(preflight(repo, "https://github.com/myngle/myngle.git", WORK).ok)
        (self.root / "b").mkdir()
        repo2 = make_repo(self.root / "b", origin="https://github.com/myngle/myngle.git")
        self.assertTrue(preflight(repo2, "https://github.com/myngle/myngle", WORK).ok)

    def test_wrong_remote_refused(self):
        repo = make_repo(self.root, origin="git@github.com:someone-else/fork.git")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        check = self.check(res, "origin_url")
        self.assertFalse(check.ok)
        self.assertIn("someone-else/fork", check.detail)
        self.assertIn(ORIGIN, check.detail)
        # Everything else was fine: the refusal is precisely about the remote.
        self.assertEqual([c.name for c in res.failures], ["origin_url"])

    def test_missing_origin_refused(self):
        repo = make_repo(self.root, origin=None)
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "origin_url").ok)
        self.assertIsNone(res.origin_url)

    def test_main_refused_even_when_expected(self):
        repo = make_repo(self.root, branch="main")
        res = preflight(repo, ORIGIN, "main")
        self.assertFalse(res.ok)
        self.assertTrue(self.check(res, "branch").ok)  # branch does match ...
        protected = self.check(res, "protected_branch")   # ... but is refused anyway
        self.assertFalse(protected.ok)
        self.assertIn("main", protected.detail)

    def test_master_refused_even_when_expected(self):
        repo = make_repo(self.root, branch="master")
        res = preflight(repo, ORIGIN, "master")
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "protected_branch").ok)

    def test_on_main_refused_when_work_branch_expected(self):
        repo = make_repo(self.root, branch="main")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "branch").ok)
        self.assertFalse(self.check(res, "protected_branch").ok)

    def test_wrong_work_branch_refused(self):
        repo = make_repo(self.root, branch="feature/other")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        check = self.check(res, "branch")
        self.assertFalse(check.ok)
        self.assertIn("feature/other", check.detail)
        self.assertIn(WORK, check.detail)
        self.assertTrue(self.check(res, "protected_branch").ok)

    def test_detached_head_refused(self):
        repo = make_repo(self.root)
        git(repo, "checkout", "-q", "--detach")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "branch").ok)
        self.assertIsNone(res.branch)

    def test_dirty_tree_modified_refused(self):
        repo = make_repo(self.root)
        (repo / "README.md").write_text("changed\n")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        check = self.check(res, "clean_tree")
        self.assertFalse(check.ok)
        self.assertIn("README.md", check.detail)
        self.assertEqual([c.name for c in res.failures], ["clean_tree"])

    def test_dirty_tree_untracked_refused(self):
        repo = make_repo(self.root)
        (repo / "new.txt").write_text("x\n")
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertIn("new.txt", self.check(res, "clean_tree").detail)

    def test_dirty_tree_staged_refused(self):
        repo = make_repo(self.root)
        (repo / "README.md").write_text("changed\n")
        git(repo, "add", "--", "README.md")
        self.assertFalse(preflight(repo, ORIGIN, WORK).ok)

    def test_require_clean_false_skips_check(self):
        repo = make_repo(self.root)
        (repo / "README.md").write_text("changed\n")
        res = preflight(repo, ORIGIN, WORK, require_clean=False)
        self.assertTrue(res.ok)
        self.assertNotIn("clean_tree", [c.name for c in res.checks])

    def test_missing_directory_refused(self):
        res = preflight(self.root / "missing", ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertEqual([c.name for c in res.checks], ["repo_exists"])
        self.assertIsNone(res.head_sha)

    def test_not_a_repo_refused(self):
        plain = self.root / "plain"
        plain.mkdir()
        res = preflight(plain, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertEqual([c.name for c in res.failures], ["is_git_repo"])

    def test_subdirectory_is_not_top_level(self):
        repo = make_repo(self.root)
        res = preflight(repo / "src", ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "is_git_repo").ok)
        self.assertIn("top level", self.check(res, "is_git_repo").detail)

    def test_unborn_head_refused(self):
        repo = self.root / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", WORK)
        git(repo, "remote", "add", "origin", ORIGIN)
        res = preflight(repo, ORIGIN, WORK)
        self.assertFalse(res.ok)
        self.assertFalse(self.check(res, "head_sha").ok)
        self.assertIsNone(res.head_sha)

    def test_invalid_arguments_raise(self):
        repo = make_repo(self.root)
        with self.assertRaises(GitSafeError):
            preflight(repo, "", WORK)
        with self.assertRaises(GitSafeError):
            preflight(repo, ORIGIN, "  ")
        with self.assertRaises(GitSafeError):
            preflight(repo, ORIGIN, None)

    def test_preflight_is_read_only(self):
        repo = make_repo(self.root)
        before = git(repo, "rev-parse", "HEAD")
        preflight(repo, ORIGIN, WORK)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(repo, "status", "--porcelain"), "")


# ---------------------------------------------------------------------------
# check_head
# ---------------------------------------------------------------------------

class HeadGuardTests(RepoTestCase):
    def test_unchanged_head_passes(self):
        repo = make_repo(self.root)
        sha = preflight(repo, ORIGIN, WORK).head_sha
        guard = check_head(repo, sha)
        self.assertTrue(guard.ok)
        self.assertEqual(guard.actual_sha, sha)
        self.assertEqual(guard.expected_sha, sha)

    def test_external_commit_detected(self):
        repo = make_repo(self.root)
        sha = preflight(repo, ORIGIN, WORK).head_sha
        # Someone else commits while the batch is running.
        (repo / "README.md").write_text("external\n")
        git(repo, "commit", "-q", "-am", "external change")
        guard = check_head(repo, sha)
        self.assertFalse(guard.ok)
        self.assertNotEqual(guard.actual_sha, sha)
        self.assertEqual(guard.actual_sha, git(repo, "rev-parse", "HEAD").strip())
        self.assertIn("drifted", guard.detail)
        self.assertIn("external change", guard.detail)
        json.dumps(guard.to_dict())

    def test_wrong_expected_sha_fails(self):
        repo = make_repo(self.root)
        guard = check_head(repo, "0" * 40)
        self.assertFalse(guard.ok)

    def test_unborn_head_fails(self):
        repo = self.root / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", WORK)
        guard = check_head(repo, "0" * 40)
        self.assertFalse(guard.ok)
        self.assertIsNone(guard.actual_sha)

    def test_invalid_expected_sha_raises(self):
        with self.assertRaises(GitSafeError):
            check_head(self.root, "")


# ---------------------------------------------------------------------------
# changed_paths
# ---------------------------------------------------------------------------

class ChangedPathsTests(RepoTestCase):
    def test_clean_repo_has_no_changes(self):
        repo = make_repo(self.root)
        self.assertEqual(changed_paths(repo), [])

    def test_reports_modified_untracked_staged_and_deleted(self):
        repo = make_repo(self.root)
        (repo / "README.md").write_text("changed\n")          # unstaged modification
        (repo / "src" / "new.py").write_text("x\n")           # untracked
        (repo / "staged.txt").write_text("s\n")
        git(repo, "add", "--", "staged.txt")                  # staged addition
        (repo / "src" / "app.py").unlink()                     # unstaged deletion
        entries = changed_paths(repo)
        by_path = {e.path: e.status for e in entries}
        self.assertEqual(by_path, {
            "README.md": " M",
            "src/new.py": "??",
            "staged.txt": "A ",
            "src/app.py": " D",
        })
        self.assertEqual([e.path for e in entries], sorted(by_path))  # sorted output
        self.assertTrue(all(e.orig_path is None for e in entries))

    def test_rename_carries_orig_path(self):
        repo = make_repo(self.root)
        git(repo, "mv", "src/app.py", "src/renamed.py")
        entries = changed_paths(repo)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "src/renamed.py")
        self.assertEqual(entries[0].orig_path, "src/app.py")
        self.assertEqual(entries[0].status, "R ")

    def test_untracked_files_listed_individually(self):
        repo = make_repo(self.root)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "a.py").write_text("a\n")
        (repo / "pkg" / "b.py").write_text("b\n")
        self.assertEqual([e.path for e in changed_paths(repo)], ["pkg/a.py", "pkg/b.py"])

    def test_not_a_repo_raises(self):
        with self.assertRaises(GitSafeError):
            changed_paths(self.root)


# ---------------------------------------------------------------------------
# patterns / validate_scope
# ---------------------------------------------------------------------------

class PatternTests(unittest.TestCase):
    def test_normalize_pattern(self):
        self.assertEqual(normalize_pattern(" ./src/ "), "src")
        self.assertEqual(normalize_pattern("src\\lib\\"), "src/lib")
        self.assertEqual(normalize_pattern("*.py"), "*.py")

    def test_normalize_rejects_dangerous_patterns(self):
        for bad in ("", "   ", ".", "./", "/", "/etc", "../x", "src/../x", None):
            with self.assertRaises(GitSafeError, msg=repr(bad)):
                normalize_pattern(bad)

    def test_directory_pattern_covers_subtree_only(self):
        self.assertTrue(path_matches("src/app.py", "src"))
        self.assertTrue(path_matches("src/deep/x.py", "src/"))
        self.assertTrue(path_matches("src", "src"))
        self.assertFalse(path_matches("src2/app.py", "src"))
        self.assertFalse(path_matches("other/src/app.py", "src"))

    def test_glob_pattern(self):
        self.assertTrue(path_matches("tests/test_x.py", "tests/test_*.py"))
        self.assertTrue(path_matches("a/b/c.md", "*.md"))  # fnmatch: * spans '/'
        self.assertFalse(path_matches("tests/test_x.py", "tests/test_*.txt"))
        self.assertFalse(path_matches("tests.py", "tests/*"))
        self.assertFalse(path_matches("mytests/x.py", "tests/*"))
        self.assertFalse(path_matches("tests/", "tests/*"))  # dir itself is not inside it
        self.assertFalse(path_matches("tests", "tests/*"))
        self.assertTrue(path_matches("tests/", "tests"))

    def test_unsafe_paths_never_match(self):
        self.assertFalse(path_matches("/etc/passwd", "*"))
        self.assertFalse(path_matches("../secret", "*"))
        self.assertFalse(path_matches("src/../x", "src"))
        self.assertFalse(path_matches("", "*"))


class ValidateScopeTests(unittest.TestCase):
    ALLOWED = ["core", "tests/test_*.py"]
    FORBIDDEN = ["secrets", "core/keys.py", "*.env"]

    def test_allowed_path_passes(self):
        res = validate_scope(["core/gitsafe.py", "tests/test_gitsafe.py"], self.ALLOWED, self.FORBIDDEN)
        self.assertTrue(res.ok)
        self.assertEqual(res.in_scope, ["core/gitsafe.py", "tests/test_gitsafe.py"])
        self.assertEqual(res.out_of_scope, [])
        self.assertEqual(res.forbidden, [])
        self.assertIn("2 path(s) in scope", res.detail)

    def test_outside_allowed_fails(self):
        res = validate_scope(["core/x.py", "bin/run.sh"], self.ALLOWED, self.FORBIDDEN)
        self.assertFalse(res.ok)
        self.assertEqual(res.in_scope, ["core/x.py"])
        self.assertEqual(res.out_of_scope, [{"path": "bin/run.sh", "reason": "not covered by allowed_paths"}])
        self.assertIn("out of scope: bin/run.sh", res.detail)

    def test_forbidden_fails_even_when_allowed(self):
        res = validate_scope(["core/keys.py", "secrets/token", "core/prod.env"], self.ALLOWED, self.FORBIDDEN)
        self.assertFalse(res.ok)
        self.assertEqual(res.in_scope, [])
        self.assertEqual(res.forbidden, [
            {"path": "core/keys.py", "pattern": "core/keys.py"},
            {"path": "secrets/token", "pattern": "secrets"},
            {"path": "core/prod.env", "pattern": "*.env"},
        ])
        self.assertIn("forbidden: core/keys.py (core/keys.py)", res.detail)

    def test_empty_allow_list_allows_nothing(self):
        res = validate_scope(["core/x.py"], [], None)
        self.assertFalse(res.ok)
        self.assertEqual(len(res.out_of_scope), 1)
        res = validate_scope(["core/x.py"], None)
        self.assertFalse(res.ok)

    def test_no_paths_is_ok(self):
        res = validate_scope([], self.ALLOWED, self.FORBIDDEN)
        self.assertTrue(res.ok)
        self.assertEqual(res.in_scope, [])

    def test_unsafe_paths_are_out_of_scope(self):
        res = validate_scope(["/etc/passwd", "../up.py"], ["*"], None)
        self.assertFalse(res.ok)
        reasons = {o["path"]: o["reason"] for o in res.out_of_scope}
        self.assertEqual(reasons["/etc/passwd"], "absolute path")
        self.assertIn("..", reasons["../up.py"])

    def test_rename_checks_both_sides(self):
        moved_in = ChangedPath(path="core/new.py", status="R ", orig_path="bin/old.sh")
        res = validate_scope([moved_in], self.ALLOWED, self.FORBIDDEN)
        self.assertFalse(res.ok)
        self.assertEqual([o["path"] for o in res.out_of_scope], ["bin/old.sh"])

    def test_accepts_changed_path_objects_and_dedupes(self):
        entries = [ChangedPath("core/a.py", " M"), ChangedPath("core/a.py", "??"), "core/b.py"]
        res = validate_scope(entries, self.ALLOWED)
        self.assertTrue(res.ok)
        self.assertEqual(res.in_scope, ["core/a.py", "core/b.py"])

    def test_string_pattern_list_rejected(self):
        with self.assertRaises(GitSafeError):
            validate_scope(["core/a.py"], "core")
        with self.assertRaises(GitSafeError):
            validate_scope(["core/a.py"], ["core"], "secrets")

    def test_to_dict_is_json_serialisable(self):
        res = validate_scope(["core/a.py", "x"], self.ALLOWED, self.FORBIDDEN)
        data = json.loads(json.dumps(res.to_dict()))
        self.assertFalse(data["ok"])
        self.assertEqual(data["allowed_paths"], self.ALLOWED)
        self.assertEqual(data["forbidden_paths"], self.FORBIDDEN)


# ---------------------------------------------------------------------------
# staging_plan
# ---------------------------------------------------------------------------

class StagingPlanTests(RepoTestCase):
    ALLOWED = ["src", "docs/*.md"]
    FORBIDDEN = ["src/secret.py"]

    def test_in_scope_changes_produce_exact_paths(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        (repo / "src" / "new.py").write_text("n\n")
        (repo / "docs").mkdir()
        (repo / "docs" / "guide.md").write_text("g\n")
        plan = staging_plan(repo, self.ALLOWED, self.FORBIDDEN)
        self.assertTrue(plan.ok, plan.to_dict())
        self.assertEqual(plan.paths, ["docs/guide.md", "src/app.py", "src/new.py"])
        self.assertEqual(plan.git_add_argv(), ["git", "add", "--", "docs/guide.md", "src/app.py", "src/new.py"])
        self.assertEqual({c.path for c in plan.changed}, set(plan.paths))
        self.assertIn("3 path(s) eligible", plan.detail)

    def test_out_of_scope_change_fails_closed(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        (repo / "README.md").write_text("touched\n")  # not allowed
        plan = staging_plan(repo, self.ALLOWED, self.FORBIDDEN)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.paths, [])                # never a partial plan
        self.assertIsNone(plan.git_add_argv())
        self.assertTrue(plan.detail.startswith("refused: "))
        self.assertIn("README.md", plan.detail)
        self.assertEqual([o["path"] for o in plan.scope.out_of_scope], ["README.md"])
        self.assertEqual(plan.scope.in_scope, ["src/app.py"])  # visible, but not staged

    def test_forbidden_change_fails_closed(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        (repo / "src" / "secret.py").write_text("token\n")
        plan = staging_plan(repo, self.ALLOWED, self.FORBIDDEN)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.paths, [])
        self.assertIsNone(plan.git_add_argv())
        self.assertIn("forbidden: src/secret.py", plan.detail)

    def test_plan_never_includes_out_of_scope_files(self):
        repo = make_repo(self.root)
        for rel in ("src/a.py", "src/b.py", "bin/x.sh", "src/secret.py"):
            path = repo / rel
            path.parent.mkdir(exist_ok=True)
            path.write_text("x\n")
        plan = staging_plan(repo, self.ALLOWED, self.FORBIDDEN)
        self.assertNotIn("bin/x.sh", plan.paths)
        self.assertNotIn("src/secret.py", plan.paths)
        self.assertEqual(plan.paths, [])  # and, being refused, nothing at all
        # A plan that is ok only ever contains allowed paths.
        (repo / "bin" / "x.sh").unlink()
        (repo / "bin").rmdir()
        (repo / "src" / "secret.py").unlink()
        plan = staging_plan(repo, self.ALLOWED, self.FORBIDDEN)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.paths, ["src/a.py", "src/b.py"])
        for path in plan.paths:
            self.assertTrue(any(path_matches(path, p) for p in self.ALLOWED))
            self.assertFalse(any(path_matches(path, p) for p in self.FORBIDDEN))

    def test_no_changes_is_ok_and_empty(self):
        repo = make_repo(self.root)
        plan = staging_plan(repo, self.ALLOWED)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.paths, [])
        self.assertIsNone(plan.git_add_argv())
        self.assertEqual(plan.detail, "no changes to stage")

    def test_deletion_and_rename_are_eligible(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").unlink()
        plan = staging_plan(repo, self.ALLOWED)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.paths, ["src/app.py"])

    def test_plan_does_not_stage_anything(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        head = git(repo, "rev-parse", "HEAD")
        status_before = git(repo, "status", "--porcelain")
        plan = staging_plan(repo, self.ALLOWED)
        self.assertTrue(plan.ok)
        self.assertEqual(git(repo, "status", "--porcelain"), status_before)  # still " M"
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")

    def test_to_dict_is_json_serialisable(self):
        repo = make_repo(self.root)
        (repo / "src" / "app.py").write_text("v2\n")
        data = json.loads(json.dumps(staging_plan(repo, self.ALLOWED, self.FORBIDDEN).to_dict()))
        self.assertTrue(data["ok"])
        self.assertEqual(data["paths"], ["src/app.py"])
        self.assertEqual(data["changed"][0]["status"], " M")
        self.assertTrue(data["scope"]["ok"])


class ModuleGuaranteesTests(unittest.TestCase):
    def test_no_shell_and_no_write_commands(self):
        source = Path(gitsafe.__file__).read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        for verb in ('"commit"', '"push"', '"reset"', '"checkout"', '"merge"', '"rebase"'):
            self.assertNotIn(verb, source, f"gitsafe must not run git {verb}")
        # "add" appears only in the argv the *caller* may run later.
        self.assertNotIn('run_git(repo, ["add"', source)

    def test_protected_branches(self):
        self.assertEqual(gitsafe.PROTECTED_BRANCHES, frozenset({"main", "master"}))


if __name__ == "__main__":
    unittest.main()
