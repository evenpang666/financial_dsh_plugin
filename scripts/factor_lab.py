#!/usr/bin/env python
"""Small, auditable A-share intraday factor research and ranking pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
import requests
from factor_models import EXECUTION_BAR, NEXT_BAR


ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "factor-lab"
BARS = STORE / "bars"
MODELS = STORE / "models"
BACKTESTS = STORE / "backtests"
MODEL = STORE / "model.json"
LATEST = STORE / "latest-ranking.json"
SETTINGS = ROOT / "config" / "factor-lab.json"
UNIVERSE_HISTORY = STORE / "universe-history.csv"
MARKET_STATE = STORE / "market-state.csv"
SOURCE_MANIFEST = STORE / "source-manifest.json"
CHINA = timezone(timedelta(hours=8))
SLOTS = tuple(NEXT_BAR)
FACTOR_NAMES = {
    "open_momentum": "开盘以来动量",
    "gap": "开盘缺口",
    "range_position": "日内区间位置",
    "range_width": "日内振幅",
    "vwap_gap": "相对日内均价",
}
MIN_STOCKS = 12
UNIVERSE_SIZE = 40
ML_TARGET_VERSION = 3
EM_MINUTE_UNAVAILABLE = threading.Event()
SINA_MINUTE_UNAVAILABLE = threading.Event()
SINA_REQUEST_GATE = threading.Lock()


def now_cn() -> datetime:
    return datetime.now(CHINA)


def log(message: str):
    print(message, flush=True)


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_settings() -> dict:
    supplied = read_json(SETTINGS)
    slot = supplied.get("backtest_slot", "14:00")
    if slot not in NEXT_BAR:
        raise ValueError(f"回测时点无效：{slot}；可选 {', '.join(NEXT_BAR)}")
    return {"backtest_slot": slot,
            "weekly_training_enabled": bool(supplied.get("weekly_training_enabled", True))}


def save_settings(slot: str, weekly_training_enabled: bool = True) -> dict:
    if slot not in NEXT_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    value = {"backtest_slot": slot, "weekly_training_enabled": bool(weekly_training_enabled)}
    write_json(SETTINGS, value)
    return value


def write_bars_cache(path: Path, frame: pd.DataFrame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def valid_number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def spot_frame() -> pd.DataFrame:
    import akshare as ak

    try:
        frame = ak.stock_zh_a_spot_em()
        source = "东方财富 stock_zh_a_spot_em"
    except Exception as primary_error:
        log(f"  东方财富 A 股快照连接失败：{primary_error}")
        log("  正在尝试新浪 A 股快照备用源（首次获取可能较慢）...")
        try:
            frame = ak.stock_zh_a_spot()
            source = "新浪 stock_zh_a_spot"
            frame = frame.copy()
            frame["代码"] = frame["代码"].astype(str).str.extract(r"(\d{6})$")[0]
        except Exception as fallback_error:
            raise RuntimeError(f"A股快照的东方财富与新浪数据源均不可用。主源：{primary_error}；备用源：{fallback_error}") from fallback_error
    needed = {"代码", "名称", "最新价", "今开", "昨收", "最高", "最低", "成交额", "成交量"}
    missing = needed - set(frame.columns)
    if missing:
        raise RuntimeError("A股行情字段缺失：" + "、".join(sorted(missing)))
    result = frame.copy()
    result.attrs["source"] = source
    log(f"  A 股快照来源：{source}，共 {len(result)} 条")
    return result


def choose_universe(frame: pd.DataFrame) -> list[dict]:
    rows = frame.copy()
    rows["成交额"] = pd.to_numeric(rows["成交额"], errors="coerce")
    rows = rows[rows["代码"].astype(str).str.fullmatch(r"[036]\d{5}")]
    rows = rows[~rows["名称"].astype(str).str.upper().str.contains("ST|退")]
    if (rows["成交额"] > 0).sum() >= MIN_STOCKS:
        rows = rows[rows["成交额"] > 0].nlargest(UNIVERSE_SIZE, "成交额")
    elif "总市值" in rows.columns:
        rows["总市值"] = pd.to_numeric(rows["总市值"], errors="coerce")
        rows = rows[rows["总市值"] > 0].nlargest(UNIVERSE_SIZE, "总市值")
    else:
        return []
    return [{"symbol": str(row["代码"]), "name": str(row["名称"])} for _, row in rows.iterrows()]


def normalized_bars(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {"time": ("时间", "日期", "datetime", "day", "time"), "open": ("开盘", "open"), "high": ("最高", "high"),
               "low": ("最低", "low"), "close": ("收盘", "close"), "volume": ("成交量", "volume"),
               "amount": ("成交额", "amount")}
    columns = {key: next((item for item in names if item in frame.columns), None) for key, names in aliases.items()}
    if any(value is None for value in columns.values()):
        raise RuntimeError("5分钟历史行情缺少时间或 OHLCV 字段。")
    result = pd.DataFrame({key: frame[value] for key, value in columns.items()})
    result["time"] = pd.to_datetime(result["time"], errors="coerce")
    for key in ("open", "high", "low", "close", "volume", "amount"):
        result[key] = pd.to_numeric(result[key], errors="coerce")
    return result.dropna().sort_values("time", kind="stable").drop_duplicates("time", keep="last")


def sina_minute_frame(symbol: str) -> pd.DataFrame:
    """Read Sina's public 5-minute JSONP with an explicit network timeout."""
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
    params = {"symbol": symbol, "scale": "5", "ma": "no", "datalen": "1970"}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    body = response.text
    opening, closing = body.find("=("), body.rfind(");")
    if opening < 0 or closing <= opening:
        raise ValueError("新浪分钟线返回格式无法解析")
    payload = json.loads(body[opening + 2:closing])
    if not isinstance(payload, list):
        raise ValueError("新浪分钟线未返回K线数组")
    return pd.DataFrame(payload)


def fetch_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    import akshare as ak

    primary_error = None
    if not EM_MINUTE_UNAVAILABLE.is_set():
        try:
            raw = ak.stock_zh_a_hist_min_em(symbol=symbol, period="5", start_date=start.strftime("%Y-%m-%d 09:30:00"),
                                            end_date=end.strftime("%Y-%m-%d 15:00:00"), adjust="")
            result = normalized_bars(raw)
            if not result.empty:
                return result
            primary_error = ValueError("东方财富未返回分钟线")
        except Exception as exc:
            primary_error = exc
            if isinstance(exc, (OSError, ConnectionError, TimeoutError)) or "HTTPSConnectionPool" in str(exc):
                if not EM_MINUTE_UNAVAILABLE.is_set():
                    log(f"  东方财富分钟线连接失败，后续股票改用新浪备用源：{exc}")
                EM_MINUTE_UNAVAILABLE.set()
    exchange = "sh" if symbol.startswith("6") else "sz"
    if SINA_MINUTE_UNAVAILABLE.is_set():
        raise RuntimeError(f"{symbol} 的东方财富与新浪分钟线连接均不可用；已停止重复请求备用源。")
    try:
        # Sina supplies at most a recent rolling window; clip to our requested period.
        with SINA_REQUEST_GATE:
            if SINA_MINUTE_UNAVAILABLE.is_set():
                raise RuntimeError("新浪备用源已不可用")
            raw = sina_minute_frame(f"{exchange}{symbol}")
            time.sleep(0.2)
        result = normalized_bars(raw)
        result = result[(result["time"] >= pd.Timestamp(start.date())) &
                        (result["time"] <= pd.Timestamp(end.date()) + pd.Timedelta(days=1))]
        if result.empty:
            raise ValueError("新浪未返回请求时段内的5分钟线")
        return result
    except Exception as fallback_error:
        if isinstance(fallback_error, (requests.RequestException, OSError, ConnectionError, TimeoutError)):
            if not SINA_MINUTE_UNAVAILABLE.is_set():
                log(f"  新浪分钟线也无法连接，停止其余备用请求：{fallback_error}")
            SINA_MINUTE_UNAVAILABLE.set()
        raise RuntimeError(f"{symbol} 的东方财富与新浪5分钟线均不可用。主源：{primary_error or '已切换备用源'}；备用源：{fallback_error}") from fallback_error


def refresh_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    cache_path = BARS / f"{symbol}.csv"
    previous = pd.DataFrame()
    if cache_path.exists():
        try:
            previous = normalized_bars(pd.read_csv(cache_path))
        except (OSError, ValueError, RuntimeError):
            previous = pd.DataFrame()
    fetch_start = start.replace(tzinfo=None)
    if not previous.empty:
        last = previous["time"].max().to_pydatetime() - timedelta(days=1)
        fetch_start = max(fetch_start, last)
    try:
        fresh = fetch_bars(symbol, fetch_start, end)
    except Exception as exc:
        if previous.empty or previous["time"].max() < pd.Timestamp(end.date() - timedelta(days=7)):
            raise
        log(f"  {symbol} 近期分钟线暂不可用，沿用已缓存的完整历史：{exc}")
        fresh = pd.DataFrame()
    if fresh.empty and previous.empty:
        return fresh
    combined = fresh if previous.empty else normalized_bars(pd.concat((previous, fresh), ignore_index=True))
    combined = combined[combined["time"] >= pd.Timestamp(start.date())]
    write_bars_cache(cache_path, combined)
    return combined


def backfill_baostock(universe: list[dict], start: datetime, end: datetime) -> dict:
    """Extend cached 5-minute bars with older BaoStock history, one socket at a time."""
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败：{login.error_msg}")
    start_local = start.replace(tzinfo=None)
    end_local = end.replace(tzinfo=None)
    filled, failures = 0, []
    try:
        for index, item in enumerate(universe, 1):
            symbol = item["symbol"]
            path = BARS / f"{symbol}.csv"
            previous = pd.DataFrame()
            if path.exists():
                try:
                    previous = normalized_bars(pd.read_csv(path))
                except (OSError, ValueError, RuntimeError):
                    pass
            # Keep an overlap so a partially cached final day is repaired.
            fetch_start = (start_local if previous.empty or previous.time.min().to_pydatetime() > start_local + timedelta(days=3)
                           else max(start_local, previous.time.max().to_pydatetime() - timedelta(days=3)))
            if fetch_start.date() > end_local.date():
                continue
            exchange = "sh" if symbol.startswith("6") else "sz"
            try:
                result = bs.query_history_k_data_plus(
                    f"{exchange}.{symbol}",
                    "date,time,code,open,high,low,close,volume,amount,adjustflag",
                    start_date=fetch_start.strftime("%Y-%m-%d"), end_date=end.strftime("%Y-%m-%d"),
                    frequency="5", adjustflag="3")
                if result.error_code != "0":
                    raise RuntimeError(result.error_msg)
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                if rows:
                    data = pd.DataFrame(rows, columns=result.fields)
                    data["time"] = pd.to_datetime(data["time"].astype(str).str[:14],
                                                  format="%Y%m%d%H%M%S", errors="coerce")
                    fresh = normalized_bars(data)
                    combined = fresh if previous.empty else normalized_bars(pd.concat([previous, fresh], ignore_index=True))
                    write_bars_cache(path, combined)
                    filled += 1
                    log(f"  BaoStock {index}/{len(universe)} {symbol}：累计 {combined.time.dt.date.nunique()} 个交易日")
                else:
                    failures.append(f"{symbol}: 空结果")
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
                log(f"  BaoStock {index}/{len(universe)} {symbol} 回填失败：{exc}")
    finally:
        bs.logout()
    return {"filled": filled, "failures": failures}


def history_samples(symbol: str, bars: pd.DataFrame,
                    market_state: pd.DataFrame | None = None) -> list[dict]:
    if bars.empty:
        return []
    frame = bars.copy()
    frame["day"] = frame["time"].dt.strftime("%Y-%m-%d")
    frame["clock"] = frame["time"].dt.strftime("%H:%M")
    # Candidate features must not depend on whether this day *later* completes.
    observed_days = [(day, group.reset_index(drop=True)) for day, group in frame.groupby("day", sort=True)
                     if group.iloc[0]["clock"] <= "09:35"]
    events = ({} if market_state is None else
              {str(row.day): row for row in market_state[market_state.symbol.eq(symbol)].itertuples(index=False)})
    first_event_day = min(events) if events else None
    samples = []
    for index, (day, group) in enumerate(observed_days):
        if index == 0:
            continue
        prev_close = valid_number(observed_days[index - 1][1].iloc[-1]["close"])
        event = events.get(day)
        if market_state is not None and event is None and first_event_day and day < first_event_day:
            continue
        if market_state is not None and event is None:
            raise RuntimeError(f"{day} {symbol} 缺少逐日公司行为数据。")
        if event is not None and prev_close:
            prev_close = (prev_close - float(event.cash_dividend)) / float(event.split_ratio)
        opening = valid_number(group.iloc[0]["open"])
        if not prev_close or not opening:
            continue
        for slot in SLOTS:
            earlier = group[group["clock"] <= slot]
            if earlier.empty or earlier.iloc[-1]["clock"] < slot:
                continue
            price = valid_number(earlier.iloc[-1]["close"])
            high = valid_number(earlier["high"].max())
            low = valid_number(earlier["low"].min())
            volume = earlier["volume"].clip(lower=0)
            vwap = inferred_vwap(earlier["amount"].clip(lower=0).sum(), volume.sum(), price)
            factors = factor_values(price, opening, prev_close, high, low, vwap)
            if factors is None:
                continue
            # The slot bar ends at the signal time. Allow a complete bar for
            # snapshot publication and order entry, then execute over the
            # following bar at its VWAP. The label exits in the same window
            # on the next complete market session.
            execution = group[group["clock"] == EXECUTION_BAR[slot]]
            entry_price = (inferred_vwap(execution.iloc[0]["amount"], execution.iloc[0]["volume"],
                                         execution.iloc[0]["close"]) if not execution.empty else None)
            exit_day, exit_price = None, None
            if index + 1 < len(observed_days):
                exit_day, next_group = observed_days[index + 1]
                exit_bars = next_group[next_group["clock"] == EXECUTION_BAR[slot]]
                if not exit_bars.empty:
                    exit_price = inferred_vwap(exit_bars.iloc[0]["amount"], exit_bars.iloc[0]["volume"],
                                               exit_bars.iloc[0]["close"])
            if exit_day and market_state is not None and exit_day not in events:
                raise RuntimeError(f"{exit_day} {symbol} 缺少逐日公司行为数据。")
            exit_event = events.get(exit_day)
            exit_value = (exit_price * float(exit_event.split_ratio) + float(exit_event.cash_dividend)
                          if exit_price and exit_event is not None else exit_price)
            samples.append({"symbol": symbol, "day": day, "slot": slot,
                            "target": (exit_value / entry_price - 1 if entry_price and exit_value else None),
                            "exit_day": exit_day, **factors})
    return samples


def factor_values(price, opening, prev_close, high, low, vwap) -> dict | None:
    values = [valid_number(value) for value in (price, opening, prev_close, high, low, vwap)]
    if any(value is None or value <= 0 for value in values):
        return None
    price, opening, prev_close, high, low, vwap = values
    if high < low or not low <= price <= high * 1.001:
        return None
    return {
        "open_momentum": price / opening - 1,
        "gap": opening / prev_close - 1,
        "range_position": (price - low) / max(high - low, 0.001) - 0.5,
        "range_width": (high - low) / opening,
        "vwap_gap": price / vwap - 1,
    }


def inferred_vwap(amount, volume, reference) -> float | None:
    amount, volume, reference = (valid_number(value) for value in (amount, volume, reference))
    if not amount or not volume or not reference or min(amount, volume, reference) <= 0:
        return None
    choices = (amount / volume, amount / (volume * 100))
    value = min(choices, key=lambda item: abs(item / reference - 1))
    return value if 0.8 <= value / reference <= 1.2 else None


def train() -> dict:
    if read_json(SOURCE_MANIFEST).get("validated"):
        return train_pit_from_cache()
    log("[1/5] 获取 A 股行情并选择40只候选股票...")
    EM_MINUTE_UNAVAILABLE.clear()
    SINA_MINUTE_UNAVAILABLE.clear()
    try:
        snapshot = spot_frame()
    except RuntimeError as exc:
        previous = read_json(MODEL)
        try:
            age = (now_cn() - datetime.fromisoformat(previous["selected_at"])).days
        except (KeyError, TypeError, ValueError):
            age = 999
        if age > 30:
            raise
        log(f"  实时快照不可用，使用最近30天内的已缓存股票池和分钟线继续训练：{exc}")
        return train_from_cache()
    universe = choose_universe(snapshot)
    method = ("训练启动时成交额前40名" if (pd.to_numeric(snapshot["成交额"], errors="coerce") > 0).sum() >= MIN_STOCKS
              else "盘前成交额不可用时按总市值前40名")
    log("  候选池口径：" + method)
    if len(universe) < MIN_STOCKS:
        previous = read_json(MODEL)
        cached = previous.get("universe", [])
        previous_time = previous.get("selected_at")
        try:
            recent_cache = previous_time and (now_cn() - datetime.fromisoformat(previous_time)).days <= 30
        except (TypeError, ValueError):
            recent_cache = False
        if len(cached) >= MIN_STOCKS and recent_cache:
            universe = cached[:UNIVERSE_SIZE]
            method = "盘前成交额不可用，沿用上次训练的候选池"
            log("  " + method + f"（{len(universe)}只；可能存在候选池时效偏差）")
        else:
            raise RuntimeError("当前快照没有足够的成交额/市值数据，且没有历史候选池；请在交易时段或收盘后重试训练。")
    STORE.mkdir(parents=True, exist_ok=True)
    BARS.mkdir(parents=True, exist_ok=True)
    start, end = now_cn() - timedelta(days=400), now_cn()
    log(f"[2/5] 回填近400日5分钟行情：共 {len(universe)} 只；BaoStock 串行访问并增量缓存。")
    try:
        backfill = backfill_baostock(universe, start, end - timedelta(days=1))
        log(f"  BaoStock 回填完成：{backfill['filled']} 只；失败 {len(backfill['failures'])} 只")
    except Exception as exc:
        log(f"  BaoStock 回填暂不可用：{exc}；尝试已有缓存及近期行情。")
    log("[3/5] 用东方财富/新浪补齐近期5分钟线...")
    samples, failed, bars_by_symbol = [], [], {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(refresh_bars, item["symbol"], start, end): item for item in universe}
        for index, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                bars = future.result()
                bars_by_symbol[item["symbol"]] = bars[bars["time"].dt.date < now_cn().date()].copy()
                stock_samples = history_samples(item["symbol"], bars_by_symbol[item["symbol"]])
                samples.extend(stock_samples)
                log(f"  {index}/{len(universe)} {item['symbol']}：{len(bars)} 根历史K线，{len(stock_samples)} 条有效时点样本")
            except Exception as exc:
                failed.append(f"{item['symbol']}: {exc}")
                log(f"  {index}/{len(universe)} {item['symbol']}：获取失败：{exc}")
    source = snapshot.attrs.get("source", "未知")
    minute_source = ("新浪备用源（东方财富连接失败）" if EM_MINUTE_UNAVAILABLE.is_set()
                     else "东方财富主源，个别股票可能使用新浪备用源")
    return publish_training(samples, bars_by_symbol, universe, failed, method, source, minute_source)


def publish_training(samples: list[dict], bars_by_symbol: dict[str, pd.DataFrame],
                     universe: list[dict], failed: list[str], method: str,
                     spot_source: str, minute_source: str,
                     point_in_time: bool = False) -> dict:
    from factor_models import fit_models

    log(f"[4/5] 准备 {len(samples)} 条盘中样本，并仅使用已完成次日交易的收益标签...")
    if len(failed) >= len(universe) - MIN_STOCKS + 1:
        log(f"  {len(failed)}/{len(universe)} 只股票取数失败；首条失败：{failed[0]}")
    universe_method = (method + "；逐日全市场成交额历史名单"
                       if point_in_time else
                       method + "；这是受限候选池，不代表全A股，也有存续偏差")
    model_id = now_cn().strftime("%Y%m%d-%H%M%S")
    log("[5/5] 训练 XGBoost 排序模型与 Elastic Net，并按验证期 Rank IC 选择...")
    ml = fit_models(samples, MODELS / model_id)
    slot = load_settings()["backtest_slot"]
    log("  模型训练已完成；滚动回测需单独运行，其绩效不代表当前发布模型权重。")
    holdout_ics = [entry.get("holdout", {}).get("rank_ic") for entry in ml["algorithms"].values()]
    holdout_warning = (" 本次两种算法的留出期 Rank IC 均为负，尚无稳定样本外证据。"
                       if holdout_ics and all(value is not None and value <= 0 for value in holdout_ics) else "")
    validation_warning = (" 两种算法的验证期均未达到正向门槛，仅按较高 Rank IC 排序，不代表有效预测。"
                          if not ml.get("validation_eligible", True) else "")
    model = {"model_id": model_id, "selected_at": now_cn().isoformat(timespec="seconds"),
             "market": "a", "target": "信号后预留一根完整5分钟K线，再按下一根K线VWAP买入至次日同一窗口VWAP卖出的价格变化",
             "universe_method": universe_method,
             "universe_policy": ("previous_day_turnover_top40" if point_in_time else "runtime_snapshot"),
             "universe": universe, "quality": "exploratory", "algorithm": ml["algorithm"],
             "target_version": ML_TARGET_VERSION,
             "algorithms": ml["algorithms"], "features": ml["features"],
             "backtest": None, "backtest_slot": slot,
             "data_sources": {"spot": spot_source, "minute": minute_source},
             "failed_symbols": failed,
             "selection_warning": "机器学习模型仅供模拟研究；滚动回测评估的是逐窗口训练规则，并非当前发布模型权重的独立绩效。"
                                  + validation_warning + holdout_warning,
             "training": {"days": ml["days"], "last_day": ml["last_day"],
                          "samples": ml["samples"], "holdout_days": ml["holdout_days"]}}
    write_json(MODEL, model)
    write_json(LATEST, {})
    log(f"训练并发布完成：{model_id}；当前排序算法 {ml['algorithm']}；训练覆盖 {ml['days']} 个交易日。")
    return model


def cached_inputs() -> tuple[list[dict], dict[str, pd.DataFrame], list[dict]]:
    model = read_json(MODEL)
    universe = model.get("universe", [])
    if len(universe) < MIN_STOCKS:
        raise RuntimeError("尚无有效候选池；请先点击“一键训练”。")
    samples, bars_by_symbol = [], {}
    for index, item in enumerate(universe, 1):
        symbol = item["symbol"]
        try:
            bars = normalized_bars(pd.read_csv(BARS / f"{symbol}.csv"))
            bars = bars[bars.time.dt.date < now_cn().date()].copy()
            bars_by_symbol[symbol] = bars
            samples.extend(history_samples(symbol, bars))
            if index % 10 == 0 or index == len(universe):
                log(f"  已载入 {index}/{len(universe)} 只历史分钟线")
        except (OSError, ValueError, RuntimeError) as exc:
            log(f"  {symbol} 本地缓存不可用：{exc}")
    if len(bars_by_symbol) < MIN_STOCKS:
        raise RuntimeError("本地完整股票分钟线少于12只，无法回测。")
    return samples, bars_by_symbol, universe


def _pit_symbol_inputs(symbol: str, path: Path, first_day: str, last_day: str,
                       cutoff_day: str, state: pd.DataFrame) -> tuple[str, pd.DataFrame, list[dict]]:
    bars = normalized_bars(pd.read_csv(path))
    study = bars[(bars.time.dt.strftime("%Y-%m-%d") <= last_day)
                 & (bars.time.dt.strftime("%Y-%m-%d") < cutoff_day)].copy()
    warmup = study[study.time.dt.strftime("%Y-%m-%d") < first_day]
    warmup_day = warmup.time.dt.strftime("%Y-%m-%d").max() if not warmup.empty else None
    bars = study[(study.time.dt.strftime("%Y-%m-%d") >= first_day)
                 | (study.time.dt.strftime("%Y-%m-%d") == warmup_day)].copy()
    return symbol, bars, history_samples(symbol, bars, state)


def backtest_inputs() -> tuple[list[dict], dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    from factor_backtest import validate_context

    try:
        universe_history = pd.read_csv(UNIVERSE_HISTORY, dtype={"symbol": str})
        market_state = pd.read_csv(MARKET_STATE, dtype={"symbol": str})
    except (OSError, ValueError) as exc:
        raise RuntimeError("缺少 universe-history.csv 或 market-state.csv；新版回测要求逐日时点数据。") from exc
    universe_history, market_state = validate_context(universe_history, market_state)
    manifest = read_json(SOURCE_MANIFEST)
    if not manifest.get("validated") or manifest.get("source_scope") != "historical_vendor_reconstruction":
        raise RuntimeError("缺少通过验收的数据来源清单；请先运行 factor_data_prep.py。")
    for name, digest in manifest.get("files", {}).items():
        if name not in ("universe-history.csv", "market-state.csv") or file_sha256(STORE / name) != digest:
            raise RuntimeError(f"数据文件 {name} 与验收清单不一致。")
    if set(manifest.get("files", {})) != {"universe-history.csv", "market-state.csv"}:
        raise RuntimeError("数据来源清单缺少回测输入文件的校验值。")
    symbols = set(universe_history.symbol)
    if set(manifest.get("bars", {})) != symbols:
        raise RuntimeError("数据来源清单未覆盖全部历史候选股分钟线。")
    first_day, last_day = min(universe_history.day), max(universe_history.day)
    samples, bars_by_symbol = [], {}
    cutoff_day = now_cn().strftime("%Y-%m-%d")
    for symbol in sorted(symbols):
        path = BARS / f"{symbol}.csv"
        if not path.exists() or file_sha256(path) != manifest["bars"][symbol]:
            raise RuntimeError(f"历史股票池 {symbol} 的分钟线与验收清单不一致。")
    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_pit_symbol_inputs, symbol, BARS / f"{symbol}.csv",
                                   first_day, last_day, cutoff_day,
                                   market_state[market_state.symbol.eq(symbol)])
                   for symbol in sorted(symbols)]
        for index, future in enumerate(as_completed(futures), 1):
            symbol, bars, stock_samples = future.result()
            bars_by_symbol[symbol] = bars
            samples.extend(stock_samples)
            if index % 40 == 0 or index == len(symbols):
                log(f"  已载入 {index}/{len(symbols)} 只历史股票的分钟线与时点样本")
    return samples, bars_by_symbol, universe_history, market_state


def train_pit_from_cache() -> dict:
    """Fit a live model from the same changing historical pool as the replay."""
    samples, bars_by_symbol, universe_history, market_state = backtest_inputs()
    provenance = read_json(SOURCE_MANIFEST)
    eligible = universe_history.merge(market_state[["day", "symbol", "is_st", "is_suspended"]],
                                      on=["day", "symbol"], how="left")
    eligible = eligible[~eligible.is_st & ~eligible.is_suspended][["day", "symbol"]]
    filtered = pd.DataFrame(samples).merge(eligible, on=["day", "symbol"], how="inner")
    final_day = max(universe_history.day)
    today = universe_history[universe_history.day.eq(final_day)]
    current = [{"symbol": symbol} for symbol in today.symbol]
    return publish_training(filtered.to_dict("records"), bars_by_symbol, current, [],
                            "前一交易日全市场成交额前40只",
                            provenance.get("daily_source", "逐日历史股票池"),
                            provenance.get("minute_source", "未复权5分钟线"),
                            point_in_time=True)


def today_pit_universe(moment: datetime) -> set[str]:
    """Use yesterday's daily turnover and today's known risk-warning status."""
    day = moment.strftime("%Y-%m-%d")
    manifest = read_json(SOURCE_MANIFEST)
    trusted_cache = (manifest.get("validated") is True and
                     set(manifest.get("files", {})) == {"universe-history.csv", "market-state.csv"} and
                     UNIVERSE_HISTORY.exists() and MARKET_STATE.exists() and
                     all(file_sha256(STORE / name) == digest
                         for name, digest in manifest["files"].items()))
    if trusted_cache:
        universe = pd.read_csv(UNIVERSE_HISTORY, dtype={"symbol": str})
        state = pd.read_csv(MARKET_STATE, dtype={"symbol": str})
        rows = universe[universe.day.eq(day)].merge(state, on=["day", "symbol"], how="left")
        if len(rows) == UNIVERSE_SIZE and rows.is_st.notna().all() and rows.is_suspended.notna().all():
            st = rows.is_st.astype(str).str.lower().isin({"true", "1", "yes"})
            halted = rows.is_suspended.astype(str).str.lower().isin({"true", "1", "yes"})
            return set(rows.loc[~st & ~halted, "symbol"])
    from factor_data_prep import RAW, _daily_from_cache, collect_daily, construct_universe, fetch_calendar_baostock

    compact = day.replace("-", "")
    calendar = fetch_calendar_baostock(day, day)
    today_row = calendar[calendar.cal_date.astype(str).eq(compact)]
    if len(today_row) != 1 or str(today_row.iloc[0].is_open) != "1":
        raise RuntimeError("交易日历未确认今天可交易。")
    previous = str(today_row.iloc[0].pretrade_date)
    previous_date = pd.Timestamp(previous).strftime("%Y-%m-%d")
    _, category, source = collect_daily(previous_date, previous_date, RAW)
    log(f"  今日历史股票池来源：{source}")
    daily = _daily_from_cache([previous], RAW, category=category)
    universe = construct_universe(daily, calendar, day, day)
    return set(universe.symbol)


def train_from_cache() -> dict:
    """Retrain a previously snapshotted candidate pool without network calls."""
    samples, bars_by_symbol, universe = cached_inputs()
    return publish_training(samples, bars_by_symbol, universe, [],
                            "沿用上次训练的候选池（本地缓存）", "本地候选池快照", "本地5分钟线缓存")


def backtest_from_cache() -> dict:
    """Re-run the research backtest without downloading or publishing a model."""
    from factor_backtest import run as run_backtest

    samples, bars_by_symbol, universe_history, market_state = backtest_inputs()
    identifier = now_cn().strftime("%Y%m%d-%H%M%S")
    summary = run_backtest(samples, bars_by_symbol, BACKTESTS / identifier,
                           slot=load_settings()["backtest_slot"],
                           universe_history=universe_history, market_state=market_state,
                           source_manifest=read_json(SOURCE_MANIFEST))
    log(f"回测完成：{summary['days']} 日；收益 {summary['stats']['total_return']:.2%}；"
        f"最大回撤 {summary['stats']['max_drawdown']:.2%}；详情：{BACKTESTS / identifier}")
    return summary


def live_factors(row: pd.Series) -> dict | None:
    price, opening, prev_close, high, low = (valid_number(row.get(key)) for key in
                                             ("最新价", "今开", "昨收", "最高", "最低"))
    amount, volume = valid_number(row.get("成交额")), valid_number(row.get("成交量"))
    vwap = inferred_vwap(amount, volume, price)
    return factor_values(price, opening, prev_close, high, low, vwap)


def is_trading_time(moment: datetime) -> bool:
    clock = moment.strftime("%H:%M")
    return moment.weekday() < 5 and ("09:35" <= clock <= "11:30" or "13:00" <= clock < "15:00")


def rank() -> dict:
    model = read_json(MODEL)
    if not model:
        raise RuntimeError("尚无已发布模型，请先点击“一键训练”。")
    if model.get("algorithm") not in ("xgboost", "elastic_net") or model.get("target_version") != ML_TARGET_VERSION:
        raise RuntimeError("旧版非 AI 因子模型或当日收盘模型已停用；请重新点击“一键训练”。")
    started = now_cn()
    if not is_trading_time(started):
        raise RuntimeError("仅在 A 股交易时段 09:35–11:30、13:00–15:00 生成盘中排名。")
    import akshare as ak

    calendar = ak.tool_trade_date_hist_sina()
    calendar_key = next((key for key in ("trade_date", "日期") if key in calendar.columns), None)
    if calendar_key is None or started.date() not in set(pd.to_datetime(calendar[calendar_key], errors="coerce").dt.date.dropna()):
        raise RuntimeError("交易日历未确认今天为 A 股交易日；本次不发布排名。")
    if (started - datetime.fromisoformat(model["selected_at"])).days > 30:
        raise RuntimeError("因子模型已超过30天，请重新点击“一键训练”。")
    if model.get("quality") == "exploratory" and (started.date() - pd.Timestamp(model["training"]["last_day"]).date()).days > 7:
        raise RuntimeError("探索性模型的训练数据已超过7天；请重新点击“一键训练”。")
    log("[1/3] 获取点击时刻的 A 股行情快照...")
    frame = spot_frame()
    fetched = now_cn()
    if fetched.date() != started.date() or not is_trading_time(fetched):
        raise RuntimeError("行情获取跨过交易时段，请重新点击推荐。")
    allowed = (today_pit_universe(fetched) if model.get("universe_policy") == "previous_day_turnover_top40"
               else {item["symbol"] for item in model["universe"]})
    if not is_trading_time(now_cn()):
        raise RuntimeError("获取当日股票池时跨过交易时段，请重新点击推荐。")
    frame = frame[frame["代码"].astype(str).isin(allowed)].copy()
    if model.get("universe_policy") == "previous_day_turnover_top40":
        frame = frame[~frame["名称"].astype(str).str.contains(r"\*?ST|退", regex=True, case=False)]
        for column in ("成交量", "成交额"):
            if column in frame.columns:
                frame = frame[pd.to_numeric(frame[column], errors="coerce").gt(0)]
    records = []
    for _, row in frame.iterrows():
        factors = live_factors(row)
        if factors:
            records.append({"symbol": str(row["代码"]), "name": str(row["名称"]),
                            "price": valid_number(row["最新价"]), **factors})
    if len(records) < MIN_STOCKS:
        raise RuntimeError(f"当前仅 {len(records)} 只候选有完整有效行情，至少需要 {MIN_STOCKS} 只。")
    data = pd.DataFrame(records)
    log(f"[2/3] 在 {len(data)} 只有效候选中用模型 {model['model_id']} 排序...")
    from factor_models import minute_of_day, predict
    backtest_slot = load_settings()["backtest_slot"]
    data["minute_of_day"] = minute_of_day(fetched.strftime("%H:%M"))
    data["score"] = predict(data, model, MODELS / model["model_id"])
    data = data.sort_values("score", ascending=False)
    ranking = []
    for position, (_, row) in enumerate(data.head(20).iterrows(), 1):
        ranking.append({"rank": position, "symbol": row["symbol"], "name": row["name"],
                        "price": round(float(row["price"]), 3), "score": round(float(row["score"]), 4),
                        "factors": {key: round(float(row[key]), 5) for key in model["features"]}})
    slot_warning = " 实时快照缺少逐股报价时间戳，无法证明与所选5分钟K线收盘时点一致；本次排名不与回测直接对应。"
    payload = {"id": fetched.strftime("%Y%m%d-%H%M%S-%f"), "market": "a", "generated_at": fetched.isoformat(timespec="seconds"),
               "model_id": model["model_id"], "model_quality": model.get("quality", "validated"),
               "algorithm": model["algorithm"], "backtest_slot": backtest_slot,
               "backtest_comparable": False,
               "candidate_count": len(data), "ranking": ranking,
               "factors": [{"key": key, "name": "盘中时点" if key == "minute_of_day" else FACTOR_NAMES.get(key, key)}
                           for key in model["features"]],
               "universe_method": model["universe_method"],
               "note": ("预测目标为预留一根完整5分钟K线后下一根K线VWAP至次日同一窗口VWAP；排名是研究结果，不是收益概率或买入指令。"
                        + model.get("selection_warning", "") + slot_warning)}
    write_json(STORE / "rankings" / f"{payload['id']}.json", payload)
    write_json(LATEST, payload)
    log("[3/3] 排名已归档并写入日报页面数据。")
    try:
        request = Request("http://127.0.0.1:8766/api/publish-ranking", data=b"{}", method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=4):
            log("日报页面已收到最新排名通知。")
    except OSError as exc:
        log(f"日报页面通知暂不可用；打开或刷新页面仍可读取排名：{exc}")
    return payload


def status() -> dict:
    model, latest = read_json(MODEL), read_json(LATEST)
    return {"trained_at": model.get("selected_at"), "model_id": model.get("model_id"),
            "selected_at": model.get("selected_at"), "quality": model.get("quality"),
            "algorithm": model.get("algorithm"), "target_version": model.get("target_version"),
            "backtest_slot": load_settings()["backtest_slot"],
            "latest_ranking_at": latest.get("generated_at"), "latest_count": latest.get("candidate_count")}


def main() -> int:
    parser = argparse.ArgumentParser(description="A股盘中 AI 因子训练、回测和即时排名")
    parser.add_argument("action", choices=("train", "train-cached", "backtest", "rank", "status"))
    args = parser.parse_args()
    try:
        result = {"train": train, "train-cached": train_from_cache, "backtest": backtest_from_cache,
                  "rank": rank, "status": status}[args.action]()
        if args.action == "status":
            log(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        log("错误：" + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
