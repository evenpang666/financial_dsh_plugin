import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import factor_backtest
import factor_models


def bars(days, opens, include_execution=True):
    rows = []
    for day, opening in zip(days, opens):
        for slot, price in (("09:35", opening), ("14:00", opening),
                            *(([("14:10", opening)] if include_execution else [])),
                            ("15:00", opening)):
            rows.append({"time": f"{day} {slot}", "open": price, "close": price,
                         "high": price, "low": price, "volume": 10000, "amount": price * 10000})
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame.time)
    return frame


class BacktestIntegrityTests(unittest.TestCase):
    def test_late_exit_is_excluded_from_one_session_training_label(self):
        samples = []
        for day, exit_day in (("2026-01-01", "2026-01-02"),
                              ("2026-01-02", "2026-01-06"),
                              ("2026-01-05", "2026-01-06"),
                              ("2026-01-06", None)):
            for stock in range(12):
                samples.append({"symbol": f"600{stock:03d}", "day": day, "slot": "14:00",
                                "exit_day": exit_day, "target": stock / 100,
                                **{key: stock / 100 for key in factor_models.FEATURES[:-1]}})
        frame = factor_models.sample_frame(samples)
        self.assertEqual(set(frame.day), {"2026-01-01", "2026-01-05"})

    def test_trade_waits_for_next_bar_and_next_day_to_sell(self):
        days = ["2026-01-05", "2026-01-06"]
        market = {"600001": bars(days, [10, 11])}
        equity, trades, stats = factor_backtest.simulate({days[0]: ["600001"], days[1]: []}, market)
        self.assertEqual(trades.side.tolist(), ["buy", "sell"])
        self.assertEqual(trades.day.tolist(), days)
        self.assertGreater(stats["costs"], 0)
        self.assertEqual(len(equity), 2)

    def test_missing_execution_bar_cannot_fill_signal(self):
        day = "2026-01-05"
        market = {"600001": bars([day], [10], include_execution=False)}
        _, trades, stats = factor_backtest.simulate({day: ["600001"]}, market)
        self.assertTrue(trades.empty)
        self.assertEqual(stats["blocked_orders"], 1)

    def test_selected_slot_uses_delayed_execution_window(self):
        day = "2026-01-05"
        market = {"600001": pd.DataFrame([
            {"time": pd.Timestamp(f"{day} 10:30"), "close": 10.0, "open": 10.0,
             "volume": 10000, "amount": 100000},
            {"time": pd.Timestamp(f"{day} 10:35"), "close": 20.0, "open": 20.0,
             "volume": 10000, "amount": 200000},
            {"time": pd.Timestamp(f"{day} 10:40"), "close": 10.0, "open": 10.0,
             "volume": 10000, "amount": 100000}])}
        _, trades, stats = factor_backtest.simulate({day: ["600001"]}, market, signal_time="10:30")
        self.assertEqual(stats["trades"], 1)
        self.assertAlmostEqual(trades.iloc[0].price, 10.005)

    def test_walk_forward_fits_only_matured_labels_and_scores_unlabeled_day(self):
        days = pd.bdate_range("2025-01-01", periods=140).strftime("%Y-%m-%d").tolist()
        samples = []
        for index, day in enumerate(days):
            for stock in range(12):
                samples.append({"symbol": f"600{stock:03d}", "day": day, "slot": "14:00",
                                "exit_day": days[index + 1] if index + 1 < len(days) else None,
                                "target": stock / 100 if index + 1 < len(days) else None,
                                **{key: stock / 100 for key in factor_models.FEATURES[:-1]}})
        fitted = []
        def capture(frame):
            fitted.append(frame.copy())
            return {"xgboost": object(), "elastic_net": object()}
        with patch.object(factor_backtest, "fit_estimators", side_effect=capture), \
             patch.object(factor_models, "fit_estimators", side_effect=capture), \
             patch.object(factor_models, "score_estimator",
                          side_effect=lambda models, name, frame: frame.open_momentum.to_numpy(dtype=float)), \
             patch.object(factor_backtest, "score_estimator",
                          side_effect=lambda models, name, frame: frame.open_momentum.to_numpy(dtype=float)):
            universe = pd.DataFrame([{"day": day, "symbol": f"600{stock:03d}",
                                      "known_at": str(pd.Timestamp(day) - pd.Timedelta(days=1))}
                                     for day in days for stock in range(12)])
            signals, folds, _ = factor_backtest.walk_forward_signals(samples, universe)
        self.assertEqual(len(folds), 1)
        self.assertTrue(all(frame.exit_day.max() < folds[0]["test_first"] for frame in fitted))
        self.assertIn(days[-1], signals)
        self.assertEqual(len(signals[days[-1]]), 12)

    def test_backtest_refuses_missing_point_in_time_inputs(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, "历史股票池"):
            factor_backtest.run([], {}, Path(directory))

    def test_star_market_requires_200_share_buy(self):
        day = "2026-01-05"
        market = {"688001": bars([day], [1500])}
        _, trades, stats = factor_backtest.simulate({day: ["688001"]}, market)
        self.assertTrue(trades.empty)
        self.assertEqual(stats["blocked_orders"], 1)

    def test_future_universe_snapshot_is_rejected(self):
        universe = pd.DataFrame([{"day": "2026-01-05", "symbol": "600001",
                                  "known_at": "2026-01-05 10:00"}])
        state = pd.DataFrame([{"day": "2026-01-05", "symbol": "600001",
                               "limit_up": 11, "limit_down": 9, "has_price_limit": True,
                               "split_ratio": 1,
                               "cash_dividend": 0, "is_st": False,
                               "is_suspended": False}])
        with self.assertRaisesRegex(RuntimeError, "交易日前已知"):
            factor_backtest.validate_context(universe, state)

    def test_limit_up_blocks_buy_and_dividend_preserves_nav(self):
        days = ["2026-01-05", "2026-01-06"]
        market = {"600001": bars(days, [10, 9])}
        state = pd.DataFrame([{"day": days[0], "symbol": "600001", "limit_up": 10.0,
                               "limit_down": 9, "has_price_limit": True,
                               "split_ratio": 1, "cash_dividend": 0,
                               "is_st": False},
                              {"day": days[1], "symbol": "600001", "limit_up": 11,
                               "limit_down": 8, "has_price_limit": True,
                               "split_ratio": 1, "cash_dividend": 1,
                               "dividend_pay_date": "2026-01-07",
                               "is_st": False}])
        _, trades, stats = factor_backtest.simulate({days[0]: ["600001"]}, market,
                                                      market_state=state)
        self.assertTrue(trades.empty)
        self.assertEqual(stats["blocked_orders"], 1)
        state.loc[0, "limit_up"] = 11
        equity, trades, _ = factor_backtest.simulate({day: ["600001"] for day in days}, market,
                                                      market_state=state)
        self.assertEqual(trades.side.tolist(), ["buy"])
        self.assertAlmostEqual(equity.equity.iloc[0], equity.equity.iloc[1], places=2)
        self.assertGreater(equity.dividend_receivable.iloc[1], 0)

    def test_dividend_becomes_spendable_only_after_payment_day(self):
        days = ["2026-01-05", "2026-01-06", "2026-01-07"]
        market = {"600001": bars(days, [10, 9, 9])}
        state = pd.DataFrame([{"day": day, "symbol": "600001", "limit_up": 12,
                               "limit_down": 8, "has_price_limit": True,
                               "split_ratio": 1, "cash_dividend": 1 if index == 1 else 0,
                               "dividend_pay_date": days[2] if index == 1 else "",
                               "is_st": False, "is_suspended": False}
                              for index, day in enumerate(days)])
        equity, trades, _ = factor_backtest.simulate(
            {day: ["600001"] for day in days}, market, market_state=state)
        self.assertEqual(trades.side.tolist(), ["buy"])
        self.assertGreater(equity.dividend_receivable.iloc[1], 0)
        self.assertEqual(equity.dividend_receivable.iloc[2], 0)
        self.assertGreater(equity.cash.iloc[2], equity.cash.iloc[1])

    def test_dividend_without_payment_date_is_rejected(self):
        universe = pd.DataFrame([{"day": "2026-01-05", "symbol": "600001",
                                  "known_at": "2026-01-04 15:00"}])
        state = pd.DataFrame([{"day": "2026-01-05", "symbol": "600001",
                               "limit_up": 11, "limit_down": 9, "has_price_limit": True,
                               "split_ratio": 1, "cash_dividend": 1,
                               "is_st": False, "is_suspended": False}])
        with self.assertRaisesRegex(RuntimeError, "到账日"):
            factor_backtest.validate_context(universe, state)

    def test_new_summary_requires_context_and_separates_slippage(self):
        day = "2026-01-05"
        symbols = [f"600{index:03d}" for index in range(12)]
        samples = [{"day": day, "symbol": symbol} for symbol in symbols]
        universe = pd.DataFrame([{"day": day, "symbol": symbol,
                                  "known_at": "2026-01-04 15:00"} for symbol in symbols])
        state = pd.DataFrame([{"day": day, "symbol": symbol, "limit_up": 11,
                               "limit_down": 9, "has_price_limit": True,
                               "split_ratio": 1, "cash_dividend": 0,
                               "is_st": False, "is_suspended": False} for symbol in symbols])
        market = {symbol: bars([day], [10]) for symbol in symbols}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(factor_backtest, "walk_forward_signals",
                          return_value=({day: [symbols[0]]}, [], {})):
            result = factor_backtest.run(samples, market, Path(directory),
                                         universe_history=universe, market_state=state)
        self.assertEqual(result["schema_version"], 2)
        self.assertIn("14:05–14:10", result["execution"])
        self.assertGreater(result["stats"]["slippage_costs"], 0)


if __name__ == "__main__":
    unittest.main()
