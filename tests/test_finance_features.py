import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


class FakeAkshare(types.ModuleType):
    __version__ = "test"

    def stock_info_a_code_name(self):
        return pd.DataFrame([
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000858", "name": "五粮液"},
            {"code": "600809", "name": "山西汾酒"},
        ])

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": ["2026-09-11", "2026-09-14"]})

    def stock_zh_a_spot_tx(self):
        return pd.DataFrame([
            {"code": "600519", "name": "贵州茅台", "zxj": 1500, "zdf": 2, "zdf_d5": 4, "zdf_d20": 8, "hsl": 2, "pe_ttm": 25, "zsz": 100},
            {"code": "000858", "name": "五粮液", "zxj": 120, "zdf": 1, "zdf_d5": 2, "zdf_d20": 3, "hsl": 3, "pe_ttm": 20, "zsz": 90},
            {"code": "000001", "name": "ST测试", "zxj": 5, "zdf": 8, "zdf_d5": 10, "zdf_d20": 20, "hsl": 5, "pe_ttm": 10, "zsz": 10},
        ])


sys.modules["akshare"] = FakeAkshare("akshare")
spec = importlib.util.spec_from_file_location("stock_data_server", ROOT / "scripts" / "stock_data_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FinanceFeatureTests(unittest.TestCase):
    def test_popularity_reads_only_rank_api_without_quote_enrichment(self):
        response = types.SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"data": [{"sc": "SZ000858", "rk": 2}, {"sc": "SH600519", "rk": 1}]})
        with (patch.object(server.requests, "post", return_value=response) as post,
              patch.object(server.ak, "stock_hot_rank_em", create=True, side_effect=AssertionError("不应调用行情补全"))):
            result = server.social_sentiment_response({"score": 30, "limit_up_count": 2, "limit_down_count": 5}, "a")
        self.assertEqual([row["代码"] for row in result["hot_stocks"]], ["600519", "000858"])
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 10))
        self.assertEqual(len(result["unavailable"]), 0)

    def test_popularity_proxy_failure_keeps_price_proxy_and_short_diagnostics(self):
        with patch.object(server.requests, "post", side_effect=server.requests.exceptions.ProxyError("HTTPSConnectionPool Unable to connect to proxy https://long.invalid")):
            result = server.social_sentiment_response({"score": 30, "limit_up_count": 2, "limit_down_count": 5}, "a")
        self.assertEqual(result["score"], 32)
        self.assertEqual(result["hot_stocks"], [])
        self.assertIn("代理", result["unavailable"][0])
        self.assertNotIn("HTTPSConnectionPool", result["unavailable"][0])

    def test_portfolio_shares_one_panorama_failure_and_keeps_three_comparison_columns(self):
        holdings = [{"symbol": "600519"}, {"symbol": "000858"}]
        analysis = {"name": "测试标的", "as_of": "2026-10-12", "quote": {"quote": {"最新价": 10}},
                    "recommendation": {"conclusion": "建议持有观察", "score_components": {"valid_signal_count": 4}},
                    "unavailable": ["市场全景：东方财富人气榜不可用：HTTPSConnectionPool ProxyError"]}
        with (patch.object(server, "portfolio_response", return_value={"holdings": holdings}),
              patch.object(server, "market_panorama_response", side_effect=RuntimeError("offline")) as panorama,
              patch.object(server, "analyze_stock_response", return_value=analysis) as analyze,
              patch("portfolio_research.strategy_review", return_value={"available": False, "rows": {}})):
            result = server.portfolio_analysis_response("a", "momentum")
        panorama.assert_called_once()
        self.assertTrue(all(call.args[2] is not None for call in analyze.call_args_list))
        self.assertEqual(len(result["position_summary"]["rows"]), 2)
        self.assertIn("suggestion3", result["position_summary"]["rows"][0])
        self.assertNotIn("HTTPSConnectionPool", str(result["holdings"]))
    def test_daily_digest_includes_flats_and_summarizes_missing_sources(self):
        lines = server.daily_digest_markdown(
            {"market_breadth": {"advancing": 20, "declining": 30, "unchanged": 50}},
            {"ranking": []}, {"events": []}, ["全球指数不可用：long diagnostic URL"],
        )
        digest = "\n".join(lines)
        self.assertIn("上涨占比 20.0%", digest)
        self.assertIn("含平盘", digest)
        self.assertIn("全球指数不可用", digest)
        self.assertNotIn("long diagnostic URL", digest)
        self.assertIn("采用默认输入", digest)
        self.assertIn("暂不计算上涨占比", "\n".join(server.daily_digest_markdown({}, {}, {}, [])))

    def setUp(self):
        self._data_tmp = tempfile.TemporaryDirectory()
        root = Path(self._data_tmp.name)
        self._snapshot_patch = patch.object(server, "REPORT_SNAPSHOT_DIR", root / "snapshots")
        self._reflection_patch = patch.object(server, "REFLECTION_FILE", root / "reflections.json")
        self._snapshot_patch.start()
        self._reflection_patch.start()
        self._attention_http_patch = patch.object(server.requests, "post", side_effect=RuntimeError("测试关注榜未接入"))
        self._attention_http_patch.start()

    def tearDown(self):
        self._attention_http_patch.stop()
        self._reflection_patch.stop()
        self._snapshot_patch.stop()
        self._data_tmp.cleanup()

    def test_daily_report_without_holdings_keeps_panorama_and_candidates(self):
        panorama = {
            "market_state": {"summary": "中性市场", "risk_level": "中", "reference_position": "40%–60%", "buy_tendency_adjustment": 0},
            "market_breadth": {"label": "均衡", "advancing": 2000, "declining": 2000, "score": 50},
            "social_sentiment": {"label": "中性", "score": 50},
            "international_events": [], "domestic_policies": [], "unavailable": [],
        }
        ranking = {"ranking": [], "method": "测试评分"}
        with patch.object(server, "portfolio_response", return_value={"holdings": []}), \
             patch.object(server, "trading_day_response", return_value={"is_trading_day": True, "source": "test"}), \
             patch.object(server, "market_panorama_response", return_value=panorama), \
             patch.object(server, "market_events_response", return_value={"events": []}), \
             patch.object(server, "candidate_ranking_response", return_value=ranking), \
             patch.object(server, "analyze_stock_response") as analyze:
            result = server.daily_report_response(5)
        self.assertTrue(result["is_trading_day"])
        self.assertFalse(result["has_holdings"])
        self.assertIn("市场全景", result["markdown"])
        self.assertIn("热门股票推荐排行", result["markdown"])
        self.assertIn("## 本期速览", result["markdown"])
        self.assertNotIn("已持仓股票", result["markdown"])
        analyze.assert_not_called()

    def test_stock_search_prefers_exact_symbol(self):
        result = server.stock_search_response("600519", 10)
        self.assertEqual(result["matches"][0], {"symbol": "600519", "name": "贵州茅台"})

    def test_trading_calendar(self):
        self.assertTrue(server.trading_day_response("2026-09-14")["is_trading_day"])
        self.assertFalse(server.trading_day_response("2026-09-13")["is_trading_day"])

    def test_candidate_ranking_is_ranked_and_excludes_st(self):
        result = server.candidate_ranking_response(5)
        self.assertEqual([item["symbol"] for item in result["ranking"]], ["600519", "000858"])
        self.assertEqual(result["ranking"][0]["rank"], 1)
        self.assertEqual(result["ranking"][0]["buy_percentage"] + result["ranking"][0]["sell_percentage"], 100)
        self.assertIn(result["ranking"][0]["recommendation"], {"建议买入", "建议持有观察", "建议卖出"})

    def test_candidate_ranking_applies_persisted_reflection_adjustment(self):
        server.persist_reflections([{
            "date": "2026-09-14", "market": "a", "symbol": "600519",
            "morning_conclusion": "建议买入", "alignment": "contradicted", "created_at": "2026-09-14T14:30:00+08:00",
        }])
        panorama = {"market_state": {"buy_tendency_adjustment": 0}, "unavailable": []}
        result = server.candidate_ranking_response(5, panorama, "a")
        item = next(row for row in result["ranking"] if row["symbol"] == "600519")
        self.assertEqual(item["reflection_adjustment"], -3)
        self.assertEqual(item["base_buy_percentage"], round(item["raw_score"] - 3))
        self.assertEqual(item["reflection_context"]["contradicted"], 1)

    def test_market_breadth_and_risk_calibration_are_auditable(self):
        frame = pd.DataFrame([
            {"zdf": -2, "zdf_d5": -4, "zdf_d20": -8},
            {"zdf": -1, "zdf_d5": -2, "zdf_d20": -3},
            {"zdf": 0, "zdf_d5": -1, "zdf_d20": -2},
            {"zdf": 1, "zdf_d5": 1, "zdf_d20": -1},
        ])
        breadth = server.market_breadth_response(frame)
        self.assertEqual(breadth["advancing"], 1)
        self.assertEqual(breadth["declining"], 2)
        self.assertLess(breadth["score"], 50)
        calibrated = server.calibrate_buy_sell(70, {"market_state": {"buy_tendency_adjustment": -12}})
        self.assertEqual(calibrated["base_buy_percentage"], 70)
        self.assertEqual(calibrated["buy_percentage"], 58)
        self.assertEqual(calibrated["buy_percentage"] + calibrated["sell_percentage"], 100)

    def test_market_panorama_splits_events_and_sets_risk_position(self):
        events = {"events": [
            {"date": "2026-09-14", "title": "美联储加息并提示衰退风险"},
            {"date": "2026-09-14", "title": "国务院印发支持消费政策"},
        ]}
        breadth = {"score": 20, "label": "弱势扩散", "advancing": 100, "declining": 400, "limit_up_count": 2, "limit_down_count": 20}
        sentiment = {"score": 25, "label": "偏谨慎", "hot_stocks": [], "unavailable": []}
        with patch.object(server, "market_breadth_response", return_value=breadth), \
             patch.object(server, "social_sentiment_response", return_value=sentiment), \
             patch.object(server, "market_events_response", return_value=events):
            result = server.market_panorama_response()
        self.assertEqual(len(result["international_events"]), 1)
        self.assertEqual(len(result["domestic_policies"]), 1)
        self.assertEqual(result["market_state"]["risk_level"], "极高")
        self.assertEqual(result["market_state"]["reference_position"], "0%–20%")
        self.assertEqual(result["market_state"]["buy_tendency_adjustment"], -20)

    def test_market_context_exposes_partial_coverage(self):
        panorama = {
            "as_of": "2026-09-14", "source": "test", "event_window": {"days": 7, "start": "2026-09-08", "end": "2026-09-14"},
            "international_events": [], "domestic_policies": [],
            "market_breadth": {"as_of": "2026-09-14", "score": 50},
            "social_sentiment": {"as_of": "2026-09-14", "hot_stocks": [], "note": "价格代理"},
            "unavailable": ["事件与政策不可用：timeout", "东方财富人气榜不可用：timeout"],
        }
        context = server.market_context_summary(panorama)
        self.assertFalse(context["complete"])
        self.assertEqual(context["coverage"]["international_events"]["status"], "unavailable")
        self.assertEqual(context["coverage"]["domestic_policies"]["status"], "unavailable")
        self.assertEqual(context["coverage"]["social_attention"]["status"], "proxy_only")
        self.assertEqual(context["event_window"]["end"], "2026-09-14")

    def test_related_enterprises_selects_industry_leaders_and_loads_announcements(self):
        info = pd.DataFrame([{"item": "股票简称", "value": "贵州茅台"}, {"item": "行业", "value": "酿酒行业"}])
        constituents = pd.DataFrame([
            {"代码": "600519", "名称": "贵州茅台"}, {"代码": "000858", "名称": "五粮液"}, {"代码": "600809", "名称": "山西汾酒"},
        ])
        spot = pd.DataFrame([
            {"代码": "600519", "名称": "贵州茅台", "总市值": 2000},
            {"代码": "000858", "名称": "五粮液", "总市值": 1000},
            {"代码": "600809", "名称": "山西汾酒", "总市值": 800},
        ])
        announcement = {"as_of": "2026-09-14", "announcements": [{"公告标题": "半年报"}]}
        with patch.object(server.ak, "stock_individual_info_em", create=True, return_value=info), \
             patch.object(server.ak, "stock_board_industry_cons_em", create=True, return_value=constituents), \
             patch.object(server.ak, "stock_zh_a_spot_em", create=True, return_value=spot), \
             patch.object(server, "announcements_response", return_value=announcement) as announcements:
            result = server.related_enterprises_response("600519", 30, 2)
        self.assertEqual(result["industry"], "酿酒行业")
        self.assertEqual([item["symbol"] for item in result["enterprises"]], ["000858", "600809"])
        self.assertEqual(result["enterprises"][0]["announcements"][0]["公告标题"], "半年报")
        self.assertEqual(announcements.call_count, 2)

    def test_analysis_has_short_and_long_horizons(self):
        with patch.object(server, "quote_response", return_value={"as_of": "2026-09-11", "quote": {"名称": "贵州茅台"}}), \
             patch.object(server, "indicators_response", return_value={"as_of": "2026-09-11", "latest": {"close": 110, "ma20": 100, "ma60": 90, "rsi14": 60}}), \
             patch.object(server, "fundamentals_response", return_value={"latest_report_period": "2026-06-30", "records": [{"净资产收益率": 15, "营业收入增长率": 8, "净利润增长率": 9}]}), \
             patch.object(server, "valuation_response", return_value={"valuation": {"市盈率-动态": 25, "市净率": 4}}), \
             patch.object(server, "announcements_response", return_value={"announcements": []}):
            result = server.analyze_stock_response("600519", panorama={
                "as_of": "2026-09-14", "market_state": {"risk_level": "高", "buy_tendency_adjustment": -12},
                "unavailable": ["事件与政策不可用：timeout"],
            })
        self.assertEqual(result["short_term"]["horizon"], "未来1–4周")
        self.assertEqual(result["long_term"]["horizon"], "未来6–24个月")
        self.assertEqual(result["latest_report_period"], "2026-06-30")
        self.assertEqual(result["recommendation"]["buy_percentage"] + result["recommendation"]["sell_percentage"], 100)
        self.assertEqual(result["recommendation"]["score_components"]["base_buy_percentage"], 82)
        self.assertEqual(result["recommendation"]["buy_percentage"], 70)
        self.assertEqual(result["recommendation"]["score_components"]["market_adjustment"], -12)
        self.assertEqual(result["recommendation"]["conclusion"], "建议买入")
        self.assertFalse(result["market_context"]["complete"])
        self.assertTrue(any("市场全景：事件与政策不可用" in item for item in result["unavailable"]))

    def test_daily_report_contains_required_tables_and_windows(self):
        analysis = {
            "symbol": "600519", "name": "贵州茅台", "as_of": "2026-09-11",
            "short_term": {"action": "短期继续跟踪", "evidence": ["价格高于MA20"]},
            "long_term": {"action": "长期进入观察池", "evidence": ["ROE稳定"]},
            "recommendation": {"buy_percentage": 70, "sell_percentage": 30, "conclusion": "建议买入"},
            "latest_report_period": "2026-06-30", "unavailable": [], "risk_note": "估值波动",
        }
        ranking = {"ranking": [{"rank": 1, "symbol": "000858", "name": "五粮液", "score": 80, "buy_percentage": 80, "sell_percentage": 20, "recommendation": "建议买入", "pct_change_1d": 1, "pct_change_5d": 2, "pct_change_20d": 3, "pe_ttm": 20}], "method": "测试评分"}
        events = {"events": [{"date": "2026-09-11", "title": "测试事件"}]}
        panorama = {
            "market_state": {"summary": "测试市场状态", "risk_level": "高", "reference_position": "20%–40%", "buy_tendency_adjustment": -12},
            "market_breadth": {"label": "偏弱", "advancing": 1000, "declining": 3000, "score": 30},
            "social_sentiment": {"label": "偏谨慎", "score": 35},
            "international_events": [{"date": "2026-09-11", "title": "测试国际事件", "tone": "negative"}],
            "domestic_policies": [{"date": "2026-09-11", "title": "测试国内政策", "tone": "positive"}],
            "unavailable": [],
        }
        with patch.object(server, "trading_day_response", return_value={"is_trading_day": True, "source": "test"}), \
             patch.object(server, "portfolio_response", return_value={"holdings": [{"symbol": "600519"}]}), \
             patch.object(server, "market_panorama_response", return_value=panorama), \
             patch.object(server, "analyze_stock_response", return_value=analysis.copy()), \
             patch.object(server, "global_market_response", return_value={"indices": [], "source": "test"}), \
             patch.object(server, "sector_focus_response", return_value={"boards": [], "missing_themes": [], "source": "test"}), \
             patch.object(server, "market_events_response", return_value=events), \
             patch.object(server, "candidate_ranking_response", return_value=ranking):
            result = server.daily_report_response(5)
        self.assertNotIn("已持仓股票", result["markdown"])
        self.assertEqual(result["portfolio"], [])
        self.assertIn("全球主要指数", result["markdown"])
        self.assertIn("重点板块", result["markdown"])
        self.assertIn("分周期研究观察名单", result["markdown"])
        self.assertIn("最近一月", result["markdown"])
        self.assertIn("热门股票推荐排行", result["markdown"])
        self.assertIn("买入倾向", result["markdown"])
        self.assertIn("卖出倾向", result["markdown"])
        self.assertIn("市场全景", result["markdown"])
        self.assertIn("国际事件", result["markdown"])
        self.assertIn("国内政策", result["markdown"])
        self.assertEqual(result["market_panorama"]["market_state"]["risk_level"], "高")


    def test_intraday_review_records_contradiction_and_updates_feedback(self):
        morning = {
            "as_of": "2026-09-15", "market": "a",
            "market_panorama": {"market_state": {"score": 65}},
            "portfolio": [],
            "candidates": {"ranking": [{
                "symbol": "600519", "name": "贵州茅台", "price": 100,
                "recommendation": "建议买入", "buy_percentage": 72, "reason": "早盘动量偏强",
            }]},
        }
        afternoon = {
            "as_of": "2026-09-15", "market": "a",
            "market_panorama": {"market_state": {"score": 50}},
            "candidates": {"ranking": [{"symbol": "600519", "name": "贵州茅台", "price": 96, "pct_change_1d": -4}]},
        }
        review = server.build_intraday_review(morning, afternoon, "a")
        self.assertEqual(review["contradicted"], 1)
        self.assertEqual(review["comparisons"][0]["return_from_morning_pct"], -4.0)
        self.assertIn("市场风险分", review["comparisons"][0]["reason_analysis"][0])
        feedback = server.reflection_adjustment("600519", "a")
        self.assertEqual(feedback["sample_size"], 1)
        self.assertEqual(feedback["adjustment"], -3)

    def test_afternoon_report_loads_morning_snapshot_and_appends_review(self):
        morning = {
            "as_of": str(server.date.today()), "market": "a", "session": "morning",
            "market_panorama": {"market_state": {"score": 60}}, "portfolio": [],
            "candidates": {"ranking": [{"symbol": "600519", "name": "贵州茅台", "price": 100, "recommendation": "建议买入", "buy_percentage": 70}]},
            "markdown": "# 日报1",
        }
        server.save_report_snapshot(morning)
        panorama = {
            "market_state": {"summary": "盘中转弱", "score": 45, "risk_level": "中", "reference_position": "40%–60%", "buy_tendency_adjustment": 0},
            "market_breadth": {"label": "偏弱", "advancing": 1000, "declining": 3000, "score": 35},
            "social_sentiment": {"label": "偏谨慎", "score": 35},
            "international_events": [], "domestic_policies": [], "unavailable": [],
        }
        ranking = {"ranking": [{"symbol": "600519", "name": "贵州茅台", "price": 96, "pct_change_1d": -4, "rank": 1, "score": 40, "buy_percentage": 40, "sell_percentage": 60, "recommendation": "建议持有观察"}], "method": "测试"}
        with patch.object(server, "trading_day_response", return_value={"is_trading_day": True, "source": "test"}), \
             patch.object(server, "portfolio_response", return_value={"holdings": []}), \
             patch.object(server, "market_panorama_response", return_value=panorama), \
             patch.object(server, "market_events_response", return_value={"events": []}), \
             patch.object(server, "candidate_ranking_response", return_value=ranking):
            report = server.daily_report_response(5, "a", "afternoon")
        self.assertEqual(report["session"], "afternoon")
        self.assertEqual(report["intraday_review"]["contradicted"], 1)
        self.assertIn("日报1与日报2一致性复盘", report["markdown"])
        self.assertIn("明显背离", report["markdown"])

    def test_hk_symbol_normalization_and_mixed_portfolio(self):
        self.assertEqual(server.normalize_hk_symbol("700"), "00700")
        self.assertEqual(server.normalize_hk_symbol("HK:00700"), "00700")
        self.assertEqual(server.normalize_hk_symbol("HKG00700"), "00700")
        self.assertEqual(server.normalize_hk_symbol("00700.HK"), "00700")
        holding = server.normalize_holding({"market": "hk", "symbol": "700", "name": "腾讯控股"})
        self.assertEqual(holding, {"symbol": "00700", "market": "hk", "name": "腾讯控股"})
        with self.assertRaises(ValueError):
            server.normalize_holding({"market": "hk", "symbol": "ABC"})

    def test_hk_search_quote_and_fundamentals_use_hk_sources(self):
        spot = pd.DataFrame([{
            "代码": "00700", "名称": "腾讯控股", "最新价": 500, "涨跌幅": 1.2,
            "成交量": 1000, "成交额": 500000, "市盈率": 22, "市净率": 4, "总市值": 4600000,
        }])
        indicators = pd.DataFrame([{"报告期": "2026-06-30", "股东权益回报率(%)": 18.5, "营业总收入": 1000}])
        with patch.object(server.ak, "stock_hk_spot_em", create=True, return_value=spot), \
             patch.object(server.ak, "stock_hk_financial_indicator_em", create=True, return_value=indicators):
            search = server.stock_search_response("700", 10, "hk")
            quote = server.quote_response("700", "hk")
            fundamentals = server.fundamentals_response("00700", "hk")
        self.assertEqual(search["matches"], [{"symbol": "00700", "name": "腾讯控股"}])
        self.assertEqual(quote["market"], "hk")
        self.assertEqual(quote["quote"]["代码"], "00700")
        self.assertEqual(quote["quote"]["市盈率-动态"], 22)
        self.assertEqual(fundamentals["market"], "hk")
        self.assertEqual(fundamentals["source"], "AKShare stock_hk_financial_indicator_em")
        self.assertEqual(fundamentals["latest_report_period"], "2026-06-30")

    def test_hk_announcements_are_not_sent_to_cninfo(self):
        with self.assertRaisesRegex(ValueError, "HKEX"):
            server.announcements_response("00700", "2026-01-01", "2026-09-15", 20, "hk")

    def test_hk_daily_report_never_reads_holdings(self):
        panorama = {
            "market_state": {"summary": "测试市场状态", "risk_level": "中", "reference_position": "40%–60%", "buy_tendency_adjustment": 0},
            "market_breadth": {"label": "均衡", "advancing": 100, "declining": 100, "score": 50},
            "social_sentiment": {"label": "中性", "score": 50},
            "international_events": [], "domestic_policies": [], "unavailable": [],
        }
        analysis = {
            "symbol": "00700", "market": "hk", "name": "腾讯控股", "as_of": "2026-09-14",
            "short_term": {"action": "短期继续跟踪", "evidence": ["价格高于MA20"]},
            "long_term": {"action": "长期继续观察", "evidence": ["PE可得"]},
            "recommendation": {"buy_percentage": 60, "sell_percentage": 40, "conclusion": "建议持有观察"},
            "latest_report_period": "2026-06-30", "unavailable": [], "risk_note": "汇率风险",
        }
        ranking = {"ranking": [], "method": "测试评分"}
        with patch.object(server, "trading_day_response", return_value={"is_trading_day": True, "source": "test"}) as calendar, \
             patch.object(server, "portfolio_response") as portfolio_call, \
             patch.object(server, "market_panorama_response", return_value=panorama) as panorama_call, \
             patch.object(server, "analyze_stock_response", return_value=analysis) as analyze_call, \
             patch.object(server, "global_market_response", return_value={"indices": [], "source": "test"}), \
             patch.object(server, "sector_focus_response", return_value={"boards": [], "missing_themes": [], "source": "test"}), \
             patch.object(server, "market_events_response", return_value={"events": []}), \
             patch.object(server, "candidate_ranking_response", return_value=ranking):
            result = server.daily_report_response(5, "hk")
        self.assertEqual(result["market"], "hk")
        self.assertEqual(result["portfolio"], [])
        portfolio_call.assert_not_called()
        analyze_call.assert_not_called()
        self.assertIn("# 港股日报1｜开盘前研究简报", result["markdown"])
        calendar.assert_called_once_with(market="hk")
        panorama_call.assert_called_once_with(market="hk")

    def test_global_and_theme_snapshots_keep_source_timestamps_and_missing_themes(self):
        global_frame = pd.DataFrame([
            {"代码": "SPX", "名称": "标普500", "最新价": 6000, "涨跌幅": -1.2, "最新行情时间": "2026-09-22 04:00:00"},
            {"代码": "N225", "名称": "日经225", "最新价": 40000, "涨跌幅": 0.4, "最新行情时间": "2026-09-23 14:00:00"},
        ])
        board_frame = pd.DataFrame([
            {"板块名称": "半导体概念", "涨跌幅": 2.1, "上涨家数": 20, "下跌家数": 10, "领涨股票": "测试股"},
            {"板块名称": "光通信", "涨跌幅": -0.5, "上涨家数": 4, "下跌家数": 8, "领涨股票": "另一股"},
        ])
        with patch.object(server.ak, "index_global_spot_em", create=True, return_value=global_frame), \
             patch.object(server.ak, "stock_board_concept_name_em", create=True, return_value=board_frame):
            global_data = server.global_market_response()
            sectors = server.sector_focus_response()
        self.assertEqual(global_data["indices"][0]["quote_time"], "2026-09-22 04:00:00")
        self.assertEqual([item["region"] for item in global_data["indices"]], ["美股", "日股"])
        self.assertEqual(sectors["missing_themes"], ["存储"])

    def test_period_recommendations_abstain_when_financial_evidence_missing(self):
        analysis = {"recommendation": {"score_components": {"long_fundamental_valuation_score": 2,
                     "short_technical_score": 2}}, "latest_report_period": None,
                    "short_term": {"evidence": ["价格高于MA20"]}, "long_term": {"evidence": ["ROE良好"]},
                    "quote": {"quote": {"涨跌幅": 2, "换手率": 3}}, "unavailable": []}
        candidates = {"ranking": [{"symbol": "600519", "name": "测试股", "price": 100, "pe_ttm": 20}]}
        with patch.object(server, "analyze_stock_response", return_value=analysis):
            result = server.horizon_recommendations(candidates, "a", {})
        self.assertEqual(result["horizons"]["long"], [])
        self.assertEqual(len(result["horizons"]["short"]), 1)
        self.assertEqual(len(result["horizons"]["ultra_short"]), 1)

    def test_portfolio_analysis_requires_saved_holdings(self):
        with patch.object(server, "portfolio_response", return_value={"holdings": []}), \
             patch.object(server, "analyze_stock_response") as analyze:
            result = server.portfolio_analysis_response("a")
        self.assertFalse(result["available"])
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
