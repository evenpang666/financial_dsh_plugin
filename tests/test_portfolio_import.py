import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stock_data_server as server

class PortfolioImportTests(unittest.TestCase):
    def setUp(self):
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

    def test_invalid_record_aborts_whole_batch_without_touching_cache(self):
        original = self.path.read_bytes()
        for quantity in (True,0,-1,1.5,float("nan"),float("inf"),"100"):
            with self.assertRaises(ValueError):
                server.import_portfolio({"holdings":[{"market":"a","symbol":"600519","shares":100},{"market":"hk","symbol":"00700","shares":quantity}]})
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
