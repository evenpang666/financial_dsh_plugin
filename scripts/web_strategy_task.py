"""Run the existing validated research pipeline without the desktop panel."""
import argparse
import json
import os
import sys
from pathlib import Path
import factor_lab as lab
from model_checkpoints import require_checkpoint
from strategy_config import configured_parameters
from backtest_cache import cache_issues, preparation_range

def main():
    if os.getenv("FINANCE_WEB_OWNED") == "1":
        from web_process_owner import watch_owner
        watch_owner()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backtest", "train"])
    parser.add_argument("--strategy", required=True, choices=tuple(lab.STRATEGIES))
    parser.add_argument("--checkpoint", default="")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--slot", default="14:00", choices=lab.SLOTS)
    parser.add_argument("--parameters", default="{}")
    args = parser.parse_args()
    try:
        purpose = "training" if args.action == "train" else "backtest"
        lab._STRATEGY_OVERRIDE, lab._CHECKPOINT_OVERRIDE = args.strategy, args.checkpoint
        key = "training_parameters" if args.action == "train" else "strategy_parameters"
        lab._SETTINGS_OVERRIDE = {f"{purpose}_start": args.start, f"{purpose}_end": args.end,
                                  "backtest_slot": args.slot, key: {args.strategy: json.loads(args.parameters)}}
        settings = lab.load_settings()
        configured_parameters(settings, args.strategy, "training" if args.action == "train" else "execution")
        checkpoint = require_checkpoint(args.strategy, args.checkpoint, lab.STORE) if args.action == "backtest" else None
        start, end = lab.research_range(settings, purpose, checkpoint)
        manifest = lab.read_json(lab.SOURCE_MANIFEST)
        issues = cache_issues(lab.STORE, manifest)
        needed = bool(issues)
        try:
            lab.ensure_range_coverage(manifest, start, end)
        except (ValueError, RuntimeError):
            needed = True
        if needed:
            from factor_data_prep import prepare, RAW
            start, end = preparation_range(start, end, manifest)
            if issues:
                print("；".join(issues), flush=True)
            print(f"正在准备并验收历史数据：{start} 至 {end}。完整缓存会复用。", flush=True)
            calendar = RAW / f"trade_cal_{start.replace('-', '')}_{end.replace('-', '')}.csv"
            if calendar.exists():
                prepare(start, end, fetch_source=False)
            else:
                prepare(start, end)
            remaining = cache_issues(lab.STORE)
            if remaining:
                raise RuntimeError("历史数据准备后仍未通过验收：" + "；".join(remaining))
        (lab.train if args.action == "train" else lab.backtest_from_cache)()
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
