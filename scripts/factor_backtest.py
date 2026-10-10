"""Chronological, trade-account backtest for the intraday factor models.

The candidate list is still selected at run time. Results are therefore a
fixed-universe research backtest, never a point-in-time whole-market test.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from factor_models import FEATURES, NEXT_BAR, fit_estimators, minute_of_day, rank_ic, sample_frame, score_estimator

DEFAULT_SLOT = "14:00"
TRAIN_DAYS = 100
VALIDATION_DAYS = 20
STEP_DAYS = 20
TOP_N = 5
INITIAL_CASH = 1_000_000.0
COMMISSION = 0.0003
MIN_COMMISSION = 5.0
SELL_TAX = 0.0005
SLIPPAGE = 0.0005
MAX_BAR_PARTICIPATION = 0.10


def _prices(bars_by_symbol: dict[str, pd.DataFrame], execution_time: str) -> dict:
    market = defaultdict(dict)
    for symbol, bars in bars_by_symbol.items():
        frame = bars.copy()
        frame["day"] = frame.time.dt.strftime("%Y-%m-%d")
        frame["clock"] = frame.time.dt.strftime("%H:%M")
        for day, group in frame.groupby("day"):
            execution = group[group.clock.eq(execution_time)]
            bar = execution.iloc[0] if not execution.empty else None
            market[day][symbol] = {
                "open": float(bar.open) if bar is not None and bar.open > 0 else None,
                "amount": float(bar.amount) if bar is not None and bar.amount > 0 else 0.0,
                "close": float(group.iloc[-1].close),
            }
    return market


def _cost(notional: float, side: str) -> float:
    return max(MIN_COMMISSION, notional * COMMISSION) + (notional * SELL_TAX if side == "sell" else 0.0)


def walk_forward_signals(samples: list[dict], slot: str = DEFAULT_SLOT) -> tuple[dict, list[dict], dict]:
    if slot not in NEXT_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    labeled = sample_frame(samples)
    candidates = pd.DataFrame(samples)
    candidates = candidates[candidates.slot.eq(slot)].dropna(subset=list(FEATURES[:-1])).copy()
    candidates["minute_of_day"] = minute_of_day(slot)
    days = sorted(candidates.day.unique())
    if len(days) < TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS:
        raise RuntimeError(f"回测至少需要 {TRAIN_DAYS + VALIDATION_DAYS + STEP_DAYS} 个完整交易日。")
    signals, folds, all_scores = {}, [], []
    for start in range(TRAIN_DAYS + VALIDATION_DAYS, len(days), STEP_DAYS):
        train_days = days[start - TRAIN_DAYS - VALIDATION_DAYS:start - VALIDATION_DAYS]
        validation_days = days[start - VALIDATION_DAYS:start]
        test_days = days[start:start + STEP_DAYS]
        # A label is available only after its exit session has completed.
        train = labeled[labeled.day.isin(train_days) & labeled.exit_day.lt(validation_days[0])]
        validation = labeled[labeled.day.isin(validation_days) & labeled.exit_day.lt(test_days[0])]
        if train.day.nunique() < 80 or validation.day.nunique() < 10:
            raise RuntimeError("滚动窗口可成熟的训练/验证标签不足；请检查停牌和分钟线缺失。")
        selection_models = fit_estimators(train)
        metrics = {name: rank_ic(validation, score_estimator(selection_models, name, validation))
                   for name in ("xgboost", "elastic_net")}
        chosen = max(metrics, key=lambda name: metrics[name]["rank_ic"])
        matured = labeled[labeled.day.isin(train_days + validation_days) & labeled.exit_day.lt(test_days[0])]
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
        folds.append({"train_first": train_days[0], "train_last": train_days[-1],
                      "validation_first": validation_days[0], "validation_last": validation_days[-1],
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
             execution_time: str = NEXT_BAR[DEFAULT_SLOT]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    market = _prices(bars_by_symbol, execution_time)
    cash, positions, marks = INITIAL_CASH, {}, {}
    trades, equity = [], []
    blocked, stale = 0, 0
    for day in sorted(signals):
        today = market.get(day, {})
        wanted = set(signals[day][:TOP_N])
        for symbol in list(positions):
            holding = positions[symbol]
            if symbol in wanted or holding["buy_day"] >= day:
                continue
            bar = today.get(symbol, {})
            raw = bar.get("open")
            if not raw or bar.get("amount", 0) <= 0:
                blocked += 1
                continue
            price = raw * (1 - SLIPPAGE)
            capacity = math.floor(bar["amount"] * MAX_BAR_PARTICIPATION / price / 100) * 100
            shares = min(holding["shares"], capacity)
            if shares <= 0:
                blocked += 1
                continue
            notional = shares * price
            fee = _cost(notional, "sell")
            cash += notional - fee
            holding["shares"] -= shares
            trades.append({"day": day, "symbol": symbol, "side": "sell", "shares": shares,
                           "price": round(price, 4), "notional": round(notional, 2), "cost": round(fee, 2)})
            if holding["shares"] == 0:
                del positions[symbol]
        # Position sizing happens at the execution bar. The day's closing price
        # is only available later and must not enter this decision.
        equity_before_buy = cash + sum(item["shares"] * (today.get(symbol, {}).get("open") or marks.get(symbol, 0.0))
                                       for symbol, item in positions.items())
        for symbol in signals[day][:TOP_N]:
            if symbol in positions:
                continue
            bar = today.get(symbol, {})
            raw = bar.get("open")
            if not raw or bar.get("amount", 0) <= 0:
                blocked += 1
                continue
            price = raw * (1 + SLIPPAGE)
            capacity = math.floor(bar["amount"] * MAX_BAR_PARTICIPATION / price / 100) * 100
            budget = min(cash - MIN_COMMISSION, equity_before_buy / TOP_N)
            shares = min(capacity, math.floor(max(0, budget) / (price * 100)) * 100)
            while shares > 0 and shares * price + _cost(shares * price, "buy") > cash:
                shares -= 100
            if shares <= 0:
                blocked += 1
                continue
            notional = shares * price
            fee = _cost(notional, "buy")
            cash -= notional + fee
            positions[symbol] = {"shares": shares, "buy_day": day}
            trades.append({"day": day, "symbol": symbol, "side": "buy", "shares": shares,
                           "price": round(price, 4), "notional": round(notional, 2), "cost": round(fee, 2)})
        for symbol, bar in today.items():
            if bar["close"] > 0:
                marks[symbol] = bar["close"]
        missing_marks = [symbol for symbol in positions if symbol not in today]
        stale += len(missing_marks)
        nav = cash + sum(item["shares"] * marks.get(symbol, 0.0) for symbol, item in positions.items())
        equity.append({"day": day, "cash": round(cash, 2), "equity": round(nav, 2),
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
             "blocked_orders": blocked, "stale_position_days": stale}
    return equity_frame, trade_frame, stats


def run(samples: list[dict], bars_by_symbol: dict[str, pd.DataFrame], destination: Path,
        slot: str = DEFAULT_SLOT) -> dict:
    if slot not in NEXT_BAR:
        raise ValueError(f"回测时点无效：{slot}")
    execution_time = NEXT_BAR[slot]
    signals, folds, oos_ic = walk_forward_signals(samples, slot)
    equity, trades, stats = simulate(signals, bars_by_symbol, execution_time)
    destination.mkdir(parents=True, exist_ok=True)
    equity.to_csv(destination / "equity.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(destination / "trades.csv", index=False, encoding="utf-8-sig")
    import json
    (destination / "folds.json").write_text(json.dumps(folds, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"kind": "fixed_universe_research_backtest",
               "algorithm_policy": "每个滚动窗口按此前20日验证Rank IC选择 XGBoost 或 Elastic Net",
               "slot": slot, "execution": execution_time,
               "first_day": min(signals), "last_day": max(signals), "days": len(signals),
               "folds": len(folds), "top_n": TOP_N, "stats": stats, "oos_rank_ic": oos_ic,
               "assumptions": {"commission": COMMISSION, "minimum_commission": MIN_COMMISSION,
                               "sell_tax": SELL_TAX, "slippage_each_side": SLIPPAGE,
                               "max_5m_amount_participation": MAX_BAR_PARTICIPATION,
                               "buy_lot_shares": 100, "sell_after_next_day": True},
               "limitations": ["股票池按本次运行时的快照选取，存在存续与选股偏差；不可当作全市场无偏回测。",
                               "5分钟OHLCV没有盘口，滑点为假设值；涨跌停、历史ST状态及公司行为未完整重建。",
                               "停牌时沿用最近收盘价估值，未成交卖单保留持仓；末日持仓按收盘价估值而非强制卖出。"]}
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
