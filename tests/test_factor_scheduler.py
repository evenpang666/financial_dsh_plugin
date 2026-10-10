import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_scheduler


class WeeklySchedulerTests(unittest.TestCase):
    def test_weekday_due_once_with_weekly_success_and_daily_retry(self):
        china = timezone(timedelta(hours=8))
        monday = datetime(2026, 10, 12, 12, 0, tzinfo=china)
        settings = {"weekly_training_enabled": True}
        self.assertTrue(factor_scheduler.due(monday, {}, {}, settings))
        self.assertFalse(factor_scheduler.due(monday, {}, {"last_attempt_date": "2026-10-12"}, settings))
        self.assertTrue(factor_scheduler.due(monday + timedelta(days=1), {},
                                             {"last_attempt_date": "2026-10-12"}, settings))
        self.assertFalse(factor_scheduler.due(monday, {"selected_at": monday.isoformat(),
                                                "target_version": factor_scheduler.factor_lab.ML_TARGET_VERSION},
                                              {}, settings))
        self.assertFalse(factor_scheduler.due(monday.replace(hour=11), {}, {}, settings))
        self.assertFalse(factor_scheduler.due(monday + timedelta(days=5), {}, {}, settings))
        self.assertFalse(factor_scheduler.due(monday, {}, {}, {"weekly_training_enabled": False}))


if __name__ == "__main__":
    unittest.main()
