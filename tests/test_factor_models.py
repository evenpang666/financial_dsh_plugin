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


class FactorModelTests(unittest.TestCase):
    def test_training_saves_both_models_and_scores_without_future_columns(self):
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
        with tempfile.TemporaryDirectory() as directory, patch.object(factor_models, "MIN_ML_DAYS", 20):
            base = Path(directory)
            result = factor_models.fit_models(samples, base)
            self.assertTrue((base / "xgboost.json").exists())
            self.assertTrue((base / "elastic_net.json").exists())
            self.assertEqual(set(result["algorithms"]), {"xgboost", "elastic_net"})
            self.assertEqual(result["days"], 24)
            frame = factor_models.sample_frame(samples).iloc[:16].copy()
            for algorithm in ("xgboost", "elastic_net"):
                scores = factor_models.predict(frame, {**result, "algorithm": algorithm}, base)
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
