"""Discover and resolve immutable, timestamped model artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from strategy_catalog import FIXED_STRATEGIES, MODEL_STRATEGIES, STRATEGIES, validate_strategy, strategy_description

STORE = Path(__file__).resolve().parent.parent / "data" / "factor-lab"
TARGET_VERSION = 3
CHECKPOINT_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_checkpoints(store: Path = STORE) -> list[dict]:
    records = [model for path in (store / "models").glob("*/checkpoint.json")
               if (model := _read(path)).get("model_id") == path.parent.name]
    # Older releases saved metadata only beside the latest artifact. Preserve
    # those discoverable versions; do not guess metadata for orphaned weights.
    records += [_read(store / f"model-{algorithm}.json") for algorithm in MODEL_STRATEGIES]
    records += [_read(store / "model.json")]
    available = {}
    for model in records:
        identity, algorithm = model.get("model_id", ""), model.get("algorithm")
        if (not isinstance(identity, str) or not CHECKPOINT_ID.fullmatch(identity)
                or algorithm not in MODEL_STRATEGIES or model.get("target_version") != TARGET_VERSION
                or not model.get("features") or not model.get("selected_at")
                or not model.get("training", {}).get("last_day")):
            continue
        artifact = store / "models" / identity / f"{algorithm}.json"
        if not artifact.is_file():
            continue
        try:
            datetime.fromisoformat(model["selected_at"])
            datetime.strptime(model["training"]["last_day"], "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        if model.get("artifact_sha256") and hashlib.sha256(artifact.read_bytes()).hexdigest() != model["artifact_sha256"]:
            continue
        if not model["training"].get("label_end_day"):
            model = {**model, "training": {**model["training"], "label_end_day": model["selected_at"][:10],
                                          "boundary_source": "conservative_training_timestamp"}}
        key = f"{algorithm}@{identity}"
        if key not in available:
            available[key] = {**model, "checkpoint_id": identity,
                              "selection_id": key,
                              "label": f"{'XGBoost' if algorithm == 'xgboost' else 'Elastic Net'} · {datetime.fromisoformat(model['selected_at']).strftime('%Y-%m-%d %H:%M:%S.%f')} · {identity[-8:]}"}
    return sorted(available.values(), key=lambda item: (item["selected_at"], item["model_id"]), reverse=True)


def migrate_legacy_checkpoints(store: Path = STORE) -> None:
    for model in list_checkpoints(store):
        path = store / "models" / model["model_id"] / "checkpoint.json"
        if path.exists():
            continue
        artifact = path.with_name(f"{model['algorithm']}.json")
        metadata = {key: value for key, value in model.items() if key not in {"selection_id", "label", "checkpoint_id"}}
        metadata["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass


def require_checkpoint(strategy: str, checkpoint_id: str | None, store: Path = STORE) -> dict:
    validate_strategy(strategy)
    if strategy not in MODEL_STRATEGIES:
        if checkpoint_id:
            raise ValueError("经典策略没有 checkpoint，请直接运行。")
        return {}
    if not checkpoint_id or not CHECKPOINT_ID.fullmatch(checkpoint_id):
        raise ValueError("请选择已经训练好的模型 checkpoint；没有训练产物时不能回测或部署。")
    model = next((item for item in list_checkpoints(store)
                  if item["model_id"] == checkpoint_id and item["algorithm"] == strategy), None)
    if model is None:
        raise ValueError("所选模型 checkpoint 不存在、文件缺失或与模型类型不匹配，请刷新列表。")
    artifact = store / "models" / checkpoint_id / f"{strategy}.json"
    digest = model.get("artifact_sha256")
    if digest and hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
        raise ValueError("所选 checkpoint 的权重文件与保存时的校验值不一致。")
    return model


def execution_options(store: Path = STORE, settings: dict | None = None) -> dict:
    checkpoints = list_checkpoints(store)
    strategies = {key: dict(STRATEGIES[key]) for key in FIXED_STRATEGIES}
    models = {item["selection_id"]: {"name": item["label"], "strategy_id": item["algorithm"],
              "checkpoint_id": item["model_id"], "trained_at": item["selected_at"],
              "training_last_day": item["training"]["last_day"]} for item in checkpoints}
    settings = settings or {}
    strategy = settings.get("strategy", FIXED_STRATEGIES[0])
    checkpoint = settings.get("checkpoint_id", "")
    selected = f"{strategy}@{checkpoint}" if strategy in MODEL_STRATEGIES else strategy
    from strategy_config import parameter_schema, configured_parameters
    for key, entry in {**strategies, **models}.items():
        algorithm = entry.get("strategy_id", key)
        entry["parameter_schema"] = parameter_schema(algorithm)
        entry["parameters"] = configured_parameters(settings, algorithm)
        entry["description"] = strategy_description(algorithm, entry["parameters"])
        if "@" in key:
            model = next(item for item in checkpoints if item["selection_id"] == key)
            entry["training_range"] = model.get("training", {})
            entry["training_parameters"] = model.get("training_parameters", {})
            entry["training_parameter_schema"] = parameter_schema(algorithm, "training")
    return {"fixed_strategies": strategies, "models": models,
            "strategies": {**strategies, **models},
            "selected_kind": settings.get("strategy_kind", "model" if strategy in MODEL_STRATEGIES else "fixed"),
            "selected": selected if selected in {**strategies, **models} else ""}


def resolve_selection(selection: str, checkpoint_id: str | None = None, store: Path = STORE) -> tuple[str, str, dict]:
    if "@" in selection:
        strategy, identity = selection.split("@", 1)
        if checkpoint_id and checkpoint_id != identity:
            raise ValueError("模型选择与 checkpoint 参数不一致。")
        checkpoint_id = identity
    else:
        strategy = selection
    model = require_checkpoint(strategy, checkpoint_id, store)
    return strategy, checkpoint_id or "", model
