"""Chronological, trade-account backtest with a daily historical universe."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from factor_models import EXECUTION_BAR, FEATURES, NEXT_BAR, fit_estimators, minute_of_day, rank_ic, sample_frame, score_estimator, select_algorithm

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
SLIPPAGE = 0.0005
MAX_BAR_PARTICIPATION = 0.10
ORDER_PRICE_BUFFER = 0.02
SCHEMA_VERSION = 2


def _prices(bars_by_symbol: dict[str, pd.DataFrame], signal_time: str, execution_time: str) -> dict:
    market = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        frame = bars.copy()
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
                "close": float(group.iloc[-1].close),
            }
    return market


def _cost(notional: float, side: str) -> float:
    return max(MIN_COMMISSION, notional * COMMISSION) + (notional * SELL_TAX if side == "sell" else 0.0)


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
    known = pd.to_datetime(universe.known_at, errors="coerce")
    if known.isna().any() or (known.dt.strftime("%Y-%m-%d") >= universe.day).any():
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
    limited = state[state.has_price_limit]
    if (state[["split_ratio", "cash_dividend"]].isna().any().any()
            or limited[["limit_up", "limit_down"]].isna().any().any()
            or (limited.limit_up <= limited.limit_down).any() or (limited.limit_down <= 0).any()
            or (state.split_ratio <= 0).any() or (state.cash_dividend < 0).any()):
        raise RuntimeError("逐日交易状态含缺失或无效的涨跌停价、拆并股或分红数据。")
    dividend = state[state.cash_dividend.gt(0)]
    pay_dates = pd.to_datetime(dividend.dividend_pay_date, errors="coerce")
    if (pay_dates.isna().any() or
            (pay_dates.dt.strftime("%Y-%m-%d").to_numpy() < dividend.day.to_numpy()).any()):
        raise RuntimeError("现金分红缺少有效到账日；拒绝将应收红利永久当作不可用现金。")
    return universe, state


def walk_forward_signals(samples: list[dict], universe_history: pd.DataFrame,
                         slot: str = DEFAULT_SLOT,
                         market_state: pd.DataFrame | None = None) -> tuple[dict, list[dict], dict]:
    if slot not in EXECUTION_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    if universe_history is None:
        raise RuntimeError("缺少逐日历史股票池；拒绝事后股票池回测。")
    eligible = universe_history[["day", "symbol"]].copy()
    if market_state is not None:
        excluded = market_state[market_state.is_st | market_state.is_suspended][["day", "symbol"]]
        eligible = eligible.merge(excluded.assign(excluded=True), on=["day", "symbol"], how="left")
        eligible = eligible[eligible.excluded.isna()][["day", "symbol"]]
    candidates = pd.DataFrame(samples).merge(eligible, on=["day", "symbol"], how="inner")
    labeled = sample_frame(candidates.to_dict("records"))
    candidates = candidates[candidates.slot.eq(slot)].dropna(subset=list(FEATURES[:-1])).copy()
    candidates["minute_of_day"] = minute_of_day(slot)
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
        chosen, metrics, _, train, validation, holdout = select_algorithm(matured)
        final_models = fit_estimators(matured)
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
             market_state: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    execution_time = execution_time or EXECUTION_BAR[signal_time]
    market = _prices(bars_by_symbol, signal_time, execution_time)
    state_by_day = ({} if market_state is None else
                    {(row.day, row.symbol): row for row in market_state.itertuples(index=False)})
    cash, receivables, pending_dividends, positions, marks = INITIAL_CASH, 0.0, defaultdict(float), {}, {}
    trades, equity = [], []
    blocked, stale, partial = 0, 0, 0
    for day in sorted(signals):
        today = market.get(day, {})
        wanted = set(signals[day][:TOP_N])
        for symbol, holding in positions.items():
            state = state_by_day.get((day, symbol))
            if market_state is not None and state is None:
                raise RuntimeError(f"{day} {symbol} 缺少公司行为或交易状态。")
            if state is not None:
                # Dividend entitlement is part of NAV but cannot fund an
                # order before the actual payment date (not in this dataset).
                dividend = holding["shares"] * float(state.cash_dividend)
                if dividend:
                    pay_day = str(getattr(state, "dividend_pay_date", ""))
                    if not pay_day:
                        raise RuntimeError(f"{day} {symbol} 缺少分红到账日。")
                    receivables += dividend
                    pending_dividends[pay_day] += dividend
                holding["shares"] = int(round(holding["shares"] * float(state.split_ratio)))
                if symbol in marks:
                    marks[symbol] = max(0.0, (marks[symbol] - float(state.cash_dividend)) / float(state.split_ratio))
        pre_sale_cash = cash
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
                        and raw * (1 - SLIPPAGE) <= float(state.limit_down))):
                blocked += 1
                continue
            price = raw * (1 - SLIPPAGE)
            capacity = math.floor(bar["amount"] * MAX_BAR_PARTICIPATION / price / 100) * 100
            shares = min(holding["shares"], capacity)
            if shares <= 0:
                blocked += 1
                continue
            if shares < holding["shares"]:
                partial += 1
            notional = shares * price
            fee = _cost(notional, "sell")
            cash += notional - fee
            holding["shares"] -= shares
            trades.append({"day": day, "symbol": symbol, "side": "sell", "shares": shares,
                           "price": round(price, 4), "notional": round(notional, 2), "cost": round(fee, 2)})
            if holding["shares"] == 0:
                del positions[symbol]
        # Order size uses only the completed signal bar and cash available
        # before this execution window. Same-window sale proceeds are not reused.
        equity_before_buy = pre_sale_cash + sum(item["shares"] * (today.get(symbol, {}).get("signal_close") or marks.get(symbol, 0.0))
                                       for symbol, item in positions.items())
        cash_for_orders = pre_sale_cash
        for symbol in signals[day][:TOP_N]:
            if symbol in positions:
                continue
            bar = today.get(symbol, {})
            raw = bar.get("vwap")
            signal_price = bar.get("signal_close")
            state = state_by_day.get((day, symbol))
            if market_state is not None and state is None:
                raise RuntimeError(f"{day} {symbol} 缺少公司行为或交易状态。")
            if (not raw or not signal_price or bar.get("amount", 0) <= 0
                    or (state is not None and (getattr(state, "is_suspended", False)
                                               or getattr(state, "is_st", False)))
                    or (state is not None and state.has_price_limit
                        and raw * (1 + SLIPPAGE) >= float(state.limit_up))):
                blocked += 1
                continue
            price = raw * (1 + SLIPPAGE)
            order_limit = signal_price * (1 + ORDER_PRICE_BUFFER)
            if price > order_limit:
                blocked += 1
                continue
            capacity = math.floor(bar["amount"] * MAX_BAR_PARTICIPATION / price / 100) * 100
            budget = min(cash_for_orders - MIN_COMMISSION, equity_before_buy / TOP_N)
            known_capacity = math.floor(bar["signal_amount"] * MAX_BAR_PARTICIPATION
                                        / order_limit / 100) * 100
            order_shares = min(known_capacity,
                               math.floor(max(0, budget) / (order_limit * 100)) * 100)
            minimum = 200 if symbol.startswith("688") else 100
            shares = min(capacity, order_shares)
            while shares >= minimum and shares * price + _cost(shares * price, "buy") > cash_for_orders:
                shares -= 100
            if shares < minimum:
                blocked += 1
                continue
            if shares < order_shares:
                partial += 1
            notional = shares * price
            fee = _cost(notional, "buy")
            cash -= notional + fee
            cash_for_orders -= shares * order_limit + _cost(shares * order_limit, "buy")
            positions[symbol] = {"shares": shares, "buy_day": day}
            trades.append({"day": day, "symbol": symbol, "side": "buy", "shares": shares,
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
                       "positions": len(positions), "stale_marks": len(missing_marks)})
    equity_frame, trade_frame = pd.DataFrame(equity), pd.DataFrame(trades)
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
             "slippage_costs": round(sum(item["notional"] * SLIPPAGE / (1 + SLIPPAGE if item["side"] == "buy" else 1 - SLIPPAGE)
                                         for item in trades), 2),
             "blocked_orders": blocked, "partial_fills": partial, "stale_position_days": stale}
    return equity_frame, trade_frame, stats


def run(samples: list[dict], bars_by_symbol: dict[str, pd.DataFrame], destination: Path,
        slot: str = DEFAULT_SLOT, universe_history: pd.DataFrame | None = None,
        market_state: pd.DataFrame | None = None,
        source_manifest: dict | None = None) -> dict:
    if slot not in EXECUTION_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    universe_history, market_state = validate_context(universe_history, market_state)
    execution_time = EXECUTION_BAR[slot]
    sample_frame_all = pd.DataFrame(samples)
    if sample_frame_all.empty:
        raise RuntimeError("没有可用的历史因子样本。")
    required = universe_history[["day", "symbol"]].drop_duplicates()
    missing_state = required.merge(market_state[["day", "symbol"]], on=["day", "symbol"], how="left", indicator=True)
    if missing_state._merge.eq("left_only").any():
        raise RuntimeError("历史股票池中有股票缺少逐日交易状态；拒绝生成绩效。")
    eligible_state = required.merge(market_state, on=["day", "symbol"], how="left")
    tradable = eligible_state[~eligible_state.is_suspended & ~eligible_state.is_st]
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
    signals, folds, oos_ic = walk_forward_signals(samples, universe_history, slot, market_state)
    equity, trades, stats = simulate(signals, bars_by_symbol, slot, execution_time, market_state)
    destination.mkdir(parents=True, exist_ok=True)
    equity.to_csv(destination / "equity.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(destination / "trades.csv", index=False, encoding="utf-8-sig")
    import json
    (destination / "folds.json").write_text(json.dumps(folds, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"kind": "point_in_time_backtest", "schema_version": SCHEMA_VERSION,
               "data_provenance": {"source_scope": source_manifest.get("source_scope"),
                                   "manifest_created_at": source_manifest.get("created_at"),
                                   "limit_source": source_manifest.get("limit_source")}
                                  if source_manifest else None,
               "model_scope": "walk_forward_policy_not_current_published_weights",
               "algorithm_policy": "每个滚动窗口沿用发布模型的训练/验证/留出分割、验证期选模及成熟标签重拟合规则",
               "slot": slot, "execution": f"{NEXT_BAR[slot]}–{execution_time} VWAP",
               "execution_bar_end": execution_time,
               "first_day": min(signals), "last_day": max(signals), "days": len(signals),
               "folds": len(folds), "top_n": TOP_N, "stats": stats, "oos_rank_ic": oos_ic,
               "assumptions": {"commission": COMMISSION, "minimum_commission": MIN_COMMISSION,
                               "sell_tax": SELL_TAX, "slippage_each_side": SLIPPAGE,
                               "max_5m_amount_participation": MAX_BAR_PARTICIPATION,
                               "order_price_buffer": ORDER_PRICE_BUFFER,
                               "buy_lot_shares": {"main_and_chinext": 100, "star": 200}, "sell_after_next_day": True},
               "limitations": ["历史股票池由供应商回填数据重建，不能证明过去每个信号日实际收到同版数据。",
                               "涨跌停价按历史基准价及交易规则重建，特殊无涨跌停情形可能需交易所原始记录核实。",
                               "回测重放逐窗口训练规则，不是当前发布模型权重的历史收益。",
                               "5分钟OHLCV没有盘口；VWAP、成交参与比例和滑点仍为模拟假设。",
                               "停牌时沿用最近收盘价估值，未成交卖单保留持仓；末日持仓按收盘价估值而非强制卖出。"]}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
