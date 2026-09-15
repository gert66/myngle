import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.intake import load, receive, update
from core.intake_watch import scan


class IntakeTests(unittest.TestCase):
    def test_receive_is_durable_and_can_be_linked(self):
        with tempfile.TemporaryDirectory() as td:
            receipt = receive("AM overnight", "Audit then build", intake_dir=td,
                              receipt_id="am-overnight-001")
            self.assertEqual(receipt["status"], "RECEIVED")
            self.assertTrue((Path(td) / "am-overnight-001.json").is_file())
            linked = update("am-overnight-001", status="SUBMITTED", job_id="job-123",
                            intake_dir=td)
            self.assertEqual(linked["job_id"], "job-123")
            self.assertEqual(load("am-overnight-001", intake_dir=td)["status"], "SUBMITTED")

    def test_stale_received_receipt_notifies_once(self):
        with tempfile.TemporaryDirectory() as td:
            receive("AM overnight", "Audit then build", intake_dir=td,
                    receipt_id="am-overnight-001")
            path = Path(td) / "am-overnight-001.json"
            payload = load("am-overnight-001", intake_dir=td)
            payload["received_at"] = "2026-09-15T04:00:00Z"
            path.write_text(__import__('json').dumps(payload), encoding="utf-8")
            calls = []
            def notifier(status, job_id, detail):
                calls.append((status, job_id, detail))
                return True
            now = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)
            self.assertEqual(scan(intake_dir=td, stale_minutes=10, notifier=notifier, now=now),
                             ["am-overnight-001"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(scan(intake_dir=td, stale_minutes=10, notifier=notifier, now=now), [])
            self.assertEqual(len(calls), 1)

    def test_submitted_receipt_never_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            receive("AM overnight", "Audit then build", intake_dir=td,
                    receipt_id="am-overnight-001")
            update("am-overnight-001", status="SUBMITTED", job_id="job-123", intake_dir=td)
            calls = []
            now = datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc)
            self.assertEqual(scan(intake_dir=td, notifier=lambda *a: calls.append(a), now=now), [])
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
