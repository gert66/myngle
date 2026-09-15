import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gitsafe import branch_freshness_gate  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


class BranchFreshnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.seed = self.root / "seed"
        self.seed.mkdir()
        git(self.seed, "init", "-q", "-b", "main")
        git(self.seed, "config", "user.name", "Test User")
        git(self.seed, "config", "user.email", "test@example.com")
        (self.seed / "README.md").write_text("base\n")
        git(self.seed, "add", "README.md")
        git(self.seed, "commit", "-q", "-m", "base")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-q", "-u", "origin", "main")
        self.repo = self.root / "work"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)], check=True)
        git(self.repo, "config", "user.name", "Test User")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "checkout", "-q", "-b", "feature/test", "origin/main")
        git(self.repo, "push", "-q", "-u", "origin", "feature/test")

    def gate(self, **kw):
        return branch_freshness_gate(
            self.repo, "gert66/example", str(self.remote), "feature/test", "main", **kw)

    def advance_main(self, count=1):
        for i in range(count):
            p = self.seed / f"base-{i}.txt"
            p.write_text(f"{i}\n")
            git(self.seed, "add", p.name)
            git(self.seed, "commit", "-q", "-m", f"base {i}")
        git(self.seed, "push", "-q", "origin", "main")

    def test_branch_up_to_date_allows_write(self):
        res = self.gate(mode="write")
        self.assertTrue(res.ok, res.to_dict())
        self.assertEqual((res.behind_by, res.ahead_by), (0, 0))
        self.assertEqual(res.current_branch, "feature/test")
        self.assertTrue(res.base_commit)

    def test_branch_one_commit_behind_blocks(self):
        self.advance_main(1)
        res = self.gate(mode="write")
        self.assertFalse(res.ok)
        self.assertEqual(res.behind_by, 1)
        self.assertIn("1 commit(s) behind", " ".join(c.detail for c in res.failures))

    def test_branch_many_commits_behind_blocks(self):
        self.advance_main(7)
        res = self.gate(mode="write")
        self.assertFalse(res.ok)
        self.assertEqual(res.behind_by, 7)

    def test_wrong_remote_blocks_before_fetch(self):
        other = self.root / "other.git"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
        git(self.repo, "remote", "set-url", "origin", str(other))
        res = self.gate(mode="write")
        self.assertFalse(res.ok)
        self.assertIn("origin_url", [c.name for c in res.failures])
        self.assertIsNone(res.behind_by)

    def test_dirty_working_tree_blocks(self):
        (self.repo / "README.md").write_text("dirty\n")
        res = self.gate(mode="write")
        self.assertFalse(res.ok)
        self.assertIn("clean_tree", [c.name for c in res.failures])

    def test_wrong_branch_blocks(self):
        git(self.repo, "checkout", "-q", "-b", "feature/wrong")
        res = self.gate(mode="write")
        self.assertFalse(res.ok)
        self.assertIn("branch", [c.name for c in res.failures])

    def test_read_only_job_may_proceed_on_safe_stale_branch(self):
        self.advance_main(3)
        res = self.gate(mode="read")
        self.assertTrue(res.ok, res.to_dict())
        self.assertEqual(res.behind_by, 0)
        self.assertIn("read-only", res.checks[-1].detail)


if __name__ == "__main__":
    unittest.main()
