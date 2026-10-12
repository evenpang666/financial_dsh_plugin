import importlib.util
import json
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
    def test_reports_archive_and_push_without_backtests_for_both_markets_and_sessions(self):
        for market in ("a", "hk"):
            for session in ("morning", "afternoon"):
                with self.subTest(market=market, session=session), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    markdown = "# 日报\n\n## 市场全景\n研究内容\n\n## 策略回测摘要\n历史绩效\n\n## 数据缺失\n保留说明"
                    with (patch.object(daily_push, "STATE_FILE", root / "state.json"),
                          patch.object(daily_push, "REPORT_DIR", root / "reports"),
                          patch.object(daily_push, "get_json", return_value={"is_trading_day": True, "markdown": markdown}),
                          patch.object(daily_push, "notify_report_site"),
                          patch("report_web_server.latest_backtest", side_effect=AssertionError("日报不应读取回测")),
                          patch.object(daily_push, "post_webhook") as post):
                        daily_push.run_once({"data_service_url": "http://test", "market": market, "webhook_url": "http://hook"}, force=True, session=session)
                    archived = next((root / "reports").glob("*.md")).read_text()
                    self.assertNotIn("策略回测摘要", archived)
                    self.assertNotIn("历史绩效", archived)
                    self.assertIn("研究内容", archived)
                    self.assertIn("保留说明", archived)
                    self.assertEqual(post.call_args.args[2], archived)

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
            self.assertNotIn("## 策略回测摘要", post.call_args.args[2])
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

    def test_manual_generation_refreshes_existing_report_without_push_or_schedule_state_changes(self):
        today = datetime.now(daily_push.CHINA_TZ).date().isoformat()
        config = {"enabled": False, "data_service_url": "http://test", "candidate_limit": 5,
                  "market": "a", "webhook_url": "http://hook", "auto_open_report": False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"
            state.write_text(json.dumps({"last_success_date": today, "completed": {f"{today}:a:morning": {"pushed": True}}}))
            before = state.read_bytes()
            with (patch.object(daily_push, "STATE_FILE", state), patch.object(daily_push, "REPORT_DIR", root / "reports"),
                  patch.object(daily_push, "get_json", side_effect=[{"is_trading_day": True, "markdown": "# first"}, {"is_trading_day": True, "markdown": "# refreshed"}]) as get,
                  patch.object(daily_push, "notify_report_site") as notify,
                  patch.object(daily_push, "post_webhook") as post, patch.object(daily_push, "open_report_page") as page):
                self.assertTrue(daily_push.run_once(config, manual=True))
                self.assertTrue(daily_push.run_once(config, manual=True))
            self.assertEqual(get.call_count, 2)
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(state.read_bytes(), before)
            post.assert_not_called(); page.assert_not_called()
            self.assertIn("# refreshed", (root / "reports" / f"{today}-a-morning.md").read_text())

    def test_manual_generation_does_not_suppress_next_scheduled_report(self):
        config = {"enabled": True, "data_service_url": "http://test", "candidate_limit": 5,
                  "market": "hk", "webhook_url": "http://hook"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(daily_push, "STATE_FILE", root / "state.json"), patch.object(daily_push, "REPORT_DIR", root / "reports"),
                  patch.object(daily_push, "get_json", return_value={"is_trading_day": True, "markdown": "# 港股报告"}) as get,
                  patch.object(daily_push, "notify_report_site"), patch.object(daily_push, "post_webhook") as post):
                self.assertTrue(daily_push.run_once(config, session="afternoon", manual=True))
                self.assertFalse((root / "state.json").exists())
                self.assertTrue(daily_push.run_once(config, session="afternoon"))
            self.assertEqual(get.call_count, 2)
            post.assert_called_once()

    def test_manual_non_trading_day_or_data_failure_preserves_previous_report(self):
        config = {"enabled": True, "data_service_url": "http://test", "market": "hk"}
        today = datetime.now(daily_push.CHINA_TZ).date().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); reports = root / "reports"; reports.mkdir()
            target = reports / f"{today}-hk-morning.md"; target.write_text("existing report")
            with (patch.object(daily_push, "STATE_FILE", root / "state.json"), patch.object(daily_push, "REPORT_DIR", reports),
                  patch.object(daily_push, "get_json", return_value={"is_trading_day": False}),
                  patch.object(daily_push, "notify_report_site") as notify):
                self.assertFalse(daily_push.run_once(config, manual=True))
                notify.assert_not_called()
            with (patch.object(daily_push, "STATE_FILE", root / "state.json"), patch.object(daily_push, "REPORT_DIR", reports),
                  patch.object(daily_push, "get_json", side_effect=URLError("offline"))):
                with self.assertRaises(URLError):
                    daily_push.run_once(config, manual=True)
            self.assertEqual(target.read_text(), "existing report")

    def test_manual_cli_selects_current_session(self):
        config = {"enabled": False}
        with (patch.object(sys, "argv", ["daily_push.py", "--manual"]),
              patch.object(daily_push, "load_config", return_value=config),
              patch.object(daily_push, "session_for_now", return_value="afternoon"),
              patch.object(daily_push, "run_once") as run, patch.object(daily_push, "run_daemon") as daemon):
            self.assertEqual(daily_push.main(), 0)
        run.assert_called_once_with(config, force=False, session="afternoon", manual=True)
        daemon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
