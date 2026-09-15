import json
import os
import tempfile
import unittest
from pathlib import Path

from core.scheduler import Scheduler, scopes_overlap, write_job_policy


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.cfg = self.root / "scheduler.json"
        dump(self.cfg, {"max_total_jobs": 3, "max_dev_jobs": 2,
                        "max_heavy_jobs": 1, "poll_seconds": 1})
        self.s = Scheduler(config_path=self.cfg, jobs_dir=self.jobs,
                           state_path=self.root / "state.json", lock_path=self.root / "lock")

    def job(self, job_id, *, repo="gert66/myngle-company-hub", repo_path=None,
            mode="write", allowed=None, resource=None, priority=0, dependencies=None,
            phase="QUEUED"):
        d = self.jobs / job_id
        d.mkdir()
        dump(d / "job.json", {"job_id": job_id, "repo": repo, "mode": mode,
                              "created_at": f"2026-09-15T06:00:{len(list(self.jobs.iterdir())):02d}Z"})
        dump(d / "state.json", {"phase": phase, "runtime": {"config": {
            "repo_path": repo_path or str(self.root / job_id),
            "allowed_paths": allowed or ["*"],
        }}})
        write_job_policy(d, resource_class=resource, priority=priority, dependencies=dependencies)
        return d

    def test_total_capacity_queues_fourth(self):
        for name in ("a", "b", "c"):
            d = self.job(name, mode="read", resource="read")
            self.assertTrue(self.s.try_acquire(d)[0])
        d = self.job("d", mode="read", resource="read")
        ok, reason = self.s.try_acquire(d)
        self.assertFalse(ok)
        self.assertIn("total capacity full", reason)

    def test_dev_capacity_is_two(self):
        self.assertTrue(self.s.try_acquire(self.job("a", allowed=["src/a"]))[0])
        self.assertTrue(self.s.try_acquire(self.job("b", allowed=["src/b"]))[0])
        ok, reason = self.s.try_acquire(self.job("c", allowed=["src/c"]))
        self.assertFalse(ok)
        self.assertIn("development capacity full", reason)

    def test_only_one_heavy_job(self):
        self.assertTrue(self.s.try_acquire(self.job("a", resource="heavy", allowed=["src/a"]))[0])
        ok, reason = self.s.try_acquire(self.job("b", resource="heavy", allowed=["src/b"]))
        self.assertFalse(ok)
        self.assertIn("heavy capacity full", reason)

    def test_same_checkout_serializes(self):
        shared = str(self.root / "shared")
        self.assertTrue(self.s.try_acquire(self.job("a", repo_path=shared, allowed=["src/a"]))[0])
        ok, reason = self.s.try_acquire(self.job("b", repo_path=shared, allowed=["src/b"]))
        self.assertFalse(ok)
        self.assertIn("same checkout", reason)

    def test_same_repo_disjoint_scopes_can_parallel(self):
        self.assertTrue(self.s.try_acquire(self.job("a", allowed=["src/a"]))[0])
        self.assertTrue(self.s.try_acquire(self.job("b", allowed=["src/b"]))[0])

    def test_same_repo_wildcard_scope_serializes(self):
        self.assertTrue(self.s.try_acquire(self.job("a", allowed=["*"]))[0])
        ok, reason = self.s.try_acquire(self.job("b", allowed=["src/b"]))
        self.assertFalse(ok)
        self.assertIn("overlapping write scope", reason)

    def test_dependency_must_be_done(self):
        dep = self.job("dep", mode="read", resource="read", phase="WORKING")
        target = self.job("target", mode="read", resource="read", dependencies=["dep"])
        ok, reason = self.s.try_acquire(target)
        self.assertFalse(ok)
        self.assertIn("dependency dep", reason)
        st = json.loads((dep / "state.json").read_text())
        st["phase"] = "DONE"
        dump(dep / "state.json", st)
        self.assertTrue(self.s.try_acquire(target)[0])

    def test_release_makes_capacity_available(self):
        a = self.job("a", allowed=["src/a"])
        b = self.job("b", allowed=["src/b"])
        c = self.job("c", allowed=["src/c"])
        self.assertTrue(self.s.try_acquire(a)[0])
        self.assertTrue(self.s.try_acquire(b)[0])
        self.assertFalse(self.s.try_acquire(c)[0])
        self.assertTrue(self.s.release("a"))
        self.assertTrue(self.s.try_acquire(c)[0])

    def test_read_jobs_can_share_repo(self):
        self.assertTrue(self.s.try_acquire(self.job("a", mode="read", resource="read"))[0])
        self.assertTrue(self.s.try_acquire(self.job("b", mode="read", resource="read"))[0])

    def test_scope_overlap_is_conservative(self):
        self.assertFalse(scopes_overlap(["src/a"], ["src/b"]))
        self.assertTrue(scopes_overlap(["src"], ["src/b"]))
        self.assertTrue(scopes_overlap(["src/*.py"], ["docs"]))
