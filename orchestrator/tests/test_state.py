import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import state as state_mod  # noqa: E402
from core.state import (  # noqa: E402
    PHASES, StateError, append_event, load_state, new_state, read_events, save_state,
)


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_state_is_valid_and_queued(self):
        s = new_state("job-1")
        self.assertEqual(s["phase"], "QUEUED")
        self.assertEqual(s["schema_version"], 1)
        self.assertIsNone(s["step_id"])
        self.assertEqual(s["attempt"], 0)

    def test_save_load_roundtrip(self):
        s = new_state("job-1")
        s["phase"] = "WORKING"
        s["step_id"] = "step-01"
        s["attempt"] = 2
        saved = save_state(self.path, s)
        loaded = load_state(self.path)
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded, s)
        # Only the canonical file remains; no temp files left behind.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["state.json"])

    def test_failure_before_replace_keeps_prior_state(self):
        original = save_state(self.path, new_state("job-1"))
        updated = dict(original, phase="PLANNING")

        with mock.patch.object(state_mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(StateError):
                save_state(self.path, updated)

        self.assertEqual(load_state(self.path), original)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["state.json"])
        # Canonical file is still complete, well-formed JSON.
        self.assertEqual(json.loads(self.path.read_text()), original)

    def test_failure_during_write_keeps_prior_state(self):
        original = save_state(self.path, new_state("job-1"))
        with mock.patch.object(state_mod.os, "fsync", side_effect=OSError("io error")):
            with self.assertRaises(StateError):
                save_state(self.path, dict(original, phase="PLANNING"))
        self.assertEqual(load_state(self.path), original)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["state.json"])

    def test_corrupt_json_raises_clear_error(self):
        self.path.write_text('{"schema_version": 1, "job_id": "job-1", "phase": "QUEU')
        with self.assertRaises(StateError) as ctx:
            load_state(self.path)
        self.assertIn("corrupt state file", str(ctx.exception))

    def test_missing_required_field_raises(self):
        s = new_state("job-1")
        del s["updated_at"]
        self.path.write_text(json.dumps(s))
        with self.assertRaises(StateError) as ctx:
            load_state(self.path)
        self.assertIn("updated_at", str(ctx.exception))

    def test_invalid_phase_rejected_on_load_and_save(self):
        s = new_state("job-1")
        s["phase"] = "SLEEPING"
        with self.assertRaises(StateError) as ctx:
            save_state(self.path, s)
        self.assertIn("phase", str(ctx.exception))
        self.assertFalse(self.path.exists())

        self.path.write_text(json.dumps(s))
        with self.assertRaises(StateError):
            load_state(self.path)

    def test_wrong_schema_version_rejected(self):
        s = new_state("job-1")
        s["schema_version"] = 2
        self.path.write_text(json.dumps(s))
        with self.assertRaises(StateError) as ctx:
            load_state(self.path)
        self.assertIn("schema_version", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(StateError):
            load_state(self.path)

    def test_all_phases_accepted(self):
        for phase in PHASES:
            save_state(self.path, new_state("job-1", phase=phase))
            self.assertEqual(load_state(self.path)["phase"], phase)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_and_read_events(self):
        first = append_event(self.path, None, "QUEUED", "job created")
        second = append_event(self.path, "QUEUED", "PLANNING", "picked up", step_id="step-01")
        self.assertEqual(set(first), {"ts", "from", "to", "reason", "step_id"})
        self.assertIsNone(first["from"])
        self.assertEqual(second["step_id"], "step-01")

        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1]), second)
        self.assertEqual(read_events(self.path), [first, second])

    def test_append_fsyncs(self):
        with mock.patch.object(state_mod.os, "fsync", wraps=os.fsync) as fsync:
            append_event(self.path, "QUEUED", "PLANNING", "x")
        self.assertTrue(fsync.called)

    def test_invalid_phase_rejected(self):
        with self.assertRaises(StateError):
            append_event(self.path, "QUEUED", "SLEEPING", "x")
        with self.assertRaises(StateError):
            append_event(self.path, "SLEEPING", "QUEUED", "x")
        self.assertFalse(self.path.exists())

    def test_empty_reason_rejected(self):
        with self.assertRaises(StateError):
            append_event(self.path, "QUEUED", "PLANNING", "  ")

    def test_read_missing_log_is_empty(self):
        self.assertEqual(read_events(self.path), [])

    def test_corrupt_line_raises(self):
        append_event(self.path, "QUEUED", "PLANNING", "ok")
        with open(self.path, "a") as fh:
            fh.write('{"ts": "broken\n')
        with self.assertRaises(StateError) as ctx:
            read_events(self.path)
        self.assertIn("line 2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
