import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_backtest
import factor_lab
import factor_models
import report_web_server
from strategy_catalog import MODEL_STRATEGIES, FIXED_STRATEGIES, TECHNICAL_STRATEGIES, STRATEGIES, rule_scores


def replay_fixture():
    days = pd.bdate_range("2025-01-01", periods=140).strftime("%Y-%m-%d").tolist()
    samples, universe, states, market = [], [], [], {}
    for stock in range(12):
        symbol = f"600{stock:03d}"
        bars = []
        for index, day in enumerate(days):
            price = 10 + index / 100 + stock / 20
            for clock in ("09:35", "14:00", "14:10", "15:00"):
                bars.append({"time": pd.Timestamp(f"{day} {clock}"), "open": price, "close": price,
                             "high": price, "low": price, "volume": 10000, "amount": price * 10000})
            universe.append({"day": day, "symbol": symbol, "known_at": f"{pd.Timestamp(day) - pd.Timedelta(days=1)}+08:00"})
            states.append({"day": day, "symbol": symbol, "is_st": False, "is_suspended": False,
                           "has_price_limit": True, "limit_up": 30, "limit_down": 1,
                           "split_ratio": 1, "cash_dividend": 0, "dividend_pay_date": ""})
            samples.append({"day": day, "slot": "14:00", "symbol": symbol,
                            "exit_day": days[index + 1] if index < len(days) - 1 else None,
                            "target": stock / 1000 if index < len(days) - 1 else None,
                            "open_momentum": (stock - 5) / 100, "gap": stock / 1000,
                            "range_position": stock / 11 - 0.5, "range_width": 0.02,
                            "vwap_gap": (stock - 5) / 100,
                            "ma_spread": 0.01 if stock >= 6 else -0.01, "ma_spread_prev": -0.01,
                            "macd_hist": 0.01 if stock >= 6 else -0.01, "macd_hist_prev": -0.01,
                            "rsi": 35 if stock >= 6 else 20, "rsi_prev": 25,
                            "bollinger_z": -1.9 if stock >= 6 else -2.5, "bollinger_z_prev": -2.5,
                            "donchian_gap": 0.01 if stock >= 6 else -0.01, "donchian_gap_prev": -0.01})
        market[symbol] = pd.DataFrame(bars)
    return samples, market, pd.DataFrame(universe), pd.DataFrame(states)


class StrategyTests(unittest.TestCase):
    def test_catalog_has_eight_fixed_strategies_plus_two_models(self):
        self.assertEqual(len(FIXED_STRATEGIES), 8)
        self.assertEqual(len(TECHNICAL_STRATEGIES), 5)
        self.assertEqual(len(STRATEGIES), 10)
    def test_rule_signals_ignore_future_labels_and_leave_no_signal_days_empty(self):
        samples, _, universe, states = replay_fixture()
        for strategy in STRATEGIES:
            if strategy in MODEL_STRATEGIES:
                continue
            with self.subTest(strategy=strategy), patch.object(factor_backtest, "fit_estimators", side_effect=AssertionError("rules must not fit")):
                expected, _, _ = factor_backtest.rule_strategy_signals(samples, universe, "14:00", states, strategy)
                unlabeled = [{key: value for key, value in row.items() if key not in {"target", "exit_day"}}
                             for row in samples]
                actual, _, _ = factor_backtest.rule_strategy_signals(unlabeled, universe, "14:00", states, strategy)
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), 20)
                self.assertTrue(all(actual.values()))
        frame = pd.DataFrame([{"open_momentum": 0, "vwap_gap": 0, "range_position": 0, "range_width": 0.001,
                              "ma_spread": 0, "ma_spread_prev": 0, "macd_hist": 0, "macd_hist_prev": 0,
                              "rsi": 50, "rsi_prev": 50, "bollinger_z": 0, "bollinger_z_prev": 0,
                              "donchian_gap": -0.01, "donchian_gap_prev": -0.01}])
        for strategy in FIXED_STRATEGIES:
            _, entries = rule_scores(frame, strategy)
            self.assertFalse(entries.any())

    def test_backtest_all_produces_independent_results_and_report_curves(self):
        inputs = replay_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(factor_lab, "BACKTESTS", root),
                  patch.object(factor_lab, "SOURCE_MANIFEST", root / "source-manifest.json"),
                  patch.object(factor_lab, "backtest_inputs", return_value=inputs) as load,
                  patch.object(factor_lab, "load_settings", return_value={"strategy": "elastic_net", "backtest_slot": "14:00"}),
                  patch.object(factor_lab, "notify_backtest"),
                  patch.object(factor_models, "_XGB_DEVICE", "cpu")):
                result = factor_lab.backtest_all_from_cache()
            load.assert_called_once()
            self.assertFalse(result["failed"])
            plan = json.loads(next((root / "plans").glob("*.json")).read_text())
            self.assertEqual(set(plan["strategy_ids"]), set(STRATEGIES))
            self.assertEqual(plan["previous_batches_on_dataset"], 0)
            self.assertEqual({run["strategy_id"] for run in result["completed"]}, set(STRATEGIES))
            periods = {(run["first_day"], run["last_day"], run["days"]) for run in result["completed"]}
            self.assertEqual(len(periods), 1)
            with patch.object(report_web_server, "BACKTEST_DIR", root):
                runs = report_web_server.backtest_runs()
                self.assertEqual(len(runs), len(STRATEGIES))
                for run in runs:
                    detail = report_web_server.read_backtest(run["run_id"], curve=True)
                    self.assertEqual(len(detail["curve"]), run["days"])
                    self.assertEqual(set(detail["downloads"]), {"equity.csv", "trades.csv"})
                    self.assertEqual(detail["strategy_id"], run["strategy_id"])
                    self.assertTrue(all(point["drawdown"] <= 0 for point in detail["curve"]))
            for run in result["completed"]:
                folds = json.loads((root / run["run_id"] / "folds.json").read_text())
                if run["strategy_id"] in MODEL_STRATEGIES:
                    self.assertEqual({fold["algorithm"] for fold in folds}, {run["strategy_id"]})
                else:
                    self.assertEqual(folds, [])

    def test_batch_keeps_successful_results_when_one_strategy_fails(self):
        def run(samples, bars_by_symbol, destination, **options):
            self.assertEqual(len(list((destination.parent / "plans").glob("*.json"))), 1)
            strategy = options["strategy"]
            if strategy == "elastic_net":
                raise RuntimeError("模拟训练失败")
            return {"strategy_id": strategy, "strategy_name": STRATEGIES[strategy]["name"],
                    "days": 20, "stats": {"total_return": 0, "max_drawdown": 0}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(factor_lab, "BACKTESTS", root),
                  patch.object(factor_lab, "backtest_inputs", return_value=([], {}, None, None)),
                  patch.object(factor_lab, "load_settings", return_value={"strategy": "xgboost", "backtest_slot": "14:00"}),
                  patch.object(factor_lab, "notify_backtest"), patch.object(factor_backtest, "run", side_effect=run) as runner):
                with self.assertRaisesRegex(RuntimeError, f"{len(STRATEGIES) - 1} 项回测完成，1 项失败"):
                    factor_lab.backtest_all_from_cache()
            self.assertEqual(runner.call_count, len(STRATEGIES))
            batch = json.loads(next((root / "batches").glob("*.json")).read_text())
            self.assertEqual(len(batch["completed"]), len(STRATEGIES) - 1)
            self.assertEqual(batch["failed"], {"elastic_net": "模拟训练失败"})
            self.assertEqual(len(list(root.glob("*/summary.json"))), len(STRATEGIES) - 1)


if __name__ == "__main__":
    unittest.main()
