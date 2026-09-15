import unittest
from datetime import datetime, timezone

from core.quota_clock import (daily_anchor_utc, due_anchor, next_anchor, surrounding_anchors,
                              due_seed_anchor, next_seed_anchor, seed_anchor_utc, surrounding_seed_anchors)


class QuotaClockTests(unittest.TestCase):
    def test_marina_0600_local_anchor(self):
        now = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(daily_anchor_utc(now, local_time="06:00", timezone_name="Europe/Amsterdam"),
                         datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc))

    def test_gert_one_hour_offset(self):
        now = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
        marina = daily_anchor_utc(now, local_time="06:00", timezone_name="Europe/Amsterdam")
        gert = daily_anchor_utc(now, local_time="07:00", timezone_name="Europe/Amsterdam")
        self.assertEqual((gert - marina).total_seconds(), 3600)

    def test_five_hour_cadence_crosses_midnight(self):
        now = datetime(2026, 9, 15, 19, 30, tzinfo=timezone.utc)
        items = surrounding_anchors(now, local_time="06:00", timezone_name="Europe/Amsterdam", count=6)
        gaps = [(b-a).total_seconds() for a,b in zip(items, items[1:])]
        self.assertTrue(all(g == 5*3600 for g in gaps))

    def test_due_only_inside_grace_window(self):
        self.assertIsNotNone(due_anchor(datetime(2026,9,15,9,1,tzinfo=timezone.utc),
                                        local_time="06:00", timezone_name="Europe/Amsterdam"))
        self.assertIsNone(due_anchor(datetime(2026,9,15,9,5,tzinfo=timezone.utc),
                                     local_time="06:00", timezone_name="Europe/Amsterdam"))

    def test_seed_cadence_stays_exactly_five_hours_across_calendar_days(self):
        seed = "2026-09-15T06:00:00"
        now = datetime(2026, 9, 16, 5, 30, tzinfo=timezone.utc)
        items = surrounding_seed_anchors(now, seed_local=seed, timezone_name="Europe/Amsterdam", count=8)
        gaps = [(b-a).total_seconds() for a,b in zip(items, items[1:])]
        self.assertTrue(all(g == 5*3600 for g in gaps))
        self.assertEqual(seed_anchor_utc(seed, timezone_name="Europe/Amsterdam"),
                         datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc))
        # 25 hours after the seed is 07:00 local next day, not a forced 06:00 re-anchor.
        self.assertIn(datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc), items)

    def test_external_seed_can_be_skipped_once_but_later_anchor_is_due(self):
        seed = "2026-09-15T06:00:00"
        self.assertEqual(due_seed_anchor(datetime(2026,9,15,4,1,tzinfo=timezone.utc),
                                         seed_local=seed, timezone_name="Europe/Amsterdam"),
                         datetime(2026,9,15,4,0,tzinfo=timezone.utc))
        self.assertEqual(next_seed_anchor(datetime(2026,9,15,4,1,tzinfo=timezone.utc),
                                          seed_local=seed, timezone_name="Europe/Amsterdam"),
                         datetime(2026,9,15,9,0,tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
