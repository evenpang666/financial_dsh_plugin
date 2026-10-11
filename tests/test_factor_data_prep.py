import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import factor_data_prep as prep


class FactorDataPrepTests(unittest.TestCase):
    def test_daily_source_prefers_only_a_usable_token_file(self):
        days = ["20260922"]
        calendar = pd.DataFrame([{"cal_date": days[0], "is_open": "1", "pretrade_date": "20260921"}])
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "source-cache"
            token_file = Path(directory) / "tushare.token"
            with (patch.object(prep, "TOKEN_FILE", token_file),
                  patch.object(prep, "fetch_calendar_baostock", return_value=calendar),
                  patch.object(prep, "collect_public", return_value=(days, "BaoStock public daily")) as public,
                  patch.object(prep, "collect_tushare", return_value=days) as tushare,
                  patch.object(prep, "_usable_tushare", return_value=False) as usable,
                  patch.dict("os.environ", {"TUSHARE_TOKEN": "environment-only"})):
                result = prep.collect_daily("2026-09-22", "2026-09-22", raw)
                self.assertEqual(result[1], "daily-public")
                usable.assert_not_called()
                tushare.assert_not_called()
                token_file.write_text("invalid-token", encoding="utf-8")
                result = prep.collect_daily("2026-09-22", "2026-09-22", raw)
                self.assertEqual(result[1], "daily-public")
                usable.assert_called_once_with("invalid-token", days[0])
                tushare.assert_not_called()
                token_file.write_text("valid-token", encoding="utf-8")
                usable.return_value = True
                result = prep.collect_daily("2026-09-22", "2026-09-22", raw)
                self.assertEqual(result[1], "daily")
                tushare.assert_called_once_with("2026-09-22", "2026-09-22", raw, token="valid-token")
                self.assertEqual(public.call_count, 2)
                tushare.side_effect = RuntimeError("source unavailable")
                result = prep.collect_daily("2026-09-22", "2026-09-22", raw)
                self.assertEqual(result[1], "daily-public")
                self.assertEqual(public.call_count, 3)

    def test_akshare_calendar_timeout_falls_back_but_disagreement_fails(self):
        with patch.object(prep.subprocess, "run", side_effect=prep.subprocess.TimeoutExpired("python", 20)):
            self.assertIn("BaoStock", prep._check_akshare_calendar(["20260922"]))
        result = SimpleNamespace(stdout='["20260922"]\n')
        with patch.object(prep.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "交易日历不一致"):
                prep._check_akshare_calendar(["20260922"])

    def test_public_daily_uses_historical_roster_and_rejects_missing_active_stock(self):
        class Result:
            error_code = "0"
            error_msg = "success"

            def __init__(self, rows, fields):
                self.rows, self.fields, self.index = rows, fields, 0

            def next(self):
                if self.index >= len(self.rows):
                    return False
                self.index += 1
                return True

            def get_row_data(self):
                return self.rows[self.index - 1]

        symbols = [f"600{index:03d}" for index in range(41)]
        calendar = pd.DataFrame([{"cal_date": "20260922", "is_open": "1", "pretrade_date": "20260921"}])
        missing = {"value": False}

        def all_stock(day):
            return Result([[f"sh.{symbol}", "1"] for symbol in symbols], ["code", "tradeStatus"])

        def history(code, fields, start_date, end_date, frequency, adjustflag):
            symbol = code[-6:]
            rows = [[day, code, "1000", "1"] for day in ("2026-09-21", "2026-09-22")
                    if not (missing["value"] and symbol == symbols[0] and day == "2026-09-22")]
            return Result(rows, ["date", "code", "amount", "tradestatus"])

        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_all_stock=all_stock, query_history_k_data_plus=history)
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "source-cache"
            basic = pd.DataFrame([{"symbol": symbol, "list_date": "20200101", "delist_date": ""}
                                  for symbol in symbols])
            with (patch.object(prep, "fetch_calendar_baostock", return_value=calendar),
                  patch.object(prep, "fetch_stock_basic_baostock", return_value=basic),
                  patch.object(prep, "_check_akshare_calendar", return_value="AKShare checked"),
                  patch.dict(sys.modules, {"baostock": fake})):
                days, source = prep.collect_public("2026-09-22", "2026-09-22", raw)
                self.assertEqual(days, ["20260922"])
                self.assertIn("AKShare checked", source)
                self.assertEqual(len(prep._daily_from_cache(["20260921"], raw, "daily-public")), 41)
                (raw / "daily-public-symbols" / f"{symbols[0]}_20260921_20260922.csv").unlink()
                missing["value"] = True
                with self.assertRaisesRegex(RuntimeError, "正常交易股票缺少日线"):
                    prep.collect_public("2026-09-22", "2026-09-22", raw)

    def test_zero_suspension_placeholders_are_removed(self):
        frame = pd.DataFrame([{"time": "2026-09-22 09:35:00", "open": 0,
                               "high": 0, "low": 0, "close": 0,
                               "volume": 0, "amount": 0},
                              {"time": "2026-09-23 09:35:00", "open": 10,
                               "high": 10, "low": 10, "close": 10,
                               "volume": 100, "amount": 1000}])
        self.assertEqual(len(prep.strip_zero_placeholders(frame)), 1)

    def test_universe_uses_only_previous_trading_day_amount(self):
        prior = [{"trade_date": "20260921", "symbol": f"600{index:03d}",
                  "amount": 1000 - index} for index in range(41)]
        current = [{"trade_date": "20260922", "symbol": "600040", "amount": 999999}]
        calendar = pd.DataFrame([{"cal_date": "20260922", "is_open": "1",
                                  "pretrade_date": "20260921"}])
        universe = prep.construct_universe(pd.DataFrame(prior + current), calendar,
                                           "2026-09-22", "2026-09-22")
        self.assertEqual(len(universe), 40)
        self.assertNotIn("600040", set(universe.symbol))
        self.assertTrue(universe.known_at.str.startswith("2026-09-21").all())

    def test_missing_exact_limit_price_fails_except_first_five_ipo_sessions(self):
        universe = pd.DataFrame([{"day": "2026-09-22", "symbol": "600001",
                                  "known_at": "2026-09-21 18:00:00+08:00"}])
        status = pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type"])
        limits = pd.DataFrame(columns=["ts_code", "trade_date", "up_limit", "down_limit"])
        basic = pd.DataFrame([{"symbol": "600001", "list_date": "20200101", "delist_date": ""}])
        actions = pd.DataFrame(columns=["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio"])
        with self.assertRaisesRegex(RuntimeError, "缺少精确涨跌停价"):
            prep.construct_market_state(universe, pd.DataFrame(), status, limits, status, basic, actions)
        basic.loc[0, "list_date"] = "20260922"
        result = prep.construct_market_state(universe, pd.DataFrame(), status, limits, status, basic, actions)
        self.assertFalse(result.iloc[0].has_price_limit)

    def test_baostock_status_reconstructs_limit_and_rejects_invalid_reference(self):
        universe = pd.DataFrame([{"day": "2026-09-22", "symbol": "600001",
                                  "known_at": "2026-09-21 18:00:00+08:00"}])
        basic = pd.DataFrame([{"symbol": "600001", "list_date": "20200101", "delist_date": ""}])
        status = pd.DataFrame([{"date": "2026-09-22", "code": "sh.600001", "preclose": "10",
                                "high": "10.5", "low": "9.5", "tradestatus": "1", "isST": "0"}])
        actions = pd.DataFrame(columns=["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio"])
        result = prep.construct_market_state_baostock(universe, basic, status, actions)
        self.assertEqual((result.iloc[0].limit_up, result.iloc[0].limit_down), (11.0, 9.0))
        status.loc[0, "high"] = "11.2"
        with self.assertRaisesRegex(RuntimeError, "价格超出"):
            prep.construct_market_state_baostock(universe, basic, status, actions)
        status.loc[0, ["preclose", "high", "low"]] = ["13.91", "15.30", "13.81"]
        actions = pd.DataFrame([{"symbol": "600001", "ex_day": "2026-09-22",
                                 "pay_day": "2026-09-22", "cash_dividend": 0.027,
                                 "split_ratio": 1.0}])
        result = prep.construct_market_state_baostock(universe, basic, status, actions)
        self.assertEqual(result.iloc[0].limit_up, 15.3)
        universe.loc[0, "day"] = "2026-07-06"
        status.loc[0, ["date", "preclose", "high", "low", "isST"]] = ["2026-07-06", "1.76", "1.93", "1.71", "1"]
        result = prep.construct_market_state_baostock(universe, basic, status,
                                                       actions.iloc[0:0])
        self.assertEqual(result.iloc[0].limit_up, 1.94)

    def test_quality_report_checks_all_candidate_bar_days(self):
        days = ("2026-09-22", "2026-09-23")
        universe = pd.DataFrame([{"day": day, "symbol": "600001",
                                  "known_at": "2026-09-21 18:00:00+08:00"} for day in days])
        state = pd.DataFrame([{"day": day, "symbol": "600001", "has_price_limit": True,
                               "limit_up": 11, "limit_down": 9, "split_ratio": 1,
                               "cash_dividend": 0, "dividend_pay_date": "",
                               "is_st": False, "is_suspended": False} for day in days])
        clocks = pd.date_range("2026-09-22 09:35", "2026-09-22 11:30", freq="5min").strftime("%H:%M").tolist()
        clocks += pd.date_range("2026-09-22 13:05", "2026-09-22 15:00", freq="5min").strftime("%H:%M").tolist()
        rows = [{"time": f"{day} {clock}:00", "open": 10, "high": 10,
                 "low": 10, "close": 10, "volume": 1000, "amount": 10000}
                for day in days for clock in clocks]
        with tempfile.TemporaryDirectory() as directory, patch.object(prep, "TOP_N", 1):
            path = Path(directory) / "600001.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            daily = pd.DataFrame([{"trade_date": "20260921", "symbol": "600001", "amount": 100}])
            good = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                       minimum_market_symbols=1)
            self.assertTrue(good["passed"], good["issues"])
            pd.DataFrame(rows[:-1]).to_csv(path, index=False)
            bad = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                      minimum_market_symbols=1)
            self.assertFalse(bad["passed"])
            self.assertEqual(bad["missing_candidate_bar_days"], 1)


if __name__ == "__main__":
    unittest.main()
