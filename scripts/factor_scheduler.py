#!/usr/bin/env python
"""Check once per weekday for a weekly AI factor training run."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import factor_lab


STATE = factor_lab.STORE / "weekly-scheduler.json"
LOG = factor_lab.STORE / "weekly-scheduler.log"


def due(now: datetime, model: dict, state: dict, settings: dict) -> bool:
    if not settings["weekly_training_enabled"] or now.weekday() >= 5 or (now.hour, now.minute) < (12, 0):
        return False
    week = now.isocalendar()[:2]
    try:
        trained = datetime.fromisoformat(model["selected_at"])
        if trained.isocalendar()[:2] == week and model.get("target_version") == factor_lab.ML_TARGET_VERSION:
            return False
    except (KeyError, TypeError, ValueError):
        pass
    return state.get("last_attempt_date") != now.date().isoformat()


def run_once(now: datetime | None = None) -> bool:
    now = now or factor_lab.now_cn()
    if not due(now, factor_lab.read_json(factor_lab.MODEL), factor_lab.read_json(STATE), factor_lab.load_settings()):
        return False
    STATE.parent.mkdir(parents=True, exist_ok=True)
    factor_lab.write_json(STATE, {"last_attempt_date": now.date().isoformat(), "started_at": now.isoformat()})
    command = [sys.executable, str(Path(__file__).with_name("factor_lab.py")), "train"]
    with LOG.open("a", encoding="utf-8") as output:
        output.write(f"\n[{now.isoformat()}] Weekly training started\n")
        output.flush()
        result = subprocess.run(command, cwd=factor_lab.ROOT, stdout=output, stderr=subprocess.STDOUT,
                                check=False)
        output.write(f"[{factor_lab.now_cn().isoformat()}] Exit code {result.returncode}\n")
    if result.returncode:
        raise RuntimeError(f"每周因子训练失败（退出码 {result.returncode}）；详情见 {LOG}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Check and train once (Windows task scheduler)")
    args = parser.parse_args()
    if args.once:
        print("已执行每周训练。" if run_once() else "今天无需训练。", flush=True)
        return 0
    while True:
        try:
            run_once()
        except Exception as error:
            print(error, file=sys.stderr, flush=True)
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
