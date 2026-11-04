import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bar_strategy as bar
TEMP = Path(__file__).resolve().parents[1] / ".test-tmp"
TEMP.mkdir(exist_ok=True)


def daily_history():
    dates = pd.bdate_range("2017-01-02", "2026-03-31")
    close = 100 + np.arange(len(dates)) * .04 + np.sin(np.arange(len(dates)) / 7)
    return pd.DataFrame({"date": dates, "open": close * .998, "high": close * 1.01,
                         "low": close * .99, "close": close, "volume": np.full(len(dates), 10000)})


class BarStrategyTests(unittest.TestCase):
    def test_holding_pool_backtest_saves_snapshot_and_keeps_custom_limit(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "BACKTESTS", Path(directory)), patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            result = bar.run("backtest", "us", "daily", "momentum", ["AAPL"], "2024-01-01", "2026-03-25", pool="portfolio", pool_as_of="2026-11-03T05:00:00Z")
            self.assertEqual(result["pool"]["id"], "portfolio")
            self.assertEqual(result["pool"]["as_of"], "2026-11-03T05:00:00Z")
            self.assertEqual(result["universe"], ["AAPL"])
            self.assertIn("持股数量", result["pool"]["membership_policy"])
        symbols = [f"{600000+i}" for i in range(100)]
        self.assertEqual(len(bar.resolve_pool("backtest", "a", "portfolio", symbols)[0]), 100)
        with self.assertRaises(ValueError):
            bar.resolve_pool("backtest", "a", "custom", symbols)
        with self.assertRaises(ValueError):
            bar.resolve_pool("train", "a", "portfolio", ["600519"])

    def test_holding_deployment_is_saved_separately_from_custom_pool(self):
        dates = pd.bdate_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=1), periods=150)
        frame = daily_history().tail(len(dates)).copy(); frame["date"] = dates
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "DEPLOYMENTS", Path(directory)), patch.object(bar, "history", return_value=frame):
            result = bar.run("deploy", "us", "daily", "momentum", ["AAPL"], "", "", pool="portfolio")
            self.assertEqual(result["pool"]["id"], "portfolio")
            self.assertTrue((Path(directory) / "latest-us-daily-momentum-portfolio.json").is_file())
            self.assertFalse((Path(directory) / "latest-us-daily-momentum-custom.json").exists())

    def test_completed_week_and_month_exclude_partial_period(self):
        frame = daily_history()
        week = bar.completed_bars(frame, "weekly", "2026-03-25")
        month = bar.completed_bars(frame, "monthly", "2026-03-25")
        self.assertEqual(str(week.date.iloc[-1].date()), "2026-03-20")
        self.assertEqual(str(month.date.iloc[-1].date()), "2026-02-27")

    def test_three_markets_and_periods_backtest_from_completed_bars(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "BACKTESTS", Path(directory)), patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            for market, symbol in (("a", "600519"), ("hk", "00700"), ("us", "AAPL")):
                for timeframe in bar.PERIODS:
                    with self.subTest(market=market, timeframe=timeframe):
                        result = bar.run("backtest", market, timeframe, "momentum", [symbol], "2024-01-01", "2026-03-25")
                        self.assertEqual(result["market"], market)
                        self.assertEqual(result["timeframe"], timeframe)
                        self.assertTrue((Path(directory) / result["run_id"] / "summary.json").is_file())
                        if timeframe == "monthly":
                            self.assertLessEqual(result["last_day"], "2026-02-27")

    def test_a_daily_target_exits_after_next_trading_day(self):
        with patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            frame = bar.panel("a", "daily", ["600519"], "2025-01-01", "2025-03-31", "momentum", bar.validate_parameters("momentum"))
        row = frame.iloc[100]
        self.assertGreater(row.exit_day, row.entry_day)
        self.assertAlmostEqual(row.target, row.exit_close / row.entry_open - 1)

    def test_market_and_timeframe_model_catalogs_are_isolated(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "MODELS", Path(directory)):
            self.assertEqual(bar.options("hk", "monthly")["timeframe"], "monthly")
            self.assertFalse(bar.options("hk", "monthly")["models"])
            with self.assertRaisesRegex(ValueError, "市场或K线周期"):
                bar.options("hk", "hourly")

    def test_a_share_index_pool_uses_current_constituents_and_cache(self):
        dates = pd.DataFrame({"日期": ["2026-10-30"] * 300,
                              "成分券代码": [f"{number:06d}" for number in range(300)]})
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "POOLS", Path(directory)), patch("akshare.index_stock_cons_csindex", return_value=dates) as fetch:
            symbols, info = bar.resolve_pool("deploy", "a", "csi300", [])
            self.assertEqual(len(symbols), 300)
            self.assertEqual(info["as_of"], "2026-10-30")
            self.assertEqual(info["name"], "沪深300")
            self.assertEqual(bar.resolve_pool("deploy", "a", "csi300", [])[0], symbols)
            fetch.assert_called_once_with(symbol="000300")
        with self.assertRaisesRegex(ValueError, "不支持"):
            bar.resolve_pool("backtest", "a", "all", [])

    def test_historical_index_pool_freezes_members_known_at_start(self):
        historical = pd.DataFrame({"updateDate": ["2024-12-30"] * 300,
                                   "code": [f"sh.{number:06d}" for number in range(600000, 600300)]})
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "POOLS", Path(directory)), \
                patch("factor_data_prep._baostock_login"), patch("factor_data_prep._baostock_logout"), \
                patch("factor_data_prep._bs_query", return_value=historical) as fetch:
            symbols, info = bar.resolve_pool("train", "a", "csi300", [], "2025-01-01")
            self.assertEqual(len(symbols), 300)
            self.assertEqual(info["as_of"], "2024-12-30")
            self.assertEqual(info["requested_for"], "2025-01-01")
            self.assertEqual(bar.resolve_pool("backtest", "a", "csi300", [], "2025-01-01")[0], symbols)
            fetch.assert_called_once()
        historical["updateDate"] = "2025-01-02"
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "POOLS", Path(directory)), \
                patch("factor_data_prep._baostock_login"), patch("factor_data_prep._baostock_logout"), \
                patch("factor_data_prep._bs_query", return_value=historical):
            with self.assertRaisesRegex(ValueError, "晚于研究开始日"):
                bar.resolve_pool("train", "a", "csi300", [], "2025-01-01")

    def test_preset_panel_reports_missing_history(self):
        symbols = [f"{number:06d}" for number in range(10)]
        caller = threading.get_ident()
        def fetch(_market, symbol, _start, _end, provider):
            self.assertEqual(provider, "baostock")
            self.assertEqual(threading.get_ident(), caller)
            if symbol == symbols[0]:
                raise ValueError("行情缺失")
            return daily_history()
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "HISTORY_CACHE", Path(directory)), patch.object(bar, "history", side_effect=fetch):
            frame = bar.panel("a", "daily", symbols, "2025-01-01", "2026-03-25", "momentum", bar.validate_parameters("momentum"), allow_missing=True)
            self.assertEqual(len(frame.attrs["valid_symbols"]), 9)
            self.assertEqual(frame.attrs["failures"][0]["symbol"], symbols[0])

    def test_historical_training_keeps_pre_delisting_samples_and_backtest_stops_on_stale_history(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "HISTORY_CACHE", Path(directory)), \
                patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            training = bar.panel("a", "daily", ["600837"], "2025-01-01", "2026-05-31",
                                 "xgboost", {}, allow_missing=True, require_recent=False)
            self.assertEqual(training.attrs["valid_symbols"], ["600837"])
            self.assertGreater(len(training[training.date >= pd.Timestamp("2025-01-01")]), 20)
            with self.assertRaisesRegex(ValueError, "提前断档"):
                bar.panel("a", "daily", ["600837"], "2025-01-01", "2026-05-31",
                          "xgboost", {}, allow_missing=True, strict_stale=True)

    def test_shorter_backtest_reuses_cached_longer_range(self):
        start, end = "2025-01-01", "2026-03-25"
        first = str((pd.Timestamp(start) - pd.Timedelta(days=500)).date())
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "HISTORY_CACHE", Path(directory)), \
                patch.object(bar, "history", side_effect=AssertionError("should use cached bars")):
            cache = Path(directory) / "a" / "baostock-qfq" / f"600519-{first}-2026-03-31.csv.gz"
            cache.parent.mkdir(parents=True)
            daily_history().to_csv(cache, index=False, compression="gzip")
            frame = bar.panel("a", "daily", ["600519"], start, end, "momentum",
                              bar.validate_parameters("momentum"), allow_missing=True)
            self.assertEqual(frame.date.max().date().isoformat(), end)

    def test_full_market_pool_keeps_all_shanghai_shenzhen_codes(self):
        snapshot = pd.DataFrame({"代码": [f"{number:06d}" for number in range(3001)] + ["830001"],
                                 "名称": ["测试"] * 3002})
        snapshot.attrs["source"] = "测试快照"
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "POOLS", Path(directory)), patch("factor_lab.spot_frame", return_value=snapshot):
            symbols, info = bar.resolve_pool("deploy", "a", "all", [])
            self.assertEqual(len(symbols), 3001)
            self.assertNotIn("830001", symbols)
            self.assertEqual(info["source"], "测试快照")

    def test_index_pool_can_train_and_records_pool_coverage(self):
        symbols = [f"{number:06d}" for number in range(10)]
        pool_info = {"id": "csi300", "name": "沪深300", "source": "测试成分名单", "as_of": "2026-03-20"}
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "MODELS", Path(directory) / "models"), patch.object(bar, "HISTORY_CACHE", Path(directory) / "history"), patch.object(bar, "resolve_pool", return_value=(symbols, pool_info)), patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            for strategy in ("elastic_net", "xgboost"):
                with self.subTest(strategy=strategy):
                    record = bar.run("train", "a", "daily", strategy, [], "2024-01-01", "2026-03-25", pool="csi300")
                    self.assertEqual(record["pool"], pool_info)
                    self.assertEqual(record["requested_count"], 10)
                    self.assertEqual(record["valid_count"], 10)
                    self.assertIn("zero_baseline_mse", record["validation"])
                    self.assertIsInstance(record["validation"]["passed"], bool)
                    if strategy == "xgboost":
                        self.assertIsNotNone(record["selected_trees"])
                        self.assertLess(record["training"]["early_stopping_start"], record["training"]["validation_start"])
                    self.assertEqual(bar.options("a", "daily")["models"][f"{strategy}@{record['model_id']}"]["pool"], pool_info)

    def test_validation_gate_rejects_misranked_predictions(self):
        symbols = [f"{number:06d}" for number in range(10)]
        frame = pd.DataFrame([{"date": day, "symbol": symbol, "target": (index - 4.5) / 100}
                              for day in pd.bdate_range("2026-01-05", periods=3)
                              for index, symbol in enumerate(symbols)])
        good = bar.validation_diagnostics(frame, frame.target.to_numpy() * .8)
        bad = bar.validation_diagnostics(frame, -frame.target.to_numpy())
        self.assertTrue(good["passed"])
        self.assertFalse(bad["passed"])
        self.assertLess(bad["daily_rank_ic"], 0)
        with patch.object(bar, "checkpoints", return_value=[{"market": "a", "timeframe": "daily",
                                                               "algorithm": "xgboost", "model_id": "legacy",
                                                               "training": {"label_end_day": "2025-01-01"}}]):
            with self.assertRaisesRegex(ValueError, "不能部署"):
                bar.score_model(frame, "a", "daily", "xgboost", "legacy", "2026-01-01", require_validated=True)
            with self.assertRaisesRegex(ValueError, "不能部署"):
                bar.run("deploy", "a", "daily", "xgboost", ["600519"], "", "", checkpoint="legacy")

    def test_star_market_minimum_buy_and_single_share_increment(self):
        days = pd.bdate_range("2026-01-05", periods=3)
        rows = []
        for symbol, price, score in (("688001", 75, 2), ("688002", 120, 1)):
            for index, day in enumerate(days):
                rows.append({"date": day, "symbol": symbol, "close": price,
                             "eligible": index == 0, "score": score,
                             "entry_day": days[1], "entry_open": price,
                             "exit_day": days[2], "exit_close": price})
        _, trades, stats = bar.simulate(pd.DataFrame(rows), "a", "daily", "2026-01-05", "2026-01-07", 5)
        self.assertEqual(trades.symbol.tolist(), ["688001"])
        self.assertGreaterEqual(trades.shares.iloc[0], 200)
        self.assertNotEqual(trades.shares.iloc[0] % 100, 0)
        self.assertGreater(stats["slippage_costs"], 0)

    def test_stale_history_cannot_be_presented_as_current_deployment(self):
        with patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            with self.assertRaisesRegex(ValueError, "未覆盖所选结束日期"):
                bar.run("deploy", "a", "daily", "momentum", ["600519"], "", "")

    def test_monthly_model_checkpoint_is_bound_to_market_and_period(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as directory, patch.object(bar, "MODELS", Path(directory) / "models"), patch.object(bar, "BACKTESTS", Path(directory) / "backtests"), patch.object(bar, "history", side_effect=lambda *_: daily_history()):
            trained = bar.run("train", "hk", "monthly", "elastic_net", ["00700"], "2020-01-01", "2025-12-31")
            checkpoint = trained["model_id"]
            self.assertIn(f"elastic_net@{checkpoint}", bar.options("hk", "monthly")["models"])
            self.assertNotIn(f"elastic_net@{checkpoint}", bar.options("a", "monthly")["models"])
            result = bar.run("backtest", "hk", "monthly", "elastic_net", ["00700"], "2026-01-01", "2026-03-31", checkpoint=checkpoint)
            self.assertEqual(result["checkpoint_id"], checkpoint)
            self.assertEqual(result["schema_version"], 3)
            self.assertEqual(result["checkpoint_trained_at"], trained["selected_at"])
            self.assertEqual(result["training_label_end"], trained["training"]["label_end_day"])
            self.assertEqual(result["assumptions"]["universe_policy"], "用户输入名单固定")


if __name__ == "__main__":
    unittest.main()
