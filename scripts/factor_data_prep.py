#!/usr/bin/env python
"""Acquire and validate historical, time-aware inputs for the factor backtest.

Raw vendor replies are cached separately. The two files consumed by the
backtester are published only after all coverage checks pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests

from factor_backtest import DEFAULT_SLOT, TRAIN_DAYS, VALIDATION_DAYS, STEP_DAYS, validate_context
from factor_models import EXECUTION_BAR


ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "factor-lab"
RAW = STORE / "source-cache"
BARS = STORE / "bars"
MINUTE_CORRECTIONS = ROOT / "data-corrections" / "minute"
TOKEN_FILE = ROOT / "config" / "tushare.token"
CHINA = timezone(timedelta(hours=8))
TOP_N = 40
MIN_CANDIDATES = 12
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "stock_st": "ts_code,trade_date,name,type",
    "stk_limit": "ts_code,trade_date,up_limit,down_limit",
    "suspend_d": "ts_code,trade_date,suspend_type,suspend_timing",
    "stock_basic": "ts_code,symbol,name,list_status,list_date,delist_date",
}


def symbol_of(value: str) -> str:
    value = str(value).split(".")[0]
    if len(value) != 6 or not value.isdigit() or value[0] not in "036":
        raise ValueError(f"非沪深 A 股股票代码：{value}")
    return value


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(value: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise RuntimeError(f"缺少原始数据：{path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _baostock_login(bs) -> None:
    try:
        login = bs.login()
    except (OSError, UnboundLocalError) as exc:
        raise RuntimeError("BaoStock 连接失败；请检查网络后重试，已下载的历史缓存会保留。") from exc
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败：{login.error_msg}")


class TushareReader:
    """Small HTTP reader; never writes or logs the token."""

    def __init__(self, token: str | None = None):
        if not token:
            token = os.environ.get("TUSHARE_TOKEN", "").strip()
        if not token:
            if TOKEN_FILE.is_file():
                token = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if not token:
            raise RuntimeError("缺少 Tushare Token；请配置 TUSHARE_TOKEN 或本机 config/tushare.token。")
        self.token = token
        self._last_daily_request = 0.0

    def query(self, name: str, params: dict) -> pd.DataFrame:
        request = {"api_name": name, "token": self.token, "params": params,
                   "fields": FIELDS[name]}
        for attempt in range(5):
            try:
                if name == "daily":
                    delay = 1.3 - (time.monotonic() - self._last_daily_request)
                    if delay > 0:
                        time.sleep(delay)
                    self._last_daily_request = time.monotonic()
                response = requests.post("http://api.tushare.pro", json=request, timeout=35)
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    if "频率超限" in str(payload.get("msg")) and attempt < 4:
                        time.sleep(65)
                        continue
                    raise RuntimeError(f"Tushare {name} 返回错误：{payload.get('msg')}")
                data = payload.get("data") or {}
                frame = pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])
                if name == "daily" and len(frame) >= 6000:
                    raise RuntimeError("单日全市场日线达到接口行数上限，需分页或更换来源。")
                if name == "stk_limit" and len(frame) >= 5800:
                    raise RuntimeError("涨跌停价达到接口行数上限，需分页或更换来源。")
                return frame
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise RuntimeError(f"Tushare {name} 请求失败：{exc}") from exc
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")


def trading_days(calendar: pd.DataFrame, start: str, end: str) -> list[str]:
    needed = {"cal_date", "is_open", "pretrade_date"}
    if not needed.issubset(calendar.columns):
        raise RuntimeError("交易日历缺少 cal_date,is_open,pretrade_date。")
    rows = calendar[calendar.is_open.astype(str).eq("1")].copy()
    rows = rows[(rows.cal_date >= start) & (rows.cal_date <= end)]
    days = sorted(rows.cal_date.unique())
    if not days or len(days) != len(rows) or rows.pretrade_date.eq("").any():
        raise RuntimeError("交易日历缺失、重复或前一交易日字段为空。")
    return days


def fetch_calendar_baostock(start: str, end: str) -> pd.DataFrame:
    import baostock as bs

    _baostock_login(bs)
    try:
        first = (pd.Timestamp(start) - pd.Timedelta(days=21)).strftime("%Y-%m-%d")
        frame = _bs_rows(bs.query_trade_dates(start_date=first, end_date=end), "交易日历")
    finally:
        bs.logout()
    open_days = frame.loc[frame.is_trading_day.astype(str).eq("1"), "calendar_date"].astype(str).tolist()
    if len(open_days) < 2:
        raise RuntimeError("BaoStock 交易日历缺少前一交易日。")
    prior = dict(zip(open_days[1:], open_days[:-1]))
    frame["cal_date"] = frame.calendar_date.astype(str).str.replace("-", "", regex=False)
    frame["is_open"] = frame.is_trading_day.astype(str)
    frame["pretrade_date"] = frame.calendar_date.map(prior).fillna("").str.replace("-", "", regex=False)
    return frame[["cal_date", "is_open", "pretrade_date"]]


def fetch_stock_basic_baostock() -> pd.DataFrame:
    import baostock as bs

    _baostock_login(bs)
    try:
        frame = _bs_rows(bs.query_stock_basic(), "上市资料")
    finally:
        bs.logout()
    frame = frame[frame.type.astype(str).eq("1")].copy()
    frame["symbol"] = frame.code.astype(str).str.split(".").str[-1]
    frame = frame[frame.symbol.str.fullmatch(r"[036]\d{5}")]
    frame["list_date"] = frame.ipoDate.astype(str).str.replace("-", "", regex=False)
    frame["delist_date"] = frame.outDate.astype(str).str.replace("-", "", regex=False)
    return frame[["symbol", "list_date", "delist_date"]]


def collect_tushare(start: str, end: str, raw: Path = RAW,
                    token: str | None = None) -> list[str]:
    reader = TushareReader(token)
    calendar_path = raw / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv"
    if not calendar_path.exists():
        calendar = fetch_calendar_baostock(start, end)
        _atomic_csv(calendar, calendar_path)
    calendar = _read_csv(calendar_path)
    days = trading_days(calendar, start.replace("-", ""), end.replace("-", ""))
    previous = calendar.set_index("cal_date").loc[days[0], "pretrade_date"]
    needed_days = sorted(set(days + [previous]))
    basic_path = raw / "stock_basic_baostock.csv"
    if not basic_path.exists():
        _atomic_csv(fetch_stock_basic_baostock(), basic_path)
    for index, day in enumerate(needed_days, 1):
        path = raw / "daily" / f"{day}.csv"
        if not path.exists():
            _atomic_csv(reader.query("daily", {"trade_date": day}), path)
        if index % 20 == 0 or index == len(needed_days):
            print(f"已缓存 {index}/{len(needed_days)} 个交易日的全市场日线", flush=True)
    return days


def _source_marker(raw: Path, start: str, end: str) -> Path:
    return raw / f"daily-source_{start.replace('-', '')}_{end.replace('-', '')}.json"


def _validated_public_daily(history: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Keep explicit halts with zero turnover; reject missing active turnover."""
    if not {"date", "code", "amount", "tradestatus"}.issubset(history):
        raise RuntimeError(f"{symbol} BaoStock 日线缺少必要字段。")
    if history.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "amount", "tradestatus"])
    if history.date.duplicated().any() or not history.code.astype(str).str.endswith(symbol).all():
        raise RuntimeError(f"{symbol} BaoStock 日线代码或日期异常。")
    history = history.copy()
    raw_amount = history.amount.astype(str).str.strip()
    history["amount"] = pd.to_numeric(raw_amount, errors="coerce")
    halted_without_turnover = history.tradestatus.eq("0") & raw_amount.eq("")
    history.loc[halted_without_turnover, "amount"] = 0.0
    if history.amount.isna().any() or history.amount.lt(0).any() or not history.tradestatus.isin({"0", "1"}).all():
        raise RuntimeError(f"{symbol} BaoStock 日线成交额或交易状态异常。")
    history["trade_date"] = history.date.astype(str).str.replace("-", "", regex=False)
    history["symbol"] = symbol
    return history[["trade_date", "symbol", "amount", "tradestatus"]]


def _tushare_file_token() -> str | None:
    """Automatic source choice intentionally requires the local token file."""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8-sig").strip() or None
    except (OSError, UnicodeError):
        return None


def _usable_tushare(token: str, probe_day: str) -> bool:
    try:
        frame = TushareReader(token).query("daily", {"trade_date": probe_day})
        return (len(frame) >= 40 and
                {"ts_code", "trade_date", "amount"}.issubset(frame.columns) and
                frame.trade_date.astype(str).eq(probe_day).all())
    except (RuntimeError, OSError, ValueError):
        return False


def _check_akshare_calendar(days: list[str]) -> str:
    """Cross-check the public-source calendar when Sina is reachable."""
    probe = (
        "import json, sys, pandas as pd, akshare as ak\n"
        "frame = ak.tool_trade_date_hist_sina()\n"
        "column = next((name for name in ('trade_date', '日期') if name in frame), None)\n"
        "if column is None: raise ValueError('missing date column')\n"
        "available = set(pd.to_datetime(frame[column], errors='coerce').dt.strftime('%Y%m%d').dropna())\n"
        "print(json.dumps(sorted(set(json.loads(sys.stdin.read())) - available)))\n"
    )
    try:
        result = subprocess.run([sys.executable, "-c", probe], input=json.dumps(days),
                                capture_output=True, text=True, timeout=20, check=True)
        missing = json.loads(result.stdout.splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        print(f"  AKShare 交易日历未能完成交叉核对：{type(exc).__name__}；继续使用 BaoStock 历史交易日历。", flush=True)
        return "BaoStock calendar (AKShare cross-check unavailable)"
    if missing:
        raise RuntimeError(f"AKShare 与 BaoStock 交易日历不一致，缺少 {missing[:5]}。")
    return "AKShare Sina calendar cross-checked with BaoStock"


def collect_public(start: str, end: str, raw: Path = RAW) -> tuple[list[str], str]:
    """Rebuild whole-market daily turnover without Tushare or a current-only roster."""
    calendar_path = raw / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv"
    if not calendar_path.exists():
        _atomic_csv(fetch_calendar_baostock(start, end), calendar_path)
    calendar = _read_csv(calendar_path)
    days = trading_days(calendar, start.replace("-", ""), end.replace("-", ""))
    previous = str(calendar.set_index("cal_date").loc[days[0], "pretrade_date"])
    needed_days = sorted(set(days + [previous]))
    calendar_source = _check_akshare_calendar(needed_days)
    basic_path = raw / "stock_basic_baostock.csv"
    if not basic_path.exists():
        _atomic_csv(fetch_stock_basic_baostock(), basic_path)
    basic = _read_csv(basic_path)
    basic_symbols = set(basic.symbol)
    if len(basic_symbols) < 40 or basic.symbol.duplicated().any():
        raise RuntimeError("BaoStock 历史证券基本资料不完整。")

    import baostock as bs

    _baostock_login(bs)
    try:
        roster_by_day = {}
        for index, day in enumerate(needed_days, 1):
            path = raw / "all-stock" / f"{day}.csv"
            if not path.exists():
                result = _bs_rows(bs.query_all_stock(day=pd.Timestamp(day).strftime("%Y-%m-%d")), "历史证券名单")
                _atomic_csv(result, path)
            roster = _read_csv(path)
            if not {"code", "tradeStatus"}.issubset(roster) or roster.code.duplicated().any():
                raise RuntimeError(f"{day} 历史证券名单缺字段或存在重复。")
            # query_all_stock also returns indices whose six-digit codes can
            # collide with Shenzhen stocks (for example sh.000003/sz.000003).
            roster = roster[roster.code.str.fullmatch(r"(?:sh\.6\d{5}|sz\.(?:00|30)\d{4})")].copy()
            roster["symbol"] = roster.code.astype(str).str.split(".").str[-1]
            missing_basic = set(roster.symbol) - basic_symbols
            if missing_basic:
                raise RuntimeError(f"{day} 历史名单有 {len(missing_basic)} 只 A 股缺少上市资料，不能按现存资料筛掉历史股票：{sorted(missing_basic)[:5]}。")
            if len(roster) < 40 or not roster.tradeStatus.isin({"0", "1"}).all():
                raise RuntimeError(f"{day} 历史 A 股名单或交易状态无效。")
            roster_by_day[day] = {
                "all": set(roster.symbol),
                "active": set(roster.loc[roster.tradeStatus.eq("1"), "symbol"]),
            }
            if index % 40 == 0 or index == len(needed_days):
                print(f"  BaoStock 历史证券名单 {index}/{len(needed_days)} 日", flush=True)

        symbols = sorted(set().union(*(item["all"] for item in roster_by_day.values())))
        first, last = pd.Timestamp(previous).strftime("%Y-%m-%d"), pd.Timestamp(days[-1]).strftime("%Y-%m-%d")
        frames = []
        for index, symbol in enumerate(symbols, 1):
            path = raw / "daily-public-symbols" / f"{symbol}_{previous}_{days[-1]}.csv"
            if not path.exists():
                code = "sh." + symbol if symbol.startswith("6") else "sz." + symbol
                history = _bs_rows(bs.query_history_k_data_plus(
                    code, "date,code,amount,tradestatus", start_date=first,
                    end_date=last, frequency="d", adjustflag="3"), "全市场未复权日线")
                _atomic_csv(history, path)
            history = _read_csv(path)
            normalized = _validated_public_daily(history, symbol)
            if normalized.empty:
                continue
            frames.append(normalized)
            if index % 100 == 0 or index == len(symbols):
                print(f"  BaoStock 全市场日线 {index}/{len(symbols)} 只", flush=True)
        if not frames:
            raise RuntimeError("BaoStock 未返回全市场历史日线。")
        all_daily = pd.concat(frames, ignore_index=True)
        if all_daily.duplicated(["trade_date", "symbol"]).any():
            raise RuntimeError("BaoStock 全市场日线存在重复。")
        daily_by_day = {day: frame for day, frame in all_daily.groupby("trade_date", sort=False)}
        for day, roster in roster_by_day.items():
            active = roster["active"]
            current = daily_by_day.get(day)
            if current is None:
                raise RuntimeError(f"{day} 缺少全市场历史日线。")
            observed_active = set(current.loc[current.tradestatus.eq("1"), "symbol"])
            missing = active - observed_active
            if missing:
                raise RuntimeError(f"{day} 有 {len(missing)} 只正常交易股票缺少日线，例如 {sorted(missing)[:5]}。")
            unexpected = observed_active - active
            if unexpected:
                raise RuntimeError(f"{day} 有 {len(unexpected)} 只日线交易状态与历史证券名单冲突。")
            day_rows = current[current.tradestatus.eq("1") & current.amount.gt(0)].copy()
            day_rows = day_rows.rename(columns={"symbol": "ts_code"})[["trade_date", "ts_code", "amount"]]
            _atomic_csv(day_rows, raw / "daily-public" / f"{day}.csv")
    finally:
        bs.logout()
    return days, f"BaoStock historical whole-market unadjusted daily amount; {calendar_source}"


def collect_daily(start: str, end: str, raw: Path = RAW) -> tuple[list[str], str, str]:
    calendar_path = raw / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv"
    if not calendar_path.exists():
        _atomic_csv(fetch_calendar_baostock(start, end), calendar_path)
    calendar = _read_csv(calendar_path)
    days = trading_days(calendar, start.replace("-", ""), end.replace("-", ""))
    probe_day = str(days[-1])
    token = _tushare_file_token()
    if token and _usable_tushare(token, probe_day):
        try:
            result = collect_tushare(start, end, raw, token=token)
            source = "Tushare Pro daily (validated local token file)"
            category = "daily"
        except (RuntimeError, OSError, ValueError):
            print("  Tushare 日线采集未完成，切换到 BaoStock 历史数据并核对 AKShare 日历。", flush=True)
            result, source = collect_public(start, end, raw)
            category = "daily-public"
    else:
        print("  本机无可用 Tushare Token 文件，使用 BaoStock 历史数据并核对 AKShare 日历。", flush=True)
        result, source = collect_public(start, end, raw)
        category = "daily-public"
    _atomic_json({"days": result, "category": category, "daily_source": source},
                 _source_marker(raw, start, end))
    return result, category, source


def _daily_from_cache(days: list[str], raw: Path = RAW, category: str = "daily") -> pd.DataFrame:
    frames = [_read_csv(raw / category / f"{day}.csv") for day in days]
    daily = pd.concat(frames, ignore_index=True)
    if not {"ts_code", "trade_date", "amount"}.issubset(daily.columns):
        raise RuntimeError("全市场日线缺少代码、日期或成交额。")
    if daily.duplicated(["trade_date", "ts_code"]).any():
        raise RuntimeError("全市场日线存在重复交易日与股票代码。")
    daily["symbol"] = daily.ts_code.map(lambda value: str(value).split(".")[0])
    daily["amount"] = pd.to_numeric(daily.amount, errors="coerce")
    if category == "daily":  # Tushare daily amount is reported in thousands of CNY.
        daily["amount"] *= 1000
    return daily


def construct_universe(daily: pd.DataFrame, calendar: pd.DataFrame,
                       start: str, end: str) -> pd.DataFrame:
    """Use yesterday's complete, all-market turnover for each session."""
    days = trading_days(calendar, start.replace("-", ""), end.replace("-", ""))
    prior = calendar.set_index("cal_date").pretrade_date.to_dict()
    records = []
    for day in days:
        previous = prior[day]
        rows = daily[daily.trade_date.eq(previous)].copy()
        rows = rows[rows.symbol.astype(str).str.fullmatch(r"[036]\d{5}")]
        rows = rows[rows.amount.gt(0)].sort_values(["amount", "symbol"], ascending=[False, True])
        if len(rows) < TOP_N:
            raise RuntimeError(f"{previous} 全市场有效日线不足 {TOP_N} 只，拒绝倒推股票池。")
        for symbol in rows.head(TOP_N).symbol:
            records.append({"day": pd.Timestamp(day).strftime("%Y-%m-%d"), "symbol": symbol,
                            "known_at": pd.Timestamp(previous).strftime("%Y-%m-%d") + " 18:00:00+08:00"})
    return pd.DataFrame(records)


def _combine_daily(raw: Path, category: str, days: list[str]) -> pd.DataFrame:
    return pd.concat([_read_csv(raw / category / f"{day}.csv") for day in days], ignore_index=True)


def construct_market_state(universe: pd.DataFrame, calendar: pd.DataFrame,
                           st: pd.DataFrame, limits: pd.DataFrame,
                           suspensions: pd.DataFrame, basic: pd.DataFrame,
                           actions: pd.DataFrame) -> pd.DataFrame:
    """Create a state row for every historically selected symbol and date.

    Missing exact limit prices are accepted only for verified first-five-session
    IPO days. A suspended stock has no executable bar regardless of limits.
    """
    dates = sorted(universe.day.unique())
    symbols = sorted(universe.symbol.unique())
    required = {"ts_code", "trade_date"}
    for name, frame in (("ST", st), ("涨跌停", limits), ("停牌", suspensions)):
        if not required.issubset(frame.columns):
            raise RuntimeError(f"{name} 原始表缺少代码或日期。")
    if not {"symbol", "list_date", "delist_date"}.issubset(basic.columns):
        raise RuntimeError("股票基础表缺少上市和退市日期。")
    if basic.duplicated("symbol").any():
        raise RuntimeError("股票基础表代码重复，无法确认 IPO 无涨跌停阶段。")
    basic_by_symbol = basic.set_index("symbol")
    st_keys = {(str(row.trade_date), str(row.ts_code).split(".")[0]) for row in st.itertuples(index=False)}
    halt_keys = {(str(row.trade_date), str(row.ts_code).split(".")[0])
                 for row in suspensions.itertuples(index=False) if str(row.suspend_type) == "S"}
    limit_by_key = {(str(row.trade_date), str(row.ts_code).split(".")[0]): row
                    for row in limits.itertuples(index=False)}
    if len(limit_by_key) != len(limits):
        raise RuntimeError("涨跌停价存在重复交易日与股票代码。")
    if not {"symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio"}.issubset(actions.columns):
        raise RuntimeError("公司行为表缺少除权日、到账日、现金分红或拆并股比例。")
    action_by_key = {}
    for row in actions.itertuples(index=False):
        if not row.ex_day:
            continue
        key = (str(row.ex_day), str(row.symbol))
        if key in action_by_key:
            raise RuntimeError(f"{key} 有多项公司行为，需逐项核实后合并。")
        action_by_key[key] = row
    sessions = [pd.Timestamp(day).strftime("%Y%m%d") for day in dates]
    records = []
    for symbol in symbols:
        if symbol not in basic_by_symbol.index:
            raise RuntimeError(f"{symbol} 缺少上市日期。")
        first = str(basic_by_symbol.loc[symbol, "list_date"])
        last = str(basic_by_symbol.loc[symbol, "delist_date"])
        ipo_days = ([day for day in sessions if day >= first][:5]
                    if first >= sessions[0] else [])
        for day in dates:
            compact = day.replace("-", "")
            if compact < first or (last and compact > last):
                continue
            key = (compact, symbol)
            halted = key in halt_keys
            limit = limit_by_key.get(key)
            upper = pd.to_numeric(getattr(limit, "up_limit", None), errors="coerce")
            lower = pd.to_numeric(getattr(limit, "down_limit", None), errors="coerce")
            if compact in ipo_days:
                has_limit, upper, lower = False, "", ""
            elif pd.isna(upper) or pd.isna(lower) or upper <= lower or lower <= 0:
                if not halted:
                    raise RuntimeError(f"{day} {symbol} 缺少精确涨跌停价，拒绝估算。")
                has_limit, upper, lower = False, "", ""
            else:
                has_limit = True
            event = action_by_key.get((day, symbol))
            records.append({"day": day, "symbol": symbol, "has_price_limit": has_limit,
                            "limit_up": upper, "limit_down": lower,
                            "split_ratio": float(event.split_ratio) if event else 1.0,
                            "cash_dividend": float(event.cash_dividend) if event else 0.0,
                            "dividend_pay_date": str(event.pay_day) if event else "",
                            "stock_credit_date": str(getattr(event, "stock_credit_date", "")) if event else "",
                            "is_st": key in st_keys, "is_suspended": halted})
    return pd.DataFrame(records)


def _bs_rows(result, label: str) -> pd.DataFrame:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock {label} 失败：{result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def fetch_daily_status_baostock(symbols: list[str], start: str, end: str,
                                raw: Path = RAW) -> pd.DataFrame:
    """Read historical ST, halt and previous close for each selected stock."""
    import baostock as bs

    _baostock_login(bs)
    frames = []
    try:
        for index, symbol in enumerate(symbols, 1):
            path = raw / "daily-status" / f"{symbol}_{start}_{end}.csv"
            if path.exists():
                frame = _read_csv(path)
            else:
                exchange = "sh" if symbol.startswith("6") else "sz"
                frame = _bs_rows(bs.query_history_k_data_plus(
                    f"{exchange}.{symbol}",
                    "date,code,preclose,high,low,tradestatus,isST",
                    start_date=start, end_date=end, frequency="d", adjustflag="3"),
                    f"{symbol} 逐日状态")
                _atomic_csv(frame, path)
            frames.append(frame)
            if index % 20 == 0 or index == len(symbols):
                print(f"已核对 {index}/{len(symbols)} 只股票的逐日状态", flush=True)
    finally:
        bs.logout()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _price_cent(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def construct_market_state_baostock(universe: pd.DataFrame, basic: pd.DataFrame,
                                    daily_status: pd.DataFrame,
                                    actions: pd.DataFrame) -> pd.DataFrame:
    """Use BaoStock's daily ST/halt fields and conservative exchange-rule limits."""
    required = {"date", "code", "preclose", "high", "low", "tradestatus", "isST"}
    if not required.issubset(daily_status.columns):
        raise RuntimeError("BaoStock 历史日线缺少交易状态字段。")
    status = daily_status.copy()
    status["symbol"] = status.code.astype(str).str.split(".").str[-1]
    if status.duplicated(["date", "symbol"]).any():
        raise RuntimeError("BaoStock 逐日状态存在重复。")
    by_key = status.set_index(["date", "symbol"])
    if basic.duplicated("symbol").any():
        raise RuntimeError("上市资料存在重复股票。")
    listed = basic.set_index("symbol")
    action_keys = {}
    for row in actions.itertuples(index=False):
        key = (str(row.ex_day), str(row.symbol))
        if key in action_keys:
            raise RuntimeError(f"{key} 多项公司行为未合并，无法核对除权价。")
        action_keys[key] = row
    days = sorted(universe.day.unique())
    first_seen = universe.groupby("symbol").day.min().to_dict()
    rows = []
    for symbol, first_day in first_seen.items():
        if symbol not in listed.index:
            raise RuntimeError(f"{symbol} 缺少上市日期。")
        listed_on = str(listed.loc[symbol, "list_date"])
        delisted_on = str(listed.loc[symbol, "delist_date"])
        ipo_sessions = ([day for day in days if day.replace("-", "") >= listed_on][:5]
                        if listed_on >= days[0].replace("-", "") else [])
        for day in days:
            if day < first_day or day.replace("-", "") < listed_on or (delisted_on and day.replace("-", "") > delisted_on):
                continue
            record = by_key.loc[(day, symbol)] if (day, symbol) in by_key.index else None
            if record is None:
                raise RuntimeError(f"{day} {symbol} 缺少历史交易状态，不能将缺失数据当作停牌。")
            if str(record.tradestatus) not in {"0", "1"} or str(record.isST) not in {"0", "1"}:
                raise RuntimeError(f"{day} {symbol} 历史交易状态或 ST 标记无效。")
            halted = str(record.tradestatus) == "0"
            st = str(record.isST) == "1"
            event = action_keys.get((day, symbol))
            split = float(event.split_ratio) if event else 1.0
            cash = float(event.cash_dividend) if event else 0.0
            pay = str(event.pay_day) if event else ""
            has_limit = day not in ipo_sessions and not halted
            upper = lower = ""
            if has_limit:
                reference = pd.to_numeric(record.preclose, errors="coerce")
                if pd.isna(reference) or reference <= 0:
                    raise RuntimeError(f"{day} {symbol} 缺少涨跌停计算基准价。")
                # BaoStock preclose is the ex-rights reference on ex dates.
                # Applying the action here again would double-adjust the limit.
                reference = Decimal(str(reference))
                if reference <= 0:
                    raise RuntimeError(f"{day} {symbol} 除权参考价无效。")
                rate = (Decimal("0.20") if symbol.startswith(("300", "301", "688", "689"))
                        else Decimal("0.05") if st and day < "2026-07-06" else Decimal("0.10"))
                upper = _price_cent(reference * (1 + rate))
                lower = _price_cent(reference * (1 - rate))
                high = pd.to_numeric(record.high, errors="coerce")
                low = pd.to_numeric(record.low, errors="coerce")
                if pd.isna(high) or pd.isna(low) or high > upper + 0.011 or low < lower - 0.011:
                    raise RuntimeError(f"{day} {symbol} 价格超出重建涨跌停价；可能涉及特殊交易规则，需补充交易所限价。")
            rows.append({"day": day, "symbol": symbol, "has_price_limit": has_limit,
                         "limit_up": upper, "limit_down": lower, "split_ratio": split,
                         "cash_dividend": cash, "dividend_pay_date": pay,
                         "stock_credit_date": str(getattr(event, "stock_credit_date", "")) if event else "",
                         "is_st": st, "is_suspended": halted})
    return pd.DataFrame(rows)


def fetch_actions_baostock(symbols: list[str], years: list[int],
                           raw: Path = RAW) -> pd.DataFrame:
    import baostock as bs

    _baostock_login(bs)
    collected = []
    try:
        for index, symbol in enumerate(symbols, 1):
            path = raw / "actions" / f"{symbol}.csv"
            if path.exists():
                frame = _read_csv(path)
            else:
                exchange = "sh" if symbol.startswith("6") else "sz"
                frames = [_bs_rows(bs.query_dividend_data(f"{exchange}.{symbol}", year=str(year),
                                                           yearType="operate"),
                                   f"{symbol} {year} 分红") for year in years]
                frame = pd.concat(frames, ignore_index=True).drop_duplicates()
                _atomic_csv(frame, path)
            collected.append(frame)
            if index % 20 == 0 or index == len(symbols):
                print(f"已核对 {index}/{len(symbols)} 只股票的公司行为", flush=True)
    finally:
        bs.logout()
    if not collected:
        raise RuntimeError("没有历史候选股票。")
    source = pd.concat(collected, ignore_index=True).drop_duplicates()
    rows = []
    for row in source.itertuples(index=False):
        ex_day = str(getattr(row, "dividOperateDate", ""))
        if not ex_day:
            continue
        cash = pd.to_numeric(getattr(row, "dividCashPsBeforeTax", ""), errors="coerce")
        stock = pd.to_numeric(getattr(row, "dividStocksPs", ""), errors="coerce")
        reserve = pd.to_numeric(getattr(row, "dividReserveToStockPs", ""), errors="coerce")
        cash = 0.0 if pd.isna(cash) else float(cash)
        stock = 0.0 if pd.isna(stock) else float(stock)
        reserve = 0.0 if pd.isna(reserve) else float(reserve)
        pay_day = str(getattr(row, "dividPayDate", ""))
        stock_day = str(getattr(row, "dividStockMarketDate", ""))
        if cash < 0 or stock < 0 or reserve < 0 or (cash > 0 and not pay_day):
            raise RuntimeError(f"{row.code} {ex_day} 公司行为缺少到账日或数值无效。")
        if stock + reserve > 0 and (not stock_day or stock_day < ex_day):
            raise RuntimeError(f"{row.code} {ex_day} 送转股缺少有效上市交易日。")
        rows.append({"symbol": symbol_of(str(row.code).split(".")[-1]),
                     "ex_day": ex_day, "pay_day": pay_day,
                     "cash_dividend": cash, "split_ratio": 1.0 + stock + reserve})
        rows[-1]["stock_credit_date"] = stock_day
    frame = pd.DataFrame(rows, columns=["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio", "stock_credit_date"])
    if frame.empty:
        return frame
    if frame.groupby(["symbol", "ex_day"]).pay_day.nunique().gt(1).any():
        raise RuntimeError("同一除权日的多笔现金分红到账日不一致，需逐项核实。")
    dated_bonus = frame[frame.stock_credit_date.ne("")]
    if dated_bonus.groupby(["symbol", "ex_day"]).stock_credit_date.nunique().gt(1).any():
        raise RuntimeError("同一除权日的送转股上市日不一致，需逐项核实。")
    frame["extra_shares"] = frame.split_ratio - 1.0
    grouped = frame.groupby(["symbol", "ex_day", "pay_day"], as_index=False).agg(
        cash_dividend=("cash_dividend", "sum"), extra_shares=("extra_shares", "sum"),
        stock_credit_date=("stock_credit_date", lambda values: next((value for value in values if value), "")))
    grouped["split_ratio"] = 1.0 + grouped.pop("extra_shares")
    return grouped[["symbol", "ex_day", "pay_day", "cash_dividend", "split_ratio", "stock_credit_date"]]


def _minute_times(day: str) -> pd.DatetimeIndex:
    return pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min").append(
        pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min"))


def first_warmup_days(universe: pd.DataFrame) -> dict[str, str]:
    """Each first candidate day needs the preceding session's closing price."""
    first = universe.sort_values("day").drop_duplicates("symbol")
    result = {}
    for row in first.itertuples(index=False):
        known = pd.Timestamp(row.known_at)
        known = known.tz_localize(CHINA) if known.tzinfo is None else known.tz_convert(CHINA)
        result[row.symbol] = known.strftime("%Y-%m-%d")
    return result


def _daily_amounts(daily: pd.DataFrame, symbols=None) -> dict:
    if not {"symbol", "trade_date", "amount"}.issubset(daily):
        return {}
    rows = daily[daily.symbol.isin(symbols)].copy() if symbols is not None else daily.copy()
    rows["day"] = pd.to_datetime(rows.trade_date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return {symbol: dict(zip(group.day, group.amount)) for symbol, group in rows.groupby("symbol")}


def minute_quality_issues(frame: pd.DataFrame, daily_amounts: dict | None = None) -> list[dict]:
    """Describe defects without deleting rows or altering vendor prices."""
    fields = ["open", "high", "low", "close", "volume", "amount"]
    if not {"time", *fields}.issubset(frame):
        return [{"reason": "缺少时间或 OHLCV 字段", "count": 1, "days": [], "examples": []}]
    times = pd.to_datetime(frame.time, errors="coerce")
    numeric = frame[fields].apply(pd.to_numeric, errors="coerce")
    issues = []
    def add(reason, mask):
        if not mask.any():
            return
        examples = []
        for index in frame.index[mask][:3]:
            row = {key: str(frame.loc[index, key]) for key in ["time", *fields]}
            if numeric.loc[index, "volume"] > 0:
                row["vwap"] = str(numeric.loc[index, "amount"] / numeric.loc[index, "volume"])
            examples.append(row)
        issues.append({"reason": reason, "count": int(mask.sum()),
                       "days": sorted(set(times[mask].dt.strftime("%Y-%m-%d").dropna())), "examples": examples})
    add("时间戳无效", times.isna())
    add("时间戳重复", times.duplicated(keep=False) & times.notna())
    add("数值缺失或非有限值", pd.Series(~np.isfinite(numeric.to_numpy()).all(axis=1), index=frame.index))
    add("价格非正值", numeric[["open", "high", "low", "close"]].le(0).any(axis=1))
    add("成交量或成交额为负数", numeric[["volume", "amount"]].lt(0).any(axis=1))
    add("成交量与成交额的零值状态冲突", numeric.volume.eq(0) ^ numeric.amount.eq(0))
    add("OHLC 超出价格区间", numeric.low.gt(numeric.high) | numeric.open.lt(numeric.low)
        | numeric.open.gt(numeric.high) | numeric.close.lt(numeric.low) | numeric.close.gt(numeric.high))
    traded = numeric.volume.gt(0) & numeric.amount.gt(0)
    vwap = numeric.amount / numeric.volume.replace(0, np.nan)
    plausible = (vwap.ge(numeric.low * 0.99) & vwap.le(numeric.high * 1.01)) | \
                ((vwap / 100).ge(numeric.low * 0.99) & (vwap / 100).le(numeric.high * 1.01))
    add("成交均价超出 K 线高低价", traded & ~plausible)
    if daily_amounts:
        totals = numeric.amount.groupby(times.dt.strftime("%Y-%m-%d")).sum()
        for day, total in totals.items():
            reference = daily_amounts.get(day)
            # Daily turnover can include block/after-hours trades. A deficit
            # is not evidence of corruption, but duplicated minute amounts
            # must not exceed the whole-day total beyond rounding tolerance.
            if reference is not None and reference > 0 and total > reference * 1.01:
                issues.append({"reason": "分钟成交额合计超出日线总成交额", "count": 1, "days": [day],
                               "examples": [{"day": day, "minute_amount": float(total), "daily_amount": float(reference)}]})
    return issues


def verified_minute_correction(symbol: str, day: str, daily_amounts: dict | None = None):
    """Known vendor defects remain repairable after live APIs lose coverage."""
    path = MINUTE_CORRECTIONS / f"{symbol}_{day}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("symbol") != symbol or record.get("day") != day or not record.get("files"):
        raise RuntimeError(f"{symbol} {day} 随项目保存的分钟线修正记录无效。")
    for name, digest in record["files"].items():
        source = MINUTE_CORRECTIONS / name
        if Path(name).name != name or not source.is_file() or _sha256(source) != digest:
            raise RuntimeError(f"{symbol} {day} 分钟线修正文件校验失败：{name}。")
    name = record.get("correction_file")
    if name not in record["files"]:
        raise RuntimeError(f"{symbol} {day} 分钟线修正记录缺少行情文件。")
    reference = (daily_amounts or {}).get(day)
    if reference is not None and abs(reference / record["daily_amount"] - 1) > 0.01:
        raise RuntimeError(f"{symbol} {day} 当前日线总额与已核验修正记录不一致。")
    frame = pd.read_csv(MINUTE_CORRECTIONS / name)
    if not _minute_cache_complete(frame, [day]) or len(frame) != 48 or minute_quality_issues(
            frame, daily_amounts or {day: record["daily_amount"]}):
        raise RuntimeError(f"{symbol} {day} 保存的分钟线修正数据未通过验收。")
    return frame, record


def repair_minute_days(symbol: str, primary: pd.DataFrame, raw: Path = RAW,
                       daily_amounts: dict | None = None, backup: pd.DataFrame | None = None) -> pd.DataFrame:
    """Replace a defective DAY with verified alternate-source bars, preserving evidence."""
    from factor_lab import normalized_bars, sina_minute_frame
    issues = minute_quality_issues(primary, daily_amounts)
    if not issues:
        return primary
    days = sorted({day for issue in issues for day in issue["days"]})
    if not days or any(not issue["days"] for issue in issues):
        raise RuntimeError(f"{symbol} 分钟线缺字段或时间无效，无法按交易日修复。")
    supplied_backup = backup is not None
    alternate = None
    corrected = primary.copy()
    corrected["time"] = pd.to_datetime(corrected.time)
    evidence = raw / "minute-repairs"
    stamp = datetime.now(CHINA).strftime("%Y%m%d-%H%M%S-%f")
    records = []
    for day in days:
        original = corrected[corrected.time.dt.strftime("%Y-%m-%d").eq(day)].copy()
        saved = None if supplied_backup else verified_minute_correction(symbol, day, daily_amounts)
        if saved is not None:
            saved_frame, saved_record = saved
            replacement = normalized_bars(saved_frame)
            backup_name = f"{symbol}_{day}_{stamp}_verified.csv"
            _atomic_csv(saved_frame, evidence / backup_name)
            source_name = saved_record["source"] + " (bundled verified correction)"
        else:
            if alternate is None:
                backup = sina_minute_frame(("sh" if symbol.startswith("6") else "sz") + symbol) if backup is None else backup
                timestamp_key = next((key for key in ("time", "day", "时间", "日期", "datetime") if key in backup), None)
                if timestamp_key is None or backup.duplicated(timestamp_key).any():
                    raise RuntimeError(f"{symbol} 新浪备用分钟线时间字段缺失或重复。")
                alternate = normalized_bars(backup)
                _atomic_csv(backup, evidence / f"{symbol}_{stamp}_sina_raw.csv")
            replacement = alternate[alternate.time.dt.strftime("%Y-%m-%d").eq(day)].copy()
            backup_name = f"{symbol}_{stamp}_sina_raw.csv"
            source_name = "Sina unadjusted 5-minute bars"
        expected = _minute_times(day)
        if replacement.empty or not replacement.time.isin(expected).all():
            raise RuntimeError(f"{symbol} {day} 新浪未覆盖所需交易日或时间口径不匹配。")
        filled = list(saved_record.get("confirmed_no_trade_windows", [])) if saved is not None else []
        for moment in filled:
            known = original[original.time.eq(pd.Timestamp(moment))]
            if len(known) != 1 or pd.to_numeric(known.iloc[0].volume) != 0 or pd.to_numeric(known.iloc[0].amount) != 0:
                raise RuntimeError(f"{symbol} {moment} 零成交窗口与已核验修正记录不一致。")
        for moment in sorted(set(expected) - set(replacement.time)):
            known = original[original.time.eq(moment)]
            before = replacement[replacement.time.lt(moment)].sort_values("time")
            # Only explicit, independently corroborated NO-TRADE windows
            # may carry the last price. Never interpolate a missing traded bar.
            if (len(known) != 1 or before.empty or pd.to_numeric(known.iloc[0].volume) != 0
                    or pd.to_numeric(known.iloc[0].amount) != 0):
                raise RuntimeError(f"{symbol} {moment} 备用源缺少有成交 K 线，不能插值或丢弃。")
            price = float(before.iloc[-1].close)
            replacement = pd.concat([replacement, pd.DataFrame([{"time": moment, "open": price,
                "high": price, "low": price, "close": price, "volume": 0, "amount": 0}])], ignore_index=True)
            filled.append(str(moment))
        replacement = replacement.sort_values("time").reset_index(drop=True)
        defects = minute_quality_issues(replacement, daily_amounts)
        if len(replacement) != 48 or defects:
            raise RuntimeError(f"{symbol} {day} 备用数据未通过验收：{[item['reason'] for item in defects]}。")
        original_path = evidence / f"{symbol}_{day}_{stamp}_original.csv"
        _atomic_csv(original, original_path)
        corrected = pd.concat([corrected[~corrected.time.dt.strftime("%Y-%m-%d").eq(day)], replacement], ignore_index=True)
        records.append({"symbol": symbol, "day": day, "source": source_name,
                        "repaired_at": datetime.now(CHINA).isoformat(timespec="seconds"),
                        "original_file": original_path.name, "original_sha256": _sha256(original_path),
                        "backup_file": backup_name, "backup_sha256": _sha256(evidence / backup_name),
                        "bundled_correction_sha256": (_sha256(MINUTE_CORRECTIONS / f"{symbol}_{day}.json") if saved is not None else None),
                        "confirmed_no_trade_windows": filled,
                        "issues": [issue for issue in issues if day in issue["days"]],
                        "minute_amount": float(replacement.amount.sum()),
                        "daily_amount": (daily_amounts or {}).get(day)})
    corrected = corrected.sort_values("time").reset_index(drop=True)
    if minute_quality_issues(corrected, daily_amounts):
        raise RuntimeError(f"{symbol} 修复后仍有分钟线异常，保留原缓存并拒绝通过。")
    for record in records:
        _atomic_json(record, evidence / f"{symbol}_{record['day']}.json")
        print(f"已用新浪备用源修复 {symbol} {record['day']} 的整日分钟线；保留原数据与修复记录。", flush=True)
    return corrected


def _minute_cache_complete(frame: pd.DataFrame, required_days: list[str]) -> bool:
    if frame.empty or not {"time", "open", "high", "low", "close", "volume", "amount"}.issubset(frame):
        return False
    if minute_quality_issues(frame):
        return False
    times = pd.to_datetime(frame.time, errors="coerce")
    return all(set(times[times.dt.strftime("%Y-%m-%d").eq(day)]) == set(_minute_times(day))
               for day in required_days)


def strip_zero_placeholders(frame: pd.DataFrame) -> pd.DataFrame:
    """BaoStock can emit zero OHLCV rows for a full-day suspension."""
    if frame.empty:
        return frame
    fields = ["open", "high", "low", "close", "volume", "amount"]
    numeric = frame[fields].apply(pd.to_numeric, errors="coerce")
    return frame.loc[~numeric.eq(0).all(axis=1)].copy()


def _fetch_minute_chunk(items: list[tuple[str, str]], end: str,
                        bars_dir: Path, required_days: dict[str, list[str]],
                        raw: Path = RAW, daily_amounts: dict | None = None) -> tuple[int, list[str]]:
    """Each process owns its BaoStock session and distinct output files."""
    import baostock as bs
    from factor_lab import normalized_bars, write_bars_cache

    _baostock_login(bs)
    errors = []
    try:
        for index, (symbol, start) in enumerate(items, 1):
            try:
                path = bars_dir / f"{symbol}.csv"
                try:
                    original_cache = pd.read_csv(path) if path.exists() else pd.DataFrame()
                    if not original_cache.empty and not {"time", "open", "high", "low", "close", "volume", "amount"}.issubset(original_cache):
                        raise ValueError("缓存缺少分钟线字段")
                except (OSError, ValueError):
                    if path.exists():
                        archive = raw / "minute-repairs" / f"{symbol}_{datetime.now(CHINA).strftime('%Y%m%d-%H%M%S-%f')}_unreadable.csv"
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        archive.write_bytes(path.read_bytes())
                    original_cache = pd.DataFrame()
                cached = strip_zero_placeholders(original_cache)
                previous = normalized_bars(original_cache) if not original_cache.empty else pd.DataFrame()
                reference = (daily_amounts or {}).get(symbol, {})
                required = sorted(set(required_days.get(symbol, []) + [start]))
                clean_previous = strip_zero_placeholders(previous)
                if len(clean_previous) != len(previous):
                    write_bars_cache(path, clean_previous)
                previous = clean_previous
                if (required_days.get(symbol) and _minute_cache_complete(cached, required)
                        and not minute_quality_issues(cached, reference)):
                    continue
                exchange = "sh" if symbol.startswith("6") else "sz"
                result = bs.query_history_k_data_plus(
                    f"{exchange}.{symbol}",
                    "date,time,code,open,high,low,close,volume,amount,adjustflag",
                    start_date=start, end_date=end, frequency="5", adjustflag="3")
                frame = _bs_rows(result, f"{symbol} 五分钟线")
                if frame.empty and previous.empty:
                    raise RuntimeError("没有历史分钟线")
                combined = previous
                if not frame.empty:
                    frame["time"] = pd.to_datetime(frame.time.astype(str).str[:14],
                                                   format="%Y%m%d%H%M%S", errors="coerce")
                    frame = strip_zero_placeholders(frame)
                    if minute_quality_issues(frame, reference):
                        print(f"{symbol} 供应商分钟线价量异常，正在获取新浪备用源校验并修复…", flush=True)
                        frame = repair_minute_days(symbol, frame, raw, reference)
                    fresh = normalized_bars(frame)
                    combined = fresh if previous.empty else normalized_bars(pd.concat([previous, fresh]))
                    combined = strip_zero_placeholders(combined)
                elif not cached.empty and minute_quality_issues(cached, reference):
                    combined = cached
                if minute_quality_issues(combined, reference):
                    print(f"{symbol} 分钟线价量异常，正在获取备用源校验并修复…", flush=True)
                    combined = repair_minute_days(symbol, combined, raw, reference)
                if required_days.get(symbol) and not _minute_cache_complete(combined, required):
                    raise RuntimeError(f"重取后的分钟线仍缺少所需完整交易日（包含 {start} 预热日）")
                write_bars_cache(path, combined)
            except (RuntimeError, OSError, ValueError, KeyError) as exc:
                errors.append(f"{symbol}: {exc}")
            finally:
                if index % 20 == 0 or index == len(items):
                    print(f"已检查 {index}/{len(items)} 只股票的 5 分钟线；失败 {len(errors)}", flush=True)
    finally:
        bs.logout()
    return len(items), errors


def fetch_minutes_baostock(symbols: list[str], start: str, end: str,
                           bars_dir: Path = BARS,
                           first_warmup: dict[str, str] | None = None,
                           required_days: dict[str, list[str]] | None = None,
                           raw: Path = RAW, daily_amounts: dict | None = None) -> None:
    """Fetch each symbol from its first warmup day in one BaoStock session."""
    items = [(symbol, (first_warmup or {}).get(symbol, start)) for symbol in symbols]
    _, failures = _fetch_minute_chunk(items, end, bars_dir, required_days or {}, raw, daily_amounts)
    if failures:
        raise RuntimeError(f"{len(failures)} 只股票的分钟线下载失败，例如 {failures[:8]}")


def quality_report(universe: pd.DataFrame, state: pd.DataFrame,
                   bars_dir: Path, daily: pd.DataFrame,
                   minimum_days: int = TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS,
                   slot: str = DEFAULT_SLOT,
                   minimum_market_symbols: int = 4500) -> dict:
    """Fail closed on any input that would distort membership or fills."""
    issues = []
    try:
        universe, state = validate_context(universe, state)
    except RuntimeError as exc:
        return {"passed": False, "issues": [str(exc)]}
    days = sorted(universe.day.unique())
    symbols = sorted(universe.symbol.unique())
    if len(days) < minimum_days + 1:
        issues.append(f"历史股票池仅 {len(days)} 日；至少需要 {minimum_days + 1} 日含首日特征预热。")
    counts = universe.groupby("day").symbol.nunique()
    if not counts.eq(TOP_N).all():
        issues.append(f"有 {(~counts.eq(TOP_N)).sum()} 个交易日的前 {TOP_N} 候选数量异常。")
    state_keys = set(zip(state.day, state.symbol))
    first_seen = universe.groupby("symbol").day.min().to_dict()
    warmup_by_symbol = first_warmup_days(universe)
    absent_state = [(day, symbol) for symbol, first in first_seen.items()
                    for day in days if day >= first and (day, symbol) not in state_keys]
    if absent_state:
        issues.append(f"候选或可能持仓股票缺少 {len(absent_state)} 条逐日交易状态。")
    state_index = state.set_index(["day", "symbol"])
    bar_days = 0
    missing_bars = 0
    missing_warmup = []
    bad_bars = {}
    amount_by_symbol = _daily_amounts(daily, symbols)
    missing_by_symbol = []
    execution = EXECUTION_BAR[slot]
    for symbol in symbols:
        path = bars_dir / f"{symbol}.csv"
        if not path.exists():
            missing_by_symbol.append(symbol)
            continue
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError) as exc:
            bad_bars[symbol] = [{"reason": f"缓存无法读取：{exc}", "count": 1, "days": [], "examples": []}]
            continue
        defects = minute_quality_issues(frame, amount_by_symbol.get(symbol))
        if defects:
            bad_bars[symbol] = defects
        if "time" not in frame:
            continue
        frame["day"] = frame.time.astype(str).str[:10]
        frame["clock"] = frame.time.astype(str).str[11:16]
        group_by_day = {day: group for day, group in frame.groupby("day")}
        warmup_day = warmup_by_symbol[symbol]
        warmup = group_by_day.get(warmup_day)
        if (warmup is None or len(warmup) != 48
                or set(pd.to_datetime(warmup.time, errors="coerce")) != set(_minute_times(warmup_day))):
            missing_warmup.append({"symbol": symbol, "day": warmup_day,
                                   "first_candidate_day": first_seen[symbol]})
        for day in (value for value in days if value >= first_seen[symbol]):
            if (day, symbol) not in state_index.index:
                continue
            status = state_index.loc[(day, symbol)]
            if bool(status.is_suspended):
                continue
            group = group_by_day.get(day)
            bar_days += 1
            if (group is None or len(group) != 48
                    or set(pd.to_datetime(group.time, errors="coerce")) != set(_minute_times(day))
                    or not {slot, execution, "15:00"}.issubset(set(group.clock))):
                missing_bars += 1
    if missing_by_symbol:
        issues.append(f"{len(missing_by_symbol)} 只历史候选股缺少分钟线文件，例如 {missing_by_symbol[:5]}。")
    if missing_bars:
        issues.append(f"{missing_bars}/{bar_days} 个非停牌候选或可能持仓交易日缺少完整 48 根 5 分钟线。")
    if missing_warmup:
        issues.append(f"{len(missing_warmup)} 只股票缺少首次入池前一交易日的完整预热分钟线，例如 {missing_warmup[:5]}。")
    if bad_bars:
        details = [f"{symbol} {item['days'][:3]}：{item['reason']}（{item['count']} 条）"
                   for symbol, defects in bad_bars.items() for item in defects]
        issues.append(f"{len(bad_bars)} 只股票的分钟线异常：" + "；".join(details[:8]) + "。")
    # Each reconstructed list must be backed by a plausible whole-market day.
    if not {"trade_date", "symbol", "amount"}.issubset(daily.columns):
        issues.append("全市场历史日线缺少交易日、代码或成交额。")
    else:
        counts_by_day = daily.groupby("trade_date").symbol.nunique()
        prior_days = set(universe.known_at.astype(str).str[:10].str.replace("-", "", regex=False))
        thin_days = [day for day in sorted(prior_days) if counts_by_day.get(day, 0) < minimum_market_symbols]
        if thin_days:
            issues.append(f"{len(thin_days)} 个股票池来源交易日的全市场日线不足{minimum_market_symbols}只，例如 {thin_days[:5]}。")
        traded = set(zip(daily.trade_date.astype(str).str[:4] + "-" +
                         daily.trade_date.astype(str).str[4:6] + "-" +
                         daily.trade_date.astype(str).str[6:8], daily.symbol.astype(str)))
        contradictory = [(row.day, row.symbol) for row in state.itertuples(index=False)
                         if (row.day, row.symbol) in traded and row.is_suspended]
        if contradictory:
            issues.append(f"{len(contradictory)} 条停牌状态与全市场成交日线冲突，例如 {contradictory[:5]}。")
    return {"passed": not issues, "issues": issues, "days": len(days),
            "symbols": len(symbols), "candidate_bar_days": bar_days,
            "missing_candidate_bar_days": missing_bars, "minute_data_issues": bad_bars,
            "missing_warmup_bar_days": len(missing_warmup), "missing_warmup_details": missing_warmup,
            "source_scope": "historical_vendor_reconstruction"}


def prepare(start: str, end: str, raw: Path = RAW, bars_dir: Path = BARS,
            store: Path = STORE, fetch_source: bool = True,
            fetch_minutes: bool = True) -> dict:
    if fetch_source:
        days, daily_category, daily_source = collect_daily(start, end, raw)
    else:
        calendar = _read_csv(raw / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv")
        days = trading_days(calendar, start.replace("-", ""), end.replace("-", ""))
        marker = _source_marker(raw, start, end)
        if marker.exists():
            choice = json.loads(marker.read_text(encoding="utf-8"))
            if (choice.get("days") != days or choice.get("category") not in {"daily", "daily-public"}
                    or not isinstance(choice.get("daily_source"), str)):
                raise RuntimeError("离线数据来源标记与交易日历不一致。")
            daily_category, daily_source = choice["category"], choice["daily_source"]
        else:
            daily_category, daily_source = "daily", "Tushare Pro daily (legacy local cache)"
    calendar = _read_csv(raw / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv")
    previous = calendar.set_index("cal_date").loc[days[0], "pretrade_date"]
    daily = _daily_from_cache(sorted(set(days + [previous])), raw, category=daily_category)
    universe = construct_universe(daily, calendar, start, end)
    symbols = sorted(universe.symbol.unique())
    staging = store / "prepared"
    _atomic_csv(universe, staging / "universe-history.csv")
    # An annual plan can be implemented in either of the following years.
    actions = fetch_actions_baostock(symbols, list(range(int(start[:4]) - 2, int(end[:4]) + 1)), raw)
    _atomic_csv(actions, staging / "corporate-actions.csv")
    basic = _read_csv(raw / "stock_basic_baostock.csv")
    daily_status = fetch_daily_status_baostock(symbols, start, end, raw)
    state = construct_market_state_baostock(universe, basic, daily_status, actions)
    _atomic_csv(state, staging / "market-state.csv")
    minute_error = None
    if fetch_minutes:
        first_warmup = first_warmup_days(universe)
        required_days = {symbol: group.loc[~group.is_suspended, "day"].tolist()
                         for symbol, group in state.groupby("symbol")}
        try:
            fetch_minutes_baostock(symbols, pd.Timestamp(previous).strftime("%Y-%m-%d"),
                                   end, bars_dir, first_warmup, required_days, raw, _daily_amounts(daily, symbols))
        except RuntimeError as exc:
            minute_error = str(exc)
    report = quality_report(universe, state, bars_dir, daily)
    if minute_error:
        report["passed"] = False
        report["issues"].insert(0, minute_error)
    _atomic_json(report, staging / "quality-report.json")
    if not report["passed"]:
        raise RuntimeError("历史数据验收未通过：" + "；".join(report["issues"]))
    _atomic_csv(universe, store / "universe-history.csv")
    _atomic_csv(state, store / "market-state.csv")
    manifest = {"validated": True, "feature_warmup_validated": True,
                "data_schema_version": 2, "source_scope": "historical_vendor_reconstruction",
                "created_at": datetime.now(CHINA).isoformat(timespec="seconds"),
                "first_day": min(universe.day), "last_day": max(universe.day),
                "daily_source": daily_source, "status_source": "BaoStock unadjusted daily isST/tradestatus",
                "listing_source": "BaoStock query_stock_basic",
                "limit_source": "exchange-rule reconstruction from BaoStock ex-rights preclose; main-board ST 5% before 2026-07-06, 10% after; not exchange supplied exact limits",
                "corporate_action_source": "BaoStock query_dividend_data",
                "minute_source": "BaoStock unadjusted 5-minute bars plus local cache; defective days repaired with verified Sina bars; all files passed per-day QA",
                "minute_repairs": [json.loads(path.read_text(encoding="utf-8"))
                                   for path in sorted((raw / "minute-repairs").glob("*.json"))],
                "known_at_rule": "previous trading day 18:00 Asia/Shanghai; reconstructed from historical vendor data",
                "files": {name: _sha256(store / name) for name in ("universe-history.csv", "market-state.csv")},
                "bars": {symbol: _sha256(bars_dir / f"{symbol}.csv") for symbol in symbols},
                "quality": report}
    _atomic_json(manifest, store / "source-manifest.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="准备并验收逐日历史回测数据")
    parser.add_argument("--start", required=True, help="首个回测输入日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="末个完整交易日 YYYY-MM-DD")
    parser.add_argument("--offline", action="store_true", help="仅使用上次采集的日线和分钟线缓存")
    parser.add_argument("--skip-minutes", action="store_true", help="仅检查现有分钟线缓存")
    args = parser.parse_args()
    try:
        report = prepare(args.start, args.end, fetch_source=not args.offline,
                         fetch_minutes=not args.skip_minutes)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"数据准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
