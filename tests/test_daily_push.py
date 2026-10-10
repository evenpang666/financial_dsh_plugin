import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("daily_push", ROOT / "scripts" / "daily_push.py")
daily_push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily_push)


class DailyPushTests(unittest.TestCase):
    def test_backtest_summary_is_appended_to_a_share_report(self):
        summary = {"available": True, "generated_at": "2026-10-11T10:00:00+08:00",
                   "first_day": "2026-01-01", "last_day": "2026-10-10", "days": 100,
                   "slot": "10:30", "execution": "10:35",
                   "stats": {"total_return": 0.12, "max_drawdown": -0.08, "trades": 50, "costs": 1000},
                   "oos_rank_ic": {"rank_ic": 0.04}}
        with patch("report_web_server.latest_backtest", return_value=summary):
            result = daily_push.with_backtest_section("# A股日报")
        self.assertIn("10:30 信号，10:35 模拟成交", result)
        self.assertIn("样本外 Rank IC 0.0400", result)

    def test_default_schedule_is_0920(self):
        with tempfile.TemporaryDirectory() as directory:
            config = daily_push.load_config(Path(directory) / "missing.json")
        self.assertEqual(config["scheduled_time"], "09:20")

    def test_default_schedule_includes_morning_and_afternoon(self):
        with tempfile.TemporaryDirectory() as directory:
            config = daily_push.load_config(Path(directory) / "missing.json")
        self.assertEqual(config["schedules"], {"morning": "09:20", "afternoon": "14:30"})

    def test_auto_open_report_is_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = daily_push.load_config(Path(directory) / "missing.json")
        self.assertTrue(config["auto_open_report"])

    def test_open_report_page_selects_generated_report(self):
        with patch.object(daily_push.webbrowser, "open_new_tab", return_value=True) as open_tab:
            daily_push.open_report_page("http://127.0.0.1:8766/", "2026-09-16-hk-afternoon")
        open_tab.assert_called_once_with("http://127.0.0.1:8766/?report=2026-09-16-hk-afternoon")

    def test_next_scheduled_run_selects_afternoon(self):
        config = {"schedules": {"morning": "09:20", "afternoon": "14:30"}}
        delay, session = daily_push.next_scheduled_run(config, datetime(2026, 9, 15, 10, 0, tzinfo=daily_push.CHINA_TZ))
        self.assertEqual(session, "afternoon")
        self.assertEqual(delay, 4.5 * 3600)

    def test_webhook_payload_labels_afternoon_report(self):
        payload = daily_push.webhook_payload("dingtalk", "# A股日报2｜收盘前研究简报")
        self.assertEqual(payload["markdown"]["title"], "A股收盘前研究简报")

    def test_supported_webhook_payloads(self):
        self.assertEqual(daily_push.webhook_payload("feishu", "x")["msg_type"], "text")
        self.assertEqual(daily_push.webhook_payload("wecom", "x")["msgtype"], "markdown")
        self.assertEqual(daily_push.webhook_payload("dingtalk", "x")["markdown"]["text"], "x")

    def test_run_once_archives_and_pushes(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5, "auto_open_report": True, "webhook_type": "generic", "webhook_url": "http://hook"}
        report = {"is_trading_day": True, "markdown": "# report"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daily_push, "STATE_FILE", Path(directory) / "state.json"), \
             patch.object(daily_push, "REPORT_DIR", Path(directory) / "reports"), \
             patch.object(daily_push, "get_json", return_value=report), \
             patch.object(daily_push, "notify_report_site") as notify, \
             patch.object(daily_push, "open_report_page") as open_page, \
             patch.object(daily_push, "post_webhook") as post:
            self.assertTrue(daily_push.run_once(config, force=True))
            self.assertEqual(post.call_args.args[:2], ("http://hook", "generic"))
            self.assertIn("# report", post.call_args.args[2])
            self.assertIn("## AI 因子滚动回测", post.call_args.args[2])
            notify.assert_called_once()
            open_page.assert_called_once()
            self.assertTrue(any((Path(directory) / "reports").glob("*.md")))

    def test_morning_and_afternoon_have_independent_success_state(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5, "market": "a", "webhook_url": ""}
        report = {"is_trading_day": True, "markdown": "# report"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daily_push, "STATE_FILE", Path(directory) / "state.json"), \
             patch.object(daily_push, "REPORT_DIR", Path(directory) / "reports"), \
             patch.object(daily_push, "get_json", return_value=report) as get_json, \
             patch.object(daily_push, "notify_report_site"):
            self.assertTrue(daily_push.run_once(config, session="morning"))
            self.assertFalse(daily_push.run_once(config, session="morning"))
            self.assertTrue(daily_push.run_once(config, session="afternoon"))
            self.assertFalse(daily_push.run_once(config, session="afternoon"))
            names = sorted(path.name for path in (Path(directory) / "reports").glob("*.md"))
        self.assertEqual(get_json.call_count, 2)
        self.assertTrue(any(name.endswith("-a-morning.md") for name in names))
        self.assertTrue(any(name.endswith("-a-afternoon.md") for name in names))

    def test_non_trading_day_does_not_push(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5, "webhook_url": "http://hook"}
        with patch.object(daily_push, "get_json", return_value={"is_trading_day": False}), \
             patch.object(daily_push, "post_webhook") as post:
            self.assertFalse(daily_push.run_once(config, force=True))
            post.assert_not_called()

    def test_no_holdings_still_archives_and_pushes_partial_report(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5, "webhook_url": "http://hook"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daily_push, "STATE_FILE", Path(directory) / "state.json"), \
             patch.object(daily_push, "REPORT_DIR", Path(directory) / "reports"), \
             patch.object(daily_push, "get_json", return_value={"is_trading_day": True, "has_holdings": False, "markdown": "# 市场全景\n\n## 热门股票推荐排行"}), \
             patch.object(daily_push, "notify_report_site") as notify, \
             patch.object(daily_push, "post_webhook") as post:
            self.assertTrue(daily_push.run_once(config, force=True))
            post.assert_called_once()
            notify.assert_called_once()
            self.assertTrue(any((Path(directory) / "reports").glob("*.md")))

    def test_report_is_archived_when_realtime_site_notification_fails(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5, "webhook_url": ""}
        report = {"is_trading_day": True, "markdown": "# 市场全景"}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(daily_push, "STATE_FILE", Path(directory) / "state.json"), \
             patch.object(daily_push, "REPORT_DIR", Path(directory) / "reports"), \
             patch.object(daily_push, "get_json", return_value=report), \
             patch.object(daily_push, "notify_report_site", side_effect=URLError("site offline")):
            self.assertTrue(daily_push.run_once(config, force=True))
            self.assertTrue(any((Path(directory) / "reports").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
