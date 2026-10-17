import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backtest_cache import cache_issues, preparation_range, sha256

class BacktestCacheTests(unittest.TestCase):
    def publish(self, root, credit="2026-01-06"):
        pd.DataFrame([{"day": "2026-01-05", "symbol": "600001", "known_at": "2026-01-02 18:00:00+08:00"}]).to_csv(root / "universe-history.csv", index=False)
        pd.DataFrame([{"day": "2026-01-05", "symbol": "600001", "has_price_limit": True,
                       "limit_up": 11, "limit_down": 9, "split_ratio": 1.1, "cash_dividend": 0,
                       "dividend_pay_date": "", "stock_credit_date": credit, "is_st": False,
                       "is_suspended": False}]).to_csv(root / "market-state.csv", index=False)
        (root / "bars").mkdir(); (root / "bars/600001.csv").write_text("verified bars", encoding="utf-8")
        manifest = {"validated": True, "data_schema_version": 2, "feature_warmup_validated": True,
                    "source_scope": "historical_vendor_reconstruction",
                    "files": {name: sha256(root / name) for name in ("universe-history.csv", "market-state.csv")},
                    "bars": {"600001": sha256(root / "bars/600001.csv")}}
        (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_legacy_validated_flag_cannot_skip_new_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn("升级", cache_issues(directory, {"validated": True})[0])

    def test_current_cache_requires_stock_credit_column_and_valid_dates(self):
        for date in ("", "2026-01-02", "2026-01-06"):
            with self.subTest(date=date), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); manifest = self.publish(root, date)
                self.assertEqual(bool(cache_issues(root, manifest)), date != "2026-01-06")
                frame = pd.read_csv(root / "market-state.csv").drop(columns="stock_credit_date")
                frame.to_csv(root / "market-state.csv", index=False)
                manifest["files"]["market-state.csv"] = sha256(root / "market-state.csv")
                self.assertIn("缺少红股上市日期列", cache_issues(root, manifest)[0])

    def test_corrupt_or_missing_minutes_cannot_be_treated_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.publish(root)
            self.assertEqual(cache_issues(root), [])
            (root / "bars/600001.csv").write_text("changed", encoding="utf-8")
            self.assertIn("分钟线", cache_issues(root)[0])
            (root / "bars/600001.csv").unlink()
            self.assertTrue(cache_issues(root))

    def test_migration_preserves_legacy_range_without_requested_dates(self):
        self.assertEqual(preparation_range("2026-10-08", "2026-10-09", {"first_day": "2025-09-08", "last_day": "2026-10-12"}), ("2025-09-08", "2026-10-12"))

if __name__ == "__main__":
    unittest.main()
