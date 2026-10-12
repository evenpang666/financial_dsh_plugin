"""Chronological, trade-account backtest with a daily historical universe."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from factor_models import EXECUTION_BAR, FEATURES, NEXT_BAR, fit_estimators, minute_of_day, rank_ic, sample_frame, score_estimator, select_algorithm
from strategy_catalog import MODEL_STRATEGIES, RULE_FEATURES, STRATEGIES, rule_scores, validate_strategy

DEFAULT_SLOT = "14:00"
TRAIN_DAYS = 100
VALIDATION_DAYS = 20
STEP_DAYS = 20
MIN_MATURED_DAYS = 100
TOP_N = 5
INITIAL_CASH = 1_000_000.0
COMMISSION = 0.0003
MIN_COMMISSION = 5.0
SELL_TAX = 0.0005
TRANSFER_FEE = 0.00001
DIVIDEND_TAX_RESERVE = 0.20
ENGINE_VERSION = 4
SLIPPAGE = 0.0005
MAX_BAR_PARTICIPATION = 0.10
ORDER_PRICE_BUFFER = 0.02
SCHEMA_VERSION = 2


def _prices(bars_by_symbol: dict[str, pd.DataFrame], signal_time: str, execution_time: str) -> dict:
    market = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        frame = bars.sort_values("time").copy()
        frame["day"] = frame.time.dt.strftime("%Y-%m-%d")
        frame["clock"] = frame.time.dt.strftime("%H:%M")
        for day, group in frame.groupby("day"):
            signal = group[group.clock.eq(signal_time)]
            execution = group[group.clock.eq(execution_time)]
            bar = execution.iloc[0] if not execution.empty else None
            signal_bar = signal.iloc[0] if not signal.empty else None
            vwap = None
            if bar is not None and bar.get("volume", 0) > 0 and bar.amount > 0 and bar.close > 0:
                candidates = (float(bar.amount / bar.volume), float(bar.amount / (bar.volume * 100)))
                inferred = min(candidates, key=lambda value: abs(value / bar.close - 1))
                if 0.8 <= inferred / bar.close <= 1.2:
                    vwap = inferred
            market[day][symbol] = {
                "signal_close": float(signal_bar.close) if signal_bar is not None and signal_bar.close > 0 else None,
                "signal_amount": float(signal_bar.amount) if signal_bar is not None and signal_bar.amount > 0 else 0.0,
                "vwap": vwap,
                "amount": float(bar.amount) if bar is not None and bar.amount > 0 else 0.0,
                "volume_shares": float(bar.amount / vwap) if vwap else 0.0,
                "close": float(group.iloc[-1].close),
            }
    return market


@dataclass(frozen=True)
class ExecutionConfig:
    slippage: float = SLIPPAGE
    commission: float = COMMISSION
    participation: float = MAX_BAR_PARTICIPATION

    def __post_init__(self):
        if (not all(math.isfinite(value) for value in (self.slippage, self.commission, self.participation))
                or not 0 <= self.slippage < 1 or self.commission < 0 or not 0 < self.participation <= 1):
            raise ValueError("无效的滑点、佣金或成交参与率。")


def _cost(notional: float, side: str, day: str = "", commission: float = COMMISSION) -> float:
    if day and day < "2015-08-01":
        raise RuntimeError("2015-08-01 前的历史费率未建模，拒绝使用现行费率回测。")
    transfer = TRANSFER_FEE if not day or day >= "2022-04-29" else 0.00002
    stamp = SELL_TAX if not day or day >= "2023-08-28" else 0.001
    return max(MIN_COMMISSION, notional * commission) + notional * transfer + (notional * stamp if side == "sell" else 0.0)


def _sell_shares(holding: int, capacity: int, symbol: str) -> int:
    minimum = 200 if symbol.startswith(("688", "689")) else 100
    if capacity >= holding:
        return holding  # One order may dispose of the entire odd-lot balance.
    shares = capacity if minimum == 200 else capacity // 100 * 100
    return shares if shares >= minimum else 0


def validate_context(universe_history: pd.DataFrame, market_state: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    universe_fields = {"day", "symbol", "known_at"}
    state_fields = {"day", "symbol", "limit_up", "limit_down", "has_price_limit",
                    "split_ratio", "cash_dividend", "is_st", "is_suspended"}
    if universe_history is None or not universe_fields.issubset(universe_history.columns):
        raise RuntimeError("缺少逐日历史股票池 day,symbol,known_at；拒绝事后股票池回测。")
    if market_state is None or not state_fields.issubset(market_state.columns):
        raise RuntimeError("缺少逐日交易状态、涨跌停价及公司行为数据；拒绝不完整回测。")
    universe = universe_history.copy()
    state = market_state.copy()
    for frame in (universe, state):
        frame["day"] = frame.day.astype(str)
        frame["symbol"] = frame.symbol.astype(str).str.zfill(6)
        if frame.duplicated(["day", "symbol"]).any():
            raise RuntimeError("历史股票池或交易状态存在重复的交易日与股票代码。")
    def known_local(value):
        try:
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                return pd.NaT
            return stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None else stamp.tz_convert("Asia/Shanghai")
        except (TypeError, ValueError):
            return pd.NaT
    known = universe.known_at.map(known_local)
    starts = pd.to_datetime(universe.day, errors="coerce").dt.tz_localize("Asia/Shanghai")
    if known.isna().any() or starts.isna().any() or (known >= starts).any():
        raise RuntimeError("历史股票池必须在交易日前已知；请提供前一交易日生成的股票池。")
    for field in ("limit_up", "limit_down", "split_ratio", "cash_dividend"):
        state[field] = pd.to_numeric(state[field], errors="coerce")
    for field in ("is_st", "is_suspended", "has_price_limit"):
        values = state[field].astype(str).str.lower()
        if not values.isin({"true", "false", "1", "0", "yes", "no"}).all():
            raise RuntimeError(f"逐日交易状态 {field} 必须明确为真或假。")
        state[field] = values.isin({"true", "1", "yes"})
    if "dividend_pay_date" not in state:
        state["dividend_pay_date"] = ""
    state["dividend_pay_date"] = state.dividend_pay_date.fillna("").astype(str)
    if "stock_credit_date" not in state:
        state["stock_credit_date"] = ""
    state["stock_credit_date"] = state.stock_credit_date.fillna("").astype(str)
    limited = state[state.has_price_limit]
    if (not np.isfinite(state[["split_ratio", "cash_dividend"]].to_numpy()).all()
            or not np.isfinite(limited[["limit_up", "limit_down"]].to_numpy()).all()
            or (limited.limit_up <= limited.limit_down).any() or (limited.limit_down <= 0).any()
            or (state.split_ratio <= 0).any() or (state.cash_dividend < 0).any()):
        raise RuntimeError("逐日交易状态含缺失或无效的涨跌停价、拆并股或分红数据。")
    dividend = state[state.cash_dividend.gt(0)]
    pay_dates = pd.to_datetime(dividend.dividend_pay_date, errors="coerce")
    if (pay_dates.isna().any() or
            (pay_dates.dt.strftime("%Y-%m-%d").to_numpy() < dividend.day.to_numpy()).any()):
        raise RuntimeError("现金分红缺少有效到账日；拒绝将应收红利永久当作不可用现金。")
    bonus = state[state.split_ratio.gt(1)]
    credit_dates = pd.to_datetime(bonus.stock_credit_date, errors="coerce")
    if (credit_dates.isna().any() or
            (credit_dates.dt.strftime("%Y-%m-%d").to_numpy() < bonus.day.to_numpy()).any()):
        raise RuntimeError("送转股缺少有效上市交易日；请重新准备历史交易状态，不能在除权日提前卖出红股。")
    return universe, state


def signal_candidates(samples: list[dict], universe_history: pd.DataFrame,
                      slot: str | None, market_state: pd.DataFrame | None, strategy: str | None = None) -> pd.DataFrame:
    eligible = universe_history[["day", "symbol"]].copy()
    if market_state is not None:
        excluded = market_state[market_state.is_st | market_state.is_suspended][["day", "symbol"]]
        eligible = eligible.merge(excluded.assign(excluded=True), on=["day", "symbol"], how="left")
        eligible = eligible[eligible.excluded.isna()][["day", "symbol"]]
    candidates = pd.DataFrame(samples).merge(eligible, on=["day", "symbol"], how="inner")
    if slot is not None:
        candidates = candidates[candidates.slot.eq(slot)]
    fields = list(RULE_FEATURES[strategy]) if strategy in RULE_FEATURES else list(FEATURES[:-1])
    if not set(fields).issubset(candidates):
        raise RuntimeError(f"历史样本缺少 {strategy or '因子模型'} 所需的信号时点指标：{sorted(set(fields) - set(candidates))}。")
    candidates = candidates.dropna(subset=fields).copy()
    candidates["minute_of_day"] = candidates.slot.map(minute_of_day)
    return candidates


def rule_strategy_signals(samples, universe_history, slot, market_state, strategy):
    candidates = signal_candidates(samples, universe_history, slot, market_state, strategy)
    days = sorted(candidates.day.unique())
    if len(days) < TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS:
        raise RuntimeError(f"策略对比至少需要 {TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS} 个完整交易日。")
    signals = {}
    for day in days[TRAIN_DAYS + VALIDATION_DAYS:]:
        rows = candidates[candidates.day.eq(day)].copy()
        rows["score"], entries = rule_scores(rows, strategy)
        rows = rows[entries].sort_values(["score", "symbol"], ascending=[False, True])
        signals[day] = rows.symbol.tolist()
    return signals, [], {"rank_ic": None, "positive_share": None, "groups": 0}


def walk_forward_signals(samples: list[dict], universe_history: pd.DataFrame,
                         slot: str = DEFAULT_SLOT,
                         market_state: pd.DataFrame | None = None,
                         algorithm: str = "xgboost") -> tuple[dict, list[dict], dict]:
    if slot not in EXECUTION_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    if universe_history is None:
        raise RuntimeError("缺少逐日历史股票池；拒绝事后股票池回测。")
    candidates = signal_candidates(samples, universe_history, None, market_state)
    labeled = sample_frame(candidates.to_dict("records"))
    candidates = candidates[candidates.slot.eq(slot)].copy()
    days = sorted(candidates.day.unique())
    if len(days) < TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS:
        raise RuntimeError(f"回测至少需要 {TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS} 个完整交易日。")
    signals, folds, all_scores = {}, [], []
    for start in range(TRAIN_DAYS + VALIDATION_DAYS, len(days), STEP_DAYS):
        history_days = days[start - TRAIN_DAYS - VALIDATION_DAYS:start]
        test_days = days[start:start + STEP_DAYS]
        matured = labeled[labeled.day.isin(history_days) & labeled.exit_day.lt(test_days[0])]
        if matured.day.nunique() < MIN_MATURED_DAYS:
            raise RuntimeError("滚动窗口可成熟的训练/验证标签不足；请检查停牌和分钟线缺失。")
        chosen, metrics, _, train, validation, holdout = select_algorithm(matured, algorithm)
        final_models = fit_estimators(matured, algorithm)
        fold_scores = []
        for day in test_days:
            rows = candidates[candidates.day.eq(day)].copy()
            if rows.empty:
                signals[day] = []
                continue
            rows["score"] = score_estimator(final_models, chosen, rows)
            rows = rows.sort_values("score", ascending=False)
            signals[day] = rows.symbol.tolist()
            valid = rows.dropna(subset=["target"])
            if len(valid) >= 12 and valid.target.nunique() >= 5:
                fold_scores.append(valid[["day", "slot", "target", "score"]])
        if fold_scores:
            all_scores.extend(fold_scores)
        folds.append({"train_first": str(train.day.min()), "train_last": str(train.day.max()),
                      "validation_first": str(validation.day.min()), "validation_last": str(validation.day.max()),
                      "holdout_first": str(holdout.day.min()), "holdout_last": str(holdout.day.max()),
                      "test_first": test_days[0], "test_last": test_days[-1],
                      "train_latest_exit_day": str(train.exit_day.max()),
                      "validation_latest_exit_day": str(validation.exit_day.max()),
                      "refit_latest_exit_day": str(matured.exit_day.max()),
                      "algorithm": chosen, "validation": metrics,
                      "train_rows": len(train), "validation_rows": len(validation)})
    oos_ic = (rank_ic(pd.concat(all_scores, ignore_index=True),
                      pd.concat(all_scores, ignore_index=True).score.to_numpy())
              if all_scores else {"rank_ic": 0.0, "positive_share": 0.0, "groups": 0})
    return signals, folds, oos_ic


def simulate(signals: dict, bars_by_symbol: dict[str, pd.DataFrame],
             signal_time: str = DEFAULT_SLOT, execution_time: str | None = None,
             market_state: pd.DataFrame | None = None, *,
             config: ExecutionConfig | None = None, market: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    execution_time = execution_time or EXECUTION_BAR[signal_time]
    config = config or ExecutionConfig()
    market = market if market is not None else _prices(bars_by_symbol, signal_time, execution_time)
    slippage, participation, commission = config.slippage, config.participation, config.commission
    state_by_day = ({} if market_state is None else
                    {(row.day, row.symbol): row for row in market_state.itertuples(index=False)})
    cash, receivables, pending_dividends, positions, marks = INITIAL_CASH, 0.0, defaultdict(float), {}, {}
    trades, equity = [], []
    blocked, stale, partial = 0, 0, 0
    dividend_tax_reserved = 0.0
    for day in sorted(signals):
        today = market.get(day, {})
        wanted = set(signals[day][:TOP_N])
        for symbol, holding in positions.items():
            state = state_by_day.get((day, symbol))
            if market_state is not None and state is None:
                raise RuntimeError(f"{day} {symbol} 缺少公司行为或交易状态。")
            if state is not None:
                # Dividend entitlement is part of NAV but cannot fund an
                # order before the actual payment date. Reserve short-holding
                # dividend tax conservatively instead of counting gross cash.
                dividend = holding["shares"] * float(state.cash_dividend)
                if dividend:
                    pay_day = str(getattr(state, "dividend_pay_date", ""))
                    if not pay_day:
                        raise RuntimeError(f"{day} {symbol} 缺少分红到账日。")
                    tax_reserve = dividend * DIVIDEND_TAX_RESERVE
                    dividend_tax_reserved += tax_reserve
                    receivables += dividend - tax_reserve
                    pending_dividends[pay_day] += dividend - tax_reserve
                holding["credits"] = [(due, quantity) for due, quantity in holding.get("credits", []) if due > day]
                previous_shares = holding["shares"]
                holding["shares"] = int(round(previous_shares * float(state.split_ratio)))
                if float(state.split_ratio) > 1:
                    due = str(getattr(state, "stock_credit_date", ""))
                    if not due or due < day:
                        raise RuntimeError(f"{day} {symbol} 送转股缺少有效上市交易日。")
                    if due > day:
                        holding["credits"].append((due, holding["shares"] - previous_shares))
                elif float(state.split_ratio) < 1:
                    holding["credits"] = [(due, int(round(quantity * float(state.split_ratio))))
                                          for due, quantity in holding["credits"]]
                if symbol in marks:
                    marks[symbol] = max(0.0, (marks[symbol] - float(state.cash_dividend)) / float(state.split_ratio))
        pre_sale_cash = cash
        # Freeze sizing before ANY execution-window fill changes holdings.
        equity_before_buy = pre_sale_cash + receivables + sum(
            item["shares"] * (today.get(symbol, {}).get("signal_close") or marks.get(symbol, 0.0))
            for symbol, item in positions.items())
        buy_orders = {}
        reserved_cash = pre_sale_cash
        for symbol in signals[day][:TOP_N]:
            if symbol in positions:
                continue
            bar = today.get(symbol, {})
            signal_price = bar.get("signal_close")
            state = state_by_day.get((day, symbol))
            if market_state is not None and state is None:
                raise RuntimeError(f"{day} {symbol} 缺少公司行为或交易状态。")
            if (not signal_price or bar.get("signal_amount", 0) <= 0
                    or (state is not None and (getattr(state, "is_suspended", False) or getattr(state, "is_st", False)))):
                blocked += 1
                continue
            order_limit = signal_price * (1 + ORDER_PRICE_BUFFER)
            known_capacity = math.floor(bar["signal_amount"] * participation / order_limit / 100) * 100
            budget = min(max(0.0, reserved_cash - MIN_COMMISSION), equity_before_buy / TOP_N)
            quantity = min(known_capacity, math.floor(budget / (order_limit * 100)) * 100)
            minimum = 200 if symbol.startswith(("688", "689")) else 100
            while quantity >= minimum and quantity * order_limit + _cost(quantity * order_limit, "buy", day, commission) > reserved_cash:
                quantity -= 100
            if quantity < minimum:
                blocked += 1
                continue
            buy_orders[symbol] = (quantity, order_limit, minimum)
            # Reserve the WHOLE planned order; an unknown rejection/partial
            # fill cannot free funds for another order in the same window.
            reserved_cash -= quantity * order_limit + _cost(quantity * order_limit, "buy", day, commission)
        for symbol in list(positions):
            holding = positions[symbol]
            if symbol in wanted or holding["buy_day"] >= day:
                continue
            bar = today.get(symbol, {})
            raw = bar.get("vwap")
            state = state_by_day.get((day, symbol))
            if (not raw or bar.get("amount", 0) <= 0 or not bar.get("signal_close")
                    or (state is not None and getattr(state, "is_suspended", False))
                    or (state is not None and state.has_price_limit
                        and raw * (1 - slippage) <= float(state.limit_down))):
                blocked += 1
                continue
            price = raw * (1 - slippage)
            capacity = math.floor(bar["volume_shares"] * participation)
            available_shares = holding["shares"] - sum(quantity for _, quantity in holding.get("credits", []))
            shares = _sell_shares(available_shares, capacity, symbol)
            if shares <= 0:
                blocked += 1
                continue
            if shares < available_shares:
                partial += 1
            notional = shares * price
            fee = _cost(notional, "sell", day, commission)
            cash += notional - fee
            if cash < -0.01:
                raise RuntimeError("卖出后现金不足以支付交易费用，拒绝隐式融资。")
            requested_shares = available_shares
            holding["shares"] -= shares
            trades.append({"day": day, "symbol": symbol, "side": "sell", "shares": shares,
                           "order_shares": requested_shares,
                           "fill_participation": shares / bar["volume_shares"],
                           "price": round(price, 4), "notional": round(notional, 2), "cost": round(fee, 2)})
            if holding["shares"] == 0:
                del positions[symbol]
        for symbol, (order_shares, order_limit, minimum) in buy_orders.items():
            bar = today.get(symbol, {})
            raw = bar.get("vwap")
            state = state_by_day.get((day, symbol))
            if (not raw or bar.get("amount", 0) <= 0
                    or (state is not None and state.has_price_limit
                        and raw * (1 + slippage) >= float(state.limit_up))):
                blocked += 1
                continue
            price = raw * (1 + slippage)
            if price > order_limit:
                blocked += 1
                continue
            capacity = math.floor(bar["volume_shares"] * participation / 100) * 100
            shares = min(capacity, order_shares)
            if shares < minimum:
                blocked += 1
                continue
            if shares < order_shares:
                partial += 1
            notional = shares * price
            fee = _cost(notional, "buy", day, commission)
            cash -= notional + fee
            if cash < -0.01:
                raise RuntimeError("订单现金预留不足，拒绝隐式融资。")
            positions[symbol] = {"shares": shares, "buy_day": day}
            trades.append({"day": day, "symbol": symbol, "side": "buy", "shares": shares,
                           "order_shares": order_shares,
                           "fill_participation": shares / bar["volume_shares"],
                           "price": round(price, 4), "notional": round(notional, 2), "cost": round(fee, 2)})
        for symbol, bar in today.items():
            if bar["close"] > 0:
                marks[symbol] = bar["close"]
        # Cash dividends are credited after this day's order window; they can
        # fund new orders only from the next trading session.
        for pay_day in [value for value in pending_dividends if value <= day]:
            amount = pending_dividends.pop(pay_day)
            receivables -= amount
            cash += amount
        missing_marks = [symbol for symbol in positions if symbol not in today]
        stale += len(missing_marks)
        nav = cash + receivables + sum(item["shares"] * marks.get(symbol, 0.0) for symbol, item in positions.items())
        equity.append({"day": day, "cash": round(cash, 2), "dividend_receivable": round(receivables, 2),
                       "equity": round(nav, 2),
                       "positions": len(positions), "stale_marks": len(missing_marks),
                       "gross_exposure": round((nav - cash - receivables) / nav, 6) if nav > 0 else 0})
    equity_frame = pd.DataFrame(equity)
    trade_frame = pd.DataFrame(trades, columns=["day", "symbol", "side", "shares", "order_shares",
                                             "fill_participation", "price", "notional", "cost"])
    if equity_frame.empty:
        raise RuntimeError("没有可模拟的评估交易日。")
    nav = equity_frame.equity.to_numpy()
    daily = np.diff(np.r_[INITIAL_CASH, nav]) / np.r_[INITIAL_CASH, nav[:-1]]
    peak = np.maximum.accumulate(np.r_[INITIAL_CASH, nav])[1:]
    stats = {"initial_cash": INITIAL_CASH, "final_equity": round(float(nav[-1]), 2),
             "total_return": round(float(nav[-1] / INITIAL_CASH - 1), 4),
             "annualized_return": round(float((nav[-1] / INITIAL_CASH) ** (252 / len(nav)) - 1), 4),
             "max_drawdown": round(float(np.min(nav / peak - 1)), 4),
             "sharpe": round(float(np.sqrt(252) * np.mean(daily) / np.std(daily, ddof=1)), 3)
             if len(daily) > 1 and np.std(daily, ddof=1) > 0 else None,
             "trades": len(trades), "costs": round(sum(item["cost"] for item in trades), 2),
             "slippage_costs": round(sum(item["notional"] * slippage / (1 + slippage if item["side"] == "buy" else 1 - slippage)
                                         for item in trades), 2),
             "dividend_tax_reserved": round(dividend_tax_reserved, 2),
             "max_gross_exposure": float(equity_frame.gross_exposure.max()),
             "max_fill_participation": float(trade_frame.fill_participation.max()) if trades else 0,
             "blocked_orders": blocked, "partial_fills": partial, "stale_position_days": stale}
    return equity_frame, trade_frame, stats


def run(samples: list[dict], bars_by_symbol: dict[str, pd.DataFrame], destination: Path,
        slot: str = DEFAULT_SLOT, universe_history: pd.DataFrame | None = None,
        market_state: pd.DataFrame | None = None,
        source_manifest: dict | None = None, strategy: str = "xgboost",
        market: dict | None = None) -> dict:
    validate_strategy(strategy)
    if slot not in EXECUTION_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    universe_history, market_state = validate_context(universe_history, market_state)
    execution_time = EXECUTION_BAR[slot]
    sample_frame_all = pd.DataFrame(samples)
    if sample_frame_all.empty:
        raise RuntimeError("没有可用的历史因子样本。")
    required_features = list(RULE_FEATURES[strategy]) if strategy in RULE_FEATURES else list(FEATURES[:-1])
    sample_fields = {"day", "slot", "symbol", *required_features}
    if not sample_fields.issubset(sample_frame_all):
        raise RuntimeError("历史样本缺少信号时点特征。")
    if sample_frame_all.duplicated(["day", "slot", "symbol"]).any():
        raise RuntimeError("历史样本存在重复股票、日期与信号时点。")
    required = universe_history[["day", "symbol"]].drop_duplicates()
    missing_state = required.merge(market_state[["day", "symbol"]], on=["day", "symbol"], how="left", indicator=True)
    if missing_state._merge.eq("left_only").any():
        raise RuntimeError("历史股票池中有股票缺少逐日交易状态；拒绝生成绩效。")
    eligible_state = required.merge(market_state, on=["day", "symbol"], how="left")
    tradable = eligible_state[~eligible_state.is_suspended & ~eligible_state.is_st]
    valid_samples = sample_frame_all[sample_frame_all.slot.eq(slot)].copy()
    finite_features = np.isfinite(valid_samples[required_features].to_numpy(dtype=float)).all(axis=1)
    sample_keys = set(zip(valid_samples.loc[finite_features, "day"], valid_samples.loc[finite_features, "symbol"]))
    first_input_day = min(universe_history.day)
    absent_features = [(row.day, row.symbol) for row in tradable.itertuples(index=False)
                       if row.day != first_input_day and (row.day, row.symbol) not in sample_keys]
    if absent_features:
        raise RuntimeError(f"非停牌候选股缺少 {len(absent_features)} 条信号特征，不能删除缺样本股票后回测，例如 {absent_features[:3]}。")
    if tradable.groupby("day").symbol.nunique().lt(12).any():
        raise RuntimeError("历史股票池有交易日的可交易股票少于12只。")
    observed = {(str(row.time)[:10], symbol, str(row.time)[11:16])
                for symbol, bars in bars_by_symbol.items() for row in bars.itertuples(index=False)}
    if any((row.day, row.symbol, clock) not in observed
           for row in tradable.itertuples(index=False)
           for clock in (slot, execution_time, "15:00")):
        raise RuntimeError("非停牌股票缺少信号、执行或收盘K线；拒绝生成绩效。")
    # Held stocks may leave tomorrow's candidate list; status and marks must
    # remain available until the end of the study period.
    state_keys = set(zip(market_state.day, market_state.symbol))
    study_days = sorted(universe_history.day.unique())
    first_seen = universe_history.groupby("symbol").day.min().to_dict()
    if any((day, symbol) not in state_keys for symbol, first in first_seen.items()
           for day in study_days if day >= first):
        raise RuntimeError("历史持仓后续日期缺少逐日交易状态。")
    if strategy in MODEL_STRATEGIES:
        signals, folds, oos_ic = walk_forward_signals(samples, universe_history, slot, market_state, strategy)
    else:
        signals, folds, oos_ic = rule_strategy_signals(samples, universe_history, slot, market_state, strategy)
    market = _prices(bars_by_symbol, slot, execution_time) if market is None else market
    equity, trades, stats = simulate(signals, bars_by_symbol, slot, execution_time, market_state, market=market)
    stress_tests = []
    for name, config in (
        ("佣金率与滑点加倍", ExecutionConfig(slippage=SLIPPAGE * 2, commission=COMMISSION * 2)),
        ("成交参与率降至 2%", ExecutionConfig(participation=0.02)),
    ):
        _, _, scenario_stats = simulate(signals, bars_by_symbol, slot, execution_time, market_state,
                                        market=market, config=config)
        stress_tests.append({"scenario": name, "slippage": config.slippage, "commission": config.commission,
                             "participation": config.participation, "stats": scenario_stats})
    destination.mkdir(parents=True, exist_ok=True)
    equity.to_csv(destination / "equity.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(destination / "trades.csv", index=False, encoding="utf-8-sig")
    import json
    (destination / "folds.json").write_text(json.dumps(folds, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"kind": "point_in_time_backtest", "schema_version": SCHEMA_VERSION,
               "engine_version": ENGINE_VERSION,
               "strategy_id": strategy, "strategy_name": STRATEGIES[strategy]["name"],
               "strategy_description": STRATEGIES[strategy]["description"],
               "data_provenance": {"source_scope": source_manifest.get("source_scope"),
                                   "manifest_created_at": source_manifest.get("created_at"),
                                   "limit_source": source_manifest.get("limit_source")}
                                  if source_manifest else None,
               "model_scope": ("walk_forward_policy_not_current_published_weights" if strategy in MODEL_STRATEGIES
                               else "fixed_signal_time_rule"),
               "algorithm_policy": (f"固定使用 {strategy}，滚动窗口按时间划分评估并以成熟标签重拟合"
                                    if strategy in MODEL_STRATEGIES else STRATEGIES[strategy]["description"]),
               "evaluation_warmup_days": TRAIN_DAYS + VALIDATION_DAYS,
               "holding_policy": "每日重审，仍在前五名则保留持仓；离开前五名或无信号时提交卖单，停牌或流动性不足时延后退出",
               "slot": slot, "execution": f"{NEXT_BAR[slot]}–{execution_time} VWAP",
               "execution_bar_end": execution_time,
               "first_day": min(signals), "last_day": max(signals), "days": len(signals),
               "folds": len(folds), "top_n": TOP_N, "stats": stats, "oos_rank_ic": oos_ic,
               "stress_tests": stress_tests,
               "audit": {"future_information": "全部买单在成交前冻结数量与现金预留；拟合仅使用测试期之前已成熟标签",
                         "survivorship": "逐日历史名单包含退市股；资料、状态或非停牌信号特征缺失时拒绝生成绩效",
                         "overfitting": "固定模型和参数、按日期拆分和滚动评估；不证明跨市场状态稳定",
                         "selection_bias": "探索性结果；多策略、时点和参数比较未做多重检验校正，择优后需独立新数据验证",
                         "liquidity": "信号期与执行期双重容量约束、部分成交；无法从五分钟线恢复盘口和订单队列"},
               "statistical_validation": {"multiple_testing_adjusted": False, "serial_correlation_adjusted_sharpe": False,
                                          "independent_post_selection_holdout": False, "risk_free_rate": 0},
               "assumptions": {"commission": COMMISSION, "minimum_commission": MIN_COMMISSION,
                               "sell_tax": SELL_TAX, "slippage_each_side": SLIPPAGE,
                               "stamp_tax_schedule": {"before_2023-08-28": 0.001, "from_2023-08-28": SELL_TAX},
                               "transfer_fee_each_side": TRANSFER_FEE,
                               "transfer_fee_schedule": {"2015-08-01_to_2022-04-28": 0.00002, "from_2022-04-29": TRANSFER_FEE},
                               "dividend_tax_reserve": DIVIDEND_TAX_RESERVE,
                               "commission_includes_exchange_fees": True,
                               "max_5m_amount_participation": MAX_BAR_PARTICIPATION,
                               "order_price_buffer": ORDER_PRICE_BUFFER,
                               "technical_indicator_timeframe": "5min",
                               "technical_indicator_warmup": "one prior observed session; adjust prior prices only for events known by signal day",
                               "buy_lot_shares": {"main_and_chinext": 100, "star_minimum": 200}, "sell_after_next_day": True},
               "limitations": ["历史股票池由供应商回填数据重建，不能证明过去每个信号日实际收到同版数据。",
                               "涨跌停价按历史基准价及交易规则重建，特殊无涨跌停情形可能需交易所原始记录核实。",
                               ("回测重放逐窗口训练规则，不是当前发布模型权重的历史收益。" if strategy in MODEL_STRATEGIES
                                else "固定规则参数未根据本次评估结果调优。"),
                               "5分钟OHLCV没有盘口；VWAP、成交参与比例和滑点仍为模拟假设。",
                               "现金分红统一预留20%税款；长期持有可能多预留，送股税及精确补扣时点未建模。",
                               "比较多个策略后选择最优者属于探索性研究；尚未进行多重检验校正或独立新数据确认。",
                               "停牌时沿用最近收盘价估值，未成交卖单保留持仓；末日持仓按收盘价估值而非强制卖出。"]}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
