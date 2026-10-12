"""Strategy definitions shared by the panel, replay and live ranking."""

MODEL_STRATEGIES = ("xgboost", "elastic_net")
TECHNICAL_STRATEGIES = ("ma_crossover", "macd", "rsi_rebound", "bollinger_reversion", "donchian_breakout")
STRATEGIES = {
    "xgboost": {"name": "XGBoost 因子模型", "description": "训练保存独立 checkpoint；回测和部署使用所选版本的冻结权重。"},
    "elastic_net": {"name": "Elastic Net 因子模型", "description": "训练保存独立 checkpoint；回测和部署使用所选版本的冻结权重。"},
    "mean_reversion": {"name": "VWAP 均值回归", "description": "价格低于截至信号时点的日内 VWAP 至少 0.5% 时做多，偏离越大排名越高。"},
    "momentum": {"name": "盘中动量", "description": "相对当日开盘涨幅至少 0.5% 时做多，涨幅越大排名越高。"},
    "breakout": {"name": "日内区间突破", "description": "价格处于截至信号时点日内区间顶部 5%，日内振幅至少 1% 且开盘以来上涨时做多。"},
    "ma_crossover": {"name": "均线金叉", "description": "5 分钟收盘价 SMA(5) 在信号 K 线上穿 SMA(20) 时做多，按均线正偏离排名。"},
    "macd": {"name": "MACD 金叉", "description": "5 分钟 MACD(12,26,9) 的 DIF 在信号 K 线上穿 DEA 时做多，按归一化柱差排名。"},
    "rsi_rebound": {"name": "RSI 超卖反弹", "description": "5 分钟 Wilder RSI(14) 在信号 K 线从 30 以下回升至 30 或以上时做多，按回升幅度排名。"},
    "bollinger_reversion": {"name": "布林带回归", "description": "5 分钟收盘价从布林带(20,2)下轨外回到带内时做多，按回归幅度排名。"},
    "donchian_breakout": {"name": "唐奇安通道突破", "description": "5 分钟收盘价在信号 K 线上穿此前 20 根 K 线最高价时做多；通道排除信号 K 线。"},
}
FIXED_STRATEGIES = tuple(strategy for strategy in STRATEGIES if strategy not in MODEL_STRATEGIES)
RULE_FEATURES = {
    "mean_reversion": ("vwap_gap",), "momentum": ("open_momentum",),
    "breakout": ("range_position", "range_width", "open_momentum"),
    "ma_crossover": ("ma_spread", "ma_spread_prev"),
    "macd": ("macd_hist", "macd_hist_prev"),
    "rsi_rebound": ("rsi", "rsi_prev"),
    "bollinger_reversion": ("bollinger_z", "bollinger_z_prev"),
    "donchian_breakout": ("donchian_gap", "donchian_gap_prev"),
}


def validate_strategy(strategy: str) -> str:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy}")
    return strategy


def strategy_description(strategy, parameters=None):
    """Describe actual rules so custom parameters never retain default wording."""
    from strategy_config import validate_parameters
    p = validate_parameters(strategy, parameters)
    descriptions = {
        "mean_reversion": lambda: f"价格低于信号时点日内 VWAP 至少 {p['deviation']:.2%} 时做多。",
        "momentum": lambda: f"相对当日开盘涨幅至少 {p['threshold']:.2%} 时做多。",
        "breakout": lambda: f"价格处于日内区间顶部 {p['range_top']:.2%}，振幅至少 {p['min_range']:.2%} 且开盘以来上涨时做多。",
        "ma_crossover": lambda: f"5 分钟 SMA({p['fast_period']}) 在信号 K 线上穿 SMA({p['slow_period']}) 时做多。",
        "macd": lambda: f"5 分钟 MACD({p['fast_period']},{p['slow_period']},{p['signal_period']}) 的 DIF 在信号 K 线上穿 DEA 时做多。",
        "rsi_rebound": lambda: f"5 分钟 Wilder RSI({p['period']}) 从 {p['oversold']:g} 以下回升至此阈值时做多。",
        "bollinger_reversion": lambda: f"5 分钟收盘价从布林带({p['period']},{p['std_dev']:g})下轨外回到带内时做多。",
        "donchian_breakout": lambda: f"5 分钟收盘价上穿此前 {p['period']} 根 K 线最高价时做多，通道排除信号 K 线。",
    }
    return (descriptions[strategy]() if strategy in descriptions else STRATEGIES[strategy]['description']) + f" 候选上限 {p['top_n']} 只。"


def rule_scores(frame, strategy: str, parameters=None):
    """Return scores and entry mask using signal-time features only."""
    validate_strategy(strategy)
    from strategy_config import validate_parameters
    params = validate_parameters(strategy, parameters)
    if strategy == "mean_reversion":
        return -frame.vwap_gap, frame.vwap_gap.le(-params["deviation"])
    if strategy == "momentum":
        return frame.open_momentum, frame.open_momentum.ge(params["threshold"])
    if strategy == "breakout":
        # range_position is centered at zero: [-0.5, 0.5].
        return (frame.range_position + frame.open_momentum,
                frame.range_position.ge(0.5 - params["range_top"]) & frame.range_width.ge(params["min_range"]) & frame.open_momentum.gt(0))
    if strategy == "ma_crossover":
        return frame.ma_spread, frame.ma_spread.gt(1e-10) & frame.ma_spread_prev.le(1e-10)
    if strategy == "macd":
        return frame.macd_hist, frame.macd_hist.gt(1e-10) & frame.macd_hist_prev.le(1e-10)
    if strategy == "rsi_rebound":
        return frame.rsi - frame.rsi_prev, frame.rsi_prev.lt(params["oversold"]) & frame.rsi.ge(params["oversold"])
    if strategy == "bollinger_reversion":
        return frame.bollinger_z - frame.bollinger_z_prev, frame.bollinger_z_prev.lt(-params["std_dev"]) & frame.bollinger_z.ge(-params["std_dev"]) & frame.bollinger_z.le(params["std_dev"])
    if strategy == "donchian_breakout":
        return frame.donchian_gap, frame.donchian_gap.gt(0) & frame.donchian_gap_prev.le(0)
    raise ValueError("因子模型需要先训练，不能使用固定规则评分。")
