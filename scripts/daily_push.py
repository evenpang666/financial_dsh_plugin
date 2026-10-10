#!/usr/bin/env python
"""Trading-day 09:20/14:30 report scheduler with archive, review, and optional webhook."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "daily-push.json"
STATE_FILE = PROJECT_ROOT / "data" / "daily-push-state.json"
REPORT_DIR = PROJECT_ROOT / "data" / "reports"
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def with_backtest_section(markdown: str) -> str:
    from report_web_server import latest_backtest

    report = latest_backtest()
    if not report.get("available"):
        return markdown + "\n\n## AI 因子滚动回测\n\n尚无回测结果；请在控制面板点击“一键训练”或“一键回测”。"
    stats = report["stats"]
    rank_ic = report.get("oos_rank_ic", {}).get("rank_ic")
    rank_ic_text = f"{rank_ic:.4f}" if isinstance(rank_ic, (int, float)) else "暂无"
    return markdown + "\n\n" + "\n".join([
        "## AI 因子滚动回测",
        "",
        f"生成时间：{report['generated_at']}；区间：{report.get('first_day', '未知')} 至 {report.get('last_day', '未知')}（{report.get('days', '未知')} 个交易日）。",
        f"{report['slot']} 信号，{report.get('execution', '下一根5分钟K线')} 模拟成交；累计收益 {stats.get('total_return', 0):.2%}，最大回撤 {stats.get('max_drawdown', 0):.2%}，样本外 Rank IC {rank_ic_text}。",
        f"模拟交易 {stats.get('trades', 0)} 笔，费用 {stats.get('costs', 0):,.2f} 元。",
        "当前股票池按训练时点选取，存在历史选股与存续偏差；价格、滑点和费用均为模拟，仅供研究。",
    ])


def log(message: str):
    print(f"[{datetime.now(CHINA_TZ):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def load_config(path: Path) -> dict:
    defaults = {
        "enabled": True,
        "data_service_url": "http://127.0.0.1:8765",
        "scheduled_time": "09:20",
        "schedules": {"morning": "09:20", "afternoon": "14:30"},
        "candidate_limit": 5,
        "market": "a",
        "report_site_url": "http://127.0.0.1:8766",
        "auto_open_report": True,
        "webhook_type": "generic",
        "webhook_url": "",
    }
    if path.exists():
        supplied = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("推送配置必须是 JSON 对象。")
        defaults.update(supplied)
        if "schedules" not in supplied and "scheduled_time" in supplied:
            defaults["schedules"] = {"morning": str(supplied["scheduled_time"]), "afternoon": "14:30"}
    env_url = os.getenv("FINANCE_AGENT_WEBHOOK_URL", "").strip()
    if env_url:
        defaults["webhook_url"] = env_url
    return defaults


def get_json(url: str, timeout: int = 120) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "dsh-finance-agent/0.2"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def webhook_payload(kind: str, markdown: str) -> dict:
    kind = kind.lower()
    heading = str(markdown).lstrip()
    is_hk = heading.startswith("# 港股")
    is_afternoon = "日报2" in heading.splitlines()[0] if heading else False
    market_name = "港股" if is_hk else "A股"
    title = f"{market_name}{'收盘前' if is_afternoon else '开盘前'}研究简报"
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": markdown}}
    if kind == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": markdown}}
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
    report_type = ("hk" if is_hk else "a_share") + ("_afternoon_review" if is_afternoon else "_morning_research")
    return {"text": markdown, "content": markdown, "type": report_type}


def post_webhook(url: str, kind: str, markdown: str):
    body = json.dumps(webhook_payload(kind, markdown), ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    with urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Webhook 返回 HTTP {response.status}")


def notify_report_site(base_url: str, report_id: str):
    body = json.dumps({"id": report_id}, ensure_ascii=False).encode("utf-8")
    request = Request(str(base_url).rstrip("/") + "/api/publish", data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    with urlopen(request, timeout=5) as response:
        if response.status >= 300:
            raise RuntimeError(f"日报网页返回 HTTP {response.status}")


def open_report_page(base_url: str, report_id: str):
    report_url = f"{str(base_url).rstrip('/')}/?report={quote(report_id, safe='')}"
    if not webbrowser.open_new_tab(report_url):
        raise RuntimeError("系统未找到可用的默认浏览器。")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(payload: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_report(report_id: str, markdown: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"{report_id}.md"
    target.write_text(markdown, encoding="utf-8")
    return target


def normalize_schedules(config: dict) -> dict[str, str]:
    supplied = config.get("schedules")
    schedules = supplied if isinstance(supplied, dict) else {"morning": config.get("scheduled_time", "09:20"), "afternoon": "14:30"}
    result = {}
    for session in ("morning", "afternoon"):
        value = str(schedules.get(session, "09:20" if session == "morning" else "14:30"))
        parse_hhmm(value)
        result[session] = value
    return result


def session_for_now(config: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(CHINA_TZ)
    schedules = normalize_schedules(config)
    afternoon_hour, afternoon_minute = parse_hhmm(schedules["afternoon"])
    boundary = now.replace(hour=afternoon_hour, minute=afternoon_minute, second=0, microsecond=0)
    return "afternoon" if now >= boundary else "morning"


def run_once(config: dict, force: bool = False, session: str = "morning") -> bool:
    if not config.get("enabled", True):
        log("推送已在配置中禁用。")
        return False
    session = str(session or "morning").strip().lower()
    if session not in {"morning", "afternoon"}:
        raise ValueError("session 必须是 morning 或 afternoon。")
    phase_name = "日报1（开盘前）" if session == "morning" else "日报2（收盘前复盘）"
    today = datetime.now(CHINA_TZ).date().isoformat()
    state = load_state()
    selected_market = str(config.get("market", "a")).strip().lower()
    if selected_market not in {"a", "hk"}:
        raise ValueError("market 必须是 a（A股）或 hk（港股）。")
    state_key = f"{today}:{selected_market}:{session}"
    completed = state.get("completed", {}) if isinstance(state.get("completed"), dict) else {}
    legacy_done = session == "morning" and state.get("last_success_date") == today
    if not force and (completed.get(state_key) or legacy_done):
        log(f"今日{phase_name}已成功生成，跳过重复推送。")
        return False
    base_url = str(config["data_service_url"]).rstrip("/")
    query = urlencode({"candidate_limit": int(config.get("candidate_limit", 5)), "market": selected_market, "session": session})
    report = get_json(f"{base_url}/v1/daily-report?{query}", timeout=300)
    if not report.get("is_trading_day"):
        log(f"今日不是交易日，不生成或推送{phase_name}。")
        return False
    markdown = str(report.get("markdown", "")).strip()
    if not markdown:
        raise RuntimeError("数据服务未返回简报正文。")
    if selected_market == "a":
        markdown = with_backtest_section(markdown)
    report_id = f"{today}-{selected_market}-{session}"
    report_path = archive_report(report_id, markdown)
    try:
        report_site_url = str(config.get("report_site_url", "http://127.0.0.1:8766"))
        notify_report_site(report_site_url, report_id)
        log(f"本地日报网页已收到{phase_name}更新通知。")
    except (HTTPError, URLError, OSError, RuntimeError) as exc:
        log(f"{phase_name}已归档，但网页即时通知失败：{exc}；页面下次刷新时仍会读取该日报。")
    if config.get("auto_open_report", False):
        try:
            open_report_page(str(config.get("report_site_url", "http://127.0.0.1:8766")), report_id)
            log(f"已在主机默认浏览器中打开{phase_name}。")
        except (OSError, RuntimeError) as exc:
            log(f"无法自动打开{phase_name}页面：{exc}")
    webhook_url = str(config.get("webhook_url", "")).strip()
    if webhook_url:
        post_webhook(webhook_url, str(config.get("webhook_type", "generic")), markdown)
        destination = "环境变量 Webhook" if os.getenv("FINANCE_AGENT_WEBHOOK_URL", "").strip() else str(config.get("webhook_type", "generic")) + " Webhook"
        log(f"{phase_name}已推送至{destination}。")
    else:
        log(f"未配置 Webhook；{phase_name}仅归档到本地。")
    completed[state_key] = {"report_path": str(report_path), "pushed": bool(webhook_url), "completed_at": datetime.now(CHINA_TZ).isoformat()}
    state.update({"completed": completed, "last_success_date": today if session == "morning" else state.get("last_success_date")})
    save_state(state)
    log(f"{phase_name}已生成：{report_path}")
    return True


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in str(hhmm).split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("推送时间必须使用 HH:MM 格式。")
    return hour, minute


def seconds_until_schedule(hhmm: str, now: datetime | None = None) -> float:
    hour, minute = parse_hhmm(hhmm)
    now = now or datetime.now(CHINA_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def next_scheduled_run(config: dict, now: datetime | None = None) -> tuple[float, str]:
    now = now or datetime.now(CHINA_TZ)
    candidates = [(seconds_until_schedule(hhmm, now), session) for session, hhmm in normalize_schedules(config).items()]
    return min(candidates, key=lambda item: item[0])


def run_daemon(config_path: Path):
    log(f"双时点日报推送服务已启动，配置：{config_path}")
    while True:
        try:
            config = load_config(config_path)
            delay, session = next_scheduled_run(config)
            log(f"下一次{session}检查将在约 {delay / 3600:.1f} 小时后执行。")
            time.sleep(delay)
            for attempt in range(1, 4):
                try:
                    run_once(load_config(config_path), session=session)
                    break
                except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                    if attempt == 3:
                        raise
                    log(f"第 {attempt} 次执行失败：{exc}；10分钟后重试。")
                    time.sleep(600)
        except KeyboardInterrupt:
            log("每日推送服务已停止。")
            return
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            log(f"连续执行失败：{exc}；60秒后重新计算下一次计划。")
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="A股/港股交易日双时点研究简报推送")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="立即检查交易日并执行一次")
    parser.add_argument("--session", choices=["morning", "afternoon"], help="指定日报1或日报2；省略时按当前时间选择")
    parser.add_argument("--force", action="store_true", help="忽略当天已成功状态，重新执行")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.once:
            run_once(config, force=args.force, session=args.session or session_for_now(config))
        else:
            run_daemon(args.config)
    except Exception as exc:
        log(f"错误：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
