import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stock_data_server as server

class PortfolioImportTests(unittest.TestCase):
    def test_report_stock_history_accepts_non_holdings_without_writing_portfolio(self):
        before = self.path.read_bytes()
        with patch.object(server, "portfolio_history_data", return_value={"available": True}) as load:
            server.stock_history_response("a", "002946", 5)
            self.assertEqual(load.call_args.args[0:2], ("a", "002946"))
            self.assertEqual(load.call_args.args[-1], 5)
            server.stock_history_response("hk", "700")
            self.assertEqual(load.call_args.args[0:2], ("hk", "00700"))
            server.stock_history_response("us", "BRK.B")
            self.assertEqual(load.call_args.args[0:2], ("us", "BRK-B"))
            for market, symbol in [("a", "report600519"), ("a", "00700"), ("hk", "00000"), ("us", "../secret"), ("invalid", "AAPL")]:
                with self.assertRaises(ValueError):
                    server.stock_history_response(market, symbol)
            with self.assertRaises(ValueError):
                server.stock_history_response("a", "002946", 20)
            self.assertEqual(load.call_count, 3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_five_year_history_is_lazy_separate_and_retains_more_daily_bars(self):
        dates=pd.bdate_range(end="2026-11-02",periods=1400)
        frame=pd.DataFrame({"date":dates,"open":10,"high":12,"low":9,"close":11,"volume":123})
        with patch.object(server,"history",return_value=frame) as fetch:
            recent=server.portfolio_history_data("a","600519","2026-11-02")
            extended=server.portfolio_history_data("a","600519","2026-11-02",5)
            self.assertEqual(len(recent["points"]),260);self.assertEqual(len(extended["points"]),1300)
            self.assertEqual(extended["history_years"],5);self.assertEqual(extended["history_through"],"2026-11-02")
            self.assertLess(extended["history_from"],recent["history_from"])
            server.portfolio_history_data("a","600519","2026-11-02",5)
            self.assertEqual(fetch.call_count,2)
        with self.assertRaises(ValueError):server.portfolio_history_data("a","600519","2026-11-02",20)

    def test_holding_history_preserves_actual_ohlcv_and_rejects_invalid_wicks(self):
        frame = pd.DataFrame({"date":["2026-10-29","2026-10-30","2026-11-02"],
                              "open":[10,11,12],"close":[11,12,13],"high":[12,10,14],"low":[9,10,11],"volume":[123456,0,765432]})
        frame.attrs.update(source="AKShare stock_zh_a_daily (Sina fallback)",volume_unit="股")
        with patch.object(server,"history",return_value=frame):
            result=server.portfolio_history_data("a","600519","2026-11-02")
        self.assertEqual(result["volume_unit"],"股")
        self.assertEqual(result["points"][0],{"date":"2026-10-29","open":10,"close":11,"high":12,"low":9,"volume":123456})
        self.assertIsNone(result["points"][1]["open"])
        self.assertIsNone(result["points"][1]["high"])
        self.assertIsNone(result["points"][1]["low"])
        self.assertEqual(result["points"][1]["close"],12)
        self.assertEqual(result["points"][1]["volume"],0)
        self.assertEqual(result["points"][2]["volume"],765432)

    def test_concurrent_holding_history_serializes_native_sina_fallback(self):
        active = {"count":0,"peak":0}
        def sina(**kwargs):
            active["count"]+=1;active["peak"]=max(active["peak"],active["count"])
            try:
                time.sleep(.02)
                return pd.DataFrame({"date":["2026-11-01","2026-11-02"],"close":[10,11]})
            finally:
                active["count"]-=1
        with patch.object(server.ak,"stock_zh_a_hist",side_effect=OSError("primary unavailable")), patch.object(server.ak,"stock_zh_a_daily",side_effect=sina):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:server.history("600519","2026-11-01","2026-11-02","qfq","a"),range(6)))
        self.assertEqual(len(results),6);self.assertEqual(active["peak"],1)

    def test_holding_quote_batch_checks_identity_timestamp_and_daily_change(self):
        self.batch_patch.stop()
        def quote(token, symbol, price, previous, change, moment):
            fields=[""]*33
            fields[2],fields[3],fields[4],fields[30],fields[32]=symbol,str(price),str(previous),moment,str(change)
            return f'v_{token}="'+"~".join(fields)+'";'
        payload=quote("sh600519","600519",110,100,10,"20261103140000")+quote("sz000858","WRONG",90,100,-10,"20261103140000")+quote("sh600000","600000",110,100,-5,"20261103140000")
        response=SimpleNamespace(text=payload,raise_for_status=lambda:None)
        with patch.object(server.requests,"get",return_value=response) as fetch:
            result=server.portfolio_quote_batch([{"symbol":"600519"},{"symbol":"000858"},{"symbol":"600000"}],"a")
        self.assertEqual(set(result),{"600519"})
        self.assertEqual(result["600519"]["change_pct"],10)
        self.assertEqual(result["600519"]["as_of"],"2026-11-03T14:00:00+08:00")
        self.assertIn("sh600519,sz000858,sh600000",fetch.call_args.args[0])

    def test_holding_quotes_use_one_batch_and_fall_back_only_for_missing_symbols(self):
        cached={"symbol":"600519","price":100,"change_pct":1,"as_of":"2026-11-03T14:00:00+08:00"}
        with patch.object(server,"portfolio_quote_batch",return_value={"600519":cached}) as batch, patch.object(server,"quote_response",return_value={"quote":{"最新价":200,"涨跌幅":-2}}) as fallback:
            result=server.portfolio_quotes_response("a")
        self.assertEqual(batch.call_count,1)
        fallback.assert_called_once_with("000858","a")
        self.assertEqual(result["quotes"][0],cached)
        self.assertEqual(result["quotes"][1]["change_pct"],-2)

    def test_holding_history_cleans_prices_excludes_partial_day_and_caches_by_market(self):
        frame = pd.DataFrame({"date": pd.to_datetime(["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-01", "2026-11-02", "2026-11-03"]).tz_localize("Asia/Shanghai"),
                              "close": [10, 11, float("nan"), -1, 12, 99], "volume": [100, 0, 20, 20, float("nan"), 100]})
        frame.attrs["source"] = "test source"
        with patch.object(server, "history", return_value=frame) as fetch:
            result = server.portfolio_history_data("a", "600519", "2026-11-02")
            self.assertEqual(result["as_of"], "2026-11-02")
            self.assertEqual([row["date"] for row in result["points"]], ["2026-10-28", "2026-10-29", "2026-11-02"])
            self.assertEqual([row["close"] for row in result["points"]], [10, 11, 12])
            self.assertIsNone(result["points"][-1]["volume"])
            self.assertEqual(result["points"][1]["volume"], 0)
            self.assertEqual((result["currency"],result["adjust"],result["source"]), ("CNY","qfq","test source"))
            server.portfolio_history_data("a", "600519", "2026-11-02")
            self.assertEqual(fetch.call_count, 1)
            server.portfolio_history_data("hk", "00700", "2026-11-02")
            self.assertEqual(fetch.call_count, 2)

    def test_holding_history_checks_saved_market_identity_before_using_cached_history(self):
        with patch.object(server, "portfolio_history_data", return_value={"available":True}) as fetch:
            server.portfolio_history_response("a", "600519")
            self.assertEqual(fetch.call_count, 1)
            with self.assertRaisesRegex(ValueError, "已保存持仓"):
                server.portfolio_history_response("hk", "00700")
            server.delete_portfolio_holding({"market":"a", "symbol":"600519"})
            with self.assertRaisesRegex(ValueError, "已保存持仓"):
                server.portfolio_history_response("a", "600519")
            self.assertEqual(fetch.call_count, 1)

    def test_holding_history_never_fabricates_a_line_with_insufficient_prices(self):
        with patch.object(server, "history", return_value=pd.DataFrame({"date":["2026-11-02"],"close":[10]})):
            with self.assertRaisesRegex(ValueError, "不足两条"):
                server.portfolio_history_data("us", "AAPL", "2026-11-02")

    def test_holding_quotes_keep_market_identity_and_partial_failures(self):
        holdings = [{"symbol": "600519"}, {"market": "hk", "symbol": "00700"}, {"symbol": "000858"}]
        def quote(symbol, market):
            if symbol == "000858":
                raise ValueError("行情暂不可用")
            return {"quote": {"最新价": 100, "涨跌幅": 1.25}, "as_of": "2026-11-03 10:00", "source": "test"}
        with patch.object(server, "portfolio_response", return_value={"holdings": holdings}), patch.object(server, "quote_response", side_effect=quote) as load:
            result = server.portfolio_quotes_response("a")
        self.assertEqual(load.call_count, 2)
        self.assertEqual([row["symbol"] for row in result["quotes"]], ["600519", "000858"])
        self.assertEqual(result["quotes"][0]["price"], 100)
        self.assertEqual(result["quotes"][0]["change_pct"], 1.25)
        self.assertIsNone(result["quotes"][1]["price"])
        self.assertIn("error", result["quotes"][1])

    def setUp(self):
        server.portfolio_history_data.cache_clear()
        self.batch_patch = patch.object(server, "portfolio_quote_batch", return_value={})
        self.batch_patch.start()
        self.addCleanup(self.batch_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "portfolio.json"
        self.patch = patch.object(server, "PORTFOLIO_FILE", self.path)
        self.patch.start()
        server.save_portfolio({"holdings": [{"symbol":"600519","shares":20,"cost_basis":1000,"note":"保留原备注"},{"symbol":"000858","shares":30}]})

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_mixed_import_updates_in_place_preserves_other_holdings_and_is_idempotent(self):
        incoming = {"holdings":[{"market":"a","symbol":"600519","shares":100},{"market":"hk","symbol":"00700","shares":200,"name":"腾讯控股"}]}
        result = server.import_portfolio(incoming)
        self.assertEqual((result["added"],result["updated"]),(1,1))
        rows = {row["symbol"]:row for row in result["holdings"]}
        self.assertEqual(rows["600519"]["cost_basis"],1000)
        self.assertEqual(rows["600519"]["note"],"保留原备注")
        self.assertEqual(rows["000858"]["shares"],30)
        self.assertEqual(rows["00700"]["market"],"hk")
        self.assertEqual(server.import_portfolio(incoming)["holdings"],result["holdings"])
        partial = server.import_portfolio({"holdings":[{"market":"a","symbol":"600519","shares":125}]})
        self.assertEqual((partial["added"], partial["updated"]), (0, 1))
        self.assertEqual({row["symbol"]: row["shares"] for row in partial["holdings"]}, {"600519":125.0,"000858":30.0,"00700":200.0})

    def test_invalid_record_aborts_whole_batch_without_touching_cache(self):
        original = self.path.read_bytes()
        for quantity in (True,0,-1,1.5,float("nan"),float("inf"),"100"):
            with self.assertRaises(ValueError):
                server.import_portfolio({"holdings":[{"market":"a","symbol":"600519","shares":100},{"market":"hk","symbol":"00700","shares":quantity}]})
            self.assertEqual(self.path.read_bytes(),original)

    def test_edit_each_market_preserves_metadata_and_other_positions(self):
        server.import_portfolio({"holdings":[{"market":"hk","symbol":"00700","shares":200,"name":"腾讯控股"}]})
        for market, symbol, quantity in (("a","600519",125),("hk","00700",345)):
            before = server.portfolio_response()["holdings"]
            result = server.update_portfolio_holding({"market":market,"symbol":symbol,"shares":quantity,"name":"不应替换","cost_basis":0})
            expected = [{**row,"shares":quantity} if (row.get("market","a"),row["symbol"]) == (market,symbol) else row for row in before]
            self.assertEqual(result["holdings"],expected)
            self.assertEqual(server.portfolio_response()["holdings"],expected)

    def test_delete_both_markets_and_last_position(self):
        server.save_portfolio({"holdings":[{"symbol":"000001","shares":100},{"market":"hk","symbol":"00001","shares":200}]})
        result = server.delete_portfolio_holding({"market":"hk","symbol":"00001"})
        self.assertEqual(result["holdings"],[{"symbol":"000001","shares":100.0}])
        self.assertEqual(server.delete_portfolio_holding({"market":"a","symbol":"000001"})["holdings"],[])
        self.assertEqual(server.portfolio_response()["holdings"],[])

    def test_invalid_edit_or_identity_leaves_cache_unchanged(self):
        original = self.path.read_bytes()
        for quantity in (None,True,0,-1,1.5,float("nan"),float("inf"),"100",1_000_000_000_001):
            with self.assertRaises(ValueError):
                server.update_portfolio_holding({"market":"a","symbol":"600519","shares":quantity})
            self.assertEqual(self.path.read_bytes(),original)
        for identity in ({"symbol":"600519"},{"market":"hk","symbol":"600519"},{"market":"a","symbol":"000000"},{"market":"a","symbol":"６００５１９"}):
            for operation in (server.update_portfolio_holding,server.delete_portfolio_holding):
                with self.assertRaises(ValueError):
                    operation({**identity,"shares":100})
                self.assertEqual(self.path.read_bytes(),original)

    def test_stale_or_wrong_market_identity_never_upserts_or_deletes_another_stock(self):
        original = self.path.read_bytes()
        for identity in ({"market":"hk","symbol":"00700"},{"market":"a","symbol":"000700"}):
            for operation in (server.update_portfolio_holding,server.delete_portfolio_holding):
                with self.assertRaisesRegex(ValueError,"已不存在"):
                    operation({**identity,"shares":100})
                self.assertEqual(self.path.read_bytes(),original)

    def test_name_lookup_is_batched_by_market_and_preview_does_not_write(self):
        original = self.path.read_bytes()
        frames = {"a":(pd.DataFrame({"code":["600519","000858"],"name":["贵州茅台","五粮液"]}),"code","name","test"),"hk":(pd.DataFrame({"code":["00700"],"name":["腾讯控股"]}),"code","name","test")}
        with patch.object(server,"stock_catalog",side_effect=lambda market:frames[market]) as catalog:
            result = server.resolve_portfolio({"holdings":[{"market":"a","symbol":"600519","shares":100},{"market":"a","symbol":"000858","shares":200},{"market":"hk","symbol":"00700","shares":300}]})
        self.assertEqual(catalog.call_count,2)
        self.assertEqual([row["name"] for row in result["holdings"]],["贵州茅台","五粮液","腾讯控股"])
        self.assertEqual(self.path.read_bytes(),original)

    def test_unavailable_catalog_keeps_user_codes_and_never_invents_names(self):
        with patch.object(server,"stock_catalog",side_effect=RuntimeError("offline")), patch.object(server,"portfolio_name_fallback",return_value=None):
            result = server.resolve_portfolio({"holdings":[{"market":"hk","symbol":"00700","shares":100,"name":"模型编造名称"}]})
        self.assertNotIn("name",result["holdings"][0])
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["holdings"][0]["shares"],100)

    def test_hk_name_fallback_must_match_requested_symbol(self):
        class Response:
            text = 'v_hk00700="100~腾讯控股~00700~0";'
            def raise_for_status(self): pass
        with patch.object(server.requests,"get",return_value=Response()):
            self.assertEqual(server.portfolio_name_fallback("00700","hk"),"腾讯控股")
            self.assertIsNone(server.portfolio_name_fallback("00981","hk"))

    def test_hk_import_resolves_from_backup_without_saving_during_lookup(self):
        original = self.path.read_bytes()
        with patch.object(server,"stock_catalog",side_effect=RuntimeError("offline")), patch.object(server,"portfolio_name_fallback",return_value="腾讯控股"):
            result = server.resolve_portfolio({"holdings":[{"market":"hk","symbol":"00700","shares":200}]})
        self.assertEqual(result["holdings"][0]["name"],"腾讯控股")
        self.assertEqual(result["unavailable"],[])
        self.assertEqual(self.path.read_bytes(),original)

if __name__ == "__main__":
    unittest.main()
