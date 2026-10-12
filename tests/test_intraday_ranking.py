import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import intraday_ranking as ranking
import factor_lab as lab


class IntradayRankingTests(unittest.TestCase):
    def setUp(self):
        self.moment = datetime(2026, 10, 12, 10, 31, tzinfo=lab.CHINA)
        self.snapshot = pd.DataFrame([{"代码": f"600{index:03d}", "名称": f"股票{index}", "最新价": 100 + index,
            "今开": 100, "昨收": 99, "最高": 101 + index, "最低": 99,
            "成交量": 1000, "成交额": (100 + index) * 100000} for index in range(65)])
        self.calendar = types.SimpleNamespace(tool_trade_date_hist_sina=lambda: pd.DataFrame({
            "trade_date": [self.moment.date() - timedelta(days=3), self.moment.date()]}))

    def run_ranking(self, strategy, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(ranking, "STORE", Path(directory)), patch.object(lab, "now_cn", return_value=self.moment),
                  patch.object(lab, "spot_frame", return_value=self.snapshot),
                  patch.object(lab, "today_pit_universe", side_effect=AssertionError("must not restrict whole market")),
                  patch.dict(sys.modules, {"akshare": self.calendar})):
                result = ranking.rank_market(strategy, **kwargs)
                persisted = lab.read_json(Path(directory) / f"full-market-{strategy}.json")
                self.assertEqual(persisted, result)
                return result

    def test_whole_market_keeps_all_scores_and_non_entries(self):
        self.snapshot.loc[0, "名称"] = "ST测试"
        self.snapshot.loc[1, "成交量"] = 0
        self.snapshot.loc[2, "代码"] = "920002"
        result = self.run_ranking("momentum")
        self.assertEqual(result["universe_count"], 65)
        self.assertEqual(result["input_count"], 62)
        self.assertEqual(len(result["ranking"]), 62)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["ranking"][0]["symbol"], "600064")
        result = self.run_ranking("mean_reversion")
        self.assertTrue(all(not row["entry"] for row in result["ranking"]))
        self.assertEqual(result["entry_count"], 0)
        self.assertTrue(result["available"])

    def test_model_scores_beyond_training_pool_without_training(self):
        model = {"model_id": "test", "selected_at": self.moment.isoformat(), "features": ["open_momentum"], "quality": "exploratory", "universe": [{"symbol": "600000"}]}
        with patch.object(ranking, "resolve_selection", return_value=("elastic_net", "test", model)), patch.object(ranking, "ranking_model", return_value=model), patch("factor_models.predict", return_value=np.arange(65)) as predict:
            result = self.run_ranking("elastic_net", checkpoint_id="test")
        self.assertEqual(len(predict.call_args.args[0]), 65)
        self.assertEqual(len(result["ranking"]), 65)
        self.assertTrue(all(row["entry"] is None for row in result["ranking"]))
        self.assertIn("未经同口径回测验证", result["note"])

    def test_minute_strategy_shares_holding_loader_and_counts_failure(self):
        def load(item, strategy, asof, previous, parameters=None):
            symbol = item["holding"]["symbol"]
            self.assertEqual(strategy, "ma_crossover")
            self.assertEqual(asof, pd.Timestamp("2026-10-12 10:30"))
            self.assertEqual(previous, "2026-10-09")
            if symbol == "600001":
                raise ValueError("分钟线缺失")
            return {"symbol": symbol, "price": 100, "ma_spread": .01, "ma_spread_prev": -.01}
        with patch("portfolio_research.load_signal_record", side_effect=load) as loader:
            result = self.run_ranking("ma_crossover")
        self.assertEqual(loader.call_count, 65)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["valid_count"], 64)
        self.assertFalse(result["coverage_complete"])
        self.assertIn("有效子集排序", result["note"])
        self.assertEqual(result["signal_at"], "2026-10-12T10:30:00")

    def test_existing_trained_checkpoints_remain_selectable_regardless_of_age(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(lab, "STORE", Path(directory)), patch.object(lab, "load_settings", return_value={"strategy": "xgboost"}):
            for algorithm in ("xgboost", "elastic_net"):
                artifact = Path(directory) / "models" / algorithm
                artifact.mkdir(parents=True)
                lab.write_json(artifact / f"{algorithm}.json", {})
                lab.write_json(artifact / "checkpoint.json", {"model_id": algorithm, "algorithm": algorithm,
                    "target_version": lab.ML_TARGET_VERSION, "features": ["open_momentum"],
                    "selected_at": (self.moment - timedelta(days=31)).isoformat(), "training": {"last_day": "2026-09-09"}})
            menu = ranking.ranking_options()
        self.assertIn("xgboost@xgboost", menu["models"])
        self.assertIn("elastic_net@elastic_net", menu["models"])
        self.assertEqual(len(menu["strategies"]), 10)

    def test_preopen_and_nontrading_day_do_not_fetch_snapshot(self):
        for moment in (self.moment.replace(hour=9, minute=20), self.moment+timedelta(days=1)):
            with patch.object(lab, "now_cn", return_value=moment), patch.object(lab, "spot_frame") as snapshot, patch.dict(sys.modules, {"akshare": self.calendar}):
                with self.assertRaises(ValueError):
                    ranking.rank_market("momentum")
                snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
