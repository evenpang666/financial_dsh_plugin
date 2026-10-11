"""Train and score small, auditable intraday cross-sectional ML models."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

FEATURES = ("open_momentum", "gap", "range_position", "range_width", "vwap_gap", "minute_of_day")
NEXT_BAR = {"09:35": "09:40", "10:30": "10:35", "11:30": "13:05",
            "13:30": "13:35", "14:00": "14:05", "14:30": "14:35", "14:50": "14:55"}
# Bars are stamped at their END. Leave one complete bar for a live snapshot
# and order submission; the execution bar is then worked over its interval.
EXECUTION_BAR = {"09:35": "09:45", "10:30": "10:40", "11:30": "13:10",
                 "13:30": "13:40", "14:00": "14:10", "14:30": "14:40", "14:50": "15:00"}
MIN_ML_DAYS = 60


def minute_of_day(slot: str) -> int:
    hour, minute = map(int, slot.split(":"))
    return hour * 60 + minute


def sample_frame(samples: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(samples)
    if frame.empty:
        raise RuntimeError("没有可训练的盘中样本。")
    frame["minute_of_day"] = frame["slot"].map(minute_of_day)
    if "exit_day" in frame.columns:
        # Exclude a halted stock's delayed next bar from one-session labels.
        days = sorted(frame["day"].dropna().unique())
        following = dict(zip(days[:-1], days[1:]))
        frame = frame[frame["exit_day"].eq(frame["day"].map(following))]
    frame = frame.dropna(subset=["day", "slot", "target", *FEATURES]).sort_values(["day", "slot", "symbol"])
    groups = frame.groupby(["day", "slot"])
    frame = frame[groups["target"].transform("size").ge(12) & groups["target"].transform("nunique").ge(5)]
    return frame.reset_index(drop=True)


def day_splits(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    days = sorted(frame["day"].unique())
    if len(days) < MIN_ML_DAYS:
        raise RuntimeError(f"机器学习训练至少需要 {MIN_ML_DAYS} 个完整历史交易日，当前只有 {len(days)} 日；请检查 BaoStock 回填与本地缓存。")
    train_end = int(len(days) * 0.6)
    validation_end = int(len(days) * 0.8)
    return (frame[frame.day.isin(days[:train_end])].copy(),
            frame[frame.day.isin(days[train_end:validation_end])].copy(),
            frame[frame.day.isin(days[validation_end:])].copy())


def rank_ic(frame: pd.DataFrame, scores: np.ndarray) -> dict:
    measured = frame[["day", "slot", "target"]].copy()
    measured["score"] = scores
    values = []
    for _, group in measured.groupby(["day", "slot"]):
        if len(group) < 12 or group.target.nunique() < 5 or group.score.nunique() < 5:
            continue
        ic = group.target.rank().corr(group.score.rank())
        if pd.notna(ic):
            values.append(float(ic))
    if not values:
        return {"rank_ic": 0.0, "positive_share": 0.0, "groups": 0}
    return {"rank_ic": round(float(np.mean(values)), 4),
            "positive_share": round(float(np.mean(np.array(values) > 0)), 3),
            "groups": len(values)}


def fit_estimators(train: pd.DataFrame) -> dict:
    from sklearn.linear_model import ElasticNet
    from sklearn.preprocessing import StandardScaler
    import xgboost as xgb

    train = train.sort_values(["day", "slot", "symbol"]).copy()
    features = list(FEATURES)
    train["excess_target"] = train.target - train.groupby(["day", "slot"]).target.transform("mean")
    scaler = StandardScaler().fit(train[features])
    linear = ElasticNet(alpha=max(1e-6, 0.01 * float(train.excess_target.std())),
                        l1_ratio=0.5, max_iter=10000, random_state=42)
    linear.fit(scaler.transform(train[features]), train.excess_target)
    train["relevance"] = train.groupby(["day", "slot"]).target.transform(
        lambda series: np.minimum((series.rank(pct=True) * 5).astype(int), 4))
    qid = train.groupby(["day", "slot"], sort=True).ngroup().to_numpy()
    ranker = xgb.XGBRanker(objective="rank:pairwise", tree_method="hist",
                           lambdarank_pair_method="mean", lambdarank_num_pair_per_sample=2,
                           n_estimators=120, max_depth=2, learning_rate=0.04,
                           min_child_weight=20, reg_lambda=10, subsample=0.8,
                           colsample_bytree=1.0, random_state=42, n_jobs=2)
    ranker.fit(train[features], train.relevance, qid=qid)
    return {"elastic_net": (scaler, linear), "xgboost": ranker}


def score_estimator(estimators: dict, algorithm: str, frame: pd.DataFrame) -> np.ndarray:
    if algorithm == "elastic_net":
        scaler, linear = estimators[algorithm]
        return linear.predict(scaler.transform(frame[list(FEATURES)]))
    return estimators[algorithm].predict(frame[list(FEATURES)])


def select_algorithm(frame: pd.DataFrame) -> tuple[str, dict, bool, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Use one chronological selection policy for deployment and replay."""
    train, validation, holdout = day_splits(frame)
    if "exit_day" in frame.columns:
        train = train[train.exit_day.lt(str(validation.day.min()))].copy()
        validation = validation[validation.exit_day.lt(str(holdout.day.min()))].copy()
    estimators = fit_estimators(train)
    results = {name: {"validation": rank_ic(validation, score_estimator(estimators, name, validation)),
                      "holdout": rank_ic(holdout, score_estimator(estimators, name, holdout))}
               for name in ("elastic_net", "xgboost")}
    eligible = [(name, result) for name, result in results.items()
                if result["validation"]["rank_ic"] > 0 and result["validation"]["positive_share"] >= 0.5]
    pool = eligible or list(results.items())
    chosen = max(pool, key=lambda item: item[1]["validation"]["rank_ic"])[0]
    return chosen, results, bool(eligible), train, validation, holdout


def fit_models(samples: list[dict], destination: Path) -> dict:
    frame = sample_frame(samples)
    chosen, results, eligible, train, validation, holdout = select_algorithm(frame)
    features = list(FEATURES)

    # Historical scores above stay untouched. The deployed model may now use
    # all labels that have matured before the present training run.
    final_estimators = fit_estimators(frame)
    destination.mkdir(parents=True, exist_ok=True)
    scaler, linear = final_estimators["elastic_net"]
    ranker = final_estimators["xgboost"]
    ranker.save_model(destination / "xgboost.json")
    linear_state = {"features": features, "coef": linear.coef_.tolist(),
                    "intercept": float(linear.intercept_), "mean": scaler.mean_.tolist(),
                    "scale": scaler.scale_.tolist()}
    (destination / "elastic_net.json").write_text(json.dumps(linear_state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"algorithm": chosen, "validation_eligible": eligible,
            "algorithms": results, "features": features,
            "days": int(frame.day.nunique()), "samples": len(frame),
            "train_days": int(train.day.nunique()), "validation_days": int(validation.day.nunique()),
            "holdout_days": int(holdout.day.nunique()), "first_day": str(frame.day.min()),
            "last_day": str(frame.day.max())}


def predict(frame: pd.DataFrame, model: dict, model_dir: Path) -> np.ndarray:
    features = model["features"]
    if model["algorithm"] == "elastic_net":
        state = json.loads((model_dir / "elastic_net.json").read_text(encoding="utf-8"))
        values = (frame[features].to_numpy(dtype=float) - np.array(state["mean"])) / np.array(state["scale"])
        return values @ np.array(state["coef"]) + state["intercept"]
    if model["algorithm"] == "xgboost":
        import xgboost as xgb
        ranker = xgb.XGBRanker()
        ranker.load_model(bytearray((model_dir / "xgboost.json").read_bytes()))
        return ranker.predict(frame[features])
    raise RuntimeError("未知的机器学习模型类型。")
