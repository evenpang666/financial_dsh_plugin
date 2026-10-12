import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_backtest as replay
import factor_lab as lab
import factor_models as models
from strategy_catalog import STRATEGIES, rule_scores
from strategy_config import (default_ranges, ensure_range_coverage, research_range,
                             validate_parameters, validate_range)
from strategy_indicators import indicator_frame
from test_strategies import replay_fixture


class StrategyConfigurationTests(unittest.TestCase):
    def test_defaults_and_invalid_parameters_for_every_strategy(self):
        for strategy in STRATEGIES:
            self.assertEqual(validate_parameters(strategy)["top_n"], 5)
            for supplied in ({"top_n": 1.5}, {"top_n": True}, {"unknown": 1},
                             {"top_n": float("nan")}, {"top_n": 0}, []):
                with self.subTest(strategy=strategy, supplied=supplied), self.assertRaises(ValueError):
                    validate_parameters(strategy, supplied)
        with self.assertRaises(ValueError):
            validate_parameters("ma_crossover", {"fast_period": 20, "slow_period": 5})
        with self.assertRaises(ValueError):
            validate_parameters("macd", {"slow_period": 40, "signal_period": 20})

    def test_default_ranges_and_checkpoint_boundary_include_collection_and_labels(self):
        defaults = default_ranges(date(2026, 10, 13))
        self.assertEqual(defaults["backtest_end"], "2026-10-12")
        self.assertLess(defaults["training_end"], defaults["backtest_start"])
        checkpoint = {"training": {"label_end_day": "2026-09-10", "requested_end": "2026-09-12"}}
        for start, end in (("2026-09-01", "2026-09-15"), ("2026-09-12", "2026-09-15"),
                           ("2026-01-01", "2026-02-01")):
            with self.assertRaisesRegex(ValueError, "重叠"):
                validate_range(start, end, checkpoint, today=date(2026, 10, 13))
        self.assertEqual(validate_range("2026-09-13", "2026-09-15", checkpoint),
                         ("2026-09-13", "2026-09-15"))
        start, _ = research_range({}, "backtest", checkpoint)
        self.assertGreater(start, "2026-09-12")
        for first, last in (("2026-02-30", "2026-03-01"), ("20260101", "2026-01-03"),
                            ("2026-09-15", "2026-09-13"), ("2026-10-13", "2026-10-13")):
            with self.assertRaises(ValueError):
                validate_range(first, last, today=date(2026, 10, 13))

    def test_partial_cache_is_rejected_instead_of_clipping_requested_dates(self):
        manifest = {"requested_start": "2026-01-01", "requested_end": "2026-03-01"}
        ensure_range_coverage(manifest, "2026-01-10", "2026-02-01")
        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            ensure_range_coverage(manifest, "2025-12-31", "2026-02-01")
        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            ensure_range_coverage(manifest, "2026-02-01", "2026-03-02")

    def test_custom_rule_thresholds_change_entry(self):
        frame = pd.DataFrame({"open_momentum": [.006, .02], "vwap_gap": [-.006, -.02],
                              "rsi_prev": [35, 20], "rsi": [41, 31]})
        self.assertEqual(rule_scores(frame, "momentum", {"threshold": .01})[1].tolist(), [False, True])
        self.assertEqual(rule_scores(frame, "mean_reversion", {"deviation": .01})[1].tolist(), [False, True])
        self.assertEqual(rule_scores(frame, "rsi_rebound", {"oversold": 40})[1].tolist(), [True, False])

    def test_custom_indicator_periods_remain_causal_and_independent(self):
        close = np.linspace(10, 20, 48)
        bars = pd.DataFrame({"time": pd.date_range("2026-09-01 09:30", periods=48, freq="5min"),
                             "open": close, "close": close, "high": close + .1, "low": close - .1})
        defaults = indicator_frame(bars)
        params = {"ma_crossover": {"fast_period": 3, "slow_period": 10},
                  "bollinger_reversion": {"period": 10}}
        custom = indicator_frame(bars, parameters=params)
        self.assertNotAlmostEqual(defaults.iloc[-1].ma_spread, custom.iloc[-1].ma_spread)
        self.assertNotAlmostEqual(defaults.iloc[-1].bollinger_z, custom.iloc[-1].bollinger_z)
        pd.testing.assert_series_equal(defaults.macd_hist, custom.macd_hist)
        truncated = indicator_frame(bars.iloc[:35], parameters=params)
        pd.testing.assert_frame_equal(custom.iloc[:35], truncated)

    def test_selected_classic_dates_and_top_n_reach_simulation(self):
        samples, bars, universe, state = replay_fixture()
        days = sorted(universe.day.unique())[10:16]
        with tempfile.TemporaryDirectory() as directory:
            result = replay.run(samples, bars, Path(directory), strategy="momentum", universe_history=universe,
                                market_state=state, start=days[0], end=days[-1],
                                parameters={"top_n": 2, "threshold": .001})
            self.assertEqual((result["first_day"], result["last_day"], result["days"]), (days[0], days[-1], 6))
            self.assertEqual(result["top_n"], 2)
            trades = pd.read_csv(Path(directory) / "trades.csv")
            self.assertLessEqual(trades[trades.side.eq("buy")].groupby("day").symbol.nunique().max(), 2)

    def test_training_filters_signal_and_label_dates_and_saves_parameters(self):
        samples = [{"day": "2026-01-01", "exit_day": "2026-01-02"},
                   {"day": "2026-01-05", "exit_day": "2026-01-06"},
                   {"day": "2026-01-06", "exit_day": "2026-01-07"},
                   {"day": "2026-01-06", "exit_day": None}]
        captured = []
        def fit(rows, destination, algorithm, parameters):
            captured.extend(rows)
            self.assertEqual(parameters["l1_ratio"], .8)
            destination.mkdir(parents=True)
            (destination / f"{algorithm}.json").write_text("{}", encoding="utf-8")
            return {"algorithm": algorithm, "algorithms": {algorithm: {}}, "features": list(models.FEATURES),
                    "days": 60, "samples": 1000, "last_day": "2026-01-05", "label_end_day": "2026-01-06", "holdout_days": 12}
        settings = {"strategy": "elastic_net", "backtest_slot": "14:00", "training_start": "2026-01-05",
                    "training_end": "2026-01-06", "training_parameters": {"elastic_net": {"l1_ratio": .8}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(lab, "STORE", root), patch.object(lab, "MODELS", root / "models"),
                  patch.object(lab, "MODEL", root / "model.json"), patch.object(lab, "LATEST", root / "latest.json"),
                  patch.object(lab, "load_settings", return_value=settings),
                  patch.object(models, "fit_models", side_effect=fit), redirect_stdout(io.StringIO())):
                result = lab.publish_training(samples, {}, [], [], "test", "test", "test")
        self.assertEqual(captured, samples[1:2])
        self.assertEqual(result["training"]["requested_end"], "2026-01-06")
        self.assertEqual(result["training_parameters"]["l1_ratio"], .8)

    def test_model_hyperparameters_reach_actual_estimators(self):
        frame = pd.DataFrame([{**{key: float(i + 1) for key in models.FEATURES},
                               "day": "2026-01-01", "slot": "14:00", "symbol": str(i),
                               "target": i / 1000} for i in range(24)])
        linear = models.fit_estimators(frame, "elastic_net", {"l1_ratio": .8, "max_iter": 2000})["elastic_net"][1]
        self.assertEqual((linear.l1_ratio, linear.max_iter), (.8, 2000))
        import xgboost
        with patch.object(models, "xgboost_device", return_value="cpu"), patch.object(xgboost, "XGBRanker") as ranker:
            models.fit_estimators(frame, "xgboost", {"n_estimators": 77, "max_depth": 3, "learning_rate": .1})
        self.assertEqual(ranker.call_args.kwargs["n_estimators"], 77)
        self.assertEqual(ranker.call_args.kwargs["max_depth"], 3)
        self.assertEqual(ranker.call_args.kwargs["learning_rate"], .1)

    def test_model_overlap_is_rejected_before_loading_any_history(self):
        checkpoint = {"training": {"label_end_day": "2026-02-01", "requested_end": "2026-02-02"}}
        settings = {"strategy": "elastic_net", "backtest_slot": "14:00", "checkpoint_id": "version",
                    "backtest_start": "2026-02-01", "backtest_end": "2026-03-01"}
        with (patch.object(lab, "load_settings", return_value=settings),
              patch.object(lab, "require_checkpoint", return_value=checkpoint), patch.object(lab, "backtest_inputs") as load):
            with self.assertRaisesRegex(ValueError, "重叠"):
                lab.backtest_from_cache()
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
