import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.recover import (RECOVERABLE_PHASES, RecoverError, discover_recoverable,
                          recover_jobs, start_job_service, user_systemd_env)
from core.state import new_state, save_state


class RecoverTests(unittest.TestCase):
    def make_job(self, root, job_id, phase):
        d = Path(root) / job_id
        d.mkdir()
        st = new_state(job_id)
        st["phase"] = phase
        save_state(d / "state.json", st)
        return d

    def test_discovers_runnable_and_waiting_only(self):
        with tempfile.TemporaryDirectory() as td:
            expected = []
            for i, phase in enumerate(sorted(RECOVERABLE_PHASES)):
                job = f"run-{i}"
                self.make_job(td, job, phase)
                expected.append(job)
            for phase in ("DONE", "ERROR", "NEEDS_HUMAN"):
                self.make_job(td, f"skip-{phase.lower()}", phase)
            found, warnings = discover_recoverable(td)
            self.assertEqual(found, sorted(expected))
            self.assertEqual(warnings, [])

    def test_corrupt_or_mismatched_state_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_job(td, "good", "QUEUED")
            bad = Path(td) / "bad"; bad.mkdir(); (bad / "state.json").write_text("{bad")
            mismatch = self.make_job(td, "mismatch", "QUEUED")
            st = new_state("other"); st["phase"] = "QUEUED"; save_state(mismatch / "state.json", st)
            found, warnings = discover_recoverable(td)
            self.assertEqual(found, ["good"])
            self.assertEqual(len(warnings), 2)

    def test_recover_starts_each_job_and_collects_failures(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_job(td, "a", "QUEUED")
            self.make_job(td, "b", "WAITING")
            calls = []
            def starter(job_id):
                calls.append(job_id)
                if job_id == "b":
                    raise RecoverError("boom")
            result = recover_jobs(td, starter=starter)
            self.assertEqual(calls, ["a", "b"])
            self.assertEqual(result["started"], ["a"])
            self.assertEqual(len(result["failures"]), 1)

    def test_start_job_service_uses_argv_and_user_bus(self):
        result = mock.Mock(returncode=0, stdout="", stderr="")
        run = mock.Mock(return_value=result)
        self.assertEqual(start_job_service("job-1", run=run), "orchestrator@job-1.service")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["systemctl", "--user", "start", "orchestrator@job-1.service"])
        self.assertIn("XDG_RUNTIME_DIR", kwargs["env"])
        self.assertFalse(kwargs["check"])

    def test_user_systemd_env_preserves_existing_values(self):
        env = user_systemd_env({"XDG_RUNTIME_DIR": "/x", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/y"})
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/x")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/y")


if __name__ == "__main__":
    unittest.main()
