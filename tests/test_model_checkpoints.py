import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_checkpoints as registry
import factor_lab as lab
import factor_models as models
import factor_backtest as replay
import intraday_ranking as ranking
import report_web_server as web
import control_panel as panel_module


def checkpoint(store, identity="version-a", algorithm="elastic_net", value=1):
    directory = store / "models" / identity
    directory.mkdir(parents=True)
    artifact = directory / f"{algorithm}.json"
    artifact.write_text(json.dumps({"coef": [value] * len(models.FEATURES), "intercept": 0,
                                    "mean": [0] * len(models.FEATURES), "scale": [1] * len(models.FEATURES)}), encoding="utf-8")
    metadata = {"model_id": identity, "algorithm": algorithm, "selected_at": "2026-01-03T12:00:00+08:00",
                "target_version": 3, "features": list(models.FEATURES), "quality": "exploratory",
                "training": {"last_day": "2026-01-01", "label_end_day": "2026-01-02"},
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    lab.write_json(directory / "checkpoint.json", metadata)
    return metadata


class CheckpointTests(unittest.TestCase):
    def test_empty_store_has_no_model_choices_and_cannot_execute_models(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            options = registry.execution_options(store, {"strategy": "xgboost"})
            self.assertEqual(options["models"], {})
            self.assertEqual(options["selected"], "")
            self.assertEqual(options["selected_kind"], "model")
            self.assertEqual(len(options["fixed_strategies"]), 8)
            with self.assertRaises(ValueError):
                registry.resolve_selection("xgboost", store=store)
            self.assertEqual(registry.resolve_selection("momentum", store=store)[:2], ("momentum", ""))

    def test_exact_version_is_used_even_when_latest_pointer_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            first = checkpoint(store, "version-a", value=1)
            second = checkpoint(store, "version-b", value=2)
            lab.write_json(store / "model-elastic_net.json", second)
            options = registry.execution_options(store, {"strategy": "elastic_net", "checkpoint_id": "version-a"})
            self.assertEqual(len(options["models"]), 2)
            self.assertEqual(options["selected"], "elastic_net@version-a")
            strategy, identity, selected = registry.resolve_selection(options["selected"], store=store)
            self.assertEqual((strategy, identity), ("elastic_net", "version-a"))
            frame = pd.DataFrame([{key: 1 for key in models.FEATURES}])
            self.assertEqual(models.predict(frame, selected, store / "models" / identity).tolist(), [6])
            self.assertEqual(models.predict(frame, second, store / "models/version-b").tolist(), [12])

    def test_missing_or_corrupt_weights_disappear_from_model_menu(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            checkpoint(store)
            artifact = store / "models/version-a/elastic_net.json"
            artifact.write_text("corrupt", encoding="utf-8")
            self.assertFalse(registry.execution_options(store)["models"])
            with self.assertRaises(ValueError):
                registry.require_checkpoint("elastic_net", "version-a", store)
            artifact.unlink()
            self.assertFalse(registry.list_checkpoints(store))

    def test_fixed_strategy_and_invalid_paths_cannot_take_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            checkpoint(store)
            for selection in ("momentum@version-a", "xgboost@version-a", "elastic_net@../version-a", "elastic_net@../../config"):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    registry.resolve_selection(selection, store=store)

    def test_explicit_fixed_strategy_does_not_inherit_a_saved_model_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(lab, "SETTINGS", Path(directory) / "settings.json"):
            lab.write_json(lab.SETTINGS, {"strategy": "elastic_net", "checkpoint_id": "version-a"})
            with patch.object(lab, "_STRATEGY_OVERRIDE", "momentum"), patch.object(lab, "_CHECKPOINT_OVERRIDE", None):
                self.assertEqual(lab.load_settings()["checkpoint_id"], "")
            with patch.object(lab, "_STRATEGY_OVERRIDE", "xgboost"), patch.object(lab, "_CHECKPOINT_OVERRIDE", None):
                self.assertEqual(lab.load_settings()["checkpoint_id"], "")

    def test_legacy_metadata_is_preserved_before_latest_pointer_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            old = checkpoint(store)
            (store / "models/version-a/checkpoint.json").unlink()
            old["training"].pop("label_end_day")
            lab.write_json(store / "model-elastic_net.json", old)
            registry.migrate_legacy_checkpoints(store)
            new = checkpoint(store, "version-b")
            lab.write_json(store / "model-elastic_net.json", new)
            selected = registry.require_checkpoint("elastic_net", "version-a", store)
            self.assertEqual(selected["training"]["label_end_day"], "2026-01-03")
            self.assertEqual(len(registry.list_checkpoints(store)), 2)

    def test_training_saves_separate_timestamped_artifacts_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            generations = []

            def fit(samples, destination, algorithm, parameters=None):
                destination.mkdir(parents=True)
                generations.append(destination.name)
                (destination / f"{algorithm}.json").write_text(json.dumps({"generation": len(generations)}), encoding="utf-8")
                return {"algorithm": algorithm, "algorithms": {algorithm: {}}, "features": list(models.FEATURES),
                        "days": 60, "samples": 1000, "last_day": "2026-01-01", "label_end_day": "2026-01-02", "holdout_days": 12}

            with (patch.object(lab, "STORE", store), patch.object(lab, "MODELS", store / "models"),
                  patch.object(lab, "MODEL", store / "model.json"), patch.object(lab, "LATEST", store / "latest.json"),
                  patch.object(lab, "now_cn", return_value=datetime(2026, 1, 3, 12, tzinfo=lab.CHINA)),
                  patch.object(lab, "load_settings", return_value={"strategy": "elastic_net", "backtest_slot": "14:00", "training_start": "2025-01-01", "training_end": "2026-01-02"}),
                  patch.object(models, "fit_models", side_effect=fit), redirect_stdout(io.StringIO())):
                first = lab.publish_training([{"day": "2026-01-01", "exit_day": "2026-01-02"}], {}, [], [], "test", "test", "test")
                second = lab.publish_training([{"day": "2026-01-01", "exit_day": "2026-01-02"}], {}, [], [], "test", "test", "test")
            self.assertNotEqual(first["model_id"], second["model_id"])
            self.assertTrue(first["model_id"].startswith("20260103-120000-"))
            self.assertEqual(len(registry.list_checkpoints(store)), 2)
            self.assertEqual(json.loads((store / "models" / first["model_id"] / "elastic_net.json").read_text())["generation"], 1)

    def test_checkpoint_backtest_freezes_weights_and_excludes_training_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            model = checkpoint(store)
            days = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]
            samples = [{"day": day, "slot": "14:00", "symbol": f"600{index:03}", "target": index / 100,
                        **{key: index / 10 for key in models.FEATURES}} for day in days for index in range(12)]
            universe = pd.DataFrame([{key: row[key] for key in ("day", "symbol")} for row in samples])
            with (patch.object(replay, "fit_estimators", side_effect=AssertionError("must not retrain")),
                  patch.object(replay, "select_algorithm", side_effect=AssertionError("must not refit"))):
                signals, folds, ic = replay.checkpoint_signals(samples, universe, "14:00", None, model, store / "models/version-a")
            self.assertEqual(set(signals), set(days[2:]))
            self.assertEqual(signals[days[2]][0], "600011")
            self.assertFalse(folds[0]["refitted"])
            self.assertEqual(folds[0]["checkpoint_id"], "version-a")
            model["training"]["label_end_day"] = days[-1]
            with self.assertRaisesRegex(RuntimeError, "至少两个交易日"):
                replay.checkpoint_signals(samples, universe, "14:00", None, model, store / "models/version-a")

    def test_model_job_pins_requested_version_and_cannot_read_another_versions_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "data/factor-lab"
            checkpoint(store)
            checkpoint(store, "version-b", value=2)
            with (patch.object(web, "PROJECT_ROOT", root), patch.object(lab, "STORE", store),
                  patch.object(lab, "load_settings", return_value={"strategy": "momentum"}),
                  patch.object(web, "ACTIVE_RANKING_JOB", ""), patch.object(web.threading, "Thread") as worker):
                state, created = web.start_ranking_job("elastic_net@version-a")
                self.assertTrue(created)
                self.assertEqual(state["checkpoint_id"], "version-a")
                self.assertEqual(worker.call_args.kwargs["args"][2], "version-a")
                result = {"available": True, "scope": "full_market", "strategy_id": "elastic_net", "algorithm": "elastic_net", "checkpoint_id": "version-b"}
                lab.write_json(store / "full-market-elastic_net-version-a.json", result)
                self.assertFalse(web.latest_ranking("elastic_net@version-a", "full_market")["available"])
                result["checkpoint_id"] = "version-a"
                lab.write_json(store / "full-market-elastic_net-version-a.json", result)
                self.assertTrue(web.latest_ranking("elastic_net@version-a", "full_market")["available"])

    def test_no_checkpoint_disables_model_actions_but_training_remains_available(self):
        train, backtest, rank = Mock(), Mock(), Mock()
        panel = SimpleNamespace(busy=False, starting_services=False, closing=False,
            factor_busy=False, action_buttons=[train, backtest, rank], utility_buttons=[], web_buttons=[],
            factor_slot_box=None, strategy_box=Mock(), factor_train_button=train,
            factor_backtest_button=backtest, factor_rank_button=rank, training_box=Mock(), strategy_kind_box=Mock(),
            strategy_kind=SimpleNamespace(get=lambda: "多因子模型策略"), strategy_choice=SimpleNamespace(get=lambda: ""))
        panel_module.ControlPanel._update_controls(panel)
        train.configure.assert_called_with(state="normal")
        backtest.configure.assert_called_with(state="disabled")
        rank.configure.assert_called_with(state="disabled")
        panel.training_box.configure.assert_called_with(state="readonly")


if __name__ == "__main__":
    unittest.main()
