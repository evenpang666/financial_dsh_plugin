import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("factor_lab", ROOT / "scripts" / "factor_lab.py")
factor_lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factor_lab)


class FactorLabTests(unittest.TestCase):
    def test_backtest_slot_settings_default_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(factor_lab, "SETTINGS", Path(directory) / "factor.json"):
            self.assertEqual(factor_lab.load_settings()["backtest_slot"], "14:00")
            factor_lab.save_settings("10:30")
            self.assertEqual(factor_lab.load_settings()["backtest_slot"], "10:30")
            with self.assertRaises(ValueError):
                factor_lab.save_settings("15:00")

    def tearDown(self):
        factor_lab.EM_MINUTE_UNAVAILABLE.clear()
        factor_lab.SINA_MINUTE_UNAVAILABLE.clear()

    def test_spot_snapshot_falls_back_to_sina_and_normalizes_codes(self):
        rows = pd.DataFrame([{"代码": "sh600519", "名称": "贵州茅台", "最新价": 100,
                              "今开": 99, "昨收": 98, "最高": 101, "最低": 97,
                              "成交额": 100000, "成交量": 1000}])
        fake_ak = types.SimpleNamespace(stock_zh_a_spot_em=lambda: (_ for _ in ()).throw(ConnectionError("Eastmoney offline")),
                                        stock_zh_a_spot=lambda: rows)
        with patch.dict(sys.modules, {"akshare": fake_ak}):
            result = factor_lab.spot_frame()
        self.assertEqual(result.iloc[0]["代码"], "600519")
        self.assertIn("新浪", result.attrs["source"])

    def test_spot_snapshot_reports_both_source_failures(self):
        def fail_primary():
            raise ConnectionError("Eastmoney HTTPSConnectionPool")
        def fail_fallback():
            raise ConnectionError("Sina timeout")
        fake_ak = types.SimpleNamespace(stock_zh_a_spot_em=fail_primary, stock_zh_a_spot=fail_fallback)
        with patch.dict(sys.modules, {"akshare": fake_ak}), self.assertRaisesRegex(RuntimeError, "新浪数据源均不可用") as caught:
            factor_lab.spot_frame()
        self.assertIn("Sina timeout", str(caught.exception))

    def test_minute_history_falls_back_once_and_normalizes_sina_fields(self):
        calls = []
        def eastmoney(**kwargs):
            calls.append(kwargs["symbol"])
            raise ConnectionError("HTTPSConnectionPool(host='push2his.eastmoney.com')")
        sina_rows = pd.DataFrame([{"day": "2026-09-22 09:35:00", "open": 100, "high": 101,
                                   "low": 99, "close": 100, "volume": 1000, "amount": 100000}])
        fake_ak = types.SimpleNamespace(stock_zh_a_hist_min_em=eastmoney)
        start, end = datetime(2026, 9, 20), datetime(2026, 9, 23)
        with patch.dict(sys.modules, {"akshare": fake_ak}), patch.object(factor_lab, "sina_minute_frame", return_value=sina_rows), patch.object(factor_lab.time, "sleep"):
            first = factor_lab.fetch_bars("600519", start, end)
            second = factor_lab.fetch_bars("600519", start, end)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first.iloc[0]["close"], 100)
        self.assertEqual(len(second), 1)

    def test_baostock_backfills_older_cache_without_discarding_recent_bars(self):
        fields = "date,time,code,open,high,low,close,volume,amount,adjustflag".split(",")
        class Result:
            error_code, error_msg = "0", "success"
            def __init__(self):
                self.fields = fields
                self.rows = [["2025-01-02", "20250102093500000", "sh.600519", "100", "101", "99", "100", "1000", "100000", "3"]]
            def next(self):
                return bool(self.rows)
            def get_row_data(self):
                return self.rows.pop(0)
        queries = []
        def query(*args, **kwargs):
            queries.append(kwargs)
            return Result()
        fake_bs = types.SimpleNamespace(login=lambda: types.SimpleNamespace(error_code="0"),
                                        logout=lambda: None, query_history_k_data_plus=query)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pd.DataFrame([{"time": "2026-09-22 09:35:00", "open": 110, "high": 111,
                           "low": 109, "close": 110, "volume": 1000, "amount": 110000}]).to_csv(base / "600519.csv", index=False)
            with patch.object(factor_lab, "BARS", base), patch.dict(sys.modules, {"baostock": fake_bs}):
                result = factor_lab.backfill_baostock([{"symbol": "600519"}],
                                                      datetime(2025, 1, 1, tzinfo=factor_lab.CHINA),
                                                      datetime(2026, 9, 22, tzinfo=factor_lab.CHINA))
            cached = pd.read_csv(base / "600519.csv")
        self.assertEqual(result["filled"], 1)
        self.assertEqual(len(cached), 2)
        self.assertEqual(queries[0]["start_date"], "2025-01-01")

    def test_recent_source_failure_uses_recent_history_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pd.DataFrame([{"time": "2026-09-22 09:35:00", "open": 100, "high": 101,
                           "low": 99, "close": 100, "volume": 1000, "amount": 100000}]).to_csv(base / "600519.csv", index=False)
            with patch.object(factor_lab, "BARS", base), \
                 patch.object(factor_lab, "fetch_bars", side_effect=ConnectionError("offline")):
                result = factor_lab.refresh_bars("600519", datetime(2025, 1, 1), datetime(2026, 9, 23))
        self.assertEqual(len(result), 1)

    def test_sina_jsonp_has_timeout_and_parses_ohlcv(self):
        response = types.SimpleNamespace(text='callback=([{"day":"2026-09-22 09:35:00","open":"100","high":"101","low":"99","close":"100","volume":"1000","amount":"100000"}]);',
                                         raise_for_status=lambda: None)
        with patch.object(factor_lab.requests, "get", return_value=response) as get:
            frame = factor_lab.sina_minute_frame("sh600519")
        self.assertEqual(frame.iloc[0]["day"], "2026-09-22 09:35:00")
        self.assertEqual(get.call_args.kwargs["timeout"], 15)

    def test_network_failure_disables_repeated_sina_minute_requests(self):
        fake_ak = types.SimpleNamespace(stock_zh_a_hist_min_em=lambda **kwargs: (_ for _ in ()).throw(ConnectionError("Eastmoney down")))
        start, end = datetime(2026, 9, 20), datetime(2026, 9, 23)
        with patch.dict(sys.modules, {"akshare": fake_ak}), \
             patch.object(factor_lab, "sina_minute_frame", side_effect=factor_lab.requests.ConnectionError("Sina down")) as sina:
            with self.assertRaisesRegex(RuntimeError, "均不可用"):
                factor_lab.fetch_bars("600519", start, end)
            with self.assertRaisesRegex(RuntimeError, "已停止重复请求"):
                factor_lab.fetch_bars("600520", start, end)
        self.assertEqual(sina.call_count, 1)

    def test_universe_falls_back_to_market_cap_before_open(self):
        rows = [{"代码": f"600{index:03d}", "名称": f"股票{index}", "成交额": 0,
                 "总市值": index + 1} for index in range(45)]
        universe = factor_lab.choose_universe(pd.DataFrame(rows))
        self.assertEqual(len(universe), factor_lab.UNIVERSE_SIZE)
        self.assertEqual(universe[0]["symbol"], "600044")

    def test_history_labels_use_next_session_execution_not_signal_close(self):
        rows = []
        for day, opening, closing in (("2026-09-21", 100, 110), ("2026-09-22", 110, 120),
                                      ("2026-09-23", 120, 130)):
            for index in range(48):
                hour = 9 + (35 + index * 5) // 60
                minute = (35 + index * 5) % 60
                if index >= 24:
                    hour, minute = 13 + ((index - 23) * 5) // 60, ((index - 23) * 5) % 60
                moment = f"{day} {hour:02d}:{minute:02d}:00"
                price = closing if index == 47 else opening + index * 0.05
                rows.append({"time": moment, "open": opening, "high": price + 1, "low": opening - 1,
                             "close": price, "volume": 100, "amount": price * 10000})
        bars = pd.DataFrame(rows)
        bars["time"] = pd.to_datetime(bars["time"])
        samples = factor_lab.history_samples("600001", bars)
        self.assertTrue(samples)
        second = [item for item in samples if item["day"] == "2026-09-22"]
        self.assertTrue(all(item["exit_day"] == "2026-09-23" for item in second))
        self.assertTrue(all(item["target"] > 0 for item in second))
        self.assertTrue(all(item["target"] is None for item in samples if item["day"] == "2026-09-23"))

    def test_historical_candidate_does_not_require_later_complete_day(self):
        rows = []
        for day in ("2026-09-21", "2026-09-22", "2026-09-23"):
            slots = ("09:35", "14:00", "14:05", "14:55") if day != "2026-09-23" else ("09:35", "14:00")
            for slot in slots:
                rows.append({"time": f"{day} {slot}", "open": 10.0, "high": 10.2,
                             "low": 9.8, "close": 10.0, "volume": 1000, "amount": 10000})
        bars = pd.DataFrame(rows)
        bars["time"] = pd.to_datetime(bars.time)
        samples = factor_lab.history_samples("600001", bars)
        candidate = [row for row in samples if row["day"] == "2026-09-23" and row["slot"] == "14:00"]
        self.assertEqual(len(candidate), 1)
        self.assertIsNone(candidate[0]["target"])

    def test_history_label_and_gap_adjust_for_split(self):
        rows = []
        days = ("2026-09-21", "2026-09-22", "2026-09-23")
        for day, price in zip(days, (10.0, 10.0, 5.0)):
            for slot in ("09:35", "14:00", "14:10", "15:00"):
                rows.append({"time": f"{day} {slot}", "open": price, "high": price,
                             "low": price, "close": price, "volume": 1000,
                             "amount": price * 1000})
        bars = pd.DataFrame(rows)
        bars["time"] = pd.to_datetime(bars.time)
        state = pd.DataFrame([{"day": day, "symbol": "600001",
                               "split_ratio": 2 if day == days[-1] else 1,
                               "cash_dividend": 0} for day in days])
        samples = factor_lab.history_samples("600001", bars, state)
        middle = next(item for item in samples if item["day"] == days[1]
                      and item["slot"] == "14:00")
        final = next(item for item in samples if item["day"] == days[2]
                     and item["slot"] == "14:00")
        self.assertAlmostEqual(middle["target"], 0)
        self.assertAlmostEqual(final["gap"], 0)

    def test_pit_samples_skip_bars_before_first_selection(self):
        rows = []
        for day in ("2026-09-21", "2026-09-22", "2026-09-23"):
            for slot in ("09:35", "14:00", "14:05", "14:10", "15:00"):
                rows.append({"time": f"{day} {slot}", "open": 10.0, "high": 10.2,
                             "low": 9.8, "close": 10.0, "volume": 1000, "amount": 10000})
        bars = pd.DataFrame(rows)
        bars["time"] = pd.to_datetime(bars.time)
        state = pd.DataFrame([{"day": "2026-09-23", "symbol": "600001",
                               "split_ratio": 1, "cash_dividend": 0}])
        samples = factor_lab.history_samples("600001", bars, state)
        self.assertTrue(samples)
        self.assertEqual({item["day"] for item in samples}, {"2026-09-23"})

    def test_live_pit_universe_uses_previous_day_public_fallback(self):
        import factor_data_prep as prep

        moment = datetime(2026, 9, 23, 10, 31, tzinfo=factor_lab.CHINA)
        calendar = pd.DataFrame([{"cal_date": "20260923", "is_open": "1",
                                  "pretrade_date": "20260922"}])
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "source-cache"
            daily_path = raw / "daily-public" / "20260922.csv"
            daily_path.parent.mkdir(parents=True)
            pd.DataFrame([{"trade_date": "20260922", "ts_code": f"600{index:03d}",
                           "amount": 1000 - index} for index in range(41)]).to_csv(daily_path, index=False)
            with (patch.object(factor_lab, "SOURCE_MANIFEST", Path(directory) / "missing-manifest.json"),
                  patch.object(prep, "RAW", raw),
                  patch.object(prep, "fetch_calendar_baostock", return_value=calendar),
                  patch.object(prep, "collect_daily", return_value=(["20260922"], "daily-public", "public")) as collect):
                universe = factor_lab.today_pit_universe(moment)
            self.assertEqual(len(universe), 40)
            self.assertNotIn("600040", universe)
            collect.assert_called_once_with("2026-09-22", "2026-09-22", raw)

    def test_legacy_non_ai_model_is_rejected(self):
        moment = datetime(2026, 9, 23, 10, 31, tzinfo=factor_lab.CHINA)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            model = {"model_id": "test-1", "selected_at": moment.isoformat(),
                     "universe_method": "test", "universe": [{"symbol": f"600{index:03d}"} for index in range(15)],
                     "factors": [{"key": "open_momentum", "name": "开盘以来动量", "weight": 1.0,
                                  "direction": 1, "holdout_ic": 0.2}]}
            (base / "model.json").write_text(json.dumps(model))
            rows = []
            for index in range(15):
                price = 100 + index
                rows.append({"代码": f"600{index:03d}", "名称": f"股票{index}", "最新价": price,
                             "今开": 100, "昨收": 99, "最高": price + 1, "最低": 99,
                             "成交量": 1000, "成交额": price * 100000})
            fake_ak = types.SimpleNamespace(tool_trade_date_hist_sina=lambda: pd.DataFrame({"trade_date": [moment.date()]}))
            with patch.object(factor_lab, "MODEL", base / "model.json"):
                with self.assertRaisesRegex(RuntimeError, "旧版非 AI"):
                    factor_lab.rank()

    def test_live_ranking_uses_selected_ml_algorithm(self):
        moment = datetime(2026, 9, 23, 10, 31, tzinfo=factor_lab.CHINA)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            model = {"model_id": "ml-test", "selected_at": moment.isoformat(),
                     "quality": "exploratory", "algorithm": "elastic_net",
                     "target_version": factor_lab.ML_TARGET_VERSION,
                     "features": ["open_momentum", "gap", "range_position", "range_width", "vwap_gap", "minute_of_day"],
                     "training": {"last_day": "2026-09-22"},
                     "universe_method": "test", "universe": [{"symbol": f"600{index:03d}"} for index in range(15)]}
            (base / "model.json").write_text(json.dumps(model), encoding="utf-8")
            rows = [{"代码": f"600{index:03d}", "名称": f"股票{index}", "最新价": 100 + index,
                     "今开": 100, "昨收": 99, "最高": 102 + index, "最低": 99,
                     "成交量": 1000, "成交额": (100 + index) * 100000} for index in range(15)]
            fake_ak = types.SimpleNamespace(tool_trade_date_hist_sina=lambda: pd.DataFrame({"trade_date": [moment.date()]}))
            import factor_models
            with patch.object(factor_lab, "STORE", base), patch.object(factor_lab, "MODEL", base / "model.json"), \
                 patch.object(factor_lab, "LATEST", base / "latest.json"), \
                 patch.object(factor_lab, "now_cn", return_value=moment), \
                 patch.object(factor_lab, "load_settings", return_value={"backtest_slot": "10:30", "weekly_training_enabled": True}), \
                 patch.object(factor_lab, "spot_frame", return_value=pd.DataFrame(rows)), \
                 patch.object(factor_lab, "urlopen", side_effect=OSError("web offline")), \
                 patch.object(factor_models, "predict", return_value=np.arange(15)) as predict, \
                 patch.dict(sys.modules, {"akshare": fake_ak}):
                result = factor_lab.rank()
            self.assertEqual(result["algorithm"], "elastic_net")
            self.assertEqual(result["ranking"][0]["symbol"], "600014")
            self.assertEqual(predict.call_args.args[1]["model_id"], "ml-test")
            self.assertTrue(predict.call_args.args[0].minute_of_day.eq(631).all())
            self.assertFalse(result["backtest_comparable"])

    def test_old_same_day_ml_model_is_rejected_before_live_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps({"algorithm": "xgboost", "model_id": "old"}), encoding="utf-8")
            with patch.object(factor_lab, "MODEL", path):
                with self.assertRaisesRegex(RuntimeError, "旧版非 AI"):
                    factor_lab.rank()

    def test_one_click_train_publishes_chosen_ml_model(self):
        import factor_models
        moment = datetime(2026, 9, 23, 18, 0, tzinfo=factor_lab.CHINA)
        snapshot = pd.DataFrame([{"代码": f"600{index:03d}", "名称": f"股票{index}",
                                  "成交额": 100000 - index} for index in range(40)])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            ml_result = {"algorithm": "xgboost", "algorithms": {"xgboost": {}, "elastic_net": {}},
                         "features": list(factor_models.FEATURES), "days": 100, "samples": 10000,
                         "last_day": "2026-09-22", "holdout_days": 20}
            with patch.object(factor_lab, "STORE", base), patch.object(factor_lab, "BARS", base / "bars"), \
                 patch.object(factor_lab, "SOURCE_MANIFEST", base / "source-manifest.json"), \
                 patch.object(factor_lab, "MODELS", base / "models"), \
                 patch.object(factor_lab, "BACKTESTS", base / "backtests"), \
                 patch.object(factor_lab, "MODEL", base / "model.json"), \
                 patch.object(factor_lab, "LATEST", base / "latest-ranking.json"), \
                 patch.object(factor_lab, "now_cn", return_value=moment), \
                 patch.object(factor_lab, "spot_frame", return_value=snapshot), \
                 patch.object(factor_lab, "backfill_baostock", return_value={"filled": 40, "failures": []}), \
                 patch.object(factor_lab, "refresh_bars", return_value=pd.DataFrame({"time": pd.to_datetime(["2026-09-22 09:35"])})), \
                 patch.object(factor_lab, "history_samples", return_value=[{"day": "2026-09-22"}]), \
                  patch.object(factor_models, "fit_models", return_value=ml_result), \
                  patch("factor_backtest.run", side_effect=AssertionError("training must not run a backtest")):
                published = factor_lab.train()
            self.assertEqual(published["algorithm"], "xgboost")
            self.assertIsNone(published["backtest"])
            self.assertEqual(published["target_version"], factor_lab.ML_TARGET_VERSION)
            self.assertEqual(json.loads((base / "model.json").read_text(encoding="utf-8"))["algorithm"], "xgboost")


if __name__ == "__main__":
    unittest.main()
