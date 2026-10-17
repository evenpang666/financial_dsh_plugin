import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import web_strategy_task as task

class WebStrategyTaskTests(unittest.TestCase):
    def test_invalid_future_dates_do_not_prepare_or_run_backtest(self):
        with patch.object(sys, "argv", ["web", "backtest", "--strategy", "momentum", "--start", "2099-01-01", "--end", "2099-01-02"]), patch.object(task.lab, "backtest_from_cache") as run:
            self.assertEqual(task.main(), 1)
            run.assert_not_called()

    def test_model_backtest_requires_existing_checkpoint(self):
        with patch.object(sys, "argv", ["web", "backtest", "--strategy", "xgboost", "--start", "2026-01-01", "--end", "2026-02-01"]), patch.object(task.lab, "backtest_from_cache") as run:
            self.assertEqual(task.main(), 1)
            run.assert_not_called()

    def test_training_uses_requested_parameters_without_requiring_a_checkpoint(self):
        with patch.object(sys, "argv", ["web", "train", "--strategy", "elastic_net", "--start", "2025-10-01", "--end", "2026-06-01", "--parameters", '{"alpha_scale": 0.001, "l1_ratio": 0.5}']), patch.object(task, "cache_issues", return_value=[]), patch.object(task.lab, "ensure_range_coverage"), patch.object(task.lab, "train") as train, patch.object(task, "require_checkpoint") as checkpoint, patch.object(task.lab, "backtest_from_cache") as backtest:
            self.assertEqual(task.main(), 0)
            train.assert_called_once_with()
            checkpoint.assert_not_called()
            backtest.assert_not_called()
            self.assertEqual(task.lab._SETTINGS_OVERRIDE["training_parameters"]["elastic_net"], {"alpha_scale": 0.001, "l1_ratio": 0.5})
            self.assertEqual(task.lab._SETTINGS_OVERRIDE["training_start"], "2025-10-01")

    def test_missing_data_is_prepared_before_existing_backtest_engine(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "argv", ["web", "backtest", "--strategy", "momentum", "--start", "2026-01-01", "--end", "2026-02-01"]), patch.object(task.lab, "STORE", Path(directory)), patch.object(task.lab, "read_json", return_value={}), patch.object(task, "cache_issues", side_effect=[["missing"], []]), patch("factor_data_prep.prepare", side_effect=lambda *args, **kwargs: calls.append(("prepare", args))), patch.object(task.lab, "backtest_from_cache", side_effect=lambda: calls.append(("backtest", None))):
            self.assertEqual(task.main(), 0)
            self.assertEqual([item[0] for item in calls], ["prepare", "backtest"])
            self.assertEqual(calls[0][1], ("2026-01-01", "2026-02-01"))

    def tearDown(self):
        task.lab._STRATEGY_OVERRIDE = None
        task.lab._CHECKPOINT_OVERRIDE = None
        task.lab._SETTINGS_OVERRIDE = {}

    def test_failed_migration_never_runs_the_backtest(self):
        with patch.object(sys, "argv", ["web", "backtest", "--strategy", "momentum", "--start", "2026-10-08", "--end", "2026-10-09"]), patch.object(task.lab, "read_json", return_value={"validated": True, "first_day": "2025-09-08", "last_day": "2026-10-09"}), patch.object(task, "cache_issues", return_value=["旧缓存缺列"]), patch("factor_data_prep.prepare", side_effect=RuntimeError("缺少红股上市日期")) as prepare, patch.object(task.lab, "backtest_from_cache") as run:
            self.assertEqual(task.main(), 1)
            self.assertEqual(prepare.call_args.args, ("2025-09-08", "2026-10-09"))
            run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
