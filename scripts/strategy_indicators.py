"""Causal five-minute indicators with one preceding session of warmup."""

import numpy as np
import pandas as pd

INDICATOR_NAMES = {
    "ma_spread": "5/20 均线偏离", "ma_spread_prev": "前根均线偏离",
    "macd_hist": "MACD 柱差 / 价格", "macd_hist_prev": "前根 MACD 柱差 / 价格",
    "rsi": "RSI(14)", "rsi_prev": "前根 RSI(14)",
    "bollinger_z": "布林带标准差位置", "bollinger_z_prev": "前根布林带位置",
    "donchian_gap": "20 根通道上轨偏离", "donchian_gap_prev": "前根通道上轨偏离",
}


def _seeded_average(series, period, alpha):
    """SMA initialization, then EMA or Wilder's recursive smoothing."""
    result = pd.Series(np.nan, index=series.index, dtype=float)
    first = series.first_valid_index()
    if first is None:
        return result
    first_position = series.index.get_loc(first)
    seed_position = first_position + period - 1
    if seed_position >= len(series):
        return result
    seeded = series.iloc[seed_position:].copy()
    seeded.iloc[0] = series.iloc[first_position:seed_position + 1].mean()
    result.iloc[seed_position:] = seeded.ewm(alpha=alpha, adjust=False).mean()
    return result


def _window_indicators(window, parameters=None):
    from strategy_config import validate_parameters
    supplied = parameters or {}
    params = {key: validate_parameters(key, supplied.get(key)) for key in
              ("ma_crossover", "macd", "rsi_rebound", "bollinger_reversion", "donchian_breakout")}
    ma_params, macd = params["ma_crossover"], params["macd"]
    rsi_period = params["rsi_rebound"]["period"]
    band_period = params["bollinger_reversion"]["period"]
    close = window.close.astype(float)
    fast, slow = close.rolling(ma_params["fast_period"]).mean(), close.rolling(ma_params["slow_period"]).mean()
    ma = fast / slow - 1
    dif = _seeded_average(close, macd["fast_period"], 2 / (macd["fast_period"] + 1)) - _seeded_average(close, macd["slow_period"], 2 / (macd["slow_period"] + 1))
    hist = (dif - _seeded_average(dif, macd["signal_period"], 2 / (macd["signal_period"] + 1))) / close
    delta = close.diff()
    gain = _seeded_average(delta.clip(lower=0), rsi_period, 1 / rsi_period)
    loss = _seeded_average(-delta.clip(upper=0), rsi_period, 1 / rsi_period)
    rsi = 100 * gain / (gain + loss)
    rsi = rsi.mask((gain + loss).le(close.abs() * 1e-12), 50)
    deviation = close.rolling(band_period).std(ddof=0)
    z = ((close - close.rolling(band_period).mean()) / deviation.replace(0, np.nan)).mask(deviation.eq(0), 0)
    # Shifting high excludes the current bar and makes a breakout possible.
    channel = window.high.astype(float).shift(1).rolling(params["donchian_breakout"]["period"]).max()
    gap = close / channel - 1
    result = pd.DataFrame({"ma_spread": ma, "macd_hist": hist, "rsi": rsi,
                           "bollinger_z": z, "donchian_gap": gap})
    for name in tuple(result.columns):
        result[name + "_prev"] = result[name].shift(1)
    return result


def indicator_frame(bars, market_state=None, parameters=None):
    """Return indicators at each bar using only prices and events known then.

    Each day initializes EMA/RSI using the previous observed session (48 bars
    when prepared). This fixed window keeps live and replay initialization
    identical without requiring extra daily history from a new user.
    """
    frame = bars.sort_values("time", kind="stable").reset_index(drop=True).copy()
    result = pd.DataFrame(np.nan, index=frame.index, columns=INDICATOR_NAMES, dtype=float)
    if frame.empty:
        return result
    events = {} if market_state is None else {
        str(row.day): row for row in market_state.itertuples(index=False)}
    previous, previous_day = None, None
    for day, group in frame.groupby(frame.time.dt.strftime("%Y-%m-%d"), sort=True):
        warmup = pd.DataFrame(columns=group.columns) if previous is None else previous.copy()
        if previous_day is not None:
            for ex_day in sorted(value for value in events if previous_day < value <= day):
                event = events[ex_day]
                cash, ratio = float(event.cash_dividend), float(event.split_ratio)
                if cash or ratio != 1:
                    # These events have already happened by this session.
                    warmup[["open", "high", "low", "close"]] = (
                        warmup[["open", "high", "low", "close"]] - cash) / ratio
                    if warmup[["open", "high", "low", "close"]].le(0).any().any():
                        raise RuntimeError(f"{day} 公司行为调整后价格无效，不能生成技术指标。")
        window = group.reset_index(drop=True) if warmup.empty else pd.concat([warmup, group], ignore_index=True)
        values = _window_indicators(window, parameters).iloc[-len(group):]
        result.loc[group.index, :] = values[list(INDICATOR_NAMES)].to_numpy()
        previous, previous_day = group, day
    return result
