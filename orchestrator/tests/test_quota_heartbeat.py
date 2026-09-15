import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.claude_runner import ClaudeResult
from core.quota_heartbeat import due_accounts, run_once

NOW = datetime(2026, 9, 15, 4, 1, tzinfo=timezone.utc)  # 06:01 Europe/Amsterdam


def config(enabled=True):
    return {
        "accounts": {
            "marina": {"claude_bin": "/fake-marina", "session_remaining": 0,
                       "session_reset_at": "2026-09-15T04:00:00Z", "weekly_remaining": 1,
                       "weekly_reset_at": "2026-09-20T00:00:00Z"},
            "gert66": {"claude_bin": "/fake-gert", "session_remaining": 1,
                       "session_reset_at": "2026-09-15T09:00:00Z", "weekly_remaining": 1,
                       "weekly_reset_at": "2026-09-20T00:00:00Z"},
        },
        "clock": {"heartbeat_enabled": enabled, "timezone": "Europe/Amsterdam", "period_hours": 5,
                  "grace_minutes": 3, "accounts": {"marina": {"daily_anchor_local": "06:00"},
                                                     "gert66": {"daily_anchor_local": "07:00"}}},
        "policy": {"session_period_hours": 5, "weekly_period_hours": 168},
    }


def success_result(*args, **kwargs):
    return ClaudeResult(argv=["fake"], cwd=".", exit_code=0, stdout="OK\n", stderr="",
                        duration_seconds=0, timed_out=False, structured=False,
                        schema_requested=False, parsed_json=None)


class QuotaHeartbeatTests(unittest.TestCase):
    def test_disabled_clock_never_has_due_accounts(self):
        self.assertEqual(due_accounts(config(False), {"last_anchor": {}}, NOW), [])

    def test_real_orchestrator_activity_inside_anchor_grace_skips_heartbeat(self):
        cfg = config(True)
        cfg["accounts"]["marina"]["last_used_at"] = "2026-09-15T04:00:30Z"
        self.assertEqual(due_accounts(cfg, {"last_anchor": {}}, NOW), [])

    def test_weekly_exhausted_account_is_not_heartbeat_called_before_reset(self):
        cfg = config(True)
        cfg["accounts"]["marina"]["weekly_remaining"] = 0
        cfg["accounts"]["marina"]["weekly_reset_at"] = "2026-09-15T05:00:00Z"
        self.assertEqual(due_accounts(cfg, {"last_anchor": {}}, NOW), [])

    def test_continuous_seed_skips_only_first_routine_then_heartbeats_five_hours_later(self):
        cfg = config(True)
        cfg["clock"]["accounts"]["marina"].update({
            "seed_local": "2026-09-15T06:00:00", "external_seed": True
        })
        self.assertEqual(due_accounts(cfg, {"last_anchor": {}}, NOW), [])
        later = datetime(2026, 9, 15, 9, 1, tzinfo=timezone.utc)
        self.assertEqual([x["account"] for x in due_accounts(cfg, {"last_anchor": {}}, later)], ["marina"])

    def test_external_daily_anchor_skips_only_the_daily_routine_anchor(self):
        cfg = config(True)
        cfg["clock"]["accounts"]["marina"]["external_daily_anchor"] = True
        self.assertEqual(due_accounts(cfg, {"last_anchor": {}}, NOW), [])
        later = datetime(2026, 9, 15, 9, 1, tzinfo=timezone.utc)  # 11:01 local
        due = due_accounts(cfg, {"last_anchor": {}}, later)
        self.assertEqual([x["account"] for x in due], ["marina"])

    def test_due_account_is_called_once_and_anchor_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, state, log = root / "quota.json", root / "hb.json", root / "hb.log"
            cfg.write_text(json.dumps(config(True)))
            calls = []
            def runner(*args, **kwargs):
                calls.append((args, kwargs))
                return success_result()
            first = run_once(config_path=cfg, heartbeat_state_path=state, log_path=log,
                             now=NOW, runner=runner)
            second = run_once(config_path=cfg, heartbeat_state_path=state, log_path=log,
                              now=NOW, runner=runner)
            self.assertEqual(first["due"], ["marina"])
            self.assertEqual(first["calls"], 1)
            self.assertEqual(second["calls"], 0)
            self.assertEqual(len(calls), 1)
            saved = json.loads(cfg.read_text())
            self.assertEqual(saved["accounts"]["marina"]["session_reset_at"], "2026-09-15T09:00:00Z")
            self.assertEqual(json.loads(state.read_text())["last_anchor"]["marina"], "2026-09-15T04:00:00Z")

    def test_dry_run_never_calls_provider_or_writes_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, state = root / "quota.json", root / "hb.json"
            cfg.write_text(json.dumps(config(True)))
            def forbidden(*args, **kwargs):
                raise AssertionError("provider must not be called")
            out = run_once(config_path=cfg, heartbeat_state_path=state, log_path=root / "hb.log",
                           now=NOW, runner=forbidden, dry_run=True)
            self.assertEqual(out, {"enabled": True, "due": ["marina"], "calls": 0})
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
