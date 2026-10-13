#!/usr/bin/env python
"""Serve archived morning and afternoon stock reports on the local network."""

from __future__ import annotations

import csv
import json
import math
import sys
import mimetypes
import os
import queue
import re
import threading
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, urlencode, parse_qs
from urllib.request import Request, urlopen


sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategy_catalog import STRATEGIES
from model_checkpoints import execution_options, resolve_selection
from report_content import without_backtest_section
from data_diagnostics import readable_issues

CHINA = timezone(timedelta(hours=8))
RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"
REPORT_DIR = PROJECT_ROOT / "data" / "reports"
RANKING_FILE = PROJECT_ROOT / "data" / "factor-lab" / "latest-ranking.json"
BACKTEST_DIR = PROJECT_ROOT / "data" / "factor-lab" / "backtests"
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "report-snapshots"
HOST = os.getenv("FINANCE_REPORT_HOST", "0.0.0.0")
PORT = int(os.getenv("FINANCE_REPORT_PORT", "8766"))
REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-(?:a|hk)-(?:morning|afternoon))?\.md$")
SUBSCRIBERS: set[queue.Queue] = set()
SUBSCRIBERS_LOCK = threading.Lock()
RANKING_JOB_LOCK = threading.Lock()
ACTIVE_RANKING_JOB = ""


def ranking_job(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("排序任务标识无效。")
    path = PROJECT_ROOT / "data" / "factor-lab" / "ranking-jobs" / f"{job_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_ranking_worker(job_id, strategy, checkpoint_id=""):
    global ACTIVE_RANKING_JOB
    from factor_lab import write_json
    directory = PROJECT_ROOT / "data" / "factor-lab" / "ranking-jobs"
    try:
        with (directory / f"{job_id}.log").open("w", encoding="utf-8") as output:
            command = [sys.executable, str(PROJECT_ROOT / "scripts" / "intraday_ranking.py"),
                       "--strategy", strategy, "--job-id", job_id]
            if checkpoint_id:
                command += ["--checkpoint", checkpoint_id]
            result = subprocess.run(command,
                                    cwd=PROJECT_ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=1200)
        state = ranking_job(job_id) or {}
        if result.returncode or state.get("status") != "completed":
            if state.get("status") != "failed":
                write_json(directory / f"{job_id}.json", {**state, "status": "failed", "message": "排序进程未完成，请检查本机排序日志并重试。"})
        else:
            publish_ranking_event(state.get("result"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        state = ranking_job(job_id) or {}
        message = "全市场分钟线取数超过20分钟，任务已停止；请稍后重试或选择快照策略。" if isinstance(exc, subprocess.TimeoutExpired) else "排序进程启动失败，请检查本机环境。"
        write_json(directory / f"{job_id}.json", {**state, "status": "failed", "message": message})
    finally:
        with RANKING_JOB_LOCK:
            if ACTIVE_RANKING_JOB == job_id:
                ACTIVE_RANKING_JOB = ""


def start_ranking_job(strategy, parameters=None):
    global ACTIVE_RANKING_JOB
    from intraday_ranking import ranking_options
    from factor_lab import write_json
    if strategy not in ranking_options()["strategies"]:
        raise ValueError("所选策略或模型当前不可用，请刷新列表并检查训练状态。")
    selection_id = strategy
    strategy, checkpoint_id, _ = resolve_selection(strategy, store=PROJECT_ROOT / "data" / "factor-lab")
    from strategy_config import configured_parameters, validate_parameters
    from factor_lab import load_settings
    parameters = (configured_parameters(load_settings(), strategy) if parameters is None
                  else validate_parameters(strategy, parameters))
    with RANKING_JOB_LOCK:
        if ACTIVE_RANKING_JOB:
            return ranking_job(ACTIVE_RANKING_JOB), False
        job_id = uuid.uuid4().hex
        state = {"job_id": job_id, "strategy_id": strategy, "strategy_name": STRATEGIES[strategy]["name"],
                 "selection_id": selection_id, "checkpoint_id": checkpoint_id or None,
                 "strategy_parameters": parameters,
                 "scope": "full_market", "status": "running", "stage": "queued", "message": "已开始全市场策略部署",
                 "started_at": datetime.now(CHINA).isoformat(timespec="seconds")}
        write_json(PROJECT_ROOT / "data" / "factor-lab" / "ranking-jobs" / f"{job_id}.json", state)
        ACTIVE_RANKING_JOB = job_id
        threading.Thread(target=run_ranking_worker, args=(job_id, strategy, checkpoint_id), daemon=True).start()
        return state, True


def publish_report_event(report_id: str):
    message = json.dumps({"id": report_id}, ensure_ascii=False)
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS)
    for subscriber in subscribers:
        subscriber.put(message)


def ranking_publication(result=None):
    result = result or {}
    strategy, checkpoint = result.get("strategy_id", ""), result.get("checkpoint_id")
    message = {"type": "ranking", "scope": result.get("scope"), "id": result.get("id"),
               "selection_id": f"{strategy}@{checkpoint}" if checkpoint else strategy,
               "strategy_name": result.get("strategy_name"), "generated_at": result.get("generated_at")}
    return message


def publish_ranking_event(result=None):
    message = ranking_publication(result)
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS)
    for subscriber in subscribers:
        subscriber.put(json.dumps(message, ensure_ascii=False))


def latest_ranking(strategy="", scope=""):
    try:
        if scope == "full_market":
            strategy, checkpoint_id, _ = resolve_selection(strategy, store=PROJECT_ROOT / "data" / "factor-lab")
            suffix = f"-{checkpoint_id}" if checkpoint_id else ""
            path = PROJECT_ROOT / "data" / "factor-lab" / f"full-market-{strategy}{suffix}.json"
        else:
            path = RANKING_FILE
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"available": False}
        if scope == "full_market" and (payload.get("scope") != scope or payload.get("strategy_id") != strategy
                                       or (payload.get("checkpoint_id") or "") != checkpoint_id):
            return {"available": False}
        return (payload if isinstance(payload, dict) and (payload.get("ranking") or payload.get("available"))
                and payload.get("algorithm") in STRATEGIES else {"available": False})
    except (OSError, ValueError):
        return {"available": False}


def read_backtest(run_id: str, curve: bool = False) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("回测标识无效。")
    directory = BACKTEST_DIR / run_id
    if directory.resolve().parent != BACKTEST_DIR.resolve():
        raise ValueError("回测路径无效。")
    try:
        path = directory / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(summary, dict) or summary.get("schema_version") != 2
                or summary.get("kind") != "point_in_time_backtest"
                or "stats" not in summary or "slot" not in summary):
            return {"available": False}
        payload = {**summary, "available": True, "run_id": run_id,
                   "strategy_id": summary.get("strategy_id", "legacy"),
                   "strategy_name": summary.get("strategy_name", "历史因子选模回测"),
                   "generated_at": summary.get("generated_at") or datetime.fromtimestamp(
                       path.stat().st_mtime, CHINA).isoformat(timespec="seconds")}
        if curve:
            points = []
            initial = float(summary["stats"].get("initial_cash", 0))
            if initial > 0 and math.isfinite(initial):
                with (directory / "equity.csv").open(encoding="utf-8-sig", newline="") as handle:
                    rows = sorted(csv.DictReader(handle), key=lambda row: row["day"])
                    peak = initial
                    for row in rows:
                        value = float(row["equity"])
                        if not math.isfinite(value) or value <= 0:
                            raise ValueError("净值数据无效。")
                        peak = max(peak, value)
                        points.append({"day": row["day"], "net_value": value / initial,
                                       "drawdown": value / peak - 1})
            payload["curve"] = points
            payload["downloads"] = {name: f"/api/backtests/{run_id}/{name}"
                                    for name in ("equity.csv", "trades.csv") if (directory / name).is_file()}
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        if curve:
            payload = read_backtest(run_id)
            return {**payload, "curve": [], "curve_error": "净值文件缺失或损坏。"}
        return {"available": False}


def backtest_runs() -> list[dict]:
    try:
        reports = sorted(BACKTEST_DIR.glob("*/summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [payload for path in reports if RUN_ID.fullmatch(path.parent.name)
            and (payload := read_backtest(path.parent.name)).get("available")]


def latest_backtest():
    return next(iter(backtest_runs()), {"available": False})


def publish_backtest_event():
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS)
    for subscriber in subscribers:
        subscriber.put(json.dumps({"type": "backtest"}))


def report_metadata(report_id: str) -> dict:
    parts = report_id.split("-")
    if len(parts) == 5 and parts[3] in {"a", "hk"} and parts[4] in {"morning", "afternoon"}:
        day, market, session = "-".join(parts[:3]), parts[3], parts[4]
    else:
        day, market, session = report_id, "a", "legacy"
    market_label = "港股" if market == "hk" else "A股"
    session_label = "日报2·收盘前" if session == "afternoon" else ("日报1·开盘前" if session == "morning" else "历史日报")
    return {"id": report_id, "date": day, "market": market, "session": session, "label": f"{day} · {market_label}{session_label}"}


def list_reports():
    if not REPORT_DIR.exists():
        return []
    reports = []
    for path in REPORT_DIR.glob("*.md"):
        if not REPORT_NAME.fullmatch(path.name):
            continue
        stat = path.stat()
        reports.append({
            **report_metadata(path.stem),
            "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "size": stat.st_size,
        })
    session_order = {"afternoon": 2, "morning": 1, "legacy": 0}
    return sorted(reports, key=lambda item: (item["date"], session_order.get(item["session"], 0), item["updated_at"]), reverse=True)


def read_report(filename: str):
    if not REPORT_NAME.fullmatch(filename):
        raise ValueError("日报文件名无效。")
    path = REPORT_DIR / filename
    if not path.exists():
        return None
    stat = path.stat()
    snapshot = {}
    try:
        snapshot = json.loads((SNAPSHOT_DIR / f"{path.stem}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if not isinstance(snapshot, dict):
        snapshot = {}
    # Do not expose private portfolio fields from historical snapshots.
    view = {key: snapshot.get(key) for key in (
        "global_markets", "sectors", "market_panorama", "horizon_recommendations",
        "intraday_review", "candidates", "weekly_events", "warnings",
    ) if key in snapshot}
    return {
        "available": True, **report_metadata(path.stem),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "markdown": without_backtest_section(path.read_text(encoding="utf-8")), "view": view,
    }


class ReportHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def send_bytes(self, status: int, body: bytes, content_type: str, cache_control: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict | list):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/health":
                return self.send_json(200, {"status": "ok", "report_count": len(list_reports())})
            if path == "/api/reports":
                return self.send_json(200, {"reports": list_reports()})
            if path == "/api/ranking/latest":
                query = parse_qs(urlparse(self.path).query)
                if query.get("scope", [""])[0] not in {"", "full_market"}:
                    return self.send_json(400, {"error": "排序范围或策略参数无效。"})
                return self.send_json(200, latest_ranking(query.get("strategy", [""])[0], query.get("scope", [""])[0]))
            if path == "/api/ranking/strategies":
                from intraday_ranking import ranking_options
                return self.send_json(200, ranking_options())
            if path == "/api/ranking/update":
                result = latest_ranking()
                return self.send_json(200, {"available": bool(result.get("available") and result.get("scope") == "full_market"),
                                            **ranking_publication(result)})
            if path == "/api/ranking/active":
                return self.send_json(200, ranking_job(ACTIVE_RANKING_JOB) if ACTIVE_RANKING_JOB else {"available": False})
            if path.startswith("/api/ranking/jobs/"):
                state = ranking_job(path.removeprefix("/api/ranking/jobs/"))
                return self.send_json(200, state) if state else self.send_json(404, {"error": "未找到排序任务。"})
            if path == "/api/portfolio-strategies":
                try:
                    settings = json.loads((PROJECT_ROOT / "config" / "factor-lab.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    settings = {}
                selected = settings.get("strategy", "xgboost") if isinstance(settings, dict) else "xgboost"
                return self.send_json(200, execution_options(PROJECT_ROOT / "data" / "factor-lab", settings))
            if path == "/api/backtest/latest":
                return self.send_json(200, latest_backtest())
            if path == "/api/backtests":
                return self.send_json(200, {"runs": backtest_runs(), "strategies": STRATEGIES})
            if path.startswith("/api/backtests/"):
                parts = path.removeprefix("/api/backtests/").split("/")
                if not RUN_ID.fullmatch(parts[0]):
                    return self.send_json(400, {"error": "回测标识无效。"})
                report = read_backtest(parts[0], curve=len(parts) == 1)
                if not report.get("available"):
                    return self.send_json(404, {"error": "未找到有效回测。"})
                if len(parts) == 1:
                    return self.send_json(200, report)
                if len(parts) == 2 and parts[1] in {"equity.csv", "trades.csv"}:
                    target = BACKTEST_DIR / parts[0] / parts[1]
                    if not target.is_file():
                        return self.send_json(404, {"error": "回测数据文件缺失。"})
                    return self.send_bytes(200, target.read_bytes(), "text/csv; charset=utf-8")
                return self.send_json(404, {"error": "Not found"})
            if path == "/api/events":
                return self.stream_events()
            if path == "/api/reports/latest":
                reports = list_reports()
                if not reports:
                    return self.send_json(200, {"available": False, "markdown": ""})
                return self.send_json(200, read_report(reports[0]["id"] + ".md"))
            if path.startswith("/api/reports/"):
                filename = path.removeprefix("/api/reports/") + ".md"
                report = read_report(filename)
                return self.send_json(200, report) if report else self.send_json(404, {"available": False, "error": "未找到该日报。"})

            if path in ("/backtests", "/backtests/"):
                asset = "backtests.html"
            else:
                asset = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            if asset not in {"index.html", "styles.css", "app.js", "report-visuals.js", "backtests.html", "backtests.js"}:
                return self.send_json(404, {"error": "Not found"})
            target = WEB_ROOT / asset
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self.send_bytes(200, target.read_bytes(), content_type, "no-cache")
        except (OSError, ValueError) as exc:
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/ranking/start":
            origin, host = self.headers.get("Origin", ""), self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self.send_json(403, {"error": "同源页面才可发起盘中排序。"})
            if self.headers.get("X-Report-Market", "a") != "a":
                return self.send_json(400, {"error": "当前盘中交易策略只支持沪深 A 股。"})
            strategy = self.headers.get("X-Ranking-Strategy", "")
            try:
                resolve_selection(strategy, store=PROJECT_ROOT / "data" / "factor-lab")
                parameters = json.loads(self.headers.get("X-Strategy-Parameters", "null"))
                state, created = start_ranking_job(strategy, parameters) if parameters is not None else start_ranking_job(strategy)
                return self.send_json(202 if created else 409, state)
            except (OSError, ValueError) as exc:
                return self.send_json(400, {"error": str(exc)})
        if path == "/api/portfolio-analysis":
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self.send_json(403, {"error": "同源页面才可请求持仓分析。"})
            market = str(self.headers.get("X-Report-Market", "a"))
            if market not in {"a", "hk"}:
                return self.send_json(400, {"error": "市场参数无效。"})
            strategy = str(self.headers.get("X-Portfolio-Strategy", ""))
            try:
                if strategy:
                    resolve_selection(strategy, store=PROJECT_ROOT / "data" / "factor-lab")
                parameters = json.loads(self.headers.get("X-Strategy-Parameters", "null"))
                if parameters is not None:
                    from strategy_config import validate_parameters
                    algorithm, _, _ = resolve_selection(strategy, store=PROJECT_ROOT / "data" / "factor-lab")
                    parameters = validate_parameters(algorithm, parameters)
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            try:
                query = urlencode({"market": market, "strategy": strategy, "parameters": json.dumps(parameters)})
                request = Request(f"http://127.0.0.1:8765/v1/portfolio-analysis?{query}",
                                  headers={"Accept": "application/json"})
                with urlopen(request, timeout=180) as response:
                    return self.send_json(200, json.loads(response.read().decode("utf-8")))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return self.send_json(502, {"error": readable_issues([f"持仓分析不可用：{exc}"])[0]})
        if path == "/api/publish-backtest":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return self.send_json(403, {"error": "Local publishing only"})
            publish_backtest_event()
            return self.send_json(200, {"published": True})
        if path == "/api/publish-ranking":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return self.send_json(403, {"error": "Local publishing only"})
            result = latest_ranking()
            if result.get("available") is False:
                return self.send_json(400, {"error": "No ranking is available"})
            publish_ranking_event(result)
            return self.send_json(200, {"published": True})
        if path != "/api/publish":
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000:
                raise ValueError("请求体大小无效。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            report_id = str(payload.get("id") or payload.get("date") or "")
            if not REPORT_NAME.fullmatch(report_id + ".md") or not (REPORT_DIR / f"{report_id}.md").exists():
                raise ValueError("日报标识无效或文件不存在。")
            publish_report_event(report_id)
            self.send_json(200, {"published": True, "id": report_id})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def stream_events(self):
        subscriber: queue.Queue = queue.Queue()
        with SUBSCRIBERS_LOCK:
            SUBSCRIBERS.add(subscriber)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(b"event: ready\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = subscriber.get(timeout=20)
                    event_type = json.loads(message).get("type")
                    event_name = event_type if event_type in {"ranking", "backtest"} else "report"
                    chunk = f"event: {event_name}\ndata: {message}\n\n".encode("utf-8")
                except queue.Empty:
                    chunk = b": keep-alive\n\n"
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.discard(subscriber)


if __name__ == "__main__":
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Stock report site: http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), ReportHandler).serve_forever()
