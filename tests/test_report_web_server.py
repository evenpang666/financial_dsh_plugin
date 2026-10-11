import importlib.util
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("report_web_server", ROOT / "scripts" / "report_web_server.py")
report_web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_web)


class ReportWebServerTests(unittest.TestCase):
    def test_empty_report_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "REPORT_DIR", Path(directory)):
            self.assertEqual(report_web.list_reports(), [])

    def test_lists_and_reads_reports_newest_first(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "REPORT_DIR", Path(directory)):
            root = Path(directory)
            (root / "2026-09-11.md").write_text("# 旧日报", encoding="utf-8")
            (root / "2026-09-14.md").write_text("# 新日报", encoding="utf-8")
            (root / "notes.md").write_text("ignore", encoding="utf-8")
            self.assertEqual([item["date"] for item in report_web.list_reports()], ["2026-09-14", "2026-09-11"])
            report = report_web.read_report("2026-09-14.md")
            self.assertTrue(report["available"])
            self.assertEqual(report["markdown"], "# 新日报")

    def test_lists_morning_and_afternoon_reports_separately(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "REPORT_DIR", Path(directory)):
            root = Path(directory)
            (root / "2026-09-15-a-morning.md").write_text("# 日报1", encoding="utf-8")
            (root / "2026-09-15-a-afternoon.md").write_text("# 日报2", encoding="utf-8")
            reports = report_web.list_reports()
            self.assertEqual([item["session"] for item in reports], ["afternoon", "morning"])
            self.assertEqual(reports[0]["id"], "2026-09-15-a-afternoon")
            self.assertIn("日报2", reports[0]["label"])

    def test_rejects_invalid_report_path(self):
        with self.assertRaises(ValueError):
            report_web.read_report("../portfolio.json")

    def test_publish_event_notifies_connected_clients(self):
        subscriber = queue.Queue()
        with report_web.SUBSCRIBERS_LOCK:
            report_web.SUBSCRIBERS.add(subscriber)
        try:
            report_web.publish_report_event("2026-09-14")
            self.assertIn("2026-09-14", subscriber.get_nowait())
        finally:
            with report_web.SUBSCRIBERS_LOCK:
                report_web.SUBSCRIBERS.discard(subscriber)

    def test_latest_factor_ranking_is_independent_of_daily_reports(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "RANKING_FILE", Path(directory) / "latest.json"):
            self.assertFalse(report_web.latest_ranking()["available"])
            report_web.RANKING_FILE.write_text(json.dumps({"model_id": "legacy", "ranking": [{"symbol": "600001"}]}))
            self.assertFalse(report_web.latest_ranking()["available"])
            report_web.RANKING_FILE.write_text(json.dumps({"model_id": "m1", "algorithm": "xgboost", "ranking": [{"symbol": "600001"}]}))
            self.assertEqual(report_web.latest_ranking()["model_id"], "m1")
            subscriber = queue.Queue()
            with report_web.SUBSCRIBERS_LOCK:
                report_web.SUBSCRIBERS.add(subscriber)
            try:
                report_web.publish_ranking_event()
                self.assertEqual(json.loads(subscriber.get_nowait())["type"], "ranking")
            finally:
                with report_web.SUBSCRIBERS_LOCK:
                    report_web.SUBSCRIBERS.discard(subscriber)

    def test_latest_backtest_reads_newest_complete_summary(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "BACKTEST_DIR", Path(directory)):
            self.assertFalse(report_web.latest_backtest()["available"])
            old = Path(directory) / "old"
            new = Path(directory) / "new"
            old.mkdir(); new.mkdir()
            (old / "summary.json").write_text(json.dumps({"slot": "14:00", "stats": {"total_return": 0.1}}))
            self.assertFalse(report_web.latest_backtest()["available"])
            (new / "summary.json").write_text(json.dumps({"schema_version": 2,
                                                           "kind": "point_in_time_backtest",
                                                           "slot": "10:30", "stats": {"total_return": 0.2}}))
            self.assertEqual(report_web.latest_backtest()["slot"], "10:30")

    def test_structured_report_view_excludes_portfolio_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "snapshots").mkdir()
            (root / "reports" / "2026-09-23-a-morning.md").write_text("# 日报1", encoding="utf-8")
            (root / "snapshots" / "2026-09-23-a-morning.json").write_text(json.dumps({
                "portfolio": [{"symbol": "600519", "quantity": 100}],
                "global_markets": {"indices": [{"code": "SPX"}]},
            }), encoding="utf-8")
            with patch.object(report_web, "REPORT_DIR", root / "reports"), \
                 patch.object(report_web, "SNAPSHOT_DIR", root / "snapshots"):
                report = report_web.read_report("2026-09-23-a-morning.md")
            self.assertIn("global_markets", report["view"])
            self.assertNotIn("portfolio", report["view"])


if __name__ == "__main__":
    unittest.main()
