import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import factor_models
import xgboost as xgb


class FactorModelTests(unittest.TestCase):
    def test_cuda_probe_uses_actual_xgboost_device(self):
        class Booster:
            def __init__(self, device):
                self.device = device

            def save_config(self):
                return json.dumps({"learner": {"generic_param": {"device": self.device}}})

        with (patch.object(factor_models, "_XGB_DEVICE", None),
              patch.object(xgb, "build_info", return_value={"USE_CUDA": True}),
              patch.object(xgb, "train", return_value=Booster("cuda:0"))):
            self.assertEqual(factor_models.xgboost_device(), "cuda")
        with (patch.object(factor_models, "_XGB_DEVICE", None),
              patch.object(xgb, "build_info", return_value={"USE_CUDA": True}),
              patch.object(xgb, "train", return_value=Booster("cpu"))):
            self.assertEqual(factor_models.xgboost_device(), "cpu")

    def test_cuda_prediction_failure_retries_on_cpu(self):
        class Ranker:
            def __init__(self):
                self.device = "cuda"
                self.attempts = 0

            def predict(self, values):
                self.attempts += 1
                if self.attempts == 1:
                    raise xgb.core.XGBoostError("CUDA unavailable")
                return np.ones(len(values))

            def set_params(self, device):
                self.device = device

            def get_booster(self):
                return self

            def set_param(self, options):
                self.device = options["device"]

        ranker = Ranker()
        frame = pd.DataFrame([{feature: 1.0 for feature in factor_models.FEATURES}])
        with patch.object(factor_models, "_XGB_DEVICE", "cuda"):
            scores = factor_models._predict_ranker(ranker, frame)
            self.assertEqual(factor_models.xgboost_device(), "cpu")
        self.assertEqual(scores.tolist(), [1.0])
        self.assertEqual(ranker.attempts, 2)
        self.assertEqual(ranker.device, "cpu")

    def test_cuda_training_failure_retries_on_cpu(self):
        devices = []

        class Ranker:
            def __init__(self, **options):
                self.device = options["device"]
                devices.append(self.device)

            def fit(self, values, relevance, qid):
                if self.device == "cuda":
                    raise xgb.core.XGBoostError("CUDA unavailable")

        rows = [{"day": "2026-01-01", "slot": "14:00", "symbol": f"600{index:03d}",
                 "target": index / 100, **{feature: index / 100 for feature in factor_models.FEATURES}}
                for index in range(12)]
        with (patch.object(factor_models, "_XGB_DEVICE", "cuda"),
              patch.object(xgb, "XGBRanker", Ranker)):
            estimators = factor_models.fit_estimators(pd.DataFrame(rows))
            self.assertEqual(factor_models.xgboost_device(), "cpu")
        self.assertEqual(devices, ["cuda", "cpu"])
        self.assertEqual(estimators["xgboost"].device, "cpu")

    def test_training_saves_only_selected_model_and_scores_without_future_columns(self):
        samples = []
        for day in pd.bdate_range("2026-01-01", periods=24):
            for slot in ("09:35", "14:30"):
                for stock in range(16):
                    x = (stock - 8) / 100
                    samples.append({"symbol": f"600{stock:03d}", "day": day.strftime("%Y-%m-%d"),
                                    "slot": slot, "target": 0.6 * x + 0.1 * x * x,
                                    "open_momentum": x, "gap": x * 0.8,
                                    "range_position": x * 0.6, "range_width": stock / 1000,
                                    "vwap_gap": x * 0.5})
        for algorithm in ("xgboost", "elastic_net"):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as directory, patch.object(factor_models, "MIN_ML_DAYS", 20):
                base = Path(directory)
                if algorithm == "elastic_net":
                    blocker = patch.object(xgb, "XGBRanker", side_effect=AssertionError("unselected XGBoost must not run"))
                else:
                    blocker = patch("sklearn.linear_model.ElasticNet.fit", side_effect=AssertionError("unselected Elastic Net must not run"))
                with blocker:
                    result = factor_models.fit_models(samples, base, algorithm)
                self.assertTrue((base / f"{algorithm}.json").exists())
                other = "elastic_net" if algorithm == "xgboost" else "xgboost"
                self.assertFalse((base / f"{other}.json").exists())
                self.assertEqual(set(result["algorithms"]), {algorithm})
                self.assertEqual(result["algorithm"], algorithm)
                self.assertEqual(result["days"], 24)
                frame = factor_models.sample_frame(samples).iloc[:16].copy()
                scores = factor_models.predict(frame, result, base)
                self.assertEqual(len(scores), 16)
                self.assertTrue(np.isfinite(scores).all())

    def test_minimum_history_is_checked_by_day(self):
        rows = [{"symbol": f"600{x:03d}", "day": "2026-01-02", "slot": "09:35",
                 "target": x / 100, "open_momentum": x / 100, "gap": x / 100,
                 "range_position": x / 100, "range_width": x / 100,
                 "vwap_gap": x / 100} for x in range(100)]
        with self.assertRaisesRegex(RuntimeError, "完整历史交易日"):
            factor_models.day_splits(factor_models.sample_frame(rows))


if __name__ == "__main__":
    unittest.main()
