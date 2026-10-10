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
                            *(([("14:05", opening)] if include_execution else [])),
                            ("14:55", opening)):
            rows.append({"time": f"{day} {slot}", "open": price, "close": price,
                         "high": price, "low": price, "volume": 10000, "amount": 1_000_000})
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

    def test_selected_slot_uses_its_next_bar_for_execution(self):
        day = "2026-01-05"
        market = {"600001": pd.DataFrame([{"time": pd.Timestamp(f"{day} 10:35"), "open": 10.0,
                                            "close": 10.0, "amount": 1_000_000}])}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(factor_backtest, "walk_forward_signals", return_value=({day: ["600001"]}, [], {})):
            result = factor_backtest.run([], market, Path(directory), slot="10:30")
        self.assertEqual(result["execution"], "10:35")
        self.assertEqual(result["stats"]["trades"], 1)

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
             patch.object(factor_backtest, "score_estimator",
                          side_effect=lambda models, name, frame: frame.open_momentum.to_numpy(dtype=float)):
            signals, folds, _ = factor_backtest.walk_forward_signals(samples)
        self.assertEqual(len(folds), 1)
        self.assertTrue(all(frame.exit_day.max() < folds[0]["test_first"] for frame in fitted))
        self.assertIn(days[-1], signals)
        self.assertEqual(len(signals[days[-1]]), 12)


if __name__ == "__main__":
    unittest.main()
