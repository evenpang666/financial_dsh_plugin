"""Validate published backtest inputs without depending on a user interface."""
import hashlib
import json
from pathlib import Path

import pandas as pd
from factor_backtest import validate_context

SCHEMA_VERSION = 2

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def cache_issues(store, manifest=None):
    """A legacy validated flag cannot attest to newer corporate-action fields."""
    store = Path(store)
    try:
        if manifest is None:
            manifest = json.loads((store / "source-manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("validated") is not True
                or manifest.get("data_schema_version") != SCHEMA_VERSION
                or manifest.get("feature_warmup_validated") is not True
                or manifest.get("source_scope") != "historical_vendor_reconstruction"):
            return ["历史缓存需升级：重新验收红股上市日期和首次入池预热分钟线。"]
        files, bars = manifest.get("files"), manifest.get("bars")
        if not isinstance(files, dict) or set(files) != {"universe-history.csv", "market-state.csv"}:
            return ["历史数据清单缺少输入文件校验值。"]
        for name, digest in files.items():
            if sha256(store / name) != digest:
                return [f"历史数据 {name} 与验收清单不一致。"]
        universe = pd.read_csv(store / "universe-history.csv", dtype={"symbol": str})
        state = pd.read_csv(store / "market-state.csv", dtype={"symbol": str})
        if "stock_credit_date" not in state:
            return ["历史交易状态缺少红股上市日期列，需要重新准备。"]
        validate_context(universe, state)
        if not isinstance(bars, dict) or not bars or set(bars) != set(universe.symbol):
            return ["历史分钟线清单未覆盖全部候选股票。"]
        for symbol, digest in bars.items():
            if len(symbol) != 6 or not symbol.isdigit():
                return ["历史分钟线清单包含无效股票代码。"]
            if sha256(store / "bars" / f"{symbol}.csv") != digest:
                return [f"历史分钟线 {symbol} 缺失或与验收清单不一致。"]
        return []
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError) as exc:
        return [f"历史缓存不可用：{exc}"]

def preparation_range(start, end, manifest):
    """Rebuild the previous published range as well as the requested interval."""
    first = manifest.get("requested_start") or manifest.get("first_day") or start
    last = manifest.get("requested_end") or manifest.get("last_day") or end
    return min(start, first), max(end, last)
