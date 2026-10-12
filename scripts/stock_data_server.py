#!/usr/bin/env python
"""Read-only local A-share data adapter for dsh-finance-agent.

Run on Windows: .\\.venv\\Scripts\\python.exe .\\scripts\\stock_data_server.py
The server intentionally binds only to 127.0.0.1 and contains no broker API.
"""

from __future__ import annotations

import json
import importlib
import math
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_diagnostics import readable_issues

try:
    import akshare as ak
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency. Activate .venv and run: pip install -r requirements.txt") from exc


HOST, PORT = "127.0.0.1", 8765
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_FILE = PROJECT_ROOT / "data" / "portfolio.json"
EVENT_ARCHIVE_FILE = PROJECT_ROOT / "data" / "market-events.json"
REPORT_SNAPSHOT_DIR = PROJECT_ROOT / "data" / "report-snapshots"
REFLECTION_FILE = PROJECT_ROOT / "data" / "research-reflections.json"
REPORT_SESSIONS = {"morning", "afternoon"}


def normalize_report_session(value: str = "morning") -> str:
    session = str(value or "morning").strip().lower()
    aliases = {"am": "morning", "preopen": "morning", "日报1": "morning", "pm": "afternoon", "preclose": "afternoon", "日报2": "afternoon"}
    session = aliases.get(session, session)
    if session not in REPORT_SESSIONS:
        raise ValueError("session 必须是 morning（日报1）或 afternoon（日报2）。")
    return session


def snapshot_path(day: str, market: str, session: str) -> Path:
    safe_day = pd.Timestamp(day).strftime("%Y-%m-%d")
    return REPORT_SNAPSHOT_DIR / f"{safe_day}-{normalize_market(market)}-{normalize_report_session(session)}.json"


def load_json_object(path: Path, default=None):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else ({} if default is None else default)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def save_report_snapshot(payload: dict):
    target = snapshot_path(payload.get("as_of", str(date.today())), payload.get("market", "a"), payload.get("session", "morning"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


def load_reflections(market: str = "a", symbol: str = "", limit: int = 20) -> list[dict]:
    market = normalize_market(market)
    payload = load_json_object(REFLECTION_FILE, {"reflections": []})
    records = payload.get("reflections", []) if isinstance(payload.get("reflections"), list) else []
    filtered = [item for item in records if isinstance(item, dict) and item.get("market", "a") == market]
    if symbol:
        normalized = normalize_symbol(symbol, market)
        filtered = [item for item in filtered if item.get("symbol") == normalized]
    return sorted(filtered, key=lambda item: (str(item.get("date", "")), str(item.get("created_at", ""))), reverse=True)[:limit]


def reflections_response(market: str = "a", symbol: str = "", limit: int = 50) -> dict:
    market = normalize_market(market)
    records = load_reflections(market, symbol, min(max(int(limit), 1), 200))
    return {
        "market": market, "symbol": normalize_symbol(symbol, market) if symbol else None,
        "as_of": str(date.today()), "reflections": records, "count": len(records),
        "source": "local research reflection archive",
        "note": "反思来自日报1与日报2的可审计快照对照，不代表因果证明或未来收益预测。",
    }


def reflection_adjustment(symbol: str, market: str = "a") -> dict:
    """Bounded feedback adjustment based only on archived, auditable intraday reviews."""
    records = load_reflections(market, symbol, 20)
    contradicted = sum(item.get("alignment") == "contradicted" for item in records)
    aligned = sum(item.get("alignment") == "aligned" for item in records)
    # Repeated misses reduce directional confidence; confirmations recover it slowly.
    score = max(-10, min(5, aligned - contradicted * 3))
    return {
        "adjustment": score, "sample_size": len(records), "aligned": aligned,
        "contradicted": contradicted, "recent": records[:5],
        "method": "近20条盘中复盘：相符+1、明显背离-3，调整限制在-10至+5个百分点。",
    }


def persist_reflections(records: list[dict]):
    existing = load_json_object(REFLECTION_FILE, {"reflections": []}).get("reflections", [])
    keyed = {}
    for item in list(existing) + list(records):
        if not isinstance(item, dict):
            continue
        key = (item.get("date"), item.get("market", "a"), item.get("symbol"), item.get("morning_conclusion"))
        keyed[key] = item
    retained = sorted(keyed.values(), key=lambda item: (str(item.get("date", "")), str(item.get("created_at", ""))), reverse=True)[:2000]
    REFLECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    REFLECTION_FILE.write_text(json.dumps({"reflections": retained}, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def first_value(record: dict, *candidates):
    """Return a value by exact key first, then by a case-insensitive substring."""
    for key in candidates:
        if key in record and record[key] not in (None, ""):
            return record[key]
    lowered = [(str(key).lower(), value) for key, value in record.items()]
    for candidate in candidates:
        needle = candidate.lower()
        for key, value in lowered:
            if needle in key and value not in (None, ""):
                return value
    return None


def to_number(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


def iso_date(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def clean_record(record):
    return {str(key): to_number(value) for key, value in record.items()}


MARKETS = {"a", "hk"}


def normalize_market(value: str = "a") -> str:
    """Normalize the public market selector without guessing from a numeric code."""
    text = str(value or "a").strip().lower().replace("-", "_")
    aliases = {
        "a": "a", "ashare": "a", "a_share": "a", "cn": "a", "中国a股": "a", "a股": "a",
        "hk": "hk", "hongkong": "hk", "hong_kong": "hk", "港股": "hk",
    }
    market = aliases.get(text)
    if market not in MARKETS:
        raise ValueError("market 必须是 a（A股）或 hk（港股）。")
    return market


def normalize_a_symbol(value):
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    digits = "".join(character for character in text if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def normalize_hk_symbol(value):
    """Hong Kong tickers are stored as a five-digit code, e.g. 700 -> 00700."""
    text = str(value or "").strip().upper()
    for prefix in ("HKG:", "HKG", "HK:", "HK"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.endswith(".HK"):
        text = text[:-3]
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not 1 <= len(text) <= 5:
        return ""
    return text.zfill(5)


def normalize_symbol(value, market: str = "a") -> str:
    market = normalize_market(market)
    symbol = normalize_a_symbol(value) if market == "a" else normalize_hk_symbol(value)
    if not symbol:
        rule = "6 位 A 股代码，例如 600519" if market == "a" else "1–5 位港股代码，例如 00700"
        raise ValueError(f"symbol 必须是{rule}。")
    return symbol


def market_label(market: str) -> str:
    return "A股" if normalize_market(market) == "a" else "港股"


def normalize_holding(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("每条持仓必须是对象。")
    market = normalize_market(item.get("market", "a"))
    symbol = normalize_symbol(item.get("symbol", ""), market)
    # Keep old A-share portfolio files byte-for-byte compatible in their shape.
    holding = {"symbol": symbol}
    if market != "a" or "market" in item:
        holding["market"] = market
    name = str(item.get("name", "")).strip()
    if name:
        holding["name"] = name[:80]
    for field in ("cost_basis", "shares"):
        if field not in item or item[field] in (None, ""):
            continue
        value = float(item[field])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} 必须是非负数。")
        holding[field] = value
    note = str(item.get("note", "")).strip()
    if note:
        holding["note"] = note[:500]
    return holding


def portfolio_response():
    if not PORTFOLIO_FILE.exists():
        return {
            "source": "local portfolio file", "as_of": str(date.today()), "holdings": [],
            "note": "尚未保存持仓；可调用 set_portfolio 仅保存代码、成本和数量等研究字段。",
        }
    try:
        payload = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
        holdings = [normalize_holding(item) for item in payload.get("holdings", [])]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"无法读取本地持仓文件：{exc}") from exc
    return {"source": "local portfolio file", "as_of": str(date.today()), "holdings": holdings}


def save_portfolio(payload: dict):
    if not isinstance(payload, dict) or not isinstance(payload.get("holdings"), list):
        raise ValueError("请求必须包含 holdings 数组。")
    if len(payload["holdings"]) > 100:
        raise ValueError("持仓数量不能超过 100。")
    holdings = [normalize_holding(item) for item in payload["holdings"]]
    symbols = [(item.get("market", "a"), item["symbol"]) for item in holdings]
    if len(symbols) != len(set(symbols)):
        raise ValueError("持仓代码不能重复。")
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps({"holdings": holdings}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"source": "local portfolio file", "as_of": str(date.today()), "holdings": holdings, "saved": True}


def stock_search_response(query: str, limit: int, market: str = "a"):
    query = str(query or "").strip()
    if not query:
        raise ValueError("query 不能为空。")
    market = normalize_market(market)
    if market == "a":
        frame = ak.stock_info_a_code_name().copy()
        source = "AKShare stock_info_a_code_name"
    else:
        frame = ak.stock_hk_spot_em().copy()
        source = "AKShare stock_hk_spot_em"
    code_key = next((key for key in ["code", "代码", "证券代码"] if key in frame.columns), None)
    name_key = next((key for key in ["name", "名称", "证券简称"] if key in frame.columns), None)
    if not code_key or not name_key:
        raise ValueError("上游股票列表字段发生变化。")
    normalizer = normalize_a_symbol if market == "a" else normalize_hk_symbol
    frame[code_key] = frame[code_key].map(normalizer)
    frame = frame.loc[frame[code_key].ne("")]
    query_symbol = normalizer(query)
    exact = frame[code_key].eq(query_symbol) | frame[name_key].astype(str).eq(query)
    partial = frame[code_key].str.contains(query, case=False, na=False, regex=False) | frame[name_key].astype(str).str.contains(query, case=False, na=False, regex=False)
    result = pd.concat([frame.loc[exact], frame.loc[partial & ~exact]]).head(limit)
    matches = [{"symbol": str(item[code_key]), "name": str(item[name_key])} for item in result.to_dict("records")]
    return {"query": query, "market": market, "source": source, "as_of": str(date.today()), "matches": matches}


def trading_day_response(day_text: str = "", market: str = "a"):
    market = normalize_market(market)
    target = pd.Timestamp(day_text).date() if day_text else date.today()
    if market == "a":
        frame = ak.tool_trade_date_hist_sina()
        key = next((key for key in ["trade_date", "日期"] if key in frame.columns), None)
        source = "AKShare tool_trade_date_hist_sina"
    else:
        # A broad HK index avoids treating a suspended individual security as a market holiday.
        frame = ak.stock_hk_index_daily_sina(symbol="HSI")
        key = next((key for key in ["date", "日期", "Date"] if key in frame.columns), None)
        source = "AKShare stock_hk_index_daily_sina (HSI)"
    if not key:
        raise ValueError("上游交易日历字段发生变化。")
    dates = {pd.Timestamp(value).date() for value in frame[key].dropna()}
    return {
        "date": str(target), "market": market, "is_trading_day": target in dates,
        "source": source, "as_of": str(max(dates)) if dates else None,
    }


def market_brief_response(event_limit: int, market: str = "a"):
    market = normalize_market(market)
    sources, errors = [], []
    indices, events = [], []
    try:
        if market == "a":
            frame = ak.stock_zh_index_spot_sina()
            wanted = {"sh000001", "sh000300", "sz399001", "sz399006"}
            subset = frame.loc[frame["代码"].astype(str).isin(wanted)]
            index_fields = [field for field in ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "成交量", "成交额"] if field in subset]
            sources.append("AKShare stock_zh_index_spot_sina")
        else:
            frame = ak.stock_hk_index_spot_sina()
            subset = frame.head(10)
            index_fields = [field for field in ["code", "代码", "name", "名称", "price", "最新价", "change", "涨跌额", "change_pct", "涨跌幅"] if field in subset]
            sources.append("AKShare stock_hk_index_spot_sina")
        indices = [clean_record(item) for item in subset[index_fields].to_dict("records")]
    except Exception as exc:
        errors.append(f"指数快照不可用：{exc}")
    try:
        frame = ak.stock_info_global_cls(symbol="全部")
        event_fields = [field for field in ["标题", "发布日期", "发布时间"] if field in frame]
        for item in frame.head(event_limit)[event_fields].to_dict("records"):
            events.append(clean_record(item))
        sources.append("AKShare stock_info_global_cls")
    except Exception as exc:
        errors.append(f"市场快讯不可用：{exc}")
    if not indices and not events:
        raise RuntimeError("；".join(errors) or "市场数据源未返回结果。")
    return {
        "market": market, "source": "; ".join(sources), "as_of": str(date.today()), "indices": indices, "events": events,
        "unavailable": errors, "note": "快讯是待核验的事件线索，不等同于价格影响或投资结论。",
    }


def market_events_response(days: int, limit: int):
    frame = ak.stock_info_global_cls(symbol="全部").copy()
    title_key = next((key for key in ["标题", "title", "内容"] if key in frame.columns), None)
    date_key = next((key for key in ["发布日期", "日期", "date"] if key in frame.columns), None)
    time_key = next((key for key in ["发布时间", "时间", "time"] if key in frame.columns), None)
    if not title_key:
        raise ValueError("上游快讯未提供标题字段。")
    cutoff = date.today() - timedelta(days=days - 1)
    fresh_events = []
    for item in frame.to_dict("records"):
        event_date = None
        if date_key and item.get(date_key) not in (None, ""):
            try:
                event_date = pd.Timestamp(item[date_key]).date()
            except (TypeError, ValueError):
                pass
        theme, path = classify_market_event(str(item.get(title_key, "")))
        fresh_events.append({
            "title": str(item.get(title_key, "")),
            "date": str(event_date) if event_date else None,
            "time": str(item.get(time_key, "")) if time_key else None,
            "theme": theme, "possible_impact_path": path,
        })
    archived_events = []
    if EVENT_ARCHIVE_FILE.exists():
        try:
            archived_events = json.loads(EVENT_ARCHIVE_FILE.read_text(encoding="utf-8")).get("events", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            archived_events = []
    combined = {}
    for event in archived_events + fresh_events:
        if not isinstance(event, dict) or not event.get("title"):
            continue
        combined[(event.get("date"), event.get("time"), event["title"])] = event
    retained = []
    archive_cutoff = date.today() - timedelta(days=89)
    for event in combined.values():
        try:
            event_date = pd.Timestamp(event.get("date")).date() if event.get("date") else None
        except (TypeError, ValueError):
            event_date = None
        if event_date is None or event_date >= archive_cutoff:
            retained.append(event)
    retained.sort(key=lambda event: (event.get("date") or "", event.get("time") or ""), reverse=True)
    EVENT_ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENT_ARCHIVE_FILE.write_text(json.dumps({"events": retained}, ensure_ascii=False, indent=2), encoding="utf-8")
    events = []
    for event in retained:
        try:
            event_date = pd.Timestamp(event.get("date")).date() if event.get("date") else None
        except (TypeError, ValueError):
            event_date = None
        if event_date and event_date < cutoff:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return {
        "window_days": days, "window_start": str(cutoff), "window_end": str(date.today()),
        "source": "AKShare stock_info_global_cls", "as_of": str(date.today()), "events": events,
        "note": "快讯为事件线索；本地滚动归档90日，涉及投资判断前应核验公告或权威原文。",
    }


def classify_market_event(title: str):
    groups = [
        (("央行", "利率", "降准", "流动性", "社融", "人民币"), "宏观与流动性", "可能经利率、汇率和风险偏好影响大盘估值；方向需结合正式政策与市场预期差核验。"),
        (("芯片", "半导体", "人工智能", "AI", "算力", "机器人"), "科技", "可能影响科技产业链订单与估值，但主题热度不能替代业绩兑现。"),
        (("地产", "房地产", "住房", "房贷"), "房地产", "可能经销售、信用和地方财政影响地产链及金融板块，需关注政策落地与基本面数据。"),
        (("消费", "旅游", "零售", "白酒", "汽车"), "消费", "可能影响消费需求预期，需用销量、价格和企业财报验证持续性。"),
        (("原油", "煤炭", "电力", "新能源", "光伏", "锂"), "能源", "可能经商品价格、成本和产能影响上下游利润，需同时评估反向价格风险。"),
        (("医药", "医疗", "药品", "创新药"), "医药", "可能影响研发、准入与需求预期，需核验监管原文、临床证据和商业化进展。"),
        (("关税", "制裁", "战争", "冲突", "出口", "贸易"), "外部风险", "可能经出口需求、供应链与风险偏好传导，事件演变和政策回应存在较大不确定性。"),
    ]
    lowered = title.lower()
    for keywords, theme, path in groups:
        if any(keyword.lower() in lowered for keyword in keywords):
            return theme, path
    return "综合事件", "与A股的直接传导关系尚待核验；先确认权威原文、涉及行业和可量化影响。"


def classify_event_scope(title: str):
    """Split news leads into international events and domestic policy signals."""
    lowered = str(title or "").lower()
    international = (
        "美联储", "欧洲央行", "日本央行", "美国", "欧盟", "欧洲", "日本", "韩国", "印度",
        "俄罗斯", "乌克兰", "中东", "以色列", "伊朗", "关税", "制裁", "贸易战", "战争",
        "冲突", "opec", "federal reserve", "fed ", "ecb", "美元", "美债",
    )
    domestic_policy = (
        "国务院", "央行", "人民银行", "证监会", "财政部", "发改委", "商务部", "工信部",
        "住建部", "金融监管总局", "政策", "降准", "降息", "逆回购", "专项债", "国常会",
        "中央政治局", "两会", "监管", "印发", "条例", "办法", "通知",
    )
    if any(word in lowered for word in international):
        return "international"
    if any(word in lowered for word in domestic_policy):
        return "domestic_policy"
    return "other"


def event_tone(title: str):
    lowered = str(title or "").lower()
    positive = ("降准", "降息", "增持", "回购", "支持", "刺激", "增长", "改善", "达成", "上调", "放宽", "注入流动性")
    negative = ("制裁", "关税", "冲突", "战争", "下调", "暴跌", "违约", "调查", "处罚", "收紧", "风险", "衰退")
    score = sum(word in lowered for word in positive) - sum(word in lowered for word in negative)
    return "positive" if score > 0 else ("negative" if score < 0 else "neutral")


def market_breadth_response(frame=None, market: str = "a"):
    """Calculate auditable cross-sectional breadth for the selected market."""
    market = normalize_market(market)
    if frame is None:
        frame = (ak.stock_zh_a_spot_tx() if market == "a" else ak.stock_hk_spot_em()).copy()
    else:
        frame = frame.copy()
    aliases = {
        "day": ["zdf", "涨跌幅"], "day5": ["zdf_d5", "5日涨跌幅"],
        "day20": ["zdf_d20", "20日涨跌幅"],
    }
    columns = {name: next((key for key in keys if key in frame.columns), None) for name, keys in aliases.items()}
    if not columns["day"]:
        raise ValueError("上游全市场行情缺少涨跌幅字段。")
    numeric = {}
    for name, column in columns.items():
        if column:
            numeric[name] = pd.to_numeric(frame[column], errors="coerce").dropna()
    day = numeric["day"]
    if day.empty:
        raise ValueError("全市场涨跌幅无有效记录。")
    advancing, declining = int((day > 0).sum()), int((day < 0).sum())
    unchanged, valid = int((day == 0).sum()), int(day.size)
    advance_ratio = advancing / valid
    ad_ratio = advancing / max(1, declining)
    above_5d = float((numeric["day5"] > 0).mean()) if "day5" in numeric and not numeric["day5"].empty else None
    above_20d = float((numeric["day20"] > 0).mean()) if "day20" in numeric and not numeric["day20"].empty else None
    median_change = float(day.median())
    score = 50 + (advance_ratio - 0.5) * 60 + max(-3, min(3, median_change)) * 4
    if above_5d is not None:
        score += (above_5d - 0.5) * 20
    if above_20d is not None:
        score += (above_20d - 0.5) * 20
    score = int(round(max(0, min(100, score))))
    label = "强势扩散" if score >= 70 else ("偏强" if score >= 57 else ("均衡" if score >= 43 else ("偏弱" if score >= 30 else "弱势扩散")))
    return {
        "market": market,
        "source": ("AKShare stock_zh_a_spot_tx" if market == "a" else "AKShare stock_hk_spot_em") + " cross-section", "as_of": str(date.today()),
        "valid_stocks": valid, "advancing": advancing, "declining": declining, "unchanged": unchanged,
        "advance_ratio": round(advance_ratio, 4), "advance_decline_ratio": round(ad_ratio, 4),
        "median_pct_change": round(median_change, 3),
        "above_5d_ratio": round(above_5d, 4) if above_5d is not None else None,
        "above_20d_ratio": round(above_20d, 4) if above_20d is not None else None,
        "limit_up_count": int((day >= 9.8).sum()), "limit_down_count": int((day <= -9.8).sum()),
        "score": score, "label": label,
    }


def popularity_rank_response():
    """Read attention ranks without AKShare's optional push2 quote enrichment."""
    response = requests.post("https://emappdata.eastmoney.com/stockrank/getAllCurrentList", json={
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "pageNo": 1, "pageSize": 100,
    }, timeout=(5, 10))
    response.raise_for_status()
    rows = response.json().get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("关注排名接口未返回记录")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_a_symbol(str(row.get("sc", "")).upper().removeprefix("SH").removeprefix("SZ").removeprefix("BJ"))
        rank = safe_float(row.get("rk"))
        if symbol and rank is not None and rank > 0:
            result.append({"代码": symbol, "当前排名": int(rank), "股票名称": row.get("name") or row.get("sName")})
    if not result:
        raise ValueError("关注排名缺少有效代码或排名")
    return sorted(result, key=lambda row: row["当前排名"])[:10]


def social_sentiment_response(breadth=None, market: str = "a"):
    """Build a conservative attention proxy and disclose its market-specific limits."""
    market = normalize_market(market)
    breadth = breadth or market_breadth_response(market=market)
    hot_stocks, unavailable = [], []
    if market == "a":
        try:
            hot_stocks = popularity_rank_response()
        except Exception as exc:
            logging.getLogger(__name__).warning("东方财富关注排名获取失败：%s", exc)
            unavailable.extend(readable_issues([f"东方财富人气榜不可用：{exc}"]))
    else:
        unavailable.append("港股关注榜尚未接入；情绪分仅使用港股价格宽度代理。")
    score = int(round(max(0, min(100,
        breadth.get("score", 50) * 0.7 +
        (65 if breadth.get("limit_up_count", 0) > breadth.get("limit_down_count", 0) else 35) * 0.3
    ))))
    label = "亢奋" if score >= 75 else ("偏乐观" if score >= 58 else ("中性" if score >= 42 else ("偏谨慎" if score >= 25 else "恐慌")))
    return {
        "market": market,
        "source": "东方财富关注排名（不依赖行情补全）+ 全市场价格情绪代理" if hot_stocks else f"{market_label(market)}价格情绪代理",
        "as_of": str(date.today()), "score": score, "label": label, "hot_stocks": hot_stocks,
        "unavailable": unavailable,
        "note": "当前分数主要反映市场参与和涨跌分布，不等同于对社交媒体文本做情感识别；人气榜仅表示关注度。",
    }


def market_calibration(panorama: dict | None):
    state = (panorama or {}).get("market_state", {})
    adjustment = int(state.get("buy_tendency_adjustment", 0) or 0)
    return max(-20, min(15, adjustment))


def calibrate_buy_sell(base_buy_percentage: int, panorama: dict | None):
    adjustment = market_calibration(panorama)
    calibrated_buy = int(round(max(5, min(95, base_buy_percentage + adjustment))))
    return {
        "base_buy_percentage": int(base_buy_percentage), "market_adjustment": adjustment,
        "buy_percentage": calibrated_buy, "sell_percentage": 100 - calibrated_buy,
    }


def market_panorama_response(market: str = "a"):
    """Combine events, policy, breadth and sentiment into one market risk regime."""
    market = normalize_market(market)
    unavailable = []
    try:
        breadth = market_breadth_response(market=market)
    except Exception as exc:
        breadth = {"score": 50, "label": "数据不足"}
        unavailable.append(f"大盘宽度不可用：{exc}")
    try:
        sentiment = social_sentiment_response(breadth, market=market)
        unavailable.extend(sentiment.get("unavailable", []))
    except Exception as exc:
        sentiment = {"score": 50, "label": "数据不足", "hot_stocks": []}
        unavailable.append(f"社交情绪不可用：{exc}")
    event_window = {
        "days": 7, "start": str(date.today() - timedelta(days=6)), "end": str(date.today()),
    }
    try:
        event_payload = market_events_response(7, 80)
        events = event_payload.get("events", [])
        event_window = {
            "days": event_payload.get("window_days", 7),
            "start": event_payload.get("window_start", event_window["start"]),
            "end": event_payload.get("window_end", event_window["end"]),
        }
    except Exception as exc:
        events = []
        unavailable.append(f"事件与政策不可用：{exc}")
    international, domestic, other = [], [], []
    for event in events:
        enriched = dict(event)
        enriched["tone"] = event_tone(event.get("title", ""))
        scope = classify_event_scope(event.get("title", ""))
        (international if scope == "international" else domestic if scope == "domestic_policy" else other).append(enriched)
    event_penalty = min(20, sum(item["tone"] == "negative" for item in international) * 4)
    policy_support = min(10, sum(item["tone"] == "positive" for item in domestic) * 2)
    market_score = int(round(max(0, min(100,
        breadth.get("score", 50) * 0.55 + sentiment.get("score", 50) * 0.25 + 10 + policy_support - event_penalty
    ))))
    risk_score = 100 - market_score
    if risk_score >= 75:
        risk_level, position_range, adjustment = "极高", "0%–20%", -20
    elif risk_score >= 60:
        risk_level, position_range, adjustment = "高", "20%–40%", -12
    elif risk_score >= 40:
        risk_level, position_range, adjustment = "中", "40%–60%", 0
    elif risk_score >= 25:
        risk_level, position_range, adjustment = "较低", "50%–70%", 8
    else:
        risk_level, position_range, adjustment = "低", "60%–80%", 12
    regime = "risk_off" if risk_score >= 60 else ("risk_on" if risk_score < 40 else "neutral")
    return {
        "market": market,
        "source": f"AKShare market news, {market_label(market)} cross-section and attention proxy", "as_of": str(date.today()),
        "event_window": event_window,
        "international_events": international[:12], "domestic_policies": domestic[:12], "other_events": other[:12],
        "social_sentiment": sentiment, "market_breadth": breadth,
        "market_state": {
            "regime": regime, "score": market_score, "risk_score": risk_score, "risk_level": risk_level,
            "reference_position": position_range, "buy_tendency_adjustment": adjustment,
            "summary": f"市场宽度{breadth.get('label', '未知')}、情绪{sentiment.get('label', '未知')}，综合风险{risk_level}。",
            "position_note": "参考仓位是面向分散组合的市场风险预算区间，不是针对个人的仓位建议；需结合自身约束。",
        },
        "unavailable": unavailable,
        "method": "市场分=宽度55%+情绪25%+中性基准10分+国内正向政策加分-国际负向事件扣分；风险分=100-市场分。个股买入倾向按风险档统一调整-20至+12个百分点。",
    }


def latest_event_date(events):
    dates = [str(item.get("date")) for item in events if isinstance(item, dict) and item.get("date")]
    return max(dates) if dates else None


def market_context_summary(panorama: dict | None):
    """Expose freshness and partial failures instead of silently treating missing inputs as neutral."""
    panorama = panorama or {}
    unavailable = list(panorama.get("unavailable", []))
    event_failed = any("事件与政策" in item for item in unavailable)
    breadth_failed = any("大盘宽度" in item for item in unavailable)
    attention_failed = any("人气榜" in item for item in unavailable)
    international = panorama.get("international_events", [])
    domestic = panorama.get("domestic_policies", [])
    sentiment = panorama.get("social_sentiment", {})
    breadth = panorama.get("market_breadth", {})

    def event_coverage(items):
        return {
            "status": "unavailable" if event_failed else ("available" if items else "available_empty"),
            "count": len(items), "latest_date": latest_event_date(items),
        }

    coverage = {
        "international_events": event_coverage(international),
        "domestic_policies": event_coverage(domestic),
        "market_breadth": {
            "status": "unavailable" if breadth_failed else ("available" if breadth else "unavailable"),
            "as_of": breadth.get("as_of"),
        },
        "social_attention": {
            "status": "proxy_only" if attention_failed or not sentiment.get("hot_stocks") else "available_with_proxy",
            "as_of": sentiment.get("as_of"),
            "note": sentiment.get("note"),
        },
    }
    complete = bool(panorama) and not unavailable and all(
        item["status"] not in {"unavailable", "proxy_only"} for item in coverage.values()
    )
    return {
        "as_of": panorama.get("as_of"), "source": panorama.get("source"),
        "event_window": panorama.get("event_window"),
        "coverage": coverage,
        "unavailable": unavailable,
        "complete": complete,
    }


def related_enterprises_response(symbol: str, days: int = 30, limit: int = 3):
    """Find same-industry large-cap peers and retrieve their recent official announcements."""
    days = min(max(int(days), 1), 180)
    limit = min(max(int(limit), 1), 5)
    unavailable = []
    info = ak.stock_individual_info_em(symbol=symbol).copy()
    item_key = next((key for key in ["item", "项目"] if key in info.columns), None)
    value_key = next((key for key in ["value", "值"] if key in info.columns), None)
    if not item_key or not value_key:
        raise ValueError("个股信息缺少 item/value 字段，无法确认所属行业。")
    details = {str(row[item_key]).strip(): to_number(row[value_key]) for row in info.to_dict("records")}
    industry = str(details.get("行业", "")).strip()
    if not industry:
        raise ValueError("个股信息未返回所属行业。")

    constituents = ak.stock_board_industry_cons_em(symbol=industry).copy()
    code_key = next((key for key in ["代码", "股票代码", "symbol", "code"] if key in constituents.columns), None)
    if not code_key:
        raise ValueError("行业成分数据缺少股票代码字段。")
    constituent_codes = {normalize_a_symbol(value) for value in constituents[code_key].dropna()}
    constituent_codes.discard("")
    try:
        spot = ak.stock_zh_a_spot_em().copy()
        spot_code = next((key for key in ["代码", "股票代码", "symbol", "code"] if key in spot.columns), None)
        spot_name = next((key for key in ["名称", "股票简称", "name"] if key in spot.columns), None)
        cap_key = next((key for key in ["总市值", "总市值(元)", "market_cap"] if key in spot.columns), None)
        if not spot_code or not spot_name or not cap_key:
            raise ValueError("全市场行情缺少代码、名称或总市值字段。")
        spot[spot_code] = spot[spot_code].astype(str).str.extract(r"(\d{6})", expand=False)
        spot[cap_key] = pd.to_numeric(spot[cap_key], errors="coerce")
        peers = spot.loc[
            spot[spot_code].isin(constituent_codes) & spot[spot_code].ne(symbol)
        ].dropna(subset=[cap_key]).nlargest(limit, cap_key)
        leaders = [
            {"symbol": str(row[spot_code]), "name": str(row[spot_name]), "market_cap": to_number(row[cap_key])}
            for row in peers.to_dict("records")
        ]
    except Exception as exc:
        raise ValueError(f"无法按总市值筛选行业龙头：{exc}") from exc

    start = str(date.today() - timedelta(days=days - 1))

    def load_peer(peer):
        result = dict(peer)
        try:
            payload = announcements_response(peer["symbol"], start, str(date.today()), 5)
            result["announcements"] = payload.get("announcements", [])
            result["announcement_as_of"] = payload.get("as_of")
        except Exception as exc:
            result["announcements"] = []
            result["unavailable"] = f"公告不可用：{exc}"
        return result

    with ThreadPoolExecutor(max_workers=max(1, len(leaders))) as executor:
        enriched = list(executor.map(load_peer, leaders))
    for peer in enriched:
        if peer.get("unavailable"):
            unavailable.append(f"{peer['symbol']} {peer['name']}：{peer['unavailable']}")
    return {
        "symbol": symbol, "industry": industry, "as_of": str(date.today()),
        "window_days": days, "window_start": start, "window_end": str(date.today()),
        "selection_method": f"东方财富行业成分中按总市值选取前{limit}家（不含目标公司）",
        "source": "AKShare stock_individual_info_em + stock_board_industry_cons_em + stock_zh_a_spot_em; CNINFO announcements",
        "enterprises": enriched, "unavailable": unavailable,
        "note": "同行龙头公告用于行业背景与风险核验，不因标题正负面直接改变个股评分。",
    }


def market_movers_response(limit: int, market: str = "a"):
    market = normalize_market(market)
    frame = (ak.stock_zh_a_spot_tx() if market == "a" else ak.stock_hk_spot_em()).copy()
    change_key = next((key for key in ["zdf", "涨跌幅"] if key in frame.columns), None)
    if not change_key:
        raise ValueError("上游数据未提供涨跌幅字段。")
    fields = [field for field in ["code", "代码", "name", "名称", "zxj", "最新价", "zd", "涨跌额", change_key, "zdf_d5", "5日涨跌幅", "zdf_d20", "20日涨跌幅", "volume", "成交量", "hsl", "换手率", "pe_ttm", "市盈率", "zsz", "总市值"] if field in frame]
    frame[change_key] = pd.to_numeric(frame[change_key], errors="coerce")
    frame = frame.dropna(subset=[change_key])
    up = [clean_record(item) for item in frame.nlargest(limit, change_key)[fields].to_dict("records")]
    down = [clean_record(item) for item in frame.nsmallest(limit, change_key)[fields].to_dict("records")]
    return {
        "market": market, "source": "AKShare stock_zh_a_spot_tx" if market == "a" else "AKShare stock_hk_spot_em", "as_of": str(date.today()),
        "top_gainers": up, "top_losers": down,
        "note": "异动列表仅用于生成待研究候选池；涨跌幅和成交信息本身不构成推荐或预测。",
    }


def candidate_ranking_response(limit: int, panorama=None, market: str = "a"):
    market = normalize_market(market)
    frame = (ak.stock_zh_a_spot_tx() if market == "a" else ak.stock_hk_spot_em()).copy()
    aliases = {
        "symbol": ["code", "代码", "股票代码", "证券代码"], "name": ["name", "名称", "股票名称", "证券简称"], "price": ["zxj", "最新价", "现价"],
        "day": ["zdf", "涨跌幅"], "day5": ["zdf_d5", "5日涨跌幅"],
        "day20": ["zdf_d20", "20日涨跌幅"], "turnover": ["hsl", "换手率"],
        "pe": ["pe_ttm", "市盈率-动态", "市盈率"], "market_cap": ["zsz", "总市值", "总市值(港元)"],
    }
    columns = {name: next((key for key in keys if key in frame.columns), None) for name, keys in aliases.items()}
    if not columns["symbol"] or not columns["name"] or not columns["day"]:
        raise ValueError("上游行情缺少代码、名称或涨跌幅字段。")
    rows = []
    for item in frame.to_dict("records"):
        raw_symbol = str(item.get(columns["symbol"], ""))
        symbol = (normalize_a_symbol(raw_symbol) if market == "a" else normalize_hk_symbol(raw_symbol))
        name = str(item.get(columns["name"], ""))
        day = safe_float(item.get(columns["day"])) if columns["day"] else None
        day5 = safe_float(item.get(columns["day5"])) if columns["day5"] else None
        day20 = safe_float(item.get(columns["day20"])) if columns["day20"] else None
        turnover = safe_float(item.get(columns["turnover"])) if columns["turnover"] else None
        pe = safe_float(item.get(columns["pe"])) if columns["pe"] else None
        if not symbol or day is None or "ST" in name.upper() or (market == "a" and day >= 9.8):
            continue
        # Transparent research score: balanced momentum, tradability and non-extreme valuation.
        momentum = max(-10, min(10, day)) * 1.2
        momentum += max(-20, min(20, day5 or 0)) * 0.8
        momentum += max(-40, min(40, day20 or 0)) * 0.35
        liquidity = 10 if turnover is not None and 1 <= turnover <= 12 else 3
        valuation = 10 if pe is not None and 0 < pe <= 40 else (5 if pe is not None and 0 < pe <= 80 else 0)
        score = round(max(0, min(100, 50 + momentum + liquidity + valuation)), 2)
        rows.append({
            "symbol": symbol, "name": name,
            "price": to_number(item.get(columns["price"])) if columns["price"] else None,
            "pct_change_1d": day, "pct_change_5d": day5, "pct_change_20d": day20,
            "turnover": turnover, "pe_ttm": pe,
            "market_cap": to_number(item.get(columns["market_cap"])) if columns["market_cap"] else None,
            "score": score,
            "reason": f"1/5/20日表现为{day:.2f}%/{(day5 or 0):.2f}%/{(day20 or 0):.2f}%，换手率{turnover if turnover is not None else '缺失'}，PE(TTM){pe if pe is not None else '缺失'}。",
            "risk": "动量可能反转；估值口径、公告与最新财报仍需逐项复核。",
        })
    ranked = sorted(rows, key=lambda item: item["score"], reverse=True)[:limit]
    if panorama is None:
        try:
            panorama = market_panorama_response(market=market)
        except Exception as exc:
            panorama = {
                "as_of": None, "unavailable": [f"市场全景不可用：{exc}"],
                "market_state": {"risk_level": "未知", "buy_tendency_adjustment": 0},
            }
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
        feedback = reflection_adjustment(item["symbol"], market)
        reflection_base = int(round(max(5, min(95, item["score"] + feedback["adjustment"]))))
        calibrated = calibrate_buy_sell(reflection_base, panorama)
        item.update(calibrated)
        item["raw_score"] = item["score"]
        item["reflection_adjustment"] = feedback["adjustment"]
        item["reflection_context"] = feedback
        item["recommendation"] = "建议买入" if item["buy_percentage"] >= 65 else ("建议卖出" if item["sell_percentage"] >= 65 else "建议持有观察")
    return {
        "market": market,
        "source": ("AKShare stock_zh_a_spot_tx" if market == "a" else "AKShare stock_hk_spot_em") + " + transparent local scoring", "as_of": str(date.today()),
        "ranking": ranked, "market_state": (panorama or {}).get("market_state"),
        "market_context": market_context_summary(panorama),
        "unavailable": list((panorama or {}).get("unavailable", [])),
        "method": "原始分=clamp(50+1日涨幅×1.2+5日涨幅×0.8+20日涨幅×0.35+流动性分+估值分,0,100)；叠加历史盘中反思校准（-10至+5），再按市场风险档调整-20至+12个百分点；卖出倾向=100-买入倾向；剔除ST，A股另剔除接近涨停标的。",
        "note": "排名按买入倾向降序；百分比不是建议仓位或收益预测。",
    }


def history(symbol: str, start: str = "", end: str = "", adjust: str = "qfq", market: str = "a") -> pd.DataFrame:
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    today = date.today()
    start_date = (pd.Timestamp(start).date() if start else today - timedelta(days=730)).strftime("%Y%m%d")
    end_date = (pd.Timestamp(end).date() if end else today).strftime("%Y%m%d")
    if market == "hk":
        frame = ak.stock_hk_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust or "")
        source = "AKShare stock_hk_hist"
    else:
        source = "AKShare stock_zh_a_hist"
        try:
            frame = ak.stock_zh_a_hist(
                symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust or "", timeout=15,
            )
        except Exception as primary_error:
            if symbol.startswith(("4", "8")):
                raise primary_error
            exchange_symbol = ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol
            frame = ak.stock_zh_a_daily(symbol=exchange_symbol, start_date=start_date, end_date=end_date, adjust=adjust or "")
            source = "AKShare stock_zh_a_daily (Sina fallback)"
    if frame.empty:
        raise ValueError("未返回历史行情；请确认代码、日期区间和数据源状态。")
    rename = {
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount", "振幅": "amplitude", "涨跌幅": "pct_change",
        "涨跌额": "change", "换手率": "turnover",
    }
    frame = frame.rename(columns=rename)
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ["open", "close", "high", "low", "volume", "amount", "amplitude", "pct_change", "change", "turnover"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.sort_values("date").reset_index(drop=True)
    frame.attrs["source"] = source
    return frame


def bars_response(symbol: str, start: str, end: str, adjust: str, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    frame = history(symbol, start, end, adjust, market)
    fields = [field for field in ["date", "open", "high", "low", "close", "volume", "amount", "turnover"] if field in frame]
    bars = []
    for row in frame[fields].to_dict("records"):
        row["date"] = iso_date(row["date"])
        bars.append(clean_record(row))
    return {
        "symbol": symbol, "market": market, "source": frame.attrs.get("source", "AKShare history"), "as_of": bars[-1]["date"],
        "adjust": adjust or "none", "bars": bars,
    }


def indicators_response(symbol: str, lookback: int, adjust: str, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    frame = history(symbol, "", "", adjust, market).tail(lookback).copy()
    close = frame["close"]
    frame["ma5"] = close.rolling(5).mean()
    frame["ma20"] = close.rolling(20).mean()
    frame["ma60"] = close.rolling(60).mean()
    delta = close.diff()
    gains = delta.clip(lower=0).rolling(14).mean()
    losses = (-delta.clip(upper=0)).rolling(14).mean()
    frame["rsi14"] = 100 - 100 / (1 + gains / losses.replace(0, np.nan))
    ema12, ema26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
    frame["macd"] = ema12 - ema26
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    frame["macd_histogram"] = frame["macd"] - frame["macd_signal"]
    returns = close.pct_change()
    drawdown = close / close.cummax() - 1
    last = frame.iloc[-1]
    latest = {key: to_number(last[key]) for key in ["close", "ma5", "ma20", "ma60", "rsi14", "macd", "macd_signal", "macd_histogram"]}
    return {
        "symbol": symbol, "market": market, "source": frame.attrs.get("source", "AKShare history") + " + local calculation", "as_of": iso_date(last["date"]),
        "adjust": adjust or "none", "lookback_trading_days": len(frame), "latest": latest,
        "annualized_volatility": to_number(returns.std() * math.sqrt(252)),
        "max_drawdown": to_number(drawdown.min()),
        "method_note": "RSI uses a 14-day simple rolling average; MACD uses EMA(12,26,9).",
    }


def spot_row(symbol: str, market: str = "a") -> dict:
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    if market == "hk":
        frame = ak.stock_hk_spot_em().copy()
        code_key = next((key for key in ["代码", "股票代码", "证券代码", "code"] if key in frame.columns), None)
        if not code_key:
            raise ValueError("港股行情缺少代码字段。")
        row = frame.loc[frame[code_key].map(normalize_hk_symbol) == symbol]
        if row.empty:
            raise ValueError("未找到该港股代码的行情快照。")
        raw = clean_record(row.iloc[0].to_dict())
        result = dict(raw)
        aliases = {
            "代码": [code_key], "名称": ["名称", "股票名称", "证券简称", "name"], "最新价": ["最新价", "现价", "price"],
            "涨跌幅": ["涨跌幅", "zdf"], "成交量": ["成交量", "volume"], "成交额": ["成交额", "amount"],
            "市盈率-动态": ["市盈率", "市盈率-动态", "pe", "pe_ttm"], "市净率": ["市净率", "pb"],
            "总市值": ["总市值", "总市值(港元)", "market_cap"],
        }
        for target, candidates in aliases.items():
            value = first_value(raw, *candidates)
            if value is not None:
                result[target] = symbol if target == "代码" else value
        result["代码"] = symbol
        result["_source"] = "AKShare stock_hk_spot_em"
        return result
    try:
        frame = ak.stock_zh_a_spot_em()
        row = frame.loc[frame["代码"].astype(str).str.zfill(6) == symbol]
        if not row.empty:
            result = clean_record(row.iloc[0].to_dict())
            result["_source"] = "AKShare stock_zh_a_spot_em"
            return result
    except Exception:
        pass
    frame = ak.stock_zh_a_spot_tx()
    digits = frame["code"].astype(str).str.replace(r"\D", "", regex=True).str[-6:]
    row = frame.loc[digits == symbol]
    if row.empty:
        raise ValueError("未找到该代码的行情快照。")
    raw = clean_record(row.iloc[0].to_dict())
    result = dict(raw)
    result.update({
        "代码": symbol, "名称": raw.get("name"), "最新价": raw.get("zxj"), "涨跌幅": raw.get("zdf"),
        "成交量": raw.get("volume"), "成交额": raw.get("turnover"), "换手率": raw.get("hsl"),
        "市盈率-动态": raw.get("pe_ttm"), "总市值": raw.get("zsz"),
        "_source": "AKShare stock_zh_a_spot_tx (Tencent fallback)",
    })
    return result


def quote_response(symbol: str, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    row = spot_row(symbol, market)
    source = row.pop("_source", "AKShare spot")
    return {
        "symbol": symbol, "market": market, "source": source, "as_of": str(row.get("更新时间") or date.today()),
        "note": "数据源快照，不是券商逐笔实时行情。", "quote": row,
    }


def valuation_response(symbol: str, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    row = spot_row(symbol, market)
    source = row.pop("_source", "AKShare spot")
    keys = ["代码", "名称", "最新价", "市盈率-动态", "市盈率-静态", "市净率", "总市值", "流通市值", "涨跌幅"]
    return {
        "symbol": symbol, "market": market, "source": source, "as_of": str(row.get("更新时间") or date.today()),
        "valuation": {key: row.get(key) for key in keys if key in row},
        "note": "PE/PB口径由上游数据源决定；请在正式决策前复核口径和时点。",
    }


def fundamentals_response(symbol: str, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    if market == "hk":
        frame = ak.stock_hk_financial_indicator_em(symbol=symbol)
        source = "AKShare stock_hk_financial_indicator_em"
    else:
        frame = ak.stock_financial_analysis_indicator(symbol=symbol)
        source = "AKShare stock_financial_analysis_indicator"
    if frame.empty:
        raise ValueError("数据源未返回财务分析指标。")
    report_key = next((key for key in ["日期", "报告期", "报告日期"] if key in frame.columns), None)
    if report_key:
        frame = frame.assign(_report_sort=pd.to_datetime(frame[report_key], errors="coerce")).sort_values("_report_sort", ascending=False).drop(columns=["_report_sort"])
    frame = frame.head(8).copy()
    records = [clean_record(item) for item in frame.to_dict("records")]
    latest_period = str(records[0].get(report_key)) if records and report_key else None
    return {
        "symbol": symbol, "market": market, "source": source, "as_of": latest_period or str(date.today()),
        "latest_report_period": latest_period,
        "report_period_field": report_key, "records": records,
        "note": "字段和口径会随上游数据源变化；比较前确认报告期与合并/归母口径。",
    }


def announcements_response(symbol: str, start: str, end: str, limit: int, market: str = "a"):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    if market == "hk":
        raise ValueError("港股公告尚未接入 HKEX 披露源；不会将港股代码误查询为巨潮 A 股公告。")
    start_date = (pd.Timestamp(start).strftime("%Y%m%d") if start else (date.today() - timedelta(days=180)).strftime("%Y%m%d"))
    end_date = pd.Timestamp(end).strftime("%Y%m%d") if end else date.today().strftime("%Y%m%d")
    frame = ak.stock_zh_a_disclosure_report_cninfo(symbol=symbol, market="沪深京", start_date=start_date, end_date=end_date)
    if frame.empty:
        return {"symbol": symbol, "market": market, "source": "CNINFO via AKShare", "as_of": str(date.today()), "announcements": []}
    records = [clean_record(item) for item in frame.head(limit).to_dict("records")]
    return {"symbol": symbol, "market": market, "source": "CNINFO via AKShare", "as_of": str(date.today()), "announcements": records}


def analyze_stock_response(symbol: str, announcement_days: int = 180, panorama=None, market: str = "a", deep_research: bool = True):
    market = normalize_market(market)
    symbol = normalize_symbol(symbol, market)
    unavailable = []

    def attempt(label, function):
        try:
            return function()
        except Exception as exc:
            unavailable.append(f"{label}不可用：{exc}")
            return {}

    quote = attempt("行情", lambda: quote_response(symbol, market))
    indicators = attempt("技术指标", lambda: indicators_response(symbol, 260, "qfq", market))
    fundamentals = attempt("财务", lambda: fundamentals_response(symbol, market))
    valuation = {}
    start = str(date.today() - timedelta(days=announcement_days - 1))
    announcements = attempt("公告", lambda: announcements_response(symbol, start, str(date.today()), 30, market)) if deep_research else {}
    related_enterprises = (attempt("相关大型企业", lambda: related_enterprises_response(symbol, 30, 3)) if market == "a" and deep_research else {
        "symbol": symbol, "market": market, "enterprises": [],
        "unavailable": ["港股同行比较尚未接入；不会将 A 股行业成分或巨潮公告套用于港股。"],
    })

    quote_row = quote.get("quote", {})
    if quote_row:
        valuation = {
            "symbol": symbol, "source": quote.get("source"), "as_of": quote.get("as_of"),
            "valuation": {key: quote_row.get(key) for key in ["代码", "名称", "最新价", "市盈率-动态", "市盈率-静态", "市净率", "总市值", "流通市值", "涨跌幅"] if key in quote_row},
        }
    else:
        valuation = attempt("估值", lambda: valuation_response(symbol, market))
    fallback_name = None
    if not quote_row:
        search = attempt("股票名称", lambda: stock_search_response(symbol, 1, market))
        if search.get("matches"):
            fallback_name = search["matches"][0].get("name")
    latest = indicators.get("latest", {})
    close = safe_float(latest.get("close") or first_value(quote_row, "最新价", "price"))
    ma20, ma60, rsi = (safe_float(latest.get(key)) for key in ("ma20", "ma60", "rsi14"))
    short_points = []
    short_score = 0
    if close is not None and ma20 is not None:
        above = close >= ma20
        short_score += 1 if above else -1
        short_points.append(f"收盘价{'高于' if above else '低于'}MA20")
    if close is not None and ma60 is not None:
        above = close >= ma60
        short_score += 1 if above else -1
        short_points.append(f"收盘价{'高于' if above else '低于'}MA60")
    if rsi is not None:
        if rsi >= 70:
            short_score -= 1
            short_points.append("RSI处于偏热区间")
        elif rsi <= 30:
            short_points.append("RSI处于偏弱区间，反转尚待确认")
        else:
            short_points.append("RSI未处于极端区间")
    short_label = "短期继续跟踪" if short_score >= 2 else ("短期谨慎观察" if short_score <= -1 else "短期等待确认")

    valuation_row = valuation.get("valuation", {})
    pe = safe_float(first_value(valuation_row, "市盈率-动态", "市盈率-静态", "pe"))
    pb = safe_float(first_value(valuation_row, "市净率", "pb"))
    latest_financial = fundamentals.get("records", [{}])[0] if fundamentals.get("records") else {}
    roe = safe_float(first_value(latest_financial, "净资产收益率", "ROE"))
    revenue_growth = safe_float(first_value(latest_financial, "营业收入增长率", "营收增长"))
    profit_growth = safe_float(first_value(latest_financial, "净利润增长率", "净利润同比增长"))
    long_points = []
    long_score = 0
    if pe is not None:
        long_points.append(f"PE约{pe:.2f}")
        long_score += 1 if 0 < pe <= 40 else (-1 if pe <= 0 or pe > 80 else 0)
    if pb is not None:
        long_points.append(f"PB约{pb:.2f}")
    if roe is not None:
        long_points.append(f"最新报告期ROE约{roe:.2f}")
        long_score += 1 if roe >= 10 else (-1 if roe < 3 else 0)
    for label, value in (("营收增速", revenue_growth), ("净利润增速", profit_growth)):
        if value is not None:
            long_points.append(f"{label}约{value:.2f}%")
            long_score += 1 if value > 0 else -1
    if not latest_financial:
        long_points.append("财务数据待补充")
    long_label = "长期进入观察池" if long_score >= 2 else ("长期谨慎复核" if long_score <= -1 else "长期继续观察")
    signal_count = sum(value is not None for value in [close, ma20, ma60, rsi, pe, roe, revenue_growth, profit_growth])
    directional_score = short_score * 7 + long_score * 6
    base_buy_percentage = int(round(max(5, min(95, 50 + directional_score)))) if signal_count else 50
    if panorama is None:
        try:
            panorama = market_panorama_response(market=market)
        except Exception as exc:
            panorama = None
            unavailable.append(f"市场全景不可用：{exc}")
    market_context = market_context_summary(panorama)
    unavailable.extend(f"市场全景：{item}" for item in market_context.get("unavailable", []))
    unavailable = list(dict.fromkeys(unavailable))
    feedback = reflection_adjustment(symbol, market)
    reflection_base = int(round(max(5, min(95, base_buy_percentage + feedback["adjustment"]))))
    calibrated = calibrate_buy_sell(reflection_base, panorama)
    calibrated["raw_base_buy_percentage"] = base_buy_percentage
    calibrated["reflection_adjustment"] = feedback["adjustment"]
    buy_percentage, sell_percentage = calibrated["buy_percentage"], calibrated["sell_percentage"]
    if signal_count < 3:
        trade_conclusion = "信息不足，暂缓决策"
    elif buy_percentage >= 65:
        trade_conclusion = "建议买入"
    elif sell_percentage >= 65:
        trade_conclusion = "建议卖出"
    else:
        trade_conclusion = "建议持有观察"
    confidence = max(10, min(90, 20 + signal_count * 9 - feedback["contradicted"] * 3))
    return {
        "symbol": symbol, "market": market, "name": first_value(quote_row, "名称", "name") or fallback_name,
        "as_of": indicators.get("as_of") or quote.get("as_of") or fundamentals.get("as_of"),
        "short_term": {"horizon": "未来1–4周", "action": short_label, "evidence": short_points or ["技术数据不足"]},
        "long_term": {"horizon": "未来6–24个月", "action": long_label, "evidence": long_points},
        "recommendation": {
            "conclusion": trade_conclusion, "buy_percentage": buy_percentage, "sell_percentage": sell_percentage,
            "confidence": confidence,
            "interpretation": "买入/卖出百分比是证据方向倾向，不是建议仓位，也不是上涨/下跌概率。",
            "method": "基础买入倾向=clamp(50+短期技术分×7+长期基本面估值分×6,5,95)，再按市场风险档调整-20至+12个百分点；校准后买入≥65为建议买入，卖出≥65为建议卖出，否则持有观察；有效信号少于3项则暂缓决策。",
            "score_components": {"short_technical_score": short_score, "long_fundamental_valuation_score": long_score, "valid_signal_count": signal_count, **calibrated},
            "market_calibration": (panorama or {}).get("market_state"),
            "basis": (short_points + long_points)[:6],
        },
        "latest_report_period": fundamentals.get("latest_report_period"),
        "financial_history": fundamentals.get("records", []),
        "recent_announcements": announcements.get("announcements", []),
        "related_large_enterprises": related_enterprises,
        "historical_reflections": feedback,
        "market_context": market_context,
        "quote": quote, "indicators": indicators, "valuation": valuation,
        "unavailable": unavailable,
        "risk_note": "买卖结论基于历史数据和公开披露，可能随价格、财报或公告变化；需结合行业周期、公告原文与个人风险承受能力复核。",
    }


def report_item_price(item: dict):
    direct = safe_float(item.get("price"))
    if direct is not None:
        return direct
    quote_container = item.get("quote") or {}
    quote_row = quote_container.get("quote", quote_container) if isinstance(quote_container, dict) else {}
    quote_price = safe_float(first_value(quote_row, "最新价", "现价", "price"))
    return quote_price if quote_price is not None else safe_float((item.get("indicators") or {}).get("latest", {}).get("close"))


def intraday_alignment(conclusion: str, return_pct):
    if return_pct is None:
        return "insufficient"
    if conclusion == "建议买入":
        return "contradicted" if return_pct <= -2 else ("aligned" if return_pct >= 0 else "inconclusive")
    if conclusion == "建议卖出":
        return "contradicted" if return_pct >= 2 else ("aligned" if return_pct <= 0 else "inconclusive")
    if conclusion == "建议持有观察":
        return "aligned" if abs(return_pct) < 2 else "inconclusive"
    return "insufficient"


def build_intraday_review(morning: dict, afternoon: dict, market: str) -> dict:
    if not morning:
        return {
            "status": "morning_report_missing", "aligned": 0, "contradicted": 0, "inconclusive": 0,
            "comparisons": [], "note": "未找到同日同市场的日报1结构化快照，日报2无法进行逐股一致性复盘。",
        }
    targets = {}
    for item in (morning.get("candidates") or {}).get("ranking", []):
        symbol = item.get("symbol")
        if symbol:
            targets[symbol] = {
                "symbol": symbol, "name": item.get("name"), "origin": "日报1候选",
                "morning_conclusion": item.get("recommendation"), "morning_buy_percentage": item.get("buy_percentage"),
                "morning_price": report_item_price(item), "morning_reason": item.get("reason"),
            }
    current_rank = {item.get("symbol"): item for item in (afternoon.get("candidates") or {}).get("ranking", [])}
    morning_market_score = safe_float(((morning.get("market_panorama") or {}).get("market_state") or {}).get("score"))
    afternoon_market_score = safe_float(((afternoon.get("market_panorama") or {}).get("market_state") or {}).get("score"))
    market_delta = (afternoon_market_score - morning_market_score) if morning_market_score is not None and afternoon_market_score is not None else None
    comparisons, reflection_records = [], []
    for symbol, baseline in targets.items():
        unavailable = []
        current = current_rank.get(symbol, {})
        current_price = report_item_price(current)
        daily_change = safe_float(current.get("pct_change_1d"))
        if current_price is None:
            try:
                quote = quote_response(symbol, market)
                current_price = report_item_price(quote)
                daily_change = safe_float(first_value(quote.get("quote", {}), "涨跌幅", "zdf"))
            except Exception as exc:
                unavailable.append(f"14:30行情不可用：{exc}")
        morning_price = safe_float(baseline.get("morning_price"))
        return_pct = round((current_price / morning_price - 1) * 100, 3) if current_price is not None and morning_price not in (None, 0) else None
        alignment = intraday_alignment(str(baseline.get("morning_conclusion") or ""), return_pct)
        reasons = []
        if market_delta is not None and market_delta <= -8 and return_pct is not None and return_pct < 0:
            reasons.append(f"市场风险分较日报1下降{abs(market_delta):.1f}分，个股下跌与市场转弱同时发生。")
        elif return_pct is not None and return_pct <= -2:
            reasons.append("个股相对日报1价格明显走弱，但仅凭行情无法确认公司层面的因果。")
        elif return_pct is not None and return_pct >= 2:
            reasons.append("个股相对日报1价格明显走强，但盘中涨幅本身不能验证基本面判断。")
        elif return_pct is not None:
            reasons.append("日报1至日报2的价格变化未达到明显背离阈值（2%）。")
        if daily_change is not None:
            reasons.append(f"数据源返回的当日涨跌幅为{daily_change:.2f}%。")
        if current.get("reason"):
            reasons.append(f"日报2量价/估值证据：{current.get('reason')}")
        name = str(baseline.get("name") or "").strip()
        event_groups = afternoon.get("market_panorama") or {}
        event_candidates = event_groups.get("international_events", []) + event_groups.get("domestic_policies", []) + event_groups.get("other_events", [])
        matching_events = [event for event in event_candidates if name and name in str(event.get("title", ""))]
        if matching_events:
            reasons.append("相关事件线索：" + "；".join(str(event.get("title")) for event in matching_events[:2]) + "。标题仅作线索，需复核原文。")
        else:
            reasons.append("当前结构化事件中未找到直接点名该公司的线索；公司公告与突发新闻仍需以正式披露原文复核。")
        if alignment == "contradicted":
            lesson = "降低单一动量或早盘快照的权重；后续同类标的需等待价格与市场宽度共同确认，并下调方向置信度。"
        elif alignment == "aligned":
            lesson = "本次盘中方向与日报1相符，但仍需跨交易日和更大样本验证，不能据单次结果放大权重。"
        else:
            lesson = "本次变化不足以确认或否定日报1；保留原权重并继续收集可验证样本。"
        record = {
            **baseline, "date": afternoon.get("as_of", str(date.today())), "market": market,
            "afternoon_price": current_price, "return_from_morning_pct": return_pct, "daily_change_pct": daily_change,
            "alignment": alignment, "market_score_change": round(market_delta, 2) if market_delta is not None else None,
            "reason_analysis": reasons, "reflection": lesson, "unavailable": unavailable,
            "created_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        }
        comparisons.append(record)
        reflection_records.append(record)
    if reflection_records:
        persist_reflections(reflection_records)
    counts = {key: sum(item["alignment"] == key for item in comparisons) for key in ("aligned", "contradicted", "inconclusive", "insufficient")}
    return {
        "status": "completed", **counts, "comparisons": comparisons,
        "threshold_note": "买入后跌幅≤-2%或卖出后涨幅≥2%记为明显背离；其余按方向相符、待观察或数据不足分类。",
        "method_note": "原因只基于两次价格、当日涨跌幅和市场风险分变化；未核验公告或新闻时不作确定性归因。",
    }


def intraday_review_markdown(review: dict) -> list[str]:
    lines = ["", "## 日报1与日报2一致性复盘｜复盘校准", ""]
    if review.get("status") != "completed":
        return lines + [f"- {review.get('note', '无法完成复盘。')}"]
    lines.extend([
        f"- 结果：相符 {review.get('aligned', 0)}；明显背离 {review.get('contradicted', 0)}；待观察 {review.get('inconclusive', 0)}；数据不足 {review.get('insufficient', 0)}。",
        f"- 判定口径：{review.get('threshold_note')}", "",
        "| 代码 | 名称 | 日报1结论 | 日报1价格 | 日报2价格 | 区间变化 | 一致性 | 原因分析 | 反思 |",
        "|---|---|---|---:|---:|---:|---|---|---|",
    ])
    labels = {"aligned": "相符", "contradicted": "明显背离", "inconclusive": "待观察", "insufficient": "数据不足"}
    for item in review.get("comparisons", []):
        values = [
            item.get("symbol"), item.get("name"), item.get("morning_conclusion"), item.get("morning_price"), item.get("afternoon_price"),
            f"{item['return_from_morning_pct']:.2f}%" if item.get("return_from_morning_pct") is not None else None,
            labels.get(item.get("alignment"), item.get("alignment")), " ".join(item.get("reason_analysis", [])), item.get("reflection"),
        ]
        lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
    return lines + ["", f"复盘方法：{review.get('method_note')}"]


def markdown_cell(value):
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def global_market_response():
    """One timestamped global-index snapshot; never fill missing regions with guesses."""
    wanted = {"000001": "A股", "399001": "A股", "HSI": "港股", "HSCEI": "港股",
              "DJIA": "美股", "SPX": "美股", "NDX": "美股", "N225": "日股",
              "KS11": "韩股", "KOSPI200": "韩股"}
    frame = ak.index_global_spot_em()
    indices = []
    for row in frame.to_dict("records"):
        code = str(row.get("代码", ""))
        if code not in wanted:
            continue
        indices.append({"region": wanted[code], "code": code, "name": str(row.get("名称", code)),
                        "price": safe_float(row.get("最新价")), "change_pct": safe_float(row.get("涨跌幅")),
                        "quote_time": str(row.get("最新行情时间") or "")})
    return {"source": "AKShare index_global_spot_em / 东方财富全球指数", "indices": indices,
            "as_of": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds")}


def sector_focus_response():
    """Match named themes against actual board rows, preserving missing-theme visibility."""
    # AKShare currently memoizes the entire concept-board frame; refresh it for the 14:30 run.
    try:
        module = importlib.import_module(ak.stock_board_concept_name_em.__module__)
        cached = getattr(module, "__stock_board_concept_name_em", None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()
    except (AttributeError, ImportError):
        pass
    frame = ak.stock_board_concept_name_em()
    themes = {"半导体": ("半导体", "芯片"), "存储": ("存储芯片", "存储"),
              "光通信": ("光通信", "CPO", "光模块")}
    boards, missing = [], []
    for theme, aliases in themes.items():
        matches = frame[frame["板块名称"].astype(str).map(lambda name: any(alias in name for alias in aliases))]
        if matches.empty:
            missing.append(theme)
            continue
        for row in matches.head(3).to_dict("records"):
            boards.append({"theme": theme, "name": row.get("板块名称"), "change_pct": safe_float(row.get("涨跌幅")),
                           "advancing": to_number(row.get("上涨家数")), "declining": to_number(row.get("下跌家数")),
                           "leader": row.get("领涨股票")})
    return {"source": "AKShare stock_board_concept_name_em / 东方财富概念板块", "boards": boards,
            "missing_themes": missing, "as_of": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds")}


def horizon_recommendations(candidates: dict, market: str, panorama: dict):
    """Analyze a small, disclosed candidate pool; abstain if period-specific evidence is absent."""
    source_rows = (candidates.get("ranking") or [])[:5]
    analyses = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(source_rows)))) as executor:
        futures = {row["symbol"]: executor.submit(analyze_stock_response, row["symbol"], 30, panorama, market, False)
                   for row in source_rows if row.get("symbol")}
        for symbol, future in futures.items():
            try:
                analyses[symbol] = future.result()
            except Exception as exc:
                analyses[symbol] = {"unavailable": [str(exc)]}
    result = {key: [] for key in ("long", "short", "ultra_short")}
    for row in source_rows:
        analysis = analyses.get(row.get("symbol"), {})
        evidence = analysis.get("recommendation", {}).get("score_components", {})
        long_score = evidence.get("long_fundamental_valuation_score")
        technical_score = evidence.get("short_technical_score")
        financial_period = analysis.get("latest_report_period")
        quote = analysis.get("quote", {}).get("quote", {})
        day_change = safe_float(first_value(quote, "涨跌幅", "zdf"))
        turnover = safe_float(first_value(quote, "换手率", "hsl"))
        common = {"symbol": row.get("symbol"), "name": row.get("name"), "price": report_item_price(row),
                  "financial_period": financial_period, "pe_ttm": row.get("pe_ttm"),
                  "missing": analysis.get("unavailable", [])[:3]}
        if long_score is not None and financial_period and long_score >= 1:
            result["long"].append({**common, "score": long_score * 10 + (technical_score or 0),
                                   "basis": "；".join(analysis.get("long_term", {}).get("evidence", [])[:4]),
                                   "risk": "财报可能滞后；估值需与行业及现金流比较。"})
        if technical_score is not None and technical_score >= 1 and long_score is not None:
            result["short"].append({**common, "score": technical_score * 10 + long_score,
                                    "basis": "；".join(analysis.get("short_term", {}).get("evidence", [])[:3]),
                                    "risk": "趋势可能反转；需结合公告及最新财报。"})
        if technical_score is not None and technical_score >= 1 and day_change is not None and turnover is not None and 0 < day_change < 8 and 1 <= turnover <= 12:
            result["ultra_short"].append({**common, "score": technical_score * 10 + day_change,
                                          "basis": f"当日涨跌{day_change:.2f}%；换手{turnover:.2f}%；" + "；".join(analysis.get("short_term", {}).get("evidence", [])[:2]),
                                          "risk": "超短线波动和交易成本高；开盘前行情不能作为盘中入场信号。"})
    for key in result:
        result[key] = sorted(result[key], key=lambda item: item["score"], reverse=True)[:5]
    return {"market": market, "source": "候选池 + 个股财务/估值/技术分析", "pool_size": len(source_rows),
            "horizons": result, "note": "仅为有限候选池的研究观察名单；证据不足时留空，不代表全市场最优或个性化买入建议。"}


def portfolio_analysis_response(market: str = "a", strategy: str = ""):
    from portfolio_research import selected_strategy, strategy_review, build_position_summary
    market = normalize_market(market)
    strategy = selected_strategy(strategy)
    holdings = [item for item in portfolio_response()["holdings"] if item.get("market", "a") == market]
    if not holdings:
        return {"market": market, "available": False, "holdings": [], "note": "未保存该市场持仓，无法分析。"}
    try:
        panorama = market_panorama_response(market)
    except Exception as error:
        logging.getLogger(__name__).warning("持仓市场全景获取失败：%s", error)
        # A missing shared panorama must not trigger another fetch per holding.
        panorama = {"unavailable": readable_issues([f"市场全景不可用：{error}"]),
                    "market_state": {"risk_level": "未知"}, "market_breadth": {"label": "数据不足"}}
    def analyze(holding):
        try:
            item = analyze_stock_response(holding["symbol"], 30, panorama, market, False)
        except Exception as exc:
            logging.getLogger(__name__).warning("持仓个股分析 %s: %s", holding["symbol"], exc)
            return {"holding": holding, "price": None, "quote": {}, "analysis": {"unavailable": readable_issues([f"个股分析不可用：{exc}"])}, "suitable_horizon": "待补充数据",
                    "strategy": "数据源暂不可用，保持原策略并人工复核，不据此调整仓位。"}
        components = item.get("recommendation", {}).get("score_components", {})
        long_score = components.get("long_fundamental_valuation_score")
        short_score = components.get("short_technical_score")
        if long_score is not None and long_score >= 2:
            horizon = "中长线观察"
        elif short_score is not None and short_score >= 1:
            horizon = "短线跟踪"
        else:
            horizon = "暂缓加仓，复核基本面与趋势"
        compact = {key: item.get(key) for key in ("name", "as_of", "latest_report_period", "short_term", "long_term", "recommendation", "valuation", "market_context")}
        compact["unavailable"] = readable_issues(item.get("unavailable", []))
        return {"holding": holding, "price": report_item_price(item), "quote": item.get("quote", {}).get("quote", {}), "analysis": compact, "suitable_horizon": horizon,
                "strategy": f"{item.get('recommendation', {}).get('conclusion', '信息不足')}；{horizon}。仅基于当前公开数据，不自动交易。"}
    with ThreadPoolExecutor(max_workers=min(4, len(holdings))) as executor:
        analyzed = list(executor.map(analyze, holdings))
    strategy_result = strategy_review(analyzed, strategy, market)
    summary = build_position_summary(analyzed, panorama, strategy_result)
    # Raw quote fields are transient strategy inputs, not needed by the UI.
    for item in analyzed:
        item.pop("quote", None)
    return {"market": market, "available": True, "as_of": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds"),
            "holdings": analyzed, "strategy_review": strategy_result, "position_summary": summary,
            "note": "配置权重用于假设研究预算，不是实际账户仓位；现金和总资产未提供，不计算交易数量。"}


def daily_digest_markdown(panorama: dict, candidates: dict, weekly: dict, warnings: list) -> list[str]:
    """Give text archives the same factual overview as the visual report."""
    breadth = panorama.get("market_breadth", {})
    counts = [safe_float(breadth.get(key)) for key in ("advancing", "declining", "unchanged")]
    if (all(value is not None and value >= 0 for value in counts[:2])
            and (counts[2] is None or counts[2] >= 0) and sum(value or 0 for value in counts) > 0):
        total = sum(value or 0 for value in counts)
        scope = "有效样本（含平盘）" if counts[2] is not None else "涨跌样本（平盘未取得）"
        breadth_note = f"上涨占比 {counts[0] / total:.1%}，共 {int(total)} 只{scope}。"
    else:
        breadth_note = "市场涨跌分布未取得，暂不计算上涨占比。"
    warning_labels = list(dict.fromkeys(str(item).split("：", 1)[0] for item in warnings))
    return [
        "## 本期速览", "",
        f"- 市场：{panorama.get('market_state', {}).get('summary', '数据不足')} {breadth_note}",
        f"- 研究范围：{len(candidates.get('ranking', []))} 只候选；最近一周归档 {len(weekly.get('events', []))} 条事件线索。候选池与新闻档案均非全市场完整覆盖。",
        "- 数据状态：" + ("；".join(warning_labels) + "。部分评分可能采用默认输入，请结合缺失项阅读。" if warning_labels else "仍需逐项核对各来源的行情时间与财报期。"),
        "- 阅读顺序：市场分布与风险 → 多周期候选对比 → 事件原文核验 → 下午一致性复盘。网页图表仅使用本期归档数据。", "",
    ]


def daily_report_response(candidate_limit: int = 5, market: str = "a", session: str = "morning"):
    market = normalize_market(market)
    session = normalize_report_session(session)
    calendar = trading_day_response(market=market)
    if not calendar["is_trading_day"]:
        return {
            "as_of": str(date.today()), "market": market, "session": session, "is_trading_day": False, "has_holdings": False,
            "empty_reason": "non_trading_day", "markdown": "", "source": calendar["source"],
            "note": "今日不是交易日，不生成日报。",
        }
    report_warnings = []
    try:
        panorama = market_panorama_response(market=market)
        report_warnings.extend(panorama.get("unavailable", []))
    except Exception as exc:
        panorama = {"market_state": {"regime": "neutral", "risk_level": "未知", "reference_position": "待数据恢复", "buy_tendency_adjustment": 0}}
        report_warnings.append(f"市场全景不可用：{exc}")

    try:
        global_markets = global_market_response()
    except Exception as exc:
        global_markets = {"indices": [], "source": "AKShare index_global_spot_em"}
        report_warnings.append(f"全球指数不可用：{exc}")
    try:
        sectors = sector_focus_response()
    except Exception as exc:
        sectors = {"boards": [], "missing_themes": ["半导体", "存储", "光通信"], "source": "AKShare stock_board_concept_name_em"}
        report_warnings.append(f"主题板块不可用：{exc}")
    try:
        weekly = market_events_response(7, 20)
        monthly = market_events_response(30, 40)
    except Exception as exc:
        report_warnings.append(f"市场事件不可用：{exc}")
        weekly = {"events": []}
        monthly = {"events": []}
    try:
        candidates = candidate_ranking_response(candidate_limit, panorama, market)
    except Exception as exc:
        report_warnings.append(f"候选排行不可用：{exc}")
        candidates = {"ranking": [], "method": "当日候选数据不可用。"}
    try:
        horizons = horizon_recommendations(candidates, market, panorama)
    except Exception as exc:
        horizons = {"horizons": {key: [] for key in ("long", "short", "ultra_short")}, "pool_size": 0}
        report_warnings.append(f"分周期分析不可用：{exc}")
    report_number = "日报1" if session == "morning" else "日报2"
    timing = "09:20开始汇总，目标于09:30开盘前送达" if session == "morning" else "14:30开始汇总，收盘前进行盘中复盘"
    phase_title = "开盘前" if session == "morning" else "收盘前"
    lines = [
        f"# {market_label(market)}{report_number}｜{phase_title}研究简报", "", f"数据日期：{date.today()}（{timing}）", "",
        *daily_digest_markdown(panorama, candidates, weekly, report_warnings),
        "## 市场全景", "",
        f"- 市场状态：{panorama.get('market_state', {}).get('summary', '数据不足')}",
        f"- 风险等级：{panorama.get('market_state', {}).get('risk_level', '未知')}；参考仓位：{panorama.get('market_state', {}).get('reference_position', '待评估')}",
        f"- 大盘宽度：{panorama.get('market_breadth', {}).get('label', '数据不足')}（上涨{panorama.get('market_breadth', {}).get('advancing', '—')} / 下跌{panorama.get('market_breadth', {}).get('declining', '—')}，宽度分{panorama.get('market_breadth', {}).get('score', '—')}）",
        f"- 社交情绪：{panorama.get('social_sentiment', {}).get('label', '数据不足')}（情绪分{panorama.get('social_sentiment', {}).get('score', '—')}；该分数含价格情绪代理）", "",
        "### 全球主要指数", "",
        "| 市场 | 指数 | 最新点位 | 涨跌幅 | 行情时间 |", "|---|---|---:|---:|---|",
        *["| " + " | ".join(markdown_cell(value) for value in (item.get("region"), item.get("name"), item.get("price"),
          f"{item['change_pct']:+.2f}%" if item.get("change_pct") is not None else None, item.get("quote_time"))) + " |"
          for item in global_markets.get("indices", [])],
        "", f"来源：{global_markets.get('source', '未取得')}。跨市场开收盘时间不同；表内为各指数自身最近行情时间，并非同一时刻同步交易。", "",
        "### 重点板块", "",
        "| 主题 | 板块 | 涨跌幅 | 上涨/下跌家数 | 领涨股 |", "|---|---|---:|---:|---|",
        *["| " + " | ".join(markdown_cell(value) for value in (item.get("theme"), item.get("name"),
          f"{item['change_pct']:+.2f}%" if item.get("change_pct") is not None else None,
          f"{item.get('advancing', '—')}/{item.get('declining', '—')}", item.get("leader"))) + " |"
          for item in sectors.get("boards", [])],
        "", f"来源：{sectors.get('source', '未取得')}。未匹配主题：" + ("、".join(sectors.get("missing_themes", [])) or "无") + "。板块仅覆盖 A 股概念板块，不代表港股或海外同行。", "",
        "### 大盘研判", "",
        f"- 当前{market_label(market)}风险等级为{panorama.get('market_state', {}).get('risk_level', '未知')}，宽度分{panorama.get('market_breadth', {}).get('score', '—')}；"
        "若上涨家数明显少于下跌家数，说明上涨集中于少数标的，需防范指数与个股体感背离。", "",
        "### 国际事件", "",
        *([f"- {event.get('date') or '日期待核验'} {event.get('title')}（{event.get('tone', 'neutral')}）" for event in panorama.get('international_events', [])[:6]] or ["- 暂无可确认的国际事件线索。"]), "",
        "### 国内政策", "",
        *([f"- {event.get('date') or '日期待核验'} {event.get('title')}（{event.get('tone', 'neutral')}）" for event in panorama.get('domestic_policies', [])[:6]] or ["- 暂无可确认的国内政策线索。"]),
    ]
    lines.extend(["", "## 财经新闻与最近一周重要事件线索", "", f"来源：{weekly.get('source', 'AKShare stock_info_global_cls')}；标题为线索，需核对原始公告。", ""])
    lines.extend([f"- {event.get('date') or '日期待核验'}【{event.get('theme', '综合事件')}】{event['title']}；可能路径：{event.get('possible_impact_path', '待核验')}" for event in weekly["events"][:10]] or ["- 暂无可得事件。"])
    weekly_titles = {event["title"] for event in weekly["events"]}
    month_only = [event for event in monthly["events"] if event["title"] not in weekly_titles]
    lines.extend(["", "## 最近一月其他重要事件线索", "", "以下为月度档案中未在上述周度列表展示的线索，日期仍可能位于本周；不代表上月事件。", ""])
    lines.extend([f"- {event.get('date') or '日期待核验'}【{event.get('theme', '综合事件')}】{event['title']}；可能路径：{event.get('possible_impact_path', '待核验')}" for event in month_only[:10]] or ["- 当前月度档案暂无额外线索。"])
    lines.extend(["", "## 热门股票推荐排行", "", "| 排名 | 代码 | 名称 | 评分 | 市场校准 | 买入倾向 | 卖出倾向 | 1日 | 5日 | 20日 | PE(TTM) | 买卖建议 | 推荐理由与风险 |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"])
    for item in candidates["ranking"]:
        lines.append("| " + " | ".join(markdown_cell(value) for value in [
            item.get("rank"), item.get("symbol"), item.get("name"), item.get("score"),
            f"{item.get('base_buy_percentage', item['buy_percentage'])}% {item.get('market_adjustment', 0):+d}",
            f"{item['buy_percentage']}%", f"{item['sell_percentage']}%", item.get("pct_change_1d"), item.get("pct_change_5d"),
            item.get("pct_change_20d"), item.get("pe_ttm"), item.get("recommendation"),
        ]) + " | " + markdown_cell(item.get("reason", "") + item.get("risk", "")) + " |")
    lines.extend(["", f"评分方法：{candidates['method']}", "", "说明：买入/卖出百分比表示当前证据的方向倾向，两者合计100%；不是仓位比例或涨跌概率。结论可能随新行情、财报和公告变化。"])
    lines.extend(["", "## 分周期研究观察名单", "", "网页上可切换中长线、短线、超短线；仅分析上述候选池，缺少有效证据时不会强行推荐。"])
    for key, label in (("long", "中长线"), ("short", "短线"), ("ultra_short", "超短线")):
        items = horizons.get("horizons", {}).get(key, [])
        summary = "；".join(f"{item.get('name') or item.get('symbol')}（{item.get('symbol')}，{item.get('basis', '证据待补')}）" for item in items[:3])
        lines.append(f"- {label}：{summary or '本期无满足证据门槛的标的'}")
    historical = load_reflections(market=market, limit=10)
    if historical:
        lines.extend(["", "## 历史反思参考", ""])
        for item in historical[:5]:
            lines.append(f"- {item.get('date')} {item.get('symbol')}：{item.get('reflection')}（一致性：{item.get('alignment')}）")
    payload = {
        "as_of": str(date.today()), "market": market, "session": session, "report_number": report_number,
        "is_trading_day": True, "has_holdings": False, "market_panorama": panorama, "portfolio": [],
        "global_markets": global_markets, "sectors": sectors, "horizon_recommendations": horizons,
        "weekly_events": weekly, "monthly_events": monthly, "candidates": candidates,
        "historical_reflections": historical, "warnings": report_warnings,
        "note": "持仓分析仅在日报网页点击持仓分析按钮后按需生成，不写入定时日报。",
    }
    if session == "afternoon":
        morning = load_json_object(snapshot_path(str(date.today()), market, "morning"), {})
        review = build_intraday_review(morning, payload, market)
        payload["intraday_review"] = review
        lines.extend(intraday_review_markdown(review))
    if report_warnings:
        # Keep raw diagnostics in the snapshot; the report needs readable missing-source labels.
        labels = list(dict.fromkeys(str(warning).split("：", 1)[0] for warning in report_warnings))
        lines.extend(["", "## 数据缺失", ""] + [f"- {label}；对应图表或分析可能不完整。" for label in labels])
    payload["markdown"] = "\n".join(lines)
    payload["snapshot_path"] = str(save_report_snapshot(payload))
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        value = lambda key, default="": query.get(key, [default])[0]
        try:
            if parsed.path == "/health":
                return self.send_json(200, {"status": "ok", "bind": f"{HOST}:{PORT}", "mode": "research-only; local portfolio storage"})
            if parsed.path == "/v1/portfolio":
                return self.send_json(200, portfolio_response())
            if parsed.path == "/v1/portfolio-analysis":
                return self.send_json(200, portfolio_analysis_response(value("market", "a"), value("strategy")))
            if parsed.path == "/v1/search":
                limit = min(max(int(value("limit", "10")), 1), 50)
                return self.send_json(200, stock_search_response(value("query"), limit, value("market", "a")))
            if parsed.path == "/v1/trading-day":
                return self.send_json(200, trading_day_response(value("date"), value("market", "a")))
            if parsed.path == "/v1/market-brief":
                limit = min(max(int(value("event_limit", "20")), 1), 50)
                return self.send_json(200, market_brief_response(limit, value("market", "a")))
            if parsed.path == "/v1/market-events":
                days = min(max(int(value("days", "7")), 1), 90)
                limit = min(max(int(value("limit", "20")), 1), 100)
                return self.send_json(200, market_events_response(days, limit))
            if parsed.path == "/v1/market-movers":
                limit = min(max(int(value("limit", "20")), 1), 50)
                return self.send_json(200, market_movers_response(limit, value("market", "a")))
            if parsed.path == "/v1/market-panorama":
                return self.send_json(200, market_panorama_response(value("market", "a")))
            if parsed.path == "/v1/candidate-ranking":
                limit = min(max(int(value("limit", "5")), 1), 20)
                return self.send_json(200, candidate_ranking_response(limit, market=value("market", "a")))
            if parsed.path == "/v1/daily-report":
                limit = min(max(int(value("candidate_limit", "5")), 1), 20)
                return self.send_json(200, daily_report_response(limit, value("market", "a"), value("session", "morning")))
            if parsed.path == "/v1/reflections":
                limit = min(max(int(value("limit", "50")), 1), 200)
                return self.send_json(200, reflections_response(value("market", "a"), value("symbol"), limit))
            market = normalize_market(value("market", "a"))
            symbol = normalize_symbol(value("symbol"), market)
            if parsed.path == "/v1/quote": payload = quote_response(symbol, market)
            elif parsed.path == "/v1/ohlcv": payload = bars_response(symbol, value("start"), value("end"), value("adjust", "qfq"), market)
            elif parsed.path == "/v1/indicators": payload = indicators_response(symbol, int(value("lookback", "260")), value("adjust", "qfq"), market)
            elif parsed.path == "/v1/fundamentals": payload = fundamentals_response(symbol, market)
            elif parsed.path == "/v1/announcements": payload = announcements_response(symbol, value("start"), value("end"), min(max(int(value("limit", "20")), 1), 100), market)
            elif parsed.path == "/v1/valuation": payload = valuation_response(symbol, market)
            elif parsed.path == "/v1/related-enterprises":
                if market != "a":
                    raise ValueError("港股同行比较尚未接入；不会将 A 股行业成分用于港股。")
                payload = related_enterprises_response(
                    symbol, min(max(int(value("days", "30")), 1), 180), min(max(int(value("limit", "3")), 1), 5),
                )
            elif parsed.path == "/v1/analysis": payload = analyze_stock_response(symbol, min(max(int(value("announcement_days", "180")), 1), 730), market=market)
            else: return self.send_json(404, {"error": "Unknown endpoint"})
            self.send_json(200, payload)
        except Exception as exc:
            self.send_json(502, {"error": f"数据查询失败：{exc}", "hint": "检查代码、网络、AKShare版本和数据源状态；不要用模型补写缺失数据。"})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/v1/portfolio":
                return self.send_json(404, {"error": "Unknown endpoint"})
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 100_000:
                raise ValueError("请求体大小无效。")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self.send_json(200, save_portfolio(payload))
        except Exception as exc:
            self.send_json(400, {"error": f"保存持仓失败：{exc}"})


if __name__ == "__main__":
    print(f"Serving on http://{HOST}:{PORT} (read-only, Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
