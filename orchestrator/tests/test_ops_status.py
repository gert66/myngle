import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from core.ops_status import build_snapshot, collect_approvals, collect_control_health, collect_handoffs, project_run


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class OpsStatusTests(unittest.TestCase):
    def _job(self, root, job_id, phase, now, heartbeat=None):
        d = Path(root) / job_id
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "job.json", {
            "job_id": job_id,
            "repo": "gert66/myngle",
            "branch": "work",
            "mode": "write",
            "created_at": iso(now - timedelta(minutes=10)),
        })
        write_json(d / "state.json", {
            "job_id": job_id,
            "phase": phase,
            "step_id": "step-1",
            "created_at": iso(now - timedelta(minutes=10)),
            "updated_at": iso(now - timedelta(seconds=10)),
            "runtime": {},
        })
        if heartbeat is not None:
            write_json(d / "heartbeat.json", heartbeat)
        return d

    def test_fresh_heartbeat_is_authoritatively_active(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            d = self._job(td, "job1", "WORKING", now, {
                "ts": iso(now - timedelta(seconds=12)),
                "pid": os.getpid(),
                "phase": "WORKING",
            })
            run = project_run(d, now=now)
            self.assertTrue(run["phase_active"])
            self.assertTrue(run["active"])
            self.assertFalse(run["stale"])
            self.assertTrue(run["process_alive"])
            self.assertEqual(run["heartbeat_age_seconds"], 12)

    def test_stale_heartbeat_moves_active_phase_to_attention(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            d = self._job(td, "job1", "WORKING", now, {
                "ts": iso(now - timedelta(minutes=3)),
                "pid": os.getpid(),
                "phase": "WORKING",
            })
            run = project_run(d, now=now)
            self.assertTrue(run["phase_active"])
            self.assertFalse(run["active"])
            self.assertTrue(run["stale"])
            self.assertTrue(run["needs_attention"])
            self.assertIn("stale", run["attention_reason"].lower())

    def test_received_intake_is_exposed_as_handoff_only_until_submitted(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            intake = Path(td) / "intake"
            intake.mkdir()
            write_json(intake / "a.json", {
                "receipt_id": "a",
                "title": "A task",
                "request": "Do the task",
                "source": "chatgpt",
                "status": "RECEIVED",
                "received_at": iso(now - timedelta(seconds=30)),
                "job_id": None,
            })
            write_json(intake / "b.json", {
                "receipt_id": "b",
                "title": "B task",
                "request": "Already submitted",
                "source": "chatgpt",
                "status": "SUBMITTED",
                "received_at": iso(now - timedelta(seconds=50)),
                "job_id": "job-b",
            })
            rows = collect_handoffs(intake_dir=intake, jobs_dir=Path(td) / "jobs", now=now)
            self.assertEqual([r["receipt_id"] for r in rows], ["a"])
            self.assertEqual(rows[0]["status"], "HANDOFF")
            self.assertEqual(rows[0]["age_seconds"], 30)

    def test_collect_approvals_projects_only_safe_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_json(root / "FB-1.json", {
                "proposal": {
                    "proposal_id": "FB-1", "reporter": "Carla", "company": "Acme",
                    "reported": "Cannot save outcome", "diagnosis": "Mapping mismatch",
                    "prepared_fix": "Align mapping", "resolution_note": "Fixed mapping",
                    "reporter_reply": "Thanks for spotting this.", "risk": "GREEN", "version": 2,
                    "changed_files": ["secret/internal/path.py"],
                },
                "fingerprint": "hidden-hash", "status": "pending",
                "created_at": "2026-09-16T08:00:00Z", "updated_at": "2026-09-16T08:05:00Z",
            })
            rows = collect_approvals(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["proposal_id"], "FB-1")
            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["fingerprint"], "hidden-hash")
            self.assertNotIn("changed_files", rows[0])

    def test_control_health_reports_fresh_approval_poller(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            hb = Path(td) / "poller.heartbeat"
            hb.touch()
            stamp = (now - timedelta(seconds=25)).timestamp()
            os.utime(hb, (stamp, stamp))
            health = collect_control_health(now=now, approval_heartbeat=hb)
            poller = health["approval_command_poller"]
            self.assertTrue(poller["healthy"])
            self.assertEqual(poller["heartbeat_age_seconds"], 25)

    def test_control_health_marks_missing_approval_poller_unhealthy(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            health = collect_control_health(now=now, approval_heartbeat=Path(td) / "missing")
            self.assertFalse(health["approval_command_poller"]["healthy"])

    @mock.patch("core.ops_status.collect_vm_trends", return_value={})
    @mock.patch("core.ops_status.collect_vm_health", return_value={})
    @mock.patch("core.ops_status.collect_usage_trends", return_value={})
    @mock.patch("core.ops_status.collect_usage_meta", return_value={})
    @mock.patch("core.ops_status.collect_usage_analytics", return_value={})
    def test_snapshot_has_handoff_stale_and_recent_sections(self, *_):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs"
            intake = root / "intake"
            jobs.mkdir(); intake.mkdir()
            self._job(jobs, "live-job", "WORKING", now, {
                "ts": iso(now - timedelta(seconds=5)), "pid": os.getpid(), "phase": "WORKING",
            })
            done = self._job(jobs, "done-job", "DONE", now)
            state = json.loads((done / "state.json").read_text())
            state["updated_at"] = iso(now - timedelta(seconds=20))
            write_json(done / "state.json", state)
            write_json(intake / "handoff.json", {
                "receipt_id": "handoff", "title": "Pending handoff", "request": "Start me",
                "source": "chatgpt", "status": "RECEIVED", "received_at": iso(now - timedelta(seconds=9)),
                "job_id": None,
            })
            snap = build_snapshot(jobs_dir=jobs, quota_file=root / "quota.json", intake_dir=intake, approvals_dir=root / "approvals", now=now)
            self.assertEqual(snap["summary"]["running"], 1)
            self.assertEqual(snap["summary"]["handoff"], 1)
            self.assertEqual(snap["summary"]["stale"], 0)
            self.assertEqual([x["receipt_id"] for x in snap["handoffs"]], ["handoff"])
            self.assertEqual([x["run_id"] for x in snap["recent"]], ["done-job"])


if __name__ == "__main__":
    unittest.main()
