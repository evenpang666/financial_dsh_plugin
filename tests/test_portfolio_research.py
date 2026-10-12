import importlib.util
import math
import random
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import portfolio_research as research
import factor_lab
from data_diagnostics import readable_issue

NOW = datetime(2026, 9, 14, 14, 30, tzinfo=research.CHINA)


def holding(symbol, action="建议买入", shares=100):
    return {"holding": {"symbol": symbol, "shares": shares}, "price": 10,
            "quote": {"昨收": 10}, "analysis": {"as_of": "2026-09-14", "name": symbol,
            "recommendation": {"conclusion": action, "buy_percentage": 83, "sell_percentage": 17,
                               "basis": ["测试证据"], "score_components": {"valid_signal_count": 6,
                               "short_technical_score": 1, "long_fundamental_valuation_score": 2}}}}


class PositionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.panorama = {"market_state": {"risk_level": "高"}, "market_breadth": {"score": 30, "label": "偏弱"}}

    def test_allocations_respect_caps_and_keep_cash_for_every_portfolio_size(self):
        rng = random.Random(42)
        for size in range(1, 101):
            weights, cash = research.allocation([rng.random() for _ in range(size)], 40)
            self.assertLessEqual(sum(weights), 40 + 1e-10)
            self.assertLessEqual(max(weights), 25)
            self.assertAlmostEqual(sum(weights) + cash, 100, places=8)
        self.assertEqual(research.allocation([0, 0], 40), ([0, 0], 100))
        self.assertEqual(research.allocation([1, None], 40), ([None, None], None))

    def test_missing_cash_or_quantities_do_not_fabricate_current_exposure(self):
        items = [holding("600519"), holding("000858", shares=None)]
        summary = research.build_position_summary(items, self.panorama, {"available": False}, NOW)
        self.assertTrue(all(row["current_holding_share_pct"] is None for row in summary["rows"]))
        self.assertTrue(all(row["suggestion2"]["weight_pct"] is None for row in summary["rows"]))
        self.assertTrue(all(row["suggestion3"]["weight_pct"] is None for row in summary["rows"]))
        self.assertNotEqual(summary["rows"][0]["suggestion1"]["weight_pct"], 83)

    def test_conflicting_signals_reduce_new_allocation_without_forcing_existing_exit(self):
        items = [holding("600519"), holding("000858", "建议卖出")]
        signals = {"available": True, "as_of": "2026-09-14T14:30:00", "rows": {
            "600519": {"allocation_score": 0, "status": "not_triggered"},
            "000858": {"allocation_score": 1, "rank": 1, "status": "triggered"}}}
        summary = research.build_position_summary(items, self.panorama, signals, NOW)
        first, second = summary["rows"]
        self.assertEqual(first["suggestion1"]["weight_pct"], 25)
        self.assertEqual(first["suggestion3"]["weight_pct"], 12.5)
        self.assertIn("不据此强制卖出", first["suggestion3"]["reason"])
        self.assertEqual(second["suggestion3"]["weight_pct"], 0)
        self.assertIn("反向", second["suggestion3"]["reason"])
        for key in ("suggestion1", "suggestion2", "suggestion3"):
            self.assertAlmostEqual(summary["stock_totals_pct"][key] + summary["cash_reserve_pct"][key], 100)

    def test_critical_market_failure_and_misaligned_dates_abstain(self):
        item = holding("600519")
        signals = {"available": True, "as_of": "2026-09-11T15:00:00", "rows": {"600519": {"allocation_score": 1}}}
        summary = research.build_position_summary([item], self.panorama, signals, NOW)
        self.assertIsNone(summary["rows"][0]["suggestion3"]["weight_pct"])
        self.panorama["unavailable"] = ["大盘宽度不可用"]
        summary = research.build_position_summary([item], self.panorama, signals, NOW)
        self.assertIsNone(summary["rows"][0]["suggestion1"]["weight_pct"])

    def test_attention_only_failure_does_not_erase_other_evidence(self):
        self.panorama["unavailable"] = ["东方财富人气榜不可用（网络代理连接失败）"]
        self.assertEqual(research.exposure_limit(self.panorama), 40)
        item = holding("600519")
        item["analysis"]["recommendation"]["score_components"]["valid_signal_count"] = 2
        self.assertIsNone(research.stock_basis(item, NOW)[0])


class StrategyReviewTests(unittest.TestCase):
    def calendar(self):
        return patch.dict(sys.modules, {"akshare": SimpleNamespace(tool_trade_date_hist_sina=lambda: pd.DataFrame({"trade_date": ["2026-09-11", "2026-09-14"]}))})

    def test_selected_fixed_strategy_uses_held_pool_and_keeps_non_entry_distinct(self):
        items = [holding("600519"), holding("000858")]
        def record(item, strategy, asof, previous, deadline=None):
            return {"symbol": item["holding"]["symbol"], "price": 10, "as_of": asof.isoformat(),
                    "open_momentum": .02 if item["holding"]["symbol"] == "600519" else -.01}
        with self.calendar(), patch.object(research, "load_signal_record", side_effect=record):
            result = research.strategy_review(items, "momentum", "a", NOW)
        self.assertTrue(result["available"])
        self.assertEqual(result["rows"]["600519"]["rank"], 1)
        self.assertEqual(result["rows"]["000858"]["status"], "not_triggered")
        self.assertIn("不等同于", result["rows"]["000858"]["note"])

    def test_one_missing_input_abstains_for_whole_numeric_strategy_allocation(self):
        with self.calendar(), patch.object(research, "load_signal_record", side_effect=[
                {"symbol": "600519", "price": 10, "as_of": "2026-09-14T14:30:00", "open_momentum": .02}, ValueError("分钟线缺失")]):
            result = research.strategy_review([holding("600519"), holding("000858")], "momentum", "a", NOW)
        self.assertFalse(result["available"])
        self.assertEqual(result["rows"]["000858"]["status"], "unavailable")

    def test_no_model_no_hk_and_preopen_do_not_produce_strategy_weights(self):
        with self.calendar(), patch.object(factor_lab, "selected_model", return_value={}), patch.object(research, "load_signal_record") as load:
            self.assertFalse(research.strategy_review([holding("600519")], "xgboost", "a", NOW)["available"])
            self.assertFalse(research.strategy_review([holding("600519")], "momentum", "a", NOW.replace(hour=9, minute=20))["available"])
            self.assertFalse(research.strategy_review([holding("00700")], "momentum", "hk", NOW)["available"])
            load.assert_not_called()

    def test_model_predictions_use_selected_model_without_training(self):
        model = {"model_id": "test", "algorithm": "elastic_net", "target_version": factor_lab.ML_TARGET_VERSION,
                 "selected_at": "2026-09-12T12:00:00+08:00", "quality": "validated"}
        with self.calendar(), patch.object(factor_lab, "selected_model", return_value=model) as chosen, \
                patch.object(research, "load_signal_record", return_value={"symbol": "600519", "price": 10, "as_of": "2026-09-14T14:30:00"}), \
                patch("factor_models.predict", return_value=[.2]) as predict:
            result = research.strategy_review([holding("600519")], "elastic_net", "a", NOW)
        self.assertTrue(result["available"])
        chosen.assert_called_once_with("elastic_net")
        self.assertEqual(predict.call_args.args[1]["model_id"], "test")

    def test_future_bars_cannot_change_signal_and_stale_cache_is_not_reused(self):
        from factor_data_prep import _minute_times
        rows = []
        for day in ("2026-09-11", "2026-09-14"):
            for index, stamp in enumerate(_minute_times(day)):
                price = 10 + (index * .01 if day == "2026-09-14" else 0)
                rows.append({"time": stamp, "open": price, "high": price + .01, "low": price - .01,
                             "close": price, "volume": 100, "amount": price * 100})
        frame = pd.DataFrame(rows)
        cutoff = pd.Timestamp("2026-09-14 14:30")
        with tempfile.TemporaryDirectory() as directory, patch.object(research, "ROOT", Path(directory)):
            cache = Path(directory) / "data/factor-lab/bars"; cache.mkdir(parents=True)
            path = cache / "600519.csv"
            frame.to_csv(path, index=False)
            baseline = research.load_signal_record(holding("600519"), "momentum", cutoff, "2026-09-11")
            changed = frame.copy(); changed.loc[changed.time.gt(cutoff), ["open", "high", "low", "close"]] = 999
            changed.to_csv(path, index=False)
            self.assertEqual(research.load_signal_record(holding("600519"), "momentum", cutoff, "2026-09-11"), baseline)
            for live in (Path(directory) / "data/factor-lab/live-bars").glob("*.csv"): live.unlink()
            frame[frame.time.lt(cutoff)].to_csv(path, index=False)
            with patch.object(factor_lab, "sina_minute_frame", return_value=frame[frame.time.lt(cutoff)]):
                with self.assertRaises(ValueError):
                    research.load_signal_record(holding("600519"), "momentum", cutoff, "2026-09-11")


class DiagnosticsTests(unittest.TestCase):
    def test_reference_close_has_provider_date_and_bounded_request(self):
        fields = ["测试", "10", "9.5", "10"] + [""] * 26 + ["2026-09-14", "15:00:00"]
        response = SimpleNamespace(text='var hq_str_sh600519="' + ",".join(fields) + '";', raise_for_status=lambda: None)
        research.reference_close_response.cache_clear()
        with patch.object(research.requests, "get", return_value=response) as request:
            self.assertEqual(research.reference_close_response("600519", "2026-09-14"), 9.5)
            self.assertEqual(research.reference_close_response("600519", "2026-09-14"), 9.5)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], (5, 10))
        with patch.object(research.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                research.reference_close_response("600519", "2026-09-15")
        research.reference_close_response.cache_clear()

    def test_proxy_exception_is_short_and_preserves_nested_source(self):
        value = readable_issue("市场全景：东方财富人气榜不可用：HTTPSConnectionPool(url='https://example.invalid/?many=codes') ProxyError Unable to connect to proxy")
        self.assertIn("市场全景：东方财富人气榜不可用", value)
        self.assertIn("网络代理连接失败", value)
        self.assertNotIn("https", value)
        self.assertLess(len(value), 70)


if __name__ == "__main__":
    unittest.main()
