"""Strategy definitions shared by the panel, replay and live ranking."""

MODEL_STRATEGIES = ("xgboost", "elastic_net")
TECHNICAL_STRATEGIES = ("ma_crossover", "macd", "rsi_rebound", "bollinger_reversion", "donchian_breakout")
STRATEGIES = {
    "xgboost": {"name": "XGBoost 因子模型", "description": "仅训练 XGBoost；按预测分数排序，滚动窗口只使用已成熟标签。"},
    "elastic_net": {"name": "Elastic Net 因子模型", "description": "仅训练 Elastic Net；按预测分数排序，滚动窗口只使用已成熟标签。"},
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


def rule_scores(frame, strategy: str):
    """Return scores and entry mask using signal-time features only."""
    validate_strategy(strategy)
    if strategy == "mean_reversion":
        return -frame.vwap_gap, frame.vwap_gap.le(-0.005)
    if strategy == "momentum":
        return frame.open_momentum, frame.open_momentum.ge(0.005)
    if strategy == "breakout":
        # range_position is centered at zero: [-0.5, 0.5].
        return (frame.range_position + frame.open_momentum,
                frame.range_position.ge(0.45) & frame.range_width.ge(0.01) & frame.open_momentum.gt(0))
    if strategy == "ma_crossover":
        return frame.ma_spread, frame.ma_spread.gt(1e-10) & frame.ma_spread_prev.le(1e-10)
    if strategy == "macd":
        return frame.macd_hist, frame.macd_hist.gt(1e-10) & frame.macd_hist_prev.le(1e-10)
    if strategy == "rsi_rebound":
        return frame.rsi - frame.rsi_prev, frame.rsi_prev.lt(30) & frame.rsi.ge(30)
    if strategy == "bollinger_reversion":
        return frame.bollinger_z - frame.bollinger_z_prev, frame.bollinger_z_prev.lt(-2) & frame.bollinger_z.ge(-2) & frame.bollinger_z.le(2)
    if strategy == "donchian_breakout":
        return frame.donchian_gap, frame.donchian_gap.gt(0) & frame.donchian_gap_prev.le(0)
    raise ValueError("因子模型需要先训练，不能使用固定规则评分。")
