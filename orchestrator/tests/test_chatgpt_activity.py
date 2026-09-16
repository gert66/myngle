import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import chatgpt_activity as ca


class ChatGPTActivityTests(unittest.TestCase):
    def test_activity_url_is_derived_from_snapshot_url(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "url.txt"
            f.write_text("https://example.test/api/public/orchestrator-snapshot\n", encoding="utf-8")
            f.write_text("https://example.test/api/public/orchestrator-snapshot\n", encoding="utf-8")
            self.assertEqual(
                ca.activity_url(f),
                "https://example.test/api/public/activity-upsert",
            )

    def test_start_persists_active_record(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(ca, "post_activity", return_value={"ok": True}):
            row = ca.start(
                "chatgpt:test-1",
                "Test task",
                project="SALES COCKPIT",
                thread="Status",
                stale_minutes=7,
                state_dir=td,
            )
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["source_type"], "chatgpt")
            self.assertEqual(row["stale_after_minutes"], 7)
            saved = json.loads(next(Path(td).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(saved["external_key"], "chatgpt:test-1")

    def test_close_reuses_record_and_sets_closed_at(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(ca, "post_activity", return_value={"ok": True}):
            ca.start("chatgpt:test-2", "Test task", state_dir=td)
            row = ca.update("chatgpt:test-2", "closed", detail="Completed", state_dir=td)
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["detail_status"], "Completed")
            self.assertTrue(row["closed_at"].endswith("Z"))

    def test_needs_action_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(ca, "post_activity", return_value={"ok": True}):
            ca.start("chatgpt:test-3", "Test task", state_dir=td)
            row = ca.update(
                "chatgpt:test-3",
                "needs_action",
                action_required="Approve",
                action_where="ChatGPT",
                state_dir=td,
            )
            self.assertEqual(row["action_required"], "Approve")
            self.assertEqual(row["action_where"], "ChatGPT")


if __name__ == "__main__":
    unittest.main()
