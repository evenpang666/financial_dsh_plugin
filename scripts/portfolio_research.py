"""Three comparable research allocations, separate from broker/account positions."""

from datetime import datetime, timedelta, timezone
import logging
import math
import re
import time
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from data_diagnostics import readable_issues
from strategy_catalog import MODEL_STRATEGIES, RULE_FEATURES, STRATEGIES, TECHNICAL_STRATEGIES, rule_scores, validate_strategy, strategy_description

ROOT = Path(__file__).resolve().parents[1]
CHINA = timezone(timedelta(hours=8))


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def selected_strategy(value=""):
    from factor_lab import load_settings
    return validate_strategy(value or load_settings()["strategy"])


def allocation(scores, exposure, cap=25.0):
    """Cap each name; excess remains cash, never redistributed above limits."""
    if exposure is None or any(value is None or not math.isfinite(value) or value < 0 for value in scores):
        return [None] * len(scores), None
    total = sum(scores)
    weights = [math.floor(min(cap, exposure * value / total) * 100) / 100 if total else 0.0 for value in scores]
    return weights, round(100 - sum(weights), 2)


def exposure_limit(panorama):
    breadth = (panorama or {}).get("market_breadth", {})
    issues = (panorama or {}).get("unavailable", [])
    if (breadth.get("label") == "数据不足" or finite(breadth.get("score")) is None
            or any("大盘宽度不可用" in item or "事件与政策不可用" in item for item in issues)):
        return None
    return {"极高": 20.0, "高": 40.0, "中": 60.0, "较低": 70.0, "低": 80.0}.get(
        (panorama or {}).get("market_state", {}).get("risk_level"))


def stock_basis(item, now):
    analysis = item.get("analysis", {})
    recommendation = analysis.get("recommendation") or {}
    components = recommendation.get("score_components") or {}
    price = finite(item.get("price"))
    try:
        day = pd.Timestamp(analysis.get("as_of")).date()
        recent = timedelta(0) <= now.date() - day <= timedelta(days=7)
    except (TypeError, ValueError):
        recent = False
    signals = finite(components.get("valid_signal_count"))
    if price is None or price <= 0 or signals is None or signals < 3 or not recent:
        return None, "信息不足，暂不形成数值配置；补齐价格、有效信号和数据日期。"
    action = recommendation.get("conclusion")
    if action == "建议卖出":
        return 0.0, "研究结论偏向减仓复核，本方案不新增配置。"
    if action not in {"建议买入", "建议持有观察"}:
        return None, "个股结论不足以形成配置。"
    short = max(0, finite(components.get("short_technical_score")) or 0)
    long = max(0, finite(components.get("long_fundamental_valuation_score")) or 0)
    return (2.0 if action == "建议买入" else 1.0) + short + long, "；".join(recommendation.get("basis", [])) or action


@lru_cache(maxsize=256)
def reference_close_response(symbol, day):
    """Fallback reference price has an explicit provider date, never a guessed date."""
    ticker = ("sh" if symbol.startswith("6") else "sz") + symbol
    response = requests.get("https://hq.sinajs.cn/list=" + ticker,
        headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}, timeout=(5, 10))
    response.raise_for_status()
    response.encoding = "gb18030"
    match = re.search(r'var hq_str_' + re.escape(ticker) + r'="([^"]*)"', response.text)
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32 or fields[30] != day:
        raise ValueError("备用昨收价的行情日期不匹配，不能用于跨日策略")
    value = finite(fields[2])
    if value is None or value <= 0:
        raise ValueError("备用行情未返回有效昨收价")
    return value


def load_signal_record(item, strategy, asof, previous_day, deadline=None, parameters=None):
    """Use verified completed 5-minute bars; keep optional cache separate from replay."""
    from factor_lab import normalized_bars, sina_minute_frame, factor_values, inferred_vwap
    from factor_data_prep import _minute_times, _minute_cache_complete, minute_quality_issues
    from strategy_indicators import indicator_frame
    symbol = item["holding"]["symbol"]
    cache = ROOT / "data" / "factor-lab" / "live-bars" / f"{symbol}.csv"
    frames = []
    for path in (ROOT / "data" / "factor-lab" / "bars" / f"{symbol}.csv", cache):
        if path.is_file():
            frames.append(normalized_bars(pd.read_csv(path)))
    bars = normalized_bars(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    if bars.empty or asof not in set(bars.time):
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("本轮策略取数达到时限，保留缺失状态并稍后重试")
        # Unlike quote enrichment, this existing fallback has a bounded timeout.
        fresh = normalized_bars(sina_minute_frame(("sh" if symbol.startswith("6") else "sz") + symbol))
        bars = normalized_bars(pd.concat([*frames, fresh], ignore_index=True))
    day = asof.strftime("%Y-%m-%d")
    bars = bars[bars.time.le(asof) & bars.time.dt.strftime("%Y-%m-%d").isin([previous_day, day])].copy()
    expected = set(_minute_times(day)[_minute_times(day) <= asof])
    today = bars[bars.time.dt.strftime("%Y-%m-%d").eq(day)]
    if (minute_quality_issues(bars) or not _minute_cache_complete(bars, [previous_day])
            or set(today.time) != expected or today.empty):
        raise ValueError("分钟线缺失、未完成或质量校验失败；不补造策略信号")
    quote = item.get("quote", {})
    previous_close = finite(quote.get("昨收"))
    if previous_close is None or previous_close <= 0:
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("本轮策略取数达到时限，无法补充昨收价")
        previous_close = reference_close_response(symbol, day)
    price = float(today.iloc[-1].close)
    vwap = inferred_vwap(today.amount.sum(), today.volume.sum(), price)
    factors = factor_values(price, float(today.iloc[0].open), previous_close, float(today.high.max()), float(today.low.min()), vwap)
    if factors is None:
        raise ValueError("分钟线无法形成有效价量特征")
    if strategy in TECHNICAL_STRATEGIES:
        prior = bars[bars.time.dt.strftime("%Y-%m-%d").eq(previous_day)]
        events = pd.DataFrame([{"day": day, "cash_dividend": 0,
                                "split_ratio": float(prior.iloc[-1].close) / previous_close}])
        factors.update(indicator_frame(bars, events, {strategy: parameters} if parameters else None).iloc[-1].to_dict())
    factors["minute_of_day"] = asof.hour * 60 + asof.minute
    needed = ("open_momentum", "gap", "range_position", "range_width", "vwap_gap", "minute_of_day") if strategy in MODEL_STRATEGIES else RULE_FEATURES[strategy]
    if any(finite(factors.get(key)) is None for key in needed):
        raise ValueError("策略所需指标预热不足")
    cache.parent.mkdir(parents=True, exist_ok=True)
    from factor_lab import write_bars_cache
    write_bars_cache(cache, bars)
    return {"symbol": symbol, "price": price, "as_of": asof.isoformat(), **factors}


def strategy_review(items, strategy, market, now=None, checkpoint_id=None, parameters=None):
    from strategy_config import validate_parameters, configured_parameters
    from factor_lab import load_settings
    parameters = (configured_parameters(load_settings(), strategy) if parameters is None
                  else validate_parameters(strategy, parameters))
    now = now or datetime.now(CHINA)
    result = {"strategy_id": strategy, "strategy_name": STRATEGIES[strategy]["name"],
              "description": strategy_description(strategy, parameters), "strategy_parameters": parameters, "scope": "仅本次已保存持仓池，独立评分；不是全市场排名",
              "available": False, "rows": {}, "as_of": None, "input_count": len(items), "valid_count": 0}
    if market != "a":
        return {**result, "note": "当前交易策略仅支持 A 股，港股结果2/3暂不形成数值配置。"}
    try:
        import akshare as ak
        from factor_lab import selected_model, MODELS, ML_TARGET_VERSION
        from factor_models import predict
        calendar = ak.tool_trade_date_hist_sina()
        key = next((key for key in ("trade_date", "日期") if key in calendar.columns), None)
        if key is None:
            raise ValueError("交易日历缺少日期")
        days = sorted(set(pd.to_datetime(calendar[key]).dt.date))
        if now.date() not in days or now.strftime("%H:%M") < "09:35":
            raise ValueError("尚无当日已完成5分钟线；休市或盘前不冒用历史信号")
        previous = [day for day in days if day < now.date()]
        if not previous:
            raise ValueError("交易日历缺少前一交易日")
        cutoff = "15:00" if now.strftime("%H:%M") >= "15:00" else "11:30" if "11:30" <= now.strftime("%H:%M") < "13:05" else pd.Timestamp(now).floor("5min").strftime("%H:%M")
        asof = pd.Timestamp(f"{now.date()} {cutoff}")
        result["as_of"] = asof.isoformat()
        model = selected_model(strategy, checkpoint_id or "") if strategy in MODEL_STRATEGIES else None
        if strategy in MODEL_STRATEGIES:
            if not model or model.get("target_version") != ML_TARGET_VERSION:
                raise ValueError("所选模型尚未有效训练，请先在网页回测页训练")
            selected_at = datetime.fromisoformat(model["selected_at"])
            if selected_at.tzinfo is None:
                selected_at = selected_at.replace(tzinfo=CHINA)
            if now < selected_at:
                raise ValueError("checkpoint 训练日期晚于当前时间")
            result.update(checkpoint_id=model["model_id"], checkpoint_trained_at=model["selected_at"])
        from concurrent.futures import ThreadPoolExecutor
        deadline = time.monotonic() + 45
        def load(item):
            symbol = item["holding"]["symbol"]
            try:
                if time.monotonic() >= deadline:
                    raise ValueError("本轮策略取数达到时限，稍后重试")
                if not symbol.startswith(("0", "3", "6")):
                    raise ValueError("当前策略只覆盖沪深A股，北交所策略尚未接入")
                name = str(item.get("analysis", {}).get("name", "")).upper()
                if "ST" in name or "退" in name:
                    raise ValueError("ST 标的仅作风险复核，不生成入场配置")
                return load_signal_record(item, strategy, asof, previous[-1].isoformat(), deadline, parameters=parameters), None
            except Exception as error:
                logging.getLogger(__name__).warning("持仓策略 %s/%s: %s", strategy, symbol, error)
                return None, {"symbol": symbol, "status": "unavailable", "note": readable_issues([f"策略信号不可用：{error}"])[0]}
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
            loaded = list(pool.map(load, items))
        records = [record for record, error in loaded if record is not None]
        result["valid_count"] = len(records)
        result["rows"] = {error["symbol"]: error for record, error in loaded if error}
        if records:
            frame = pd.DataFrame(records)
            if strategy in MODEL_STRATEGIES:
                frame["score"] = predict(frame, model, MODELS / model["model_id"])
                frame["entry"] = True
            else:
                frame["score"], frame["entry"] = rule_scores(frame, strategy, parameters)
            if not frame.score.map(lambda value: finite(value) is not None).all():
                raise ValueError("策略评分包含无效数值")
            frame["rank"] = frame.loc[frame.entry, "score"].rank(method="min", ascending=False)
            deployed = set(frame[frame.entry].sort_values(["score", "symbol"], ascending=[False, True]).head(parameters["top_n"]).symbol)
            result["deployment_candidates"] = sorted(deployed)
            for row in frame.to_dict("records"):
                rank = int(row["rank"]) if row["entry"] else None
                result["rows"][row["symbol"]] = {"status": "triggered" if row["entry"] else "not_triggered",
                    "score": round(float(row["score"]), 5), "rank": rank,
                    "allocation_score": 1.0 / rank if rank and row["symbol"] in deployed else 0.0,
                    "as_of": row["as_of"], "note": "已满足所选策略入场条件" if rank else "未触发新增入场；不等同于原持仓卖出信号"}
        result["available"] = len(records) == len(items)
        if not result["available"]:
            result["scope"] += "；本次名次仅覆盖输入有效的子集，不能视为完整持仓排序"
        result["note"] = "使用已完成5分钟线，与日线研究的周期不同；收盘后仅为收盘快照研究。" if result["available"] else "部分持仓缺少策略证据，整组结果2暂不形成数值配置。"
    except Exception as error:
        logging.getLogger(__name__).warning("持仓策略评估未完成：%s", error)
        result["note"] = str(error)[:180] if not any(word in str(error) for word in ("HTTP", "Connection", "Proxy", "https://")) else readable_issues([f"策略分析不可用：{error}"])[0]
        result["available"] = False
    return result


def build_position_summary(items, panorama, strategy_result, now=None):
    now = now or datetime.now(CHINA)
    exposure = exposure_limit(panorama)
    basis = [stock_basis(item, now) for item in items]
    weights1, cash1 = allocation([score for score, reason in basis], exposure)
    rows2 = [strategy_result.get("rows", {}).get(item["holding"]["symbol"], {}) for item in items]
    scores2 = [row.get("allocation_score") if strategy_result.get("available") else None for row in rows2]
    weights2, cash2 = allocation(scores2, exposure)
    comparable = all(str(item.get("analysis", {}).get("as_of", ""))[:10] == str(strategy_result.get("as_of", ""))[:10] for item in items)
    weights3 = [round(min(one, (one + two) / 2), 2) if one is not None and two is not None and comparable else None
                for one, two in zip(weights1, weights2)]
    cash3 = round(100 - sum(weights3), 2) if all(value is not None for value in weights3) else None
    amounts = [finite(item.get("holding", {}).get("shares")) for item in items]
    prices = [finite(item.get("price")) for item in items]
    complete_values = all(shares is not None and shares >= 0 and price is not None and price > 0 for shares, price in zip(amounts, prices))
    values = [shares * price for shares, price in zip(amounts, prices)] if complete_values else []
    total = sum(values) if values else 0
    rows = []
    for i, (item, signal) in enumerate(zip(items, rows2)):
        one, two, three = weights1[i], weights2[i], weights3[i]
        if three is None:
            reason = "结果1或2缺少完整证据，或数据日期不一致，暂缓综合配置。"
        elif one == 0 and two > 0:
            reason = "策略入场与个股研究反向，暂停新增配置并复核基本面。"
        elif one > 0 and two == 0:
            reason = "研究支持但策略未触发，新增研究配置折半；不据此强制卖出现有持仓。"
        elif one > 0 and two > 0:
            reason = "两类证据均支持；取两方案均值，并以结果1为风险上限。"
        else:
            reason = "两方案均不分配新增研究预算，保留现金并复核原持仓。"
        rows.append({"symbol": item["holding"]["symbol"], "name": item.get("analysis", {}).get("name") or item["holding"].get("name"),
            "analysis_as_of": item.get("analysis", {}).get("as_of"), "strategy_as_of": signal.get("as_of") or strategy_result.get("as_of"),
            "current_holding_share_pct": round(values[i] / total * 100, 2) if total else None,
            "suggestion1": {"weight_pct": one, "action": item.get("analysis", {}).get("recommendation", {}).get("conclusion", "信息不足"), "reason": basis[i][1]},
            "suggestion2": {"weight_pct": two, "rank": signal.get("rank"), "score": signal.get("score"), "status": signal.get("status", "unavailable"), "reason": signal.get("note", strategy_result.get("note", "策略数据不足"))},
            "suggestion3": {"weight_pct": three, "reason": reason}})
    return {"rows": rows, "cash_reserve_pct": {"suggestion1": cash1, "suggestion2": cash2, "suggestion3": cash3},
        "stock_totals_pct": {"suggestion1": round(sum(weights1), 2) if cash1 is not None else None,
                             "suggestion2": round(sum(weights2), 2) if cash2 is not None else None,
                             "suggestion3": round(sum(weights3), 2) if cash3 is not None else None},
        "exposure_limit_pct": exposure, "single_name_limit_pct": 25,
        "basis": "三组权重均以假设研究预算=100%（股票配置+预留现金）为分母，仅用于比较新增资金配置，不是个人总资产仓位或调仓指令。当前持仓占比仅在数量及价格全部有效时按已登记股票市值计算，不含现金。",
        "method1": "结果1来自现有个股研究工具的规则评分，未另行调用大语言模型。买入基础权重2、观察1、卖出0，加上非负技术/基本面分；归一至市场风险预算上限，单股最多25%，余量留现金。不是把买入倾向转换为仓位。",
        "method2": "在本次持仓池上使用所选策略的同一评分/入场规则；有效入场按1/名次分配风险预算，未触发不分配新增资金；单股最多25%。缺失任一持仓输入则整组权重暂缺。",
        "method3": "完整且同日的两方案逐股复核：综合权重=min(结果1, (结果1+结果2)/2)，不重新归一化放大仓位，未使用金额留作现金；分歧和失效条件仍需智能体解释。"}
