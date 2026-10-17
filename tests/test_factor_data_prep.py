import sys
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import factor_data_prep as prep


class FactorDataPrepTests(unittest.TestCase):
    @staticmethod
    def minute_day(day="2026-09-22"):
        return pd.DataFrame([{"time": time, "open": 10.0, "high": 10.0, "low": 10.0,
                             "close": 10.0, "volume": 1000, "amount": 10000.0}
                            for time in prep._minute_times(day)])

    def test_complete_cache_with_invalid_prices_must_not_be_skipped(self):
        frame = self.minute_day()
        frame.loc[2, "amount"] = 20000
        self.assertFalse(prep._minute_cache_complete(frame, ["2026-09-22"]))
        issues = prep.minute_quality_issues(frame)
        self.assertEqual(issues[0]["reason"], "成交均价超出 K 线高低价")
        self.assertEqual(issues[0]["examples"][0]["time"], "2026-09-22 09:45:00")

    def test_saved_vendor_correction_works_for_new_user_without_live_backup(self):
        original = pd.read_csv(prep.MINUTE_CORRECTIONS / "601091_2026-09-21.baostock.csv")
        with tempfile.TemporaryDirectory() as directory, patch("factor_lab.sina_minute_frame", side_effect=AssertionError("recent API must not be needed")):
            result = prep.repair_minute_days("601091", original, Path(directory), {"2026-09-21": 6770866376.39})
            self.assertEqual(len(result), 48)
            self.assertFalse(prep.minute_quality_issues(result))
            record = json.loads((Path(directory) / "minute-repairs/601091_2026-09-21.json").read_text())
            self.assertIn("bundled verified correction", record["source"])
            self.assertTrue(record["backup_sha256"])

    def test_saved_correction_must_match_hash_and_current_daily_turnover(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in prep.MINUTE_CORRECTIONS.glob("601091_2026-09-21.*"):
                shutil.copyfile(source, root / source.name)
            with patch.object(prep, "MINUTE_CORRECTIONS", root):
                with self.assertRaisesRegex(RuntimeError, "日线总额.*不一致"):
                    prep.verified_minute_correction("601091", "2026-09-21", {"2026-09-21": 1})
                with (root / "601091_2026-09-21.csv").open("a") as target:
                    target.write("corrupt\n")
                with self.assertRaisesRegex(RuntimeError, "修正文件校验失败"):
                    prep.verified_minute_correction("601091", "2026-09-21")

    def test_empty_vendor_reply_does_not_report_success_for_incomplete_cache(self):
        from unittest.mock import Mock
        day = self.minute_day()
        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_history_k_data_plus=Mock(return_value=object()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prep._atomic_csv(day.iloc[:20], root / "bars/600001.csv")
            with patch.dict(sys.modules, {"baostock": fake}), patch.object(prep, "_bs_rows", return_value=pd.DataFrame()):
                _, failures = prep._fetch_minute_chunk([("600001", "2026-09-22")], "2026-09-22", root / "bars",
                    {"600001": ["2026-09-22"]}, root / "raw")
            self.assertEqual(len(failures), 1)
            self.assertIn("预热日", failures[0])
            self.assertEqual(len(pd.read_csv(root / "bars/600001.csv")), 20)

    def test_invalid_primary_and_backup_do_not_publish_or_overwrite_cache(self):
        bad = self.minute_day(); bad.loc[2, "amount"] = 20000
        reply = bad.copy(); reply["time"] = reply.time.dt.strftime("%Y%m%d%H%M%S000")
        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_history_k_data_plus=lambda *args, **kwargs: object())
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path = root / "bars/600001.csv"
                if existing:
                    prep._atomic_csv(bad, path)
                before = path.read_bytes() if existing else None
                with (patch.dict(sys.modules, {"baostock": fake}), patch.object(prep, "_bs_rows", side_effect=lambda result, label: reply.copy()),
                      patch("factor_lab.sina_minute_frame", return_value=bad)):
                    _, failures = prep._fetch_minute_chunk([("600001", "2026-09-22")], "2026-09-22", root / "bars",
                        {"600001": ["2026-09-22"]}, root / "raw")
                self.assertEqual(len(failures), 1)
                self.assertIn("备用数据未通过验收", failures[0])
                self.assertEqual(path.read_bytes() if path.exists() else None, before)
                self.assertFalse(list((root / "raw/minute-repairs").glob("*.json")))

    def test_unreadable_cache_is_archived_and_downloaded_again(self):
        good = self.minute_day()
        reply = good.copy(); reply["time"] = reply.time.dt.strftime("%Y%m%d%H%M%S000")
        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_history_k_data_plus=lambda *args, **kwargs: object())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "bars/600001.csv"
            path.parent.mkdir(); path.write_text("time,close\ninvalid,10\n")
            with patch.dict(sys.modules, {"baostock": fake}), patch.object(prep, "_bs_rows", return_value=reply):
                _, failures = prep._fetch_minute_chunk([("600001", "2026-09-22")], "2026-09-22", root / "bars",
                    {"600001": ["2026-09-22"]}, root / "raw")
            self.assertEqual(failures, [])
            self.assertTrue(prep._minute_cache_complete(pd.read_csv(path), ["2026-09-22"]))
            self.assertIn("invalid", next((root / "raw/minute-repairs").glob("*unreadable.csv")).read_text())

    def test_new_user_and_interrupted_download_prepare_all_first_day_features(self):
        import factor_lab
        from backtest_cache import cache_issues
        symbols = ["601091", "002463"]
        dates = ["20260921", "20260922", "20260923"]
        calendar = pd.DataFrame([{"cal_date": date, "is_open": "1", "pretrade_date": previous}
                                 for date, previous in zip(dates[1:], dates[:-1])])
        original = pd.read_csv(prep.MINUTE_CORRECTIONS / "601091_2026-09-21.baostock.csv")
        primary = {}
        for symbol in symbols:
            days = [self.minute_day(pd.Timestamp(date).strftime("%Y-%m-%d")) for date in dates]
            if symbol == "601091":
                for day in days:
                    day[["open", "high", "low", "close"]] = 37.72
                    day["amount"] = day.volume * 37.72
                days[0] = original
            primary[symbol] = pd.concat(days, ignore_index=True)
            primary[symbol]["time"] = pd.to_datetime(primary[symbol].time)
        for mode in ("new", "interrupted", "missing_warmup"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                store = Path(directory); raw = store / "raw"; bars = store / "bars"
                calls = []
                def collect(start, end, target):
                    prep._atomic_csv(calendar, target / "trade_cal_20260922_20260923.csv")
                    prep._atomic_csv(pd.DataFrame([{"symbol": symbol, "list_date": "20200101", "delist_date": ""}
                                                   for symbol in symbols]), target / "stock_basic_baostock.csv")
                    for date in dates:
                        amounts = {"601091": 1810560, "002463": 2000000}
                        if date == dates[0]:
                            amounts["601091"] = 6770866376.39
                        prep._atomic_csv(pd.DataFrame([{"trade_date": date, "ts_code": symbol,
                                                       "amount": amounts[symbol]} for symbol in symbols]),
                                         target / "daily-public" / f"{date}.csv")
                    return dates[1:], "daily-public", "test vendor"
                def history(code, fields, start_date, end_date, frequency, adjustflag):
                    symbol = code[-6:]; calls.append((symbol, start_date))
                    frame = primary[symbol].copy()
                    frame = frame[frame.time.dt.strftime("%Y-%m-%d").between(start_date, end_date)].copy()
                    if mode == "missing_warmup" and symbol == "002463":
                        frame = frame[frame.time.dt.strftime("%Y-%m-%d").eq("2026-09-23")].copy()
                    frame["time"] = frame.time.dt.strftime("%Y%m%d%H%M%S000")
                    return frame
                fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                                       query_history_k_data_plus=history)
                status = pd.DataFrame([{"date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                    "code": ("sh." if symbol.startswith("6") else "sz.") + symbol,
                    "preclose": price, "high": price, "low": price, "tradestatus": "1", "isST": "0"}
                    for date in dates[1:] for symbol, price in (("601091", 37.72), ("002463", 10))])
                actions = pd.DataFrame(columns=["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio", "stock_credit_date"])
                real_quality = prep.quality_report
                def check(universe, state, cache, daily):
                    return real_quality(universe, state, cache, daily, minimum_days=1, minimum_market_symbols=1)
                if mode != "new":
                    prep._atomic_csv(primary["601091"].iloc[48:68], bars / "601091.csv")
                    # Complete current day but missing the late entrant's warmup.
                    prep._atomic_csv(primary["002463"].iloc[-48:], bars / "002463.csv")
                with (patch.object(prep, "TOP_N", 1), patch.object(prep, "collect_daily", side_effect=collect),
                      patch.object(prep, "fetch_actions_baostock", return_value=actions),
                      patch.object(prep, "fetch_daily_status_baostock", return_value=status),
                      patch.object(prep, "_bs_rows", side_effect=lambda result, label: result),
                      patch.object(prep, "quality_report", side_effect=check),
                      patch.dict(sys.modules, {"baostock": fake}),
                      patch.object(factor_lab, "sina_minute_frame", side_effect=AssertionError("live backup unavailable")),
                      redirect_stdout(io.StringIO())):
                    if mode == "missing_warmup":
                        with self.assertRaisesRegex(RuntimeError, "预热"):
                            prep.prepare("2026-09-22", "2026-09-23", raw, bars, store)
                        self.assertFalse((store / "source-manifest.json").exists())
                        self.assertFalse((store / "universe-history.csv").exists())
                        self.assertFalse((store / "market-state.csv").exists())
                        failed = json.loads((store / "prepared/quality-report.json").read_text())
                        self.assertFalse(failed["passed"])
                        self.assertEqual(failed["missing_warmup_bar_days"], 1)
                        continue
                    report = prep.prepare("2026-09-22", "2026-09-23", raw, bars, store)
                    self.assertTrue(report["passed"], report)
                    self.assertEqual(set(calls), {("601091", "2026-09-21"), ("002463", "2026-09-22")})
                    universe = pd.read_csv(store / "universe-history.csv", dtype={"symbol": str})
                    state = pd.read_csv(store / "market-state.csv", dtype={"symbol": str})
                    samples = []
                    for symbol in symbols:
                        _, _, stock_samples = factor_lab._pit_symbol_inputs(symbol, bars / f"{symbol}.csv",
                            "2026-09-22", "2026-09-23", "2026-09-24", state[state.symbol.eq(symbol)])
                        samples.extend(stock_samples)
                    keys = {(row["day"], row["symbol"]) for row in samples if row["slot"] == "14:00"}
                    self.assertTrue(set(zip(universe.day, universe.symbol)).issubset(keys))
                    count = len(calls)
                    prep.prepare("2026-09-22", "2026-09-23", raw, bars, store)
                    self.assertEqual(len(calls), count)  # Valid cache is reused.
                self.assertFalse(cache_issues(store))

    def test_alternate_day_repair_preserves_evidence_and_only_fills_confirmed_no_trade_window(self):
        primary = self.minute_day()
        primary.loc[1, ["volume", "amount"]] = [0, 0]
        backup = primary.drop(index=1).copy()
        primary.loc[2, "amount"] = 20000
        reference = {"2026-09-22": 470000}
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            repaired = prep.repair_minute_days("600001", primary, raw, reference, backup)
            self.assertEqual(len(repaired), 48)
            self.assertFalse(prep.minute_quality_issues(repaired, reference))
            self.assertEqual(repaired.iloc[1].volume, 0)
            self.assertEqual(repaired.iloc[2].amount, 10000)
            originals = list((raw / "minute-repairs").glob("*original.csv"))
            self.assertEqual(pd.read_csv(originals[0]).iloc[2].amount, 20000)
            missing_traded = backup.drop(index=3)
            with self.assertRaisesRegex(RuntimeError, "不能插值或丢弃"):
                prep.repair_minute_days("600001", primary, raw, reference, missing_traded)

    def test_fetcher_repairs_an_invalid_cached_day_and_logs_cached_completion(self):
        from unittest.mock import Mock
        good = self.minute_day()
        bad = good.copy(); bad.loc[2, "amount"] = 20000
        response = bad.copy()
        response["time"] = response.time.dt.strftime("%Y%m%d%H%M%S000")
        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_history_k_data_plus=Mock(return_value=object()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bars = root / "bars"; raw = root / "raw"
            prep._atomic_csv(bad, bars / "600001.csv")
            with (patch.dict(sys.modules, {"baostock": fake}),
                  patch.object(prep, "_bs_rows", return_value=response),
                  patch("factor_lab.sina_minute_frame", return_value=good)):
                _, failures = prep._fetch_minute_chunk([("600001", "2026-09-22")], "2026-09-22", bars,
                    {"600001": ["2026-09-22"]}, raw, {"600001": {"2026-09-22": 480000}})
            self.assertEqual(failures, [])
            fake.query_history_k_data_plus.assert_not_called()  # The verified alternate fixes the cache directly.
            fake.query_history_k_data_plus.reset_mock()
            output = io.StringIO()
            with patch.dict(sys.modules, {"baostock": fake}), redirect_stdout(output):
                _, failures = prep._fetch_minute_chunk([("600001", "2026-09-22")], "2026-09-22", bars,
                    {"600001": ["2026-09-22"]}, raw)
            fake.query_history_k_data_plus.assert_not_called()
            self.assertIn("已检查 1/1", output.getvalue())

    def test_daily_sources_use_same_currency_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            prep._atomic_csv(pd.DataFrame([{"ts_code": "600001.SH", "trade_date": "20260922", "amount": 2}]),
                             raw / "daily" / "20260922.csv")
            prep._atomic_csv(pd.DataFrame([{"ts_code": "600001", "trade_date": "20260922", "amount": 2000}]),
                             raw / "daily-public" / "20260922.csv")
            self.assertEqual(prep._daily_from_cache(["20260922"], raw, "daily").iloc[0].amount,
                             prep._daily_from_cache(["20260922"], raw, "daily-public").iloc[0].amount)

    def test_warmup_is_required_before_each_symbols_first_membership(self):
        universe = pd.DataFrame([
            {"symbol": "600001", "day": "2025-09-04", "known_at": "2025-09-03 18:00:00+08:00"},
            {"symbol": "002463", "day": "2025-09-08", "known_at": "2025-09-05 18:00:00+08:00"},
            {"symbol": "002463", "day": "2025-09-09", "known_at": "2025-09-08 18:00:00+08:00"},
        ])
        self.assertEqual(prep.first_warmup_days(universe.sample(frac=1, random_state=1)),
                         {"600001": "2025-09-03", "002463": "2025-09-05"})

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
            rows = [[f"sh.{symbol}", "1"] for symbol in symbols]
            rows += [["sh.000003", "1"], ["sh.000004", "1"], ["sz.000004", "1"], ["sz.399001", "1"]]
            return Result(rows, ["code", "tradeStatus"])

        def history(code, fields, start_date, end_date, frequency, adjustflag):
            if code == "sz.000003":
                raise AssertionError("上证指数不能作为深市股票下载")
            symbol = code[-6:]
            rows = [[day, code, "1000", "1"] for day in ("2026-09-21", "2026-09-22")
                    if not (missing["value"] and symbol == symbols[0] and day == "2026-09-22")]
            return Result(rows, ["date", "code", "amount", "tradestatus"])

        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
                               query_all_stock=all_stock, query_history_k_data_plus=history)
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "source-cache"
            basic = pd.DataFrame([{"symbol": symbol, "list_date": "20200101", "delist_date": ""}
                                  for symbol in symbols + ["000003", "000004"]])
            with (patch.object(prep, "fetch_calendar_baostock", return_value=calendar),
                  patch.object(prep, "fetch_stock_basic_baostock", return_value=basic),
                  patch.object(prep, "_check_akshare_calendar", return_value="AKShare checked"),
                  patch.dict(sys.modules, {"baostock": fake})):
                days, source = prep.collect_public("2026-09-22", "2026-09-22", raw)
                self.assertEqual(days, ["20260922"])
                self.assertIn("AKShare checked", source)
                self.assertEqual(len(prep._daily_from_cache(["20260921"], raw, "daily-public")), 42)
                prep._atomic_csv(basic[basic.symbol.ne(symbols[0])], raw / "stock_basic_baostock.csv")
                with self.assertRaisesRegex(RuntimeError, "不能按现存资料筛掉历史股票"):
                    prep.collect_public("2026-09-22", "2026-09-22", raw)
                prep._atomic_csv(basic, raw / "stock_basic_baostock.csv")
                (raw / "daily-public-symbols" / f"{symbols[0]}_20260921_20260922.csv").unlink()
                missing["value"] = True
                with self.assertRaisesRegex(RuntimeError, "正常交易股票缺少日线"):
                    prep.collect_public("2026-09-22", "2026-09-22", raw)

    def test_public_daily_accepts_blank_turnover_only_for_suspended_stock(self):
        history = pd.DataFrame([
            {"date": "2026-04-27", "code": "sz.000004", "amount": "90590077.6700", "tradestatus": "1"},
            {"date": "2026-04-28", "code": "sz.000004", "amount": "", "tradestatus": "0"},
        ])
        result = prep._validated_public_daily(history, "000004")
        self.assertEqual(result.amount.tolist(), [90590077.67, 0.0])
        self.assertEqual(result.tradestatus.tolist(), ["1", "0"])
        history.loc[1, "tradestatus"] = "1"
        with self.assertRaisesRegex(RuntimeError, "日线成交额或交易状态异常"):
            prep._validated_public_daily(history, "000004")

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

    def test_missing_daily_status_cannot_be_imputed_as_suspension(self):
        universe = pd.DataFrame([{"day": "2026-09-22", "symbol": "600001"}])
        basic = pd.DataFrame([{"symbol": "600001", "list_date": "20200101", "delist_date": ""}])
        actions = pd.DataFrame(columns=["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio"])
        status = pd.DataFrame(columns=["date", "code", "preclose", "high", "low", "tradestatus", "isST"])
        with self.assertRaisesRegex(RuntimeError, "不能将缺失数据当作停牌"):
            prep.construct_market_state_baostock(universe, basic, status, actions)
        status.loc[0] = ["2026-09-22", "sh.600001", "", "", "", "0", "0"]
        result = prep.construct_market_state_baostock(universe, basic, status, actions)
        self.assertTrue(result.iloc[0].is_suspended)

    def test_baostock_actions_keep_actual_bonus_listing_date(self):
        source = pd.DataFrame([{"code": "sh.600001", "dividOperateDate": "2026-05-05",
            "dividPayDate": "2026-05-05", "dividCashPsBeforeTax": "0.2", "dividStocksPs": "0",
            "dividReserveToStockPs": "0.1", "dividStockMarketDate": "2026-05-08"}])
        fake = SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"baostock": fake}):
            raw = Path(directory)
            prep._atomic_csv(source, raw / "actions" / "600001.csv")
            result = prep.fetch_actions_baostock(["600001"], [2026], raw)
            self.assertEqual(result.iloc[0].stock_credit_date, "2026-05-08")
            self.assertAlmostEqual(result.iloc[0].split_ratio, 1.1)
            source.loc[0, "dividStockMarketDate"] = ""
            prep._atomic_csv(source, raw / "actions" / "600001.csv")
            with self.assertRaisesRegex(RuntimeError, "送转股缺少有效上市交易日"):
                prep.fetch_actions_baostock(["600001"], [2026], raw)

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
        warmup_rows = self.minute_day("2026-09-21").to_dict("records")
        with tempfile.TemporaryDirectory() as directory, patch.object(prep, "TOP_N", 1):
            path = Path(directory) / "600001.csv"
            pd.DataFrame(warmup_rows + rows).to_csv(path, index=False)
            daily = pd.DataFrame([{"trade_date": "20260921", "symbol": "600001", "amount": 480000}])
            good = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                       minimum_market_symbols=1)
            self.assertTrue(good["passed"], good["issues"])
            pd.DataFrame(rows).to_csv(path, index=False)
            no_warmup = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                            minimum_market_symbols=1)
            self.assertFalse(no_warmup["passed"])
            self.assertEqual(no_warmup["missing_candidate_bar_days"], 0)
            self.assertEqual(no_warmup["missing_warmup_details"], [{"symbol": "600001",
                "day": "2026-09-21", "first_candidate_day": "2026-09-22"}])
            pd.DataFrame(warmup_rows + rows[:-1]).to_csv(path, index=False)
            bad = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                      minimum_market_symbols=1)
            self.assertFalse(bad["passed"])
            self.assertEqual(bad["missing_candidate_bar_days"], 1)
            broken = pd.DataFrame(warmup_rows + rows)
            broken.loc[50, "amount"] = 20000
            broken.loc[51, "time"] = broken.loc[50, "time"]
            broken.to_csv(path, index=False)
            bad = prep.quality_report(universe, state, Path(directory), daily, minimum_days=1,
                                      minimum_market_symbols=1)
            self.assertFalse(bad["passed"])
            self.assertEqual(len(bad["minute_data_issues"]), 1)
            self.assertIn("600001", " ".join(bad["issues"]))
            self.assertIn("2026-09-22", " ".join(bad["issues"]))
            self.assertEqual({item["reason"] for item in bad["minute_data_issues"]["600001"]},
                             {"时间戳重复", "成交均价超出 K 线高低价"})
            self.assertEqual(bad["missing_candidate_bar_days"], 1)


if __name__ == "__main__":
    unittest.main()
