"""Completed daily, weekly and monthly bar strategy experiments for A/HK/US.

Signals use a completed bar and trades
begin on the following bar; A-share daily exits one more day later for T+1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from strategy_catalog import FIXED_STRATEGIES, MODEL_STRATEGIES, STRATEGIES, rule_scores
from strategy_config import parameter_schema, validate_parameters
from us_strategy import FEATURES, features, save_json

ROOT = Path(__file__).resolve().parent.parent
BACKTESTS = ROOT / "data/factor-lab/backtests"
MODELS = ROOT / "data/bar-models"
DEPLOYMENTS = ROOT / "data/bar-deployments"
POOLS = ROOT / "data/bar-pools"
HISTORY_CACHE = ROOT / "data/bar-history"
PERIODS = {"daily": "日线", "weekly": "周线", "monthly": "月线"}
ZONES = {"a": "Asia/Shanghai", "hk": "Asia/Hong_Kong", "us": "America/New_York"}
BAR_NAMES = {"momentum": "K线动量", "breakout": "区间突破"}
POOL_NAMES = {"custom": "自定股票池", "portfolio": "持仓股票池", "csi300": "沪深300", "csi500": "中证500", "all": "全盘"}


class IncompleteHistoryError(ValueError):
    """A historical constituent stops trading before the requested end."""


def historical_index_pool(pool, start):
    """Freeze index members known at the research period's first date."""
    if not start:
        raise ValueError("历史指数股票池必须指定开始日期。")
    day = pd.Timestamp(start).date().isoformat()
    cache = POOLS / f"{pool}-asof-{day}.json"
    minimum = 250 if pool == "csi300" else 450
    if cache.is_file():
        record = json.loads(cache.read_text(encoding="utf-8"))
        if (record.get("id") == pool and record.get("requested_for") == day
                and record.get("as_of", day) <= day and len(record.get("symbols", [])) >= minimum):
            return record["symbols"], {key: value for key, value in record.items() if key != "symbols"}
    import baostock as bs
    from factor_data_prep import _baostock_login, _baostock_logout, _bs_query
    _baostock_login(bs)
    try:
        query = bs.query_hs300_stocks if pool == "csi300" else bs.query_zz500_stocks
        frame = _bs_query(bs, f"{POOL_NAMES[pool]}历史成分", query, date=day)
    finally:
        _baostock_logout(bs)
    if frame.empty or not {"code", "updateDate"}.issubset(frame.columns):
        raise ValueError(f"{POOL_NAMES[pool]}在 {day} 的历史成分名单不可用，不能以当前名单代替。")
    updates = pd.to_datetime(frame.updateDate, errors="coerce")
    if updates.isna().any() or (updates.dt.date > pd.Timestamp(day).date()).any():
        raise ValueError("历史指数成分名单包含晚于研究开始日的更新日期。")
    codes = frame.code.astype(str).str.extract(r"(?:sh|sz)\.([036]\d{5})$", expand=False).dropna().tolist()
    codes = list(dict.fromkeys(codes))
    if len(codes) < minimum:
        raise ValueError(f"{POOL_NAMES[pool]}在 {day} 的历史成分仅 {len(codes)} 只，低于 {minimum} 只门槛。")
    record = {"id": pool, "name": POOL_NAMES[pool], "source": "BaoStock 指定日期历史指数成分",
              "as_of": updates.max().date().isoformat(), "requested_for": day,
              "membership_policy": "研究起始日成分固定", "symbols": codes}
    save_json(cache, record)
    return codes, {key: value for key, value in record.items() if key != "symbols"}


def resolve_pool(action, market, pool, symbols, start=None):
    if pool not in POOL_NAMES or (market != "a" and pool not in ("custom", "portfolio")) or (pool == "all" and action != "deploy") or (pool == "portfolio" and action == "train"):
        raise ValueError("当前任务或市场不支持所选股票池。")
    if pool == "portfolio":
        return normalize_symbols(market, symbols, limit=100), {"id": pool, "name": POOL_NAMES[pool], "source": "本地已保存持仓快照", "as_of": None,
            "membership_policy": "启动时持仓名单固定；仅使用代码，不按实际持股数量或成本配置；当前名单用于历史回测存在幸存者偏差"}
    if pool == "custom":
        return normalize_symbols(market, symbols), {"id": pool, "name": POOL_NAMES[pool], "source": "用户输入", "as_of": None}
    if pool in ("csi300", "csi500") and action in ("train", "backtest"):
        return historical_index_pool(pool, start)
    today = datetime.now(ZoneInfo(ZONES[market])).date().isoformat()
    cache = POOLS / f"{pool}-{today}.json"
    if cache.is_file():
        record = json.loads(cache.read_text(encoding="utf-8"))
        if len(record.get("symbols", [])) >= (250 if pool == "csi300" else 450 if pool == "csi500" else 3000):
            return record["symbols"], {key: record[key] for key in ("id", "name", "source", "as_of")}
    import akshare as ak
    if pool in ("csi300", "csi500"):
        index_code = "000300" if pool == "csi300" else "000905"
        frame = ak.index_stock_cons_csindex(symbol=index_code)
        code_column = next((column for column in frame.columns if "成分券代码" in str(column)), None)
        date_column = next((column for column in frame.columns if str(column) == "日期"), None)
        if code_column is None:
            raise ValueError("中证指数成分名单缺少成分券代码字段。")
        codes = frame[code_column].astype(str).str.extract(r"(\d{6})$", expand=False).dropna().tolist()
        source = f"中证指数最新成分名单 {index_code}（AKShare）"
        as_of = str(frame[date_column].iloc[0]) if date_column and len(frame) else today
        minimum = 250 if pool == "csi300" else 450
    else:
        from factor_lab import spot_frame
        frame = spot_frame()
        codes = frame["代码"].astype(str).str.extract(r"([036]\d{5})$", expand=False).dropna().tolist()
        source, as_of, minimum = frame.attrs.get("source", "A股实时快照"), today, 3000
    codes = list(dict.fromkeys(code for code in codes if re.fullmatch(r"[036]\d{5}", code)))
    if len(codes) < minimum:
        raise ValueError(f"{POOL_NAMES[pool]}名单仅取得 {len(codes)} 只，未达到完整性门槛 {minimum} 只，已停止任务。")
    record = {"id": pool, "name": POOL_NAMES[pool], "source": source, "as_of": as_of, "symbols": codes}
    save_json(cache, record)
    return codes, {key: record[key] for key in ("id", "name", "source", "as_of")}


def strategy_name(strategy):
    return BAR_NAMES.get(strategy, STRATEGIES[strategy]["name"])


def selection(market, timeframe):
    if market not in ZONES or timeframe not in PERIODS:
        raise ValueError("市场或K线周期无效。")
    return market, timeframe


def normalize_symbols(market, symbols, limit=50):
    selection(market, "daily")
    if isinstance(symbols, str):
        symbols = [part.strip() for part in symbols.replace("，", ",").split(",")]
    if not isinstance(symbols, list):
        raise ValueError("请提供股票代码列表。")
    if market == "a" and any(not str(value).isdigit() or len(str(value)) != 6 for value in symbols):
        raise ValueError("A股代码须为6位数字。")
    if market == "hk" and any(not str(value).isdigit() or not 1 <= len(str(value)) <= 5 for value in symbols):
        raise ValueError("港股代码须为1–5位数字。")
    if market == "us":
        from us_market import normalize_symbol
    else:
        from stock_data_server import normalize_symbol as market_symbol
        normalize_symbol = lambda value: market_symbol(value, market)
    result = list(dict.fromkeys(normalize_symbol(value) for value in symbols if value))
    if not 1 <= len(result) <= limit:
        raise ValueError(f"请选择 1–{limit} 只当前研究股票；固定股票池可能存在幸存者偏差。")
    return result


def completed_bars(daily, timeframe, cutoff):
    """Discard partial current/terminal periods, preserving actual trading dates."""
    if timeframe not in PERIODS:
        raise ValueError("K线周期无效。")
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None)
    frame = frame.sort_values("date").drop_duplicates("date")
    frame = frame[frame.date.dt.date <= pd.Timestamp(cutoff).date()]
    if frame.empty:
        return frame
    if timeframe == "daily":
        return frame.reset_index(drop=True)
    periods = frame.date.dt.to_period("W-FRI" if timeframe == "weekly" else "M")
    frame = frame.assign(_period=periods)
    frame = frame[frame._period.map(lambda value: value.end_time.date() <= pd.Timestamp(cutoff).date())]
    if frame.empty:
        return frame.drop(columns="_period")
    grouped = frame.groupby("_period", sort=True)
    result = grouped.agg(date=("date", "last"), open=("open", "first"),
                         high=("high", "max"), low=("low", "min"),
                         close=("close", "last"), volume=("volume", "sum"))
    return result.reset_index(drop=True)


def baostock_history(symbol, start, end):
    """Read A-share forward-adjusted daily bars without a JavaScript runtime."""
    import baostock as bs
    from baostock.common import context
    from factor_data_prep import _baostock_login, _bs_query
    if getattr(context, "default_socket", None) is None:
        _baostock_login(bs)
    code = ("sh." if symbol.startswith("6") else "sz.") + symbol
    frame = _bs_query(bs, f"{symbol} 前复权日线", bs.query_history_k_data_plus,
                      code, "date,open,high,low,close,volume,tradestatus",
                      start_date=start, end_date=end, frequency="d", adjustflag="2")
    if frame.empty:
        raise ValueError("BaoStock 未返回历史行情。")
    frame = frame[frame["tradestatus"] == "1"].drop(columns="tradestatus").copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().sort_values("date").reset_index(drop=True)
    frame.attrs["source"] = "BaoStock 前复权日线"
    return frame


def history(market, symbol, start, end, provider="default"):
    if market == "a" and provider == "baostock":
        return baostock_history(symbol, start, end)
    if market == "us":
        from us_market import history as fetch
        return fetch(symbol, start, end, "split_dividend")
    from stock_data_server import history as fetch
    return fetch(symbol, start, end, "qfq", market)


def panel(market, timeframe, symbols, start, end, strategy, parameters,
          allow_missing=False, require_recent=True, strict_stale=False):
    lookback = {"daily": 500, "weekly": 1200, "monthly": 2600}[timeframe]
    first = str((pd.Timestamp(start) - pd.Timedelta(days=lookback)).date())
    frames = []
    failures = []
    use_baostock = market == "a" and allow_missing
    def one(symbol):
        cache = HISTORY_CACHE / market / ("baostock-qfq" if use_baostock else "default") / f"{symbol}-{first}-{end}.csv.gz"
        cached = cache if cache.is_file() else None
        if allow_missing and cached is None and cache.parent.is_dir():
            # A previously fetched longer range can satisfy a shorter rerun.
            supersets = [path for path in cache.parent.glob(f"{symbol}-{first}-*.csv.gz")
                        if path.name[-17:-7] >= end]
            cached = min(supersets, key=lambda path: path.name[-17:-7], default=None)
        if allow_missing and cached is not None:
            daily = pd.read_csv(cached, parse_dates=["date"])
        else:
            daily = history(market, symbol, first, end, "baostock" if use_baostock else "default")
            if allow_missing and not daily.empty:
                cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix(".tmp")
                daily.to_csv(temporary, index=False, compression="gzip")
                temporary.replace(cache)
        bars = completed_bars(daily, timeframe, end)
        if len(bars) < 3:
            raise ValueError(f"{symbol} 已完成{PERIODS[timeframe]}不足，无法形成回测信号。")
        max_gap = {"daily": 10, "weekly": 21, "monthly": 65}[timeframe]
        if require_recent and (pd.Timestamp(end) - bars.date.max()).days > max_gap:
            raise IncompleteHistoryError(f"{symbol} 的{PERIODS[timeframe]}行情截至 {bars.date.max().date()}，未覆盖所选结束日期。")
        bars = features(bars, {strategy: parameters} if strategy in FIXED_STRATEGIES else {})
        if market == "a" and timeframe == "daily":
            bars["exit_close"] = bars.close.shift(-2)
            bars["exit_day"] = bars.date.shift(-2)
            bars["target"] = bars.exit_close / bars.entry_open - 1
        else:
            bars["exit_day"] = bars.entry_day
        bars["symbol"] = symbol
        return bars
    if allow_missing:
        if use_baostock:
            from factor_data_prep import BaoStockNetworkError
        try:
            for number, symbol in enumerate(symbols, 1):
                try:
                    frames.append(one(symbol))
                except Exception as exc:
                    if use_baostock and isinstance(exc, BaoStockNetworkError):
                        raise
                    if strict_stale and isinstance(exc, IncompleteHistoryError):
                        raise ValueError(f"历史指数成分 {symbol} 提前断档，无法可靠模拟退出；已停止回测。{exc}") from exc
                    failures.append({"symbol": symbol, "reason": str(exc)[:200]})
                if number == 1 or number % 10 == 0 or number == len(symbols):
                    print(f"读取 {PERIODS[timeframe]}历史行情 {number}/{len(symbols)}；可用 {len(frames)}，缺失 {len(failures)}", flush=True)
        finally:
            if use_baostock:
                import baostock as bs
                from factor_data_prep import _baostock_logout
                _baostock_logout(bs)
        minimum = max(1, math.ceil(len(symbols) * .9))
        if len(frames) < minimum:
            raise ValueError(f"股票池历史行情仅可用 {len(frames)}/{len(symbols)} 只，低于 90% 覆盖门槛；示例错误：{failures[:3]}")
    else:
        for symbol in symbols:
            print(f"读取 {market.upper()} {symbol} {PERIODS[timeframe]}…", flush=True)
            frames.append(one(symbol))
    result = pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])
    result.attrs["failures"] = failures
    result.attrs["valid_symbols"] = sorted(row.symbol.iloc[0] for row in frames)
    return result


def checkpoints(market, timeframe):
    selection(market, timeframe)
    result = []
    for path in (MODELS / market / timeframe).glob("*/checkpoint.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if (row["market"] == market and row["timeframe"] == timeframe
                    and row["model_id"] == path.parent.name
                    and hashlib.sha256((path.parent / "weights.json").read_bytes()).hexdigest() == row["artifact_sha256"]):
                result.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda row: row["selected_at"], reverse=True)


def options(market="a", timeframe="daily"):
    selection(market, timeframe)
    fixed = {key: {"name": f"{PERIODS[timeframe]} · {strategy_name(key)}",
                   "description": "完整K线收盘形成信号，下一根K线执行。",
                   "parameter_schema": parameter_schema(key),
                   "parameters": validate_parameters(key)} for key in FIXED_STRATEGIES}
    models = {f"{row['algorithm']}@{row['model_id']}": {
        "name": f"{PERIODS[timeframe]} · {STRATEGIES[row['algorithm']]['name']} · {row['model_id']}",
        "strategy_id": row["algorithm"], "checkpoint_id": row["model_id"],
        "pool": row.get("pool"), "requested_count": row.get("requested_count"), "valid_count": row.get("valid_count"),
        "selected_at": row.get("selected_at"), "validation_mse": row.get("validation_mse"),
        "validation": row.get("validation"),
        "training_range": row["training"], "parameter_schema": parameter_schema(row["algorithm"]),
        "parameters": validate_parameters(row["algorithm"])} for row in checkpoints(market, timeframe)}
    training = {key: {"name": f"{PERIODS[timeframe]} · {STRATEGIES[key]['name']}",
                      "parameter_schema": parameter_schema(key, "training")} for key in MODEL_STRATEGIES}
    return {"market": market, "timeframe": timeframe, "fixed_strategies": fixed,
            "models": models, "strategies": {**fixed, **models}, "training": training,
            "selected": next(iter(fixed)), "selected_kind": "fixed"}


def validation_diagnostics(validation, prediction):
    measured = validation[["date", "symbol", "target"]].copy()
    measured["prediction"] = prediction
    mse = float(np.mean((measured.prediction - measured.target) ** 2))
    baseline_mse = float(np.mean(measured.target ** 2))
    daily_ic, daily_excess, daily_top_returns = [], [], []
    for _, group in measured.groupby("date"):
        if len(group) >= 10:
            ic = group[["prediction", "target"]].corr(method="spearman").iloc[0, 1]
            if np.isfinite(ic):
                daily_ic.append(float(ic))
        picks = group[group.prediction > 0].nlargest(5, "prediction")
        if not picks.empty:
            daily_top_returns.append(float(picks.target.mean()))
            if len(group) >= 10:
                daily_excess.append(float(picks.target.mean() - group.target.mean()))
    cross_sectional = measured.symbol.nunique() >= 10
    mean_ic = float(np.mean(daily_ic)) if daily_ic else None
    top5_excess = float(np.mean(daily_excess)) if daily_excess else None
    top5_return = float(np.mean(daily_top_returns)) if daily_top_returns else None
    # The daily simulation assumes 5 bp fee and 5 bp slippage on each side.
    estimated_roundtrip_cost = .002
    passed = (mse < baseline_mse and
              top5_return is not None and top5_return > estimated_roundtrip_cost and
              (not cross_sectional or mean_ic is not None and mean_ic > 0 and top5_excess is not None and top5_excess > 0))
    reasons = []
    if mse >= baseline_mse:
        reasons.append("均方误差未优于零收益基线")
    if top5_return is None or top5_return <= estimated_roundtrip_cost:
        reasons.append("预测为正的前五名平均收益未覆盖约20基点双边交易成本")
    if cross_sectional and (mean_ic is None or mean_ic <= 0):
        reasons.append("逐日排序相关性未为正")
    if cross_sectional and (top5_excess is None or top5_excess <= 0):
        reasons.append("前五名未跑赢同期股票池等权均值")
    return {"passed": bool(passed), "mse": mse, "zero_baseline_mse": baseline_mse,
            "daily_rank_ic": mean_ic, "top5_excess_return": top5_excess,
            "top5_gross_return": top5_return, "estimated_roundtrip_cost": estimated_roundtrip_cost,
            "positive_prediction_fraction": float((measured.prediction > 0).mean()),
            "sample_count": len(measured), "day_count": measured.date.nunique(),
            "reason": "；".join(reasons) if reasons else "验证门槛通过"}


def fit_model(frame, market, timeframe, strategy, start, end, parameters,
              pool_info=None, requested_symbols=None, failures=None):
    clean = frame[(frame.date >= pd.Timestamp(start)) & (frame.exit_day <= pd.Timestamp(end))].dropna(subset=FEATURES + ["target"])
    days = sorted(clean.date.unique())
    if len(days) < 30:
        raise ValueError(f"模型训练至少需要30根有完整特征与标签的{PERIODS[timeframe]}。")
    split = pd.Timestamp(days[int(len(days) * .8)])
    fit = clean[clean.exit_day < split]
    validation = clean[clean.date >= split]
    if len(fit) < 20 or validation.empty:
        raise ValueError("训练或验证样本不足。")
    x, y = fit[FEATURES].to_numpy(), fit.target.to_numpy()
    tuning_start, selected_trees = None, None
    if strategy == "elastic_net":
        from sklearn.linear_model import ElasticNet
        mean, scale = x.mean(axis=0), x.std(axis=0)
        scale[scale == 0] = 1
        model = ElasticNet(alpha=parameters.get("alpha_scale", .01) * max(float(np.std(y)), 1e-8),
                           l1_ratio=parameters.get("l1_ratio", .5), max_iter=int(parameters.get("max_iter", 10000))).fit((x - mean) / scale, y)
        weights = {"mean": mean.tolist(), "scale": scale.tolist(), "coef": model.coef_.tolist(), "intercept": float(model.intercept_)}
        prediction = ((validation[FEATURES].to_numpy() - mean) / scale) @ model.coef_ + model.intercept_
    else:
        from xgboost import XGBRegressor
        configuration = dict(n_estimators=int(parameters.get("n_estimators", 120)), max_depth=int(parameters.get("max_depth", 2)),
                             learning_rate=parameters.get("learning_rate", .04), min_child_weight=parameters.get("min_child_weight", 20),
                             reg_lambda=parameters.get("reg_lambda", 10), subsample=parameters.get("subsample", .8), random_state=42, n_jobs=2)
        # Select the tree count inside the fitting window; keep the final 20%
        # untouched for the acceptance check.
        if len(days) >= 80:
            tuning_start = pd.Timestamp(days[int(len(days) * .65)])
            inner_fit = clean[clean.exit_day < tuning_start]
            tuning = clean[(clean.date >= tuning_start) & (clean.exit_day < split)]
            if len(inner_fit) >= 20 and not tuning.empty:
                probe = XGBRegressor(**configuration, early_stopping_rounds=20)
                probe.fit(inner_fit[FEATURES].to_numpy(), inner_fit.target.to_numpy(),
                          eval_set=[(tuning[FEATURES].to_numpy(), tuning.target.to_numpy())], verbose=False)
                selected_trees = probe.best_iteration + 1
                configuration["n_estimators"] = selected_trees
            else:
                tuning_start = None
        model = XGBRegressor(**configuration).fit(x, y)
        weights = None
        prediction = model.predict(validation[FEATURES].to_numpy())
    diagnostics = validation_diagnostics(validation, prediction)
    identity = f"{market}-{timeframe}-{strategy}-{datetime.now(ZoneInfo(ZONES[market])):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    artifact = MODELS / market / timeframe / identity / "weights.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if weights is None:
        model.save_model(artifact)
    else:
        save_json(artifact, weights)
    record = {"market": market, "timeframe": timeframe, "model_id": identity, "algorithm": strategy,
              "features": FEATURES, "selected_at": datetime.now(ZoneInfo(ZONES[market])).isoformat(),
              "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
              "training": {"first_day": str(fit.date.min().date()), "last_day": str(fit.date.max().date()),
                           "label_end_day": str(clean.exit_day.max().date()), "validation_start": str(split.date()),
                           "early_stopping_start": str(tuning_start.date()) if tuning_start is not None else None},
              "validation_mse": diagnostics["mse"], "validation": diagnostics,
              "selected_trees": selected_trees,
              "training_parameters": parameters, "universe": sorted(frame.symbol.unique()),
              "pool": pool_info or {"id": "custom", "name": "自定股票池", "source": "用户输入", "as_of": None},
              "requested_count": len(requested_symbols) if requested_symbols is not None else frame.symbol.nunique(),
              "valid_count": frame.symbol.nunique(), "missing_symbols": failures or []}
    save_json(artifact.parent / "checkpoint.json", record)
    return record


def score_model(frame, market, timeframe, strategy, checkpoint, first_signal, require_validated=False):
    record = next((item for item in checkpoints(market, timeframe)
                   if item["model_id"] == checkpoint and item["algorithm"] == strategy), None)
    if not record:
        raise ValueError("请选择本市场、本周期有效的模型 checkpoint。")
    if require_validated and record.get("validation", {}).get("passed") is not True:
        raise ValueError("该模型未通过或未记录验证门槛，仅可用于回测研究，不能部署。")
    if record["training"]["label_end_day"] >= first_signal:
        raise ValueError("回测或部署信号日期必须晚于模型训练标签结束日。")
    artifact = MODELS / market / timeframe / checkpoint / "weights.json"
    if strategy == "elastic_net":
        weight = json.loads(artifact.read_text(encoding="utf-8"))
        return ((frame[FEATURES].to_numpy() - weight["mean"]) / weight["scale"]) @ np.array(weight["coef"]) + weight["intercept"]
    from xgboost import XGBRegressor
    model = XGBRegressor()
    model.load_model(artifact)
    return model.predict(frame[FEATURES].to_numpy())


def scored(frame, market, timeframe, strategy, checkpoint, params, first_signal, require_validated=False):
    if strategy in MODEL_STRATEGIES:
        valid = frame[FEATURES].notna().all(axis=1)
        frame = frame.copy()
        frame["score"] = np.nan
        frame.loc[valid, "score"] = score_model(frame.loc[valid], market, timeframe, strategy, checkpoint, first_signal, require_validated)
        frame["eligible"] = frame.score > 0
    else:
        frame = frame.copy()
        frame["score"], frame["eligible"] = rule_scores(frame, strategy, params)
    return frame


def simulate(frame, market, timeframe, start, end, top_n, fee_bps=5, slippage_bps=5):
    initial = 100000.0
    cash, curve, trades, slippage_costs = initial, [], [], 0.0
    eligible = frame[(frame.date >= pd.Timestamp(start)) & (frame.exit_day <= pd.Timestamp(end))
                     & frame.eligible].dropna(subset=["score", "entry_open", "exit_close"])
    dates = sorted(frame[(frame.date >= pd.Timestamp(start)) & (frame.date <= pd.Timestamp(end))].date.dropna().unique())
    if len(dates) < 2:
        raise ValueError("所选区间少于两根完整的执行K线，请扩大日期范围。")
    pending = None
    for date in dates:
        if pending is not None:
            if date >= pending[0]:
                cash += pending[1]
                trades.extend(pending[2])
                pending = None
            # The prior A-share position closes at today's close. It cannot
            # fund a new trade at today's open, even when settlement is today.
            marked = 0.0
            if pending is not None:
                closes = frame[frame.date == date].set_index("symbol").close.to_dict()
                marked = sum(row["shares"] * (float(closes.get(row["symbol"], row["entry"])) - row["entry"])
                             - row["shares"] * row["entry"] * fee_bps / 10000 for row in pending[2])
            curve.append({"day": str(pd.Timestamp(date).date()), "cash": cash, "equity": cash + marked})
            continue
        group = eligible[eligible.entry_day == date].nlargest(top_n, "score")
        budget = cash / top_n
        delta, batch, exit_day = 0.0, [], None
        for row in group.itertuples():
            if row.entry_open <= 0 or row.exit_close <= 0:
                continue
            entry = row.entry_open * (1 + slippage_bps / 10000)
            exit_price = row.exit_close * (1 - slippage_bps / 10000)
            affordable = budget / (entry * (1 + fee_bps / 10000))
            if market == "a" and row.symbol.startswith(("688", "689")):
                # STAR Market buys require at least 200 shares; increments above
                # that minimum may be one share.
                shares = math.floor(affordable)
                if shares < 200:
                    continue
            else:
                lot = 100 if market == "a" else 1
                shares = math.floor(affordable / lot) * lot
            if shares <= 0:
                continue
            cost = shares * (entry + exit_price) * fee_bps / 10000
            slippage_costs += shares * ((entry - row.entry_open) + (row.exit_close - exit_price))
            delta += shares * (exit_price - entry) - cost
            exit_day = pd.Timestamp(row.exit_day) if exit_day is None else max(exit_day, pd.Timestamp(row.exit_day))
            batch.append({"day": str(pd.Timestamp(row.exit_day).date()), "signal_day": str(row.date.date()),
                          "symbol": row.symbol, "shares": shares, "entry": entry,
                          "exit": exit_price, "cost": cost, "return": exit_price / entry - 1})
        if market == "a" and timeframe == "daily" and batch:
            pending = (exit_day, delta, batch)
        else:
            cash += delta
            trades.extend(batch)
        if pending is not None:
            closes = frame[frame.date == date].set_index("symbol").close.to_dict()
            marked = sum(row["shares"] * (float(closes.get(row["symbol"], row["entry"])) - row["entry"])
                         - row["shares"] * row["entry"] * fee_bps / 10000 for row in pending[2])
        else:
            marked = 0.0
        curve.append({"day": str(pd.Timestamp(date).date()), "cash": cash, "equity": cash + marked})
    nav = np.array([initial] + [row["equity"] for row in curve])
    returns = nav[1:] / nav[:-1] - 1
    annual = {"daily": 252, "weekly": 52, "monthly": 12}[timeframe]
    stats = {"initial_cash": initial, "final_equity": cash, "total_return": cash / initial - 1,
             "annualized_return": (cash / initial) ** (annual / len(curve)) - 1,
             "max_drawdown": float(np.min(nav / np.maximum.accumulate(nav) - 1)),
             "sharpe": float(returns.mean() / returns.std() * np.sqrt(annual)) if returns.std() else 0.0,
             "trades": len(trades), "costs": sum(row["cost"] for row in trades),
             "slippage_costs": slippage_costs, "trading_days": len(curve)}
    return pd.DataFrame(curve), pd.DataFrame(trades, columns=["day", "signal_day", "symbol", "shares", "entry", "exit", "cost", "return"]), stats


def run(action, market, timeframe, strategy, symbols, start, end, parameters=None, checkpoint="", pool="custom", pool_as_of=""):
    selection(market, timeframe)
    if strategy not in STRATEGIES:
        raise ValueError("策略无效。")
    if action == "train" and strategy not in MODEL_STRATEGIES:
        raise ValueError("仅因子模型支持训练。")
    if action == "backtest" and strategy in MODEL_STRATEGIES and not checkpoint:
        raise ValueError("模型回测须选择本周期已训练 checkpoint。")
    params = validate_parameters(strategy, parameters, "training" if action == "train" else "execution")
    today = datetime.now(ZoneInfo(ZONES[market])).date()
    if action == "deploy":
        end = str(today - timedelta(days=1))
        start = str(today - timedelta(days={"daily": 120, "weekly": 450, "monthly": 1800}[timeframe]))
    elif pd.Timestamp(start).date() > pd.Timestamp(end).date() or pd.Timestamp(end).date() >= today:
        raise ValueError("日期无效；结束日期须早于当前市场日期。")
    if action == "deploy" and strategy in MODEL_STRATEGIES:
        selected = next((row for row in checkpoints(market, timeframe)
                         if row["model_id"] == checkpoint and row["algorithm"] == strategy), None)
        if selected is None or selected.get("validation", {}).get("passed") is not True:
            raise ValueError("该模型未通过或未记录验证门槛，仅可用于回测研究，不能部署。")
    symbols, pool_info = resolve_pool(action, market, pool, symbols, start)
    if pool == "portfolio":
        pool_info["as_of"] = pool_as_of or datetime.now(ZoneInfo(ZONES[market])).isoformat(timespec="seconds")
    frame = panel(market, timeframe, symbols, start, end, strategy, params,
                  allow_missing=pool not in ("custom", "portfolio"), require_recent=action != "train",
                  strict_stale=action == "backtest" and pool in ("csi300", "csi500"))
    failures = frame.attrs.get("failures", [])
    valid_symbols = frame.attrs.get("valid_symbols", symbols)
    if action == "train":
        return fit_model(frame, market, timeframe, strategy, start, end, params, pool_info, symbols, failures)
    first_signal = str(pd.Timestamp(start).date()) if action == "backtest" else str(frame.date.max().date())
    frame = scored(frame, market, timeframe, strategy, checkpoint, params, first_signal, require_validated=action == "deploy")
    if action == "deploy":
        latest = frame[frame.date == frame.date.max()].dropna(subset=["score"])
        ranked = latest.sort_values("score", ascending=False)
        rows = [{"rank": rank, "symbol": row.symbol, "score": float(row.score), "entry": bool(row.eligible),
                 "price": float(row.close), "signal_day": str(row.date.date())}
                for rank, row in enumerate(ranked.itertuples(), 1)]
        result = {"available": True, "market": market, "timeframe": timeframe, "period_label": PERIODS[timeframe],
                  "strategy_id": strategy, "strategy_name": strategy_name(strategy), "checkpoint_id": checkpoint or None,
                  "symbols": symbols, "pool": pool_info, "missing_symbols": failures,
                  "strategy_parameters": params, "generated_at": datetime.now(ZoneInfo(ZONES[market])).isoformat(),
                  "as_of": str(frame.date.max().date()), "ranking": rows, "input_count": len(symbols),
                  "valid_count": len(valid_symbols), "ranked_count": len(rows),
                  "note": "持仓池使用启动时保存的股票名单，不按实际持股数量配置；信号使用已完成K线。" if pool == "portfolio" else "指数及全盘使用当前股票名单；信号使用已完成K线，不代表交易指令。"}
        DEPLOYMENTS.mkdir(parents=True, exist_ok=True)
        suffix = f"-{checkpoint}" if checkpoint else ""
        save_json(DEPLOYMENTS / f"latest-{market}-{timeframe}-{strategy}{suffix}-{pool}.json", result)
        return result
    curve, trades, stats = simulate(frame, market, timeframe, start, end, params["top_n"])
    checkpoint_record = next((row for row in checkpoints(market, timeframe) if row["model_id"] == checkpoint), None) if checkpoint else None
    oos = None
    if strategy in MODEL_STRATEGIES:
        evaluated = frame[(frame.date >= pd.Timestamp(start)) & (frame.exit_day <= pd.Timestamp(end))].dropna(subset=["score", "target"])
        if not evaluated.empty:
            oos = validation_diagnostics(evaluated, evaluated.score.to_numpy())
    identity = f"{market}-{timeframe}-{strategy}-{datetime.now(ZoneInfo(ZONES[market])):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    directory = BACKTESTS / identity
    directory.mkdir(parents=True, exist_ok=True)
    curve.to_csv(directory / "equity.csv", index=False)
    trades.to_csv(directory / "trades.csv", index=False)
    result = {"kind": "bar_backtest", "schema_version": 3, "market": market, "timeframe": timeframe,
              "period_label": PERIODS[timeframe], "currency": {"a": "CNY", "hk": "HKD", "us": "USD"}[market],
              "run_id": identity, "slot": f"{PERIODS[timeframe]}收盘信号 → 下一根K线执行",
              "strategy_id": strategy, "strategy_name": strategy_name(strategy),
              "generated_at": datetime.now(ZoneInfo(ZONES[market])).isoformat(), "start": start, "end": end,
              "first_day": str(curve.day.iloc[0]), "last_day": str(curve.day.iloc[-1]), "days": len(curve),
              "execution": "next completed bar; A-share daily T+1 exit" if market == "a" and timeframe == "daily" else "next completed bar open to close",
              "stats": stats, "parameters": params, "checkpoint_id": checkpoint or None, "universe": symbols,
              "checkpoint_trained_at": checkpoint_record.get("selected_at") if checkpoint_record else None,
              "training_label_end": checkpoint_record.get("training", {}).get("label_end_day") if checkpoint_record else None,
              "oos_rank_ic": {"rank_ic": oos["daily_rank_ic"], "top5_excess_return": oos["top5_excess_return"],
                              "top5_gross_return": oos["top5_gross_return"]} if oos else None,
              "pool": pool_info, "valid_universe": valid_symbols, "missing_symbols": failures,
              "assumptions": {"fee_bps_per_side": 5, "slippage_bps_per_side": 5,
                              "universe_policy": pool_info.get("membership_policy", "用户输入名单固定"), "shorting": False,
                              "a_share_daily_exit": "T+1, following trading day close" if market == "a" and timeframe == "daily" else None},
              "limitations": [f"{pool_info['name']}采用 {pool_info['as_of'] or '用户输入'} 的固定名单；历史指数预设冻结研究起始日成分，期间不动态调仓；有效行情 {len(valid_symbols)}/{len(symbols)} 只。",
                              "复权价格与统一费率、滑点是研究模拟，不等于真实成交；未模拟涨跌停、停牌和税费细项。"]}
    if (checkpoint_record and checkpoint_record.get("pool", {}).get("id") in ("csi300", "csi500")
            and checkpoint_record["pool"].get("as_of", "") > checkpoint_record.get("training", {}).get("first_day", "")):
        result["limitations"].append("该模型训练时使用晚于训练开始日的指数成分名单，含未来股票池偏差；请按新历史成分规则重新训练。")
    save_json(directory / "summary.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["train", "backtest", "deploy"])
    parser.add_argument("--market", required=True, choices=tuple(ZONES))
    parser.add_argument("--timeframe", default="daily", choices=tuple(PERIODS))
    parser.add_argument("--strategy", required=True, choices=tuple(STRATEGIES))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--pool", default="custom", choices=tuple(POOL_NAMES))
    parser.add_argument("--pool-as-of", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--parameters", default="{}")
    parser.add_argument("--checkpoint", default="")
    args = parser.parse_args()
    if os.getenv("FINANCE_WEB_OWNED") == "1":
        from web_process_owner import watch_owner
        watch_owner()
    result = run(args.action, args.market, args.timeframe, args.strategy, args.symbols,
                 args.start, args.end, json.loads(args.parameters), args.checkpoint, args.pool, args.pool_as_of)
    output = ({key: result[key] for key in ("available", "pool", "input_count", "valid_count", "ranked_count")}
              if args.action == "deploy" else
              {key: result[key] for key in ("model_id", "pool", "requested_count", "valid_count", "validation_mse")}
              if args.action == "train" else result)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
