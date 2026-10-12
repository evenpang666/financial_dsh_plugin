import importlib.util
import io
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("report_web_server", ROOT / "scripts" / "report_web_server.py")
report_web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_web)


class ReportWebServerTests(unittest.TestCase):
    def handler(self, path, headers=None):
        handler = object.__new__(report_web.ReportHandler)
        handler.path = path
        handler.headers = headers or {}
        handler.send_json = Mock()
        handler.send_bytes = Mock()
        return handler

    def test_portfolio_strategy_menu_reads_only_public_strategy_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "PROJECT_ROOT", Path(directory)):
            config = Path(directory) / "config"; config.mkdir()
            (config / "factor-lab.json").write_text(json.dumps({"strategy": "momentum"}))
            handler = self.handler("/api/portfolio-strategies")
            handler.do_GET()
        payload = handler.send_json.call_args.args[1]
        self.assertEqual(payload["selected"], "momentum")
        self.assertEqual(set(payload["fixed_strategies"]), set(report_web.STRATEGIES) - {"xgboost", "elastic_net"})
        self.assertEqual(payload["models"], {})
        self.assertNotIn("portfolio", payload)

    def test_portfolio_review_forwards_selected_strategy_and_rejects_invalid_choice(self):
        handler = self.handler("/api/portfolio-analysis", {"X-Report-Market": "a", "X-Portfolio-Strategy": "macd"})
        response = io.BytesIO(json.dumps({"available": True, "position_summary": {"rows": []}}).encode())
        with patch.object(report_web, "urlopen", return_value=response) as request:
            handler.do_POST()
        self.assertIn("strategy=macd", request.call_args.args[0].full_url)
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        handler = self.handler("/api/portfolio-analysis", {"X-Portfolio-Strategy": "invalid"})
        with patch.object(report_web, "urlopen") as request:
            handler.do_POST()
        request.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 400)

    def test_portfolio_network_error_does_not_leak_stack_or_url(self):
        handler = self.handler("/api/portfolio-analysis")
        with patch.object(report_web, "urlopen", side_effect=OSError("ProxyError HTTPSConnectionPool https://example.invalid/?long=query")):
            handler.do_POST()
        message = handler.send_json.call_args.args[1]["error"]
        self.assertIn("代理", message)
        self.assertNotIn("https", message)

    def test_whole_market_ranking_request_validates_origin_and_strategy(self):
        handler = self.handler("/api/ranking/start", {"Host": "localhost:8766", "Origin": "http://evil.invalid", "X-Ranking-Strategy": "momentum"})
        with patch.object(report_web, "start_ranking_job") as start:
            handler.do_POST()
        start.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 403)
        for headers in ({"X-Ranking-Strategy": "invalid"}, {"X-Ranking-Strategy": "momentum", "X-Report-Market": "hk"}):
            handler = self.handler("/api/ranking/start", headers)
            with patch.object(report_web, "start_ranking_job") as start:
                handler.do_POST()
            start.assert_not_called()
            self.assertEqual(handler.send_json.call_args.args[0], 400)
        handler = self.handler("/api/ranking/start", {"X-Ranking-Strategy": "momentum"})
        with patch.object(report_web, "start_ranking_job", return_value=({"job_id": "a"*32}, True)) as start:
            handler.do_POST()
        start.assert_called_once_with("momentum")
        self.assertEqual(handler.send_json.call_args.args[0], 202)

    def test_whole_market_result_cannot_fall_back_to_legacy_or_other_strategy(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "PROJECT_ROOT", Path(directory)):
            root = Path(directory) / "data" / "factor-lab"; root.mkdir(parents=True)
            path = root / "full-market-momentum.json"
            self.assertFalse(report_web.latest_ranking("momentum", "full_market")["available"])
            for payload in ({"available": True, "scope": "full_market", "strategy_id": "macd", "algorithm": "macd"}, {"available": True, "algorithm": "momentum"}):
                path.write_text(json.dumps(payload))
                self.assertFalse(report_web.latest_ranking("momentum", "full_market")["available"])
            path.write_text(json.dumps({"available": True, "scope": "full_market", "strategy_id": "momentum", "algorithm": "momentum", "ranking": []}))
            self.assertTrue(report_web.latest_ranking("momentum", "full_market")["available"])

    def test_ranking_job_is_single_instance_and_rejects_unavailable_model(self):
        import intraday_ranking
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "PROJECT_ROOT", Path(directory)), patch.object(report_web, "ACTIVE_RANKING_JOB", ""), patch.object(intraday_ranking, "ranking_options", return_value={"strategies": {"momentum": {}}}), patch.object(report_web.threading, "Thread") as worker:
            state, created = report_web.start_ranking_job("momentum")
            attached, duplicate = report_web.start_ranking_job("momentum")
            self.assertTrue(created)
            self.assertFalse(duplicate)
            self.assertEqual(state["job_id"], attached["job_id"])
            worker.assert_called_once()
            with self.assertRaises(ValueError):
                report_web.start_ranking_job("xgboost")

    def test_deployment_parameters_are_validated_and_frozen_in_job(self):
        import intraday_ranking
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(report_web, "PROJECT_ROOT", Path(directory)),
              patch.object(report_web, "ACTIVE_RANKING_JOB", ""),
              patch.object(intraday_ranking, "ranking_options", return_value={"strategies": {"momentum": {}}}),
              patch.object(report_web.threading, "Thread") as worker):
            with self.assertRaises(ValueError):
                report_web.start_ranking_job("momentum", {"top_n": 0})
            worker.assert_not_called()
            state, created = report_web.start_ranking_job("momentum", {"threshold": .01, "top_n": 3})
            self.assertTrue(created)
            self.assertEqual(state["strategy_parameters"], {"threshold": .01, "top_n": 3})
            persisted = report_web.ranking_job(state["job_id"])
            self.assertEqual(persisted["strategy_parameters"], state["strategy_parameters"])

    def test_portfolio_invalid_parameters_fail_before_forwarding_request(self):
        for raw in ('{"top_n":0}', '[]', '{invalid'):
            handler = self.handler("/api/portfolio-analysis", {"X-Portfolio-Strategy": "momentum", "X-Strategy-Parameters": raw})
            with patch.object(report_web, "urlopen") as request:
                handler.do_POST()
            request.assert_not_called()
            self.assertEqual(handler.send_json.call_args.args[0], 400)

    def test_ranking_worker_timeout_reports_failure_and_releases_single_job(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "PROJECT_ROOT", Path(directory)), patch.object(report_web, "ACTIVE_RANKING_JOB", "a"*32):
            root = Path(directory) / "data" / "factor-lab" / "ranking-jobs"; root.mkdir(parents=True)
            path = root / (("a"*32) + ".json")
            path.write_text(json.dumps({"status": "running", "job_id": "a"*32}))
            with patch.object(report_web.subprocess, "run", side_effect=report_web.subprocess.TimeoutExpired("rank", 1200)), patch.object(report_web, "publish_ranking_event") as publish:
                report_web.run_ranking_worker("a"*32, "momentum")
            self.assertEqual(json.loads(path.read_text())["status"], "failed")
            self.assertEqual(report_web.ACTIVE_RANKING_JOB, "")
            publish.assert_not_called()

    def test_backtests_routes_and_script_are_served_independently(self):
        for path in ("/backtests", "/backtests/", "/backtests.html", "/backtests.js"):
            with self.subTest(path=path):
                handler = object.__new__(report_web.ReportHandler)
                handler.path = path
                handler.send_bytes = Mock()
                handler.send_json = Mock()
                handler.do_GET()
                self.assertEqual(handler.send_bytes.call_args.args[0], 200)
                body = handler.send_bytes.call_args.args[1].decode()
                self.assertIn("backtestContent", body)
                self.assertNotIn("/api/reports", body)
                self.assertNotIn("/api/portfolio-analysis", body)
                handler.send_json.assert_not_called()

    def test_daily_page_and_script_do_not_load_backtests(self):
        html = (report_web.WEB_ROOT / "index.html").read_text()
        script = (report_web.WEB_ROOT / "app.js").read_text()
        self.assertIn('href="/backtests"', html)
        self.assertNotIn('id="backtestContent"', html)
        self.assertNotIn("/api/backtests", script)
        self.assertNotIn("loadBacktest", script)

    def test_historical_backtest_summary_is_hidden_without_rewriting_archive(self):
        source = "# 旧日报\r\n\r\n## 市场全景\r\n市场内容\r\n\r\n## 策略回测摘要\r\n历史收益\r\n### 回测方法\r\n旧方法\r\n\r\n## 数据缺失\r\n缺失说明"
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "REPORT_DIR", Path(directory)):
            path = Path(directory) / "2026-10-12-a-morning.md"
            path.write_bytes(source.encode())
            report = report_web.read_report(path.name)
            self.assertIn("市场内容", report["markdown"])
            self.assertIn("缺失说明", report["markdown"])
            self.assertNotIn("历史收益", report["markdown"])
            self.assertNotIn("旧方法", report["markdown"])
            self.assertEqual(path.read_bytes(), source.encode())

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

    def test_no_rule_triggers_is_a_valid_published_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "RANKING_FILE", Path(directory) / "latest.json"):
            payload = {"algorithm": "momentum", "available": True, "ranking": []}
            report_web.RANKING_FILE.write_text(json.dumps(payload))
            self.assertEqual(report_web.latest_ranking(), payload)

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
                "candidates": {"ranking": [{"symbol": "000001"}]},
                "weekly_events": {"events": [{"title": "测试新闻"}]},
                "warnings": ["全球指数不可用"],
            }), encoding="utf-8")
            with patch.object(report_web, "REPORT_DIR", root / "reports"), \
                 patch.object(report_web, "SNAPSHOT_DIR", root / "snapshots"):
                report = report_web.read_report("2026-09-23-a-morning.md")
            self.assertIn("global_markets", report["view"])
            self.assertNotIn("portfolio", report["view"])
            self.assertEqual(report["view"]["candidates"]["ranking"][0]["symbol"], "000001")
            self.assertEqual(report["view"]["weekly_events"]["events"][0]["title"], "测试新闻")
            self.assertIn("warnings", report["view"])

    def test_curve_uses_initial_cash_for_net_value_and_drawdown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(report_web, "BACKTEST_DIR", Path(directory)):
            root = Path(directory) / "test-mean_reversion"; root.mkdir()
            (root / "summary.json").write_text(json.dumps({"schema_version": 2, "kind": "point_in_time_backtest",
                "strategy_id": "mean_reversion", "strategy_name": "均值回归", "slot": "14:00", "stats": {"initial_cash": 100}}))
            (root / "equity.csv").write_text("day,equity\n2026-01-01,90\n2026-01-02,110\n2026-01-05,99\n")
            payload = report_web.read_backtest(root.name, curve=True)
            self.assertEqual([point["net_value"] for point in payload["curve"]], [0.9, 1.1, 0.99])
            self.assertAlmostEqual(payload["curve"][0]["drawdown"], -0.1)
            self.assertAlmostEqual(payload["curve"][-1]["drawdown"], -0.1)
            (root / "equity.csv").write_text("day,equity\n2026-01-01,nan\n")
            self.assertEqual(report_web.read_backtest(root.name, curve=True)["curve"], [])
            with self.assertRaises(ValueError):
                report_web.read_backtest("../secret")


if __name__ == "__main__":
    unittest.main()
