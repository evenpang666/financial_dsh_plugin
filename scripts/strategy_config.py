"""Shared parameter contracts and inclusive research date ranges."""

import math
from datetime import date, datetime, timedelta, timezone


def field(label, default, minimum, maximum, integer=False):
    return dict(label=label, default=default, min=minimum, max=maximum,
                type="integer" if integer else "number")


TOP = {"top_n": field("候选/持仓数量上限", 5, 1, 40, True)}
EXECUTION_PARAMETERS = {
    "mean_reversion": {"deviation": field("VWAP 下偏阈值（比例）", .005, .00001, .5)},
    "momentum": {"threshold": field("开盘涨幅阈值（比例）", .005, 0, .5)},
    "breakout": {"range_top": field("区间顶部比例", .05, .001, .5),
                 "min_range": field("最低振幅（比例）", .01, 0, 1)},
    "ma_crossover": {"fast_period": field("短均线周期", 5, 2, 39, True),
                     "slow_period": field("长均线周期", 20, 3, 40, True)},
    "macd": {"fast_period": field("快线周期", 12, 2, 39, True),
             "slow_period": field("慢线周期", 26, 3, 40, True),
             "signal_period": field("信号线周期", 9, 2, 20, True)},
    "rsi_rebound": {"period": field("RSI 周期", 14, 2, 40, True),
                    "oversold": field("超卖阈值", 30, 1, 99)},
    "bollinger_reversion": {"period": field("布林带周期", 20, 2, 40, True),
                            "std_dev": field("标准差倍数", 2, .1, 5)},
    "donchian_breakout": {"period": field("通道周期", 20, 2, 40, True)},
    "xgboost": {}, "elastic_net": {},
}
TRAINING_PARAMETERS = {
    "xgboost": {"n_estimators": field("树数量", 120, 1, 2000, True),
                "max_depth": field("树深度", 2, 1, 12, True),
                "learning_rate": field("学习率", .04, .0001, 1),
                "min_child_weight": field("最小子节点权重", 20, 0, 1000),
                "reg_lambda": field("L2 正则", 10, 0, 1000),
                "subsample": field("样本采样比例", .8, .1, 1)},
    "elastic_net": {"alpha_scale": field("目标标准差正则系数", .01, .000001, 10),
                    "l1_ratio": field("L1 正则占比", .5, 0, 1),
                    "max_iter": field("最大迭代次数", 10000, 100, 100000, True)},
}


def parameter_schema(strategy, purpose="execution"):
    if strategy not in EXECUTION_PARAMETERS:
        raise ValueError(f"未知策略：{strategy}")
    return (TRAINING_PARAMETERS.get(strategy, {}) if purpose == "training"
            else {**TOP, **EXECUTION_PARAMETERS[strategy]})


def validate_parameters(strategy, supplied=None, purpose="execution"):
    schema = parameter_schema(strategy, purpose)
    supplied = {} if supplied is None else supplied
    if not isinstance(supplied, dict) or set(supplied) - set(schema):
        raise ValueError(f"{strategy} 超参数字段无效")
    result = {}
    for key, spec in schema.items():
        raw = supplied.get(key, spec["default"])
        try:
            value = float(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{spec['label']}必须是数字") from None
        if (isinstance(raw, bool) or not math.isfinite(value)
                or not spec["min"] <= value <= spec["max"]
                or spec["type"] == "integer" and not value.is_integer()):
            raise ValueError(f"{spec['label']}需在 {spec['min']}–{spec['max']} 之间"
                             + ("且为整数" if spec["type"] == "integer" else ""))
        result[key] = int(value) if spec["type"] == "integer" else value
    if strategy in ("ma_crossover", "macd") and purpose == "execution":
        if result["fast_period"] >= result["slow_period"]:
            raise ValueError("快线/短均线周期必须小于慢线/长均线周期")
        if strategy == "macd" and result["slow_period"] + result["signal_period"] > 48:
            raise ValueError("MACD 慢线与信号周期之和不能超过 48 根预热线")
    return result


def configured_parameters(settings, strategy, purpose="execution"):
    key = "training_parameters" if purpose == "training" else "strategy_parameters"
    return validate_parameters(strategy, settings.get(key, {}).get(strategy), purpose)


def default_ranges(today=None):
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    if isinstance(today, datetime):
        today = today.date()
    end = today - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return {"training_start": (end - timedelta(days=400)).isoformat(),
            "training_end": (end - timedelta(days=120)).isoformat(),
            "backtest_start": (end - timedelta(days=119)).isoformat(),
            "backtest_end": end.isoformat()}


def training_boundary(checkpoint):
    training = checkpoint.get("training", {})
    label_end = training.get("label_end_day")
    if not label_end:
        raise ValueError("checkpoint 缺少训练标签结束日，请重新训练")
    return max(label_end, training.get("requested_end", label_end), training.get("last_day", label_end))


def validate_range(start, end, checkpoint=None, today=None):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError("日期须使用 YYYY-MM-DD 格式") from None
    if first.isoformat() != start or last.isoformat() != end or first > last:
        raise ValueError("起始日期不能晚于结束日期，格式须为 YYYY-MM-DD")
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    if last >= today:
        raise ValueError("仅能使用今天之前已完成的历史数据")
    if checkpoint:
        cutoff = training_boundary(checkpoint)
        if first <= date.fromisoformat(cutoff):
            raise ValueError(f"回测区间不能与训练集或训练标签重叠；起始日期必须晚于 {cutoff}")
    return start, end


def research_range(settings, purpose, checkpoint=None):
    defaults = default_ranges()
    start, end = settings.get(f"{purpose}_start"), settings.get(f"{purpose}_end")
    if not start:
        start = defaults[f"{purpose}_start"]
        if checkpoint:
            cutoff = training_boundary(checkpoint)
            if cutoff:
                start = max(start, (date.fromisoformat(cutoff) + timedelta(days=1)).isoformat())
    return validate_range(start, end or defaults[f"{purpose}_end"], checkpoint)


def ensure_range_coverage(manifest, start, end):
    first = manifest.get("requested_start", manifest.get("first_day"))
    last = manifest.get("requested_end", manifest.get("last_day"))
    if not first or not last or start < first or end > last:
        raise RuntimeError(f"本地验收数据未覆盖 {start} 至 {end}，请先准备完整区间的历史数据。")
