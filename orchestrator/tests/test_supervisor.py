import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.supervisor import MARKER_FILE, notify_once, seconds_until, supervise
from core.state import load_json


class FakeMachine:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.index = 0
        self.resume_calls = 0
        self.run_calls = 0

    def status(self):
        return self.statuses[self.index]

    def resume(self, force=False):
        self.resume_calls += 1
        if self.index < len(self.statuses) - 1:
            self.index += 1
        return self.statuses[self.index]["phase"]

    def run(self):
        self.run_calls += 1
        if self.index < len(self.statuses) - 1:
            self.index += 1
        return self.statuses[self.index]["phase"]


def st(phase, **extra):
    base = {"phase": phase, "job_id": "job1", "updated_at": "2026-09-13T15:00:00+00:00",
            "step_id": "b1", "wait_until": None, "retry_phase": None,
            "human_question": None, "last_error": None}
    base.update(extra)
    return base


class SupervisorTests(unittest.TestCase):
    def test_seconds_until_missing_is_zero(self):
        self.assertEqual(seconds_until(None), 0.0)

    def test_notify_once_deduplicates_same_terminal_state(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            notifier = lambda *args: calls.append(args)
            status = st("DONE")
            self.assertTrue(notify_once(td, status, notifier=notifier))
            self.assertFalse(notify_once(td, status, notifier=notifier))
            self.assertEqual(len(calls), 1)
            self.assertEqual(load_json(Path(td) / MARKER_FILE)["phase"], "DONE")

    def test_notification_detail_includes_usage_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            notifier = lambda *args: calls.append(args)
            status = st("DONE", totals={
                "claude_calls": 4, "cost_usd": 0.1234, "input_tokens": 1000,
                "output_tokens": 200, "cache_read_input_tokens": 700,
            })
            self.assertTrue(notify_once(td, status, notifier=notifier))
            detail = calls[0][2]
            self.assertIn("$0.1234", detail)
            self.assertIn("4 call(s)", detail)
            self.assertIn("cache-read 700", detail)

    def test_changed_terminal_state_notifies_again(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            notifier = lambda *args: calls.append(args)
            self.assertTrue(notify_once(td, st("ERROR", last_error="x"), notifier=notifier))
            newer = st("ERROR", updated_at="2026-09-13T15:01:00+00:00", last_error="y")
            self.assertTrue(notify_once(td, newer, notifier=notifier))
            self.assertEqual(len(calls), 2)

    def test_waiting_resumes_then_finishes(self):
        waiting = st("WAITING", wait_until="2000-01-01T00:00:00+00:00", retry_phase="WORKING")
        fake = FakeMachine([waiting, st("DONE")])
        with mock.patch("core.supervisor.Machine", return_value=fake), \
             mock.patch("core.supervisor.notify_once", return_value=True):
            phase = supervise("/tmp/job1", runner=object(), sleep=lambda _: None)
        self.assertEqual(phase, "DONE")
        self.assertEqual(fake.resume_calls, 1)
        self.assertEqual(fake.run_calls, 0)
