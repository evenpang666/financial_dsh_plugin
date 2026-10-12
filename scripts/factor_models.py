"""Train and score small, auditable intraday cross-sectional ML models."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from strategy_catalog import MODEL_STRATEGIES

FEATURES = ("open_momentum", "gap", "range_position", "range_width", "vwap_gap", "minute_of_day")
NEXT_BAR = {"09:35": "09:40", "10:30": "10:35", "11:30": "13:05",
            "13:30": "13:35", "14:00": "14:05", "14:30": "14:35", "14:50": "14:55"}
# Bars are stamped at their END. Leave one complete bar for a live snapshot
# and order submission; the execution bar is then worked over its interval.
EXECUTION_BAR = {"09:35": "09:45", "10:30": "10:40", "11:30": "13:10",
                 "13:30": "13:40", "14:00": "14:10", "14:30": "14:40", "14:50": "15:00"}
MIN_ML_DAYS = 60
_XGB_DEVICE: str | None = None


def xgboost_device() -> str:
    """Check that CUDA works at runtime; a CUDA-enabled wheel alone is insufficient."""
    global _XGB_DEVICE
    if _XGB_DEVICE is not None:
        return _XGB_DEVICE
    import xgboost as xgb

    device = "cpu"
    if xgb.build_info().get("USE_CUDA", False):
        try:
            probe = xgb.DMatrix(np.array([[0], [1], [2], [3]], dtype=np.float32),
                                label=np.array([0, 1, 0, 1], dtype=np.float32))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                booster = xgb.train({"tree_method": "hist", "device": "cuda", "nthread": 1},
                                    probe, num_boost_round=1)
            actual = json.loads(booster.save_config())["learner"]["generic_param"]["device"]
            if actual.startswith("cuda"):
                device = "cuda"
        except xgb.core.XGBoostError:
            pass
    _XGB_DEVICE = device
    print(f"XGBoost 计算设备：{'CUDA' if device == 'cuda' else 'CPU（CUDA 不可用）'}", flush=True)
    return device


def _fallback_to_cpu(error: Exception) -> None:
    global _XGB_DEVICE
    _XGB_DEVICE = "cpu"
    print(f"XGBoost CUDA 运行失败，改用 CPU：{str(error).splitlines()[0][:240]}", flush=True)


def _predict_ranker(ranker, frame: pd.DataFrame, features=FEATURES) -> np.ndarray:
    import xgboost as xgb

    values = frame[list(features)]
    try:
        return ranker.predict(values)
    except xgb.core.XGBoostError as error:
        if xgboost_device() != "cuda":
            raise
        _fallback_to_cpu(error)
        ranker.set_params(device="cpu")
        ranker.get_booster().set_param({"device": "cpu"})
        return ranker.predict(values)


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


def fit_estimators(train: pd.DataFrame, algorithm: str = "xgboost", parameters=None) -> dict:
    from strategy_config import validate_parameters
    params = validate_parameters(algorithm, parameters, "training")
    if algorithm not in MODEL_STRATEGIES:
        raise ValueError(f"未知因子模型：{algorithm}")
    train = train.sort_values(["day", "slot", "symbol"]).copy()
    features = list(FEATURES)
    if algorithm == "elastic_net":
        from sklearn.linear_model import ElasticNet
        from sklearn.preprocessing import StandardScaler

        train["excess_target"] = train.target - train.groupby(["day", "slot"]).target.transform("mean")
        scaler = StandardScaler().fit(train[features])
        linear = ElasticNet(alpha=max(1e-6, params["alpha_scale"] * float(train.excess_target.std())),
                            l1_ratio=params["l1_ratio"], max_iter=params["max_iter"], random_state=42)
        linear.fit(scaler.transform(train[features]), train.excess_target)
        return {"elastic_net": (scaler, linear)}
    import xgboost as xgb

    train["relevance"] = train.groupby(["day", "slot"]).target.transform(
        lambda series: np.minimum((series.rank(pct=True) * 5).astype(int), 4))
    qid = train.groupby(["day", "slot"], sort=True).ngroup().to_numpy()
    ranker_options = dict(objective="rank:pairwise", tree_method="hist",
                          lambdarank_pair_method="mean", lambdarank_num_pair_per_sample=2,
                          **params,
                          colsample_bytree=1.0, random_state=42, n_jobs=2)
    device = xgboost_device()
    ranker = xgb.XGBRanker(**ranker_options, device=device)
    try:
        ranker.fit(train[features], train.relevance, qid=qid)
    except xgb.core.XGBoostError as error:
        if device != "cuda":
            raise
        _fallback_to_cpu(error)
        ranker = xgb.XGBRanker(**ranker_options, device="cpu")
        ranker.fit(train[features], train.relevance, qid=qid)
    return {"xgboost": ranker}


def score_estimator(estimators: dict, algorithm: str, frame: pd.DataFrame) -> np.ndarray:
    if algorithm == "elastic_net":
        scaler, linear = estimators[algorithm]
        return linear.predict(scaler.transform(frame[list(FEATURES)]))
    return _predict_ranker(estimators[algorithm], frame)


def select_algorithm(frame: pd.DataFrame, algorithm: str = "xgboost", parameters=None) -> tuple[str, dict, bool, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Evaluate the user-selected model with chronological splits."""
    train, validation, holdout = day_splits(frame)
    if "exit_day" in frame.columns:
        train = train[train.exit_day.lt(str(validation.day.min()))].copy()
        validation = validation[validation.exit_day.lt(str(holdout.day.min()))].copy()
    estimators = fit_estimators(train, algorithm, parameters) if parameters is not None else fit_estimators(train, algorithm)
    results = {name: {"validation": rank_ic(validation, score_estimator(estimators, name, validation)),
                      "holdout": rank_ic(holdout, score_estimator(estimators, name, holdout))}
               for name in (algorithm,)}
    metric = results[algorithm]["validation"]
    eligible = metric["rank_ic"] > 0 and metric["positive_share"] >= 0.5
    return algorithm, results, eligible, train, validation, holdout


def fit_models(samples: list[dict], destination: Path, algorithm: str = "xgboost", parameters=None) -> dict:
    frame = sample_frame(samples)
    chosen, results, eligible, train, validation, holdout = select_algorithm(frame, algorithm, parameters)
    features = list(FEATURES)

    # Historical scores above stay untouched. The deployed model may now use
    # all labels that have matured before the present training run.
    final_estimators = fit_estimators(frame, algorithm, parameters) if parameters is not None else fit_estimators(frame, algorithm)
    destination.mkdir(parents=True, exist_ok=True)
    if algorithm == "xgboost":
        final_estimators["xgboost"].save_model(destination / "xgboost.json")
    else:
        scaler, linear = final_estimators["elastic_net"]
        linear_state = {"features": features, "coef": linear.coef_.tolist(),
                        "intercept": float(linear.intercept_), "mean": scaler.mean_.tolist(),
                        "scale": scaler.scale_.tolist()}
        (destination / "elastic_net.json").write_text(json.dumps(linear_state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"algorithm": chosen, "validation_eligible": eligible,
            "algorithms": results, "features": features,
            "days": int(frame.day.nunique()), "samples": len(frame),
            "train_days": int(train.day.nunique()), "validation_days": int(validation.day.nunique()),
            "holdout_days": int(holdout.day.nunique()), "first_day": str(frame.day.min()),
            "last_day": str(frame.day.max()),
            "label_end_day": str(frame.exit_day.max()) if "exit_day" in frame else str(frame.day.max())}


def predict(frame: pd.DataFrame, model: dict, model_dir: Path) -> np.ndarray:
    features = model["features"]
    if model["algorithm"] == "elastic_net":
        state = json.loads((model_dir / "elastic_net.json").read_text(encoding="utf-8"))
        values = (frame[features].to_numpy(dtype=float) - np.array(state["mean"])) / np.array(state["scale"])
        return values @ np.array(state["coef"]) + state["intercept"]
    if model["algorithm"] == "xgboost":
        import xgboost as xgb
        device = xgboost_device()
        ranker = xgb.XGBRanker(device=device)
        ranker.load_model(bytearray((model_dir / "xgboost.json").read_bytes()))
        try:
            ranker.get_booster().set_param({"device": device})
        except xgb.core.XGBoostError as error:
            if device != "cuda":
                raise
            _fallback_to_cpu(error)
            ranker.set_params(device="cpu")
            ranker.get_booster().set_param({"device": "cpu"})
        return _predict_ranker(ranker, frame, features)
    raise RuntimeError("未知的机器学习模型类型。")
