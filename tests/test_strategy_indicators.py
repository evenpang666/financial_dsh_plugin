import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_lab
import factor_backtest
from strategy_indicators import INDICATOR_NAMES, indicator_frame
from strategy_catalog import TECHNICAL_STRATEGIES, RULE_FEATURES, rule_scores


def session(day, prices):
    from factor_data_prep import _minute_times
    return pd.DataFrame({"time": _minute_times(day), "open": prices, "close": prices,
                         "high": prices, "low": prices, "volume": 1000,
                         "amount": np.asarray(prices) * 1000})


class IndicatorTests(unittest.TestCase):
    def test_constant_prices_are_finite_and_have_no_cross_signals(self):
        bars = pd.concat([session("2026-09-21", [37.72] * 48), session("2026-09-22", [37.72] * 48)], ignore_index=True)
        values = indicator_frame(bars).iloc[48:]
        self.assertTrue(np.isfinite(values.to_numpy()).all())
        self.assertTrue(values.rsi.eq(50).all())
        for strategy in TECHNICAL_STRATEGIES:
            self.assertFalse(rule_scores(values, strategy)[1].any(), strategy)

    def test_moving_average_bollinger_and_rsi_known_prices(self):
        prices = np.arange(1, 49, dtype=float)
        values = indicator_frame(session("2026-09-21", prices))
        last = values.iloc[-1]
        self.assertAlmostEqual(last.ma_spread, prices[-5:].mean() / prices[-20:].mean() - 1)
        self.assertAlmostEqual(last.bollinger_z, (prices[-1] - prices[-20:].mean()) / prices[-20:].std(ddof=0))
        self.assertEqual(last.rsi, 100)
        falling = indicator_frame(session("2026-09-21", prices[::-1]))
        self.assertEqual(falling.iloc[-1].rsi, 0)
        self.assertTrue(values.iloc[:33].macd_hist.isna().all())
        self.assertTrue(np.isfinite(values.iloc[34].macd_hist_prev))

    def test_future_prices_and_events_cannot_change_signal_features(self):
        bars = pd.concat([session("2026-09-21", np.linspace(10, 11, 48)),
                          session("2026-09-22", np.linspace(11, 10, 48)),
                          session("2026-09-23", [10] * 48)], ignore_index=True)
        cutoff = pd.Timestamp("2026-09-22 14:00")
        before = indicator_frame(bars)
        altered = bars.copy()
        future = altered.time.gt(cutoff)
        altered.loc[future, ["open", "high", "low", "close"]] = 999
        events = pd.DataFrame([{"day": "2026-09-23", "split_ratio": 2, "cash_dividend": 1}])
        after = indicator_frame(altered, events)
        pd.testing.assert_frame_equal(before[bars.time.le(cutoff)], after[bars.time.le(cutoff)])
        truncated = indicator_frame(bars[bars.time.le(cutoff)])
        pd.testing.assert_frame_equal(before[bars.time.le(cutoff)], truncated)
        sample = next(row for row in factor_lab.history_samples("600001", bars)
                      if row["day"] == "2026-09-22" and row["slot"] == "14:00")
        changed = next(row for row in factor_lab.history_samples("600001", altered)
                       if row["day"] == "2026-09-22" and row["slot"] == "14:00")
        for feature in INDICATOR_NAMES:
            self.assertEqual(sample[feature], changed[feature])

    def test_split_and_dividend_do_not_create_false_price_reversals(self):
        bars = pd.concat([session("2026-09-21", [100] * 48), session("2026-09-22", [49] * 48)], ignore_index=True)
        events = pd.DataFrame([{"day": "2026-09-22", "split_ratio": 2, "cash_dividend": 2}])
        values = indicator_frame(bars, events).iloc[48:]
        self.assertTrue(values.rsi.eq(50).all())
        self.assertTrue(np.isfinite(values.to_numpy()).all())
        for strategy in TECHNICAL_STRATEGIES:
            self.assertFalse(rule_scores(values, strategy)[1].any(), strategy)

    def test_donchian_channel_excludes_current_bar_high(self):
        bars = session("2026-09-21", [10] * 48)
        bars.loc[47, ["high", "close", "amount"]] = [100, 12, 12000]
        values = indicator_frame(bars)
        self.assertAlmostEqual(values.iloc[-1].donchian_gap, 0.2)
        self.assertTrue(rule_scores(values.iloc[[-1]], "donchian_breakout")[1].iloc[0])

    def test_rule_cross_requires_a_transition_and_breakout_uses_centered_range(self):
        rows = pd.DataFrame({"ma_spread": [0.01, 0.01], "ma_spread_prev": [-0.01, 0.01],
                             "macd_hist": [0.01, 0.01], "macd_hist_prev": [-0.01, 0.01],
                             "rsi": [35, 35], "rsi_prev": [25, 35],
                             "bollinger_z": [-1.5, -1.5], "bollinger_z_prev": [-2.5, -1.5],
                             "donchian_gap": [0.01, 0.01], "donchian_gap_prev": [-0.01, 0.01]})
        for strategy in TECHNICAL_STRATEGIES:
            self.assertEqual(rule_scores(rows, strategy)[1].tolist(), [True, False])
        boundary = pd.DataFrame({"range_position": [0.45, 0.44], "range_width": [0.02, 0.02], "open_momentum": [0.01, 0.01]})
        self.assertEqual(rule_scores(boundary, "breakout")[1].tolist(), [True, False])

    def test_missing_technical_features_are_rejected_before_backtesting(self):
        from tests.test_strategies import replay_fixture
        samples, market, universe, state = replay_fixture()
        samples[-1]["macd_hist"] = np.nan
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, "不能删除缺样本股票"):
            factor_backtest.run(samples, market, Path(directory), universe_history=universe, market_state=state, strategy="macd")

    def test_live_indicators_exclude_future_bars_and_preserve_accepted_cache(self):
        bars = pd.concat([session("2026-09-21", [100] * 48), session("2026-09-22", [49] * 48)], ignore_index=True)
        moment = datetime(2026, 9, 22, 10, 31, tzinfo=factor_lab.CHINA)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root / "bars/600001.csv"
            cache.parent.mkdir(); bars.to_csv(cache, index=False)
            before = cache.read_bytes()
            with patch.object(factor_lab, "STORE", root), patch.object(factor_lab, "BARS", root / "bars"), patch.object(factor_lab, "fetch_bars", side_effect=AssertionError("cache covers cutoff")):
                values = factor_lab.live_technical_factors("600001", moment, "2026-09-21", 49)
            self.assertEqual(values["indicator_at"], "2026-09-22 10:30:00")
            self.assertEqual(values["rsi"], 50)
            self.assertEqual(cache.read_bytes(), before)
            live = pd.read_csv(root / "live-bars/600001.csv")
            self.assertEqual(live.time.max(), values["indicator_at"])

    def test_live_ranking_uses_selected_technical_strategy_without_a_model(self):
        moment = datetime(2026, 9, 23, 10, 31, tzinfo=factor_lab.CHINA)
        rows = [{"代码": f"600{index:03d}", "名称": f"股票{index}", "最新价": 100,
                 "今开": 100, "昨收": 99, "最高": 101, "最低": 99,
                 "成交量": 1000, "成交额": 100000} for index in range(15)]
        import types
        fake = types.SimpleNamespace(tool_trade_date_hist_sina=lambda: pd.DataFrame({"trade_date": ["2026-09-22", "2026-09-23"]}))
        indicators = {"ma_spread": 0.01, "ma_spread_prev": -0.01, "macd_hist": 0.01, "macd_hist_prev": -0.01,
                      "rsi": 35, "rsi_prev": 25, "bollinger_z": -1.5, "bollinger_z_prev": -2.5,
                      "donchian_gap": 0.01, "donchian_gap_prev": -0.01, "indicator_at": "2026-09-23 10:30:00"}
        for strategy in TECHNICAL_STRATEGIES:
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with (patch.object(factor_lab, "STORE", root), patch.object(factor_lab, "LATEST", root / "latest.json"),
                      patch.object(factor_lab, "load_settings", return_value={"strategy": strategy, "backtest_slot": "14:00"}),
                      patch.object(factor_lab, "now_cn", return_value=moment), patch.object(factor_lab, "spot_frame", return_value=pd.DataFrame(rows)),
                      patch.object(factor_lab, "today_pit_universe", return_value={row["代码"] for row in rows}),
                      patch.object(factor_lab, "live_technical_factors", return_value=indicators) as compute,
                      patch.object(factor_lab, "urlopen", side_effect=OSError("offline")), patch.dict(sys.modules, {"akshare": fake})):
                    result = factor_lab.rank()
                    if strategy == TECHNICAL_STRATEGIES[0]:
                        published = (root / "latest.json").read_bytes()
                        with patch.object(factor_lab, "is_trading_time", side_effect=[True, True, True, False]):
                            with self.assertRaisesRegex(RuntimeError, "行情准备跨过交易时段"):
                                factor_lab.rank()
                        self.assertEqual((root / "latest.json").read_bytes(), published)
                self.assertEqual(result["strategy_id"], strategy)
                self.assertEqual(len(result["ranking"]), 15)
                self.assertEqual(compute.call_count, 30 if strategy == TECHNICAL_STRATEGIES[0] else 15)
                self.assertIn(indicators["indicator_at"], result["note"])
                self.assertEqual(set(result["ranking"][0]["factors"]), set(RULE_FEATURES[strategy]))


if __name__ == "__main__":
    unittest.main()
