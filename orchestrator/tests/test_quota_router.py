import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.quota_router import QuotaRouter, rank_routes

NOW = datetime(2026, 9, 14, 21, 18, tzinfo=timezone.utc)


def state():
    return {
        "policy": {"weekly_reserve": 0.15, "session_reserve": 0.10, "fable_eligible_roles": ["brain", "worker", "reviewer"]},
        "accounts": {
            "gert66": {"plan_factor": 1, "claude_bin": "/gert", "session_remaining": .67,
                       "session_reset_at": "2026-09-15T00:39:00Z", "weekly_remaining": .74,
                       "weekly_reset_at": "2026-09-17T07:59:00Z"},
            "marina": {"plan_factor": 5, "claude_bin": "/marina", "session_remaining": 1,
                       "session_reset_at": "2026-09-15T03:59:00Z", "weekly_remaining": 0,
                       "weekly_reset_at": "2026-09-15T03:59:00Z",
                       "buckets": {"all": {"remaining": 0, "reset_at": "2026-09-15T03:59:00Z"},
                                   "fable": {"remaining": .82, "reset_at": "2026-09-15T03:59:00Z"}}},
        },
    }


class QuotaRouterTests(unittest.TestCase):
    def test_reviewer_uses_expiring_marina_fable(self):
        ranked = rank_routes(state(), role="reviewer", now=NOW)
        self.assertEqual((ranked[0]["account"], ranked[0]["bucket"], ranked[0]["model"]), ("marina", "fable", "fable"))

    def test_worker_uses_expiring_marina_fable_when_available(self):
        ranked = rank_routes(state(), role="worker", now=NOW)
        self.assertEqual((ranked[0]["account"], ranked[0]["bucket"]), ("marina", "fable"))

    def test_explicit_sonnet_preserves_model_and_uses_gert(self):
        ranked = rank_routes(state(), role="reviewer", requested_model="sonnet", now=NOW)
        self.assertEqual(ranked[0]["account"], "gert66")
        self.assertEqual(ranked[0]["model"], "sonnet")

    def test_plan_factor_changes_absolute_capacity(self):
        s = state()
        s["accounts"]["marina"]["weekly_remaining"] = .20
        s["accounts"]["marina"]["buckets"]["all"]["remaining"] = .20
        ranked = rank_routes(s, role="worker", now=NOW)
        self.assertEqual(ranked[0]["account"], "marina")

    def test_router_reloads_state_each_selection(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "quota.json"
            path.write_text(json.dumps(state()))
            r = QuotaRouter(path, clock=lambda: NOW)
            self.assertEqual(r.select("worker", "sonnet")["account"], "gert66")
            s = state(); s["accounts"]["gert66"]["weekly_remaining"] = 0
            path.write_text(json.dumps(s))
            self.assertIsNone(r.select("worker", "sonnet"))

    def test_elapsed_weekly_reset_rolls_bucket_to_full(self):
        s = state()
        later = datetime(2026, 9, 15, 4, 1, tzinfo=timezone.utc)
        ranked = rank_routes(s, role="worker", now=later)
        self.assertTrue(any(r["account"] == "marina" and r["weekly_remaining"] == 1.0 for r in ranked))

    def test_mark_rate_limited_persists_cooldown_to_next_clock_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "quota.json"
            st = state()
            st["clock"] = {
                "timezone": "Europe/Amsterdam", "period_hours": 5,
                "accounts": {"marina": {"daily_anchor_local": "06:00"}},
            }
            st["accounts"]["marina"]["session_reset_at"] = "2026-09-14T20:00:00Z"
            path.write_text(json.dumps(st))
            r = QuotaRouter(path, clock=lambda: NOW)
            reset = r.mark_rate_limited({"account": "marina", "bucket": "all"})
            self.assertEqual(reset, "2026-09-15T00:00:00Z")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["accounts"]["marina"]["session_remaining"], 0.0)
            self.assertEqual(saved["accounts"]["marina"]["cooldown_until"], reset)

    def test_next_available_at_waits_for_earliest_usable_account(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "quota.json"
            st = state()
            st["accounts"]["gert66"]["session_remaining"] = 0
            st["accounts"]["gert66"]["session_reset_at"] = "2026-09-14T22:30:00Z"
            st["accounts"]["marina"]["session_remaining"] = 0
            st["accounts"]["marina"]["session_reset_at"] = "2026-09-15T00:00:00Z"
            st["accounts"]["marina"]["weekly_remaining"] = 0
            st["accounts"]["marina"]["buckets"]["all"]["remaining"] = 0
            path.write_text(json.dumps(st))
            r = QuotaRouter(path, clock=lambda: NOW)
            self.assertEqual(r.next_available_at("worker", requested_model="sonnet"), "2026-09-14T22:30:00Z")


if __name__ == "__main__":
    unittest.main()
