"""On-demand whole-market ranking, independent of the training/backtest universe."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import argparse

import pandas as pd

import factor_lab as lab
from data_diagnostics import readable_issues
from strategy_catalog import MODEL_STRATEGIES, RULE_FEATURES, STRATEGIES, TECHNICAL_STRATEGIES, rule_scores, validate_strategy

STORE = lab.STORE


def ranking_model(strategy, now):
    model = lab.selected_model(strategy)
    if not model:
        raise ValueError("模型尚未完成有效训练，请在控制面板训练后刷新策略列表。")
    try:
        selected = datetime.fromisoformat(model["selected_at"])
        if selected.tzinfo is None:
            selected = selected.replace(tzinfo=lab.CHINA)
        if not timedelta(0) <= now - selected <= timedelta(days=30):
            raise ValueError("模型超过30天或训练日期异常，请重新训练。")
        if model.get("quality") == "exploratory" and not timedelta(0) <= now.date() - pd.Timestamp(model["training"]["last_day"]).date() <= timedelta(days=7):
            raise ValueError("探索性模型训练数据超过7天，请重新训练。")
    except (KeyError, TypeError) as exc:
        raise ValueError("模型训练信息不完整，请重新训练。") from exc
    return model


def ranking_options():
    now = lab.now_cn()
    choices, unavailable = {}, {}
    for strategy, definition in STRATEGIES.items():
        if strategy not in MODEL_STRATEGIES:
            choices[strategy] = {**definition, "kind": "fixed_rule"}
            continue
        try:
            model = ranking_model(strategy, now)
            choices[strategy] = {**definition, "kind": "model", "model_id": model["model_id"],
                                 "trained_at": model["selected_at"], "quality": model.get("quality")}
        except (ValueError, OSError) as exc:
            unavailable[strategy] = str(exc)
    selected = lab.load_settings()["strategy"]
    return {"selected": selected if selected in choices else next(iter(choices)),
            "strategies": choices, "unavailable_models": unavailable,
            "scope": "full_market", "market": "a"}


def rank_market(strategy, progress=lambda **state: None):
    validate_strategy(strategy)
    started = lab.now_cn()
    model = ranking_model(strategy, started) if strategy in MODEL_STRATEGIES else None
    if not lab.is_trading_time(started):
        raise ValueError("全市场盘中排序仅在 A 股交易时段 09:35–11:30、13:00–15:00 可用。")
    import akshare as ak
    calendar = ak.tool_trade_date_hist_sina()
    key = next((key for key in ("trade_date", "日期") if key in calendar.columns), None)
    days = sorted(set(pd.to_datetime(calendar[key], errors="coerce").dt.date.dropna())) if key else []
    if started.date() not in days:
        raise ValueError("交易日历未确认今天为交易日，本次不生成盘中排序。")
    progress(stage="snapshot", message="正在获取全市场盘中快照")
    snapshot = lab.spot_frame()
    fetched = lab.now_cn()
    if fetched.date() != started.date() or not lab.is_trading_time(fetched):
        raise ValueError("行情获取跨过交易时段，请重新排序。")
    total = len(snapshot)
    frame = snapshot.copy()
    frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    eligible = frame["代码"].str.fullmatch(r"[036]\d{5}") & ~frame["名称"].astype(str).str.contains("ST|退", case=False)
    for column in ("最新价", "成交量", "成交额"):
        eligible &= pd.to_numeric(frame[column], errors="coerce").gt(0)
    frame = frame[eligible].drop_duplicates("代码").copy()
    input_count = len(frame)
    progress(stage="features", input_count=input_count, universe_count=total, processed_count=0,
             message=f"已取得 {total} 只快照，准备 {input_count} 只沪深正常交易股票的特征")
    records, failures = [], []
    asof = pd.Timestamp(fetched).tz_localize(None).floor("5min")
    if "11:30" <= fetched.strftime("%H:%M") < "13:05":
        asof = asof.replace(hour=11, minute=30)
    if strategy in TECHNICAL_STRATEGIES:
        previous = [day for day in days if day < fetched.date()]
        if not previous:
            raise ValueError("交易日历缺少前一交易日，无法预热技术指标。")
        from portfolio_research import load_signal_record
        def load(row):
            try:
                item = {"holding": {"symbol": row["代码"]}, "quote": {"昨收": row.get("昨收")}}
                record = load_signal_record(item, strategy, asof, previous[-1].isoformat())
                return {**record, "name": str(row["名称"]), "indicator_at": asof.isoformat()}, None
            except Exception as exc:
                return None, {"symbol": row["代码"], "reason": readable_issues([f"策略信号不可用：{exc}"])[0]}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(load, row) for row in frame.to_dict("records")]
            for index, future in enumerate(as_completed(futures), 1):
                record, failure = future.result()
                if record: records.append(record)
                if failure: failures.append(failure)
                if index % 20 == 0 or index == input_count:
                    progress(stage="features", processed_count=index, valid_count=len(records),
                             failed_count=len(failures), message=f"分钟线处理 {index}/{input_count}，有效 {len(records)} 只")
    else:
        from factor_models import minute_of_day
        for row in frame.to_dict("records"):
            features = lab.live_factors(pd.Series(row))
            if features:
                records.append({"symbol": row["代码"], "name": str(row["名称"]),
                                "price": lab.valid_number(row["最新价"]), "minute_of_day": minute_of_day(fetched.strftime("%H:%M")), **features})
            else:
                failures.append({"symbol": row["代码"], "reason": "快照缺少有效价量特征"})
    if not records:
        raise ValueError("全市场快照未提供可评分的有效股票，请检查行情来源后重试。")
    completed = lab.now_cn()
    if completed.date() != fetched.date():
        raise ValueError("取数跨日，本次不发布盘中排名。")
    progress(stage="scoring", processed_count=input_count, valid_count=len(records), failed_count=len(failures),
             message=f"正在对 {len(records)} 只有效股票评分")
    data = pd.DataFrame(records)
    if model:
        from factor_models import predict
        data["score"] = predict(data, model, lab.MODELS / model["model_id"])
        data["entry"] = None
        features = model["features"]
    else:
        data["score"], data["entry"] = rule_scores(data, strategy)
        features = RULE_FEATURES[strategy]
    finite_scores = data.score.map(lambda value: lab.valid_number(value) is not None)
    for symbol in data.loc[~finite_scores, "symbol"]:
        failures.append({"symbol": symbol, "reason": "模型或策略评分无效"})
    data = data[finite_scores].sort_values(["score", "symbol"], ascending=[False, True], kind="stable")
    if data.empty:
        raise ValueError("所选策略没有产生有效评分。")
    ranks = data.score.rank(method="min", ascending=False)
    rows = []
    for index, row in data.iterrows():
        rows.append({"rank": int(ranks.loc[index]), "symbol": row.symbol, "name": row["name"],
                     "price": round(float(row.price), 3), "score": round(float(row.score), 6),
                     "entry": bool(row.entry) if model is None else None,
                     "factors": {key: round(float(row[key]), 6) for key in features},
                     **({"indicator_at": row.indicator_at} if strategy in TECHNICAL_STRATEGIES else {})})
    note = "扫描全市场快照；范围为正常交易的沪深A股，剔除ST、退市标记、无成交及北交所股票。全部有效评分均保留；固定策略未触发入场的股票也参与评分，入场状态单独标注。"
    if model:
        note += "现有模型训练股票池与全市场应用范围可能不同，扩大范围未经同口径回测验证；分数不是收益概率或买入指令。" + model.get("selection_warning", "")
    if strategy not in TECHNICAL_STRATEGIES:
        note += "快照缺少逐股报价时间戳，无法保证全体报价同步，也不能与持仓的5分钟收盘评分直接对比。"
    else:
        note += "采用与持仓排序一致的已完成5分钟线、前一交易日预热及质量校验。"
    if failures:
        note += f"有 {len(failures)} 只符合范围的股票缺少有效评分，本次仅为有效子集排序。"
    payload = {"available": True, "id": fetched.strftime("%Y%m%d-%H%M%S-%f"), "scope": "full_market", "market": "a",
               "strategy_id": strategy, "algorithm": strategy, "strategy_name": STRATEGIES[strategy]["name"],
               "generated_at": fetched.isoformat(timespec="seconds"), "completed_at": completed.isoformat(timespec="seconds"),
               "signal_at": asof.isoformat() if strategy in TECHNICAL_STRATEGIES else fetched.isoformat(timespec="seconds"),
               "model_quality": model.get("quality", "validated") if model else "fixed_rule",
               "model_id": model["model_id"] if model else None,
               "universe_count": total, "input_count": input_count, "excluded_count": total-input_count,
               "valid_count": len(data), "candidate_count": len(data), "failed_count": len(failures),
               "coverage_complete": len(data) == input_count, "entry_count": int(data.entry.sum()) if model is None else None,
               "ranking": rows, "failures": failures, "backtest_comparable": False, "note": note}
    lab.write_json(STORE / "rankings" / f"{payload['id']}.json", payload)
    lab.write_json(STORE / f"full-market-{strategy}.json", payload)
    return payload


def run_job(strategy, job_id):
    path = STORE / "ranking-jobs" / f"{job_id}.json"
    state = lab.read_json(path)
    def progress(**updates):
        state.update(updates)
        state["updated_at"] = lab.now_cn().isoformat(timespec="seconds")
        lab.write_json(path, state)
    try:
        result = rank_market(strategy, progress)
        progress(status="completed", message="盘中排序已完成", result=result)
    except Exception as exc:
        message = readable_issues([f"盘中排序不可用：{exc}"])[0] if any(key in str(exc) for key in ("HTTP", "Proxy", "https://", "Connection")) else str(exc)[:240]
        progress(status="failed", message=message)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=tuple(STRATEGIES), required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if not args.job_id.isalnum() or len(args.job_id) != 32:
        parser.error("invalid job id")
    run_job(args.strategy, args.job_id)
