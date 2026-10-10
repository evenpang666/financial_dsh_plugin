#!/usr/bin/env python
"""Serve archived morning and afternoon stock reports on the local network."""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


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


def publish_report_event(report_id: str):
    message = json.dumps({"id": report_id}, ensure_ascii=False)
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS)
    for subscriber in subscribers:
        subscriber.put(message)


def publish_ranking_event():
    with SUBSCRIBERS_LOCK:
        subscribers = list(SUBSCRIBERS)
    for subscriber in subscribers:
        subscriber.put(json.dumps({"type": "ranking"}))


def latest_ranking():
    try:
        payload = json.loads(RANKING_FILE.read_text(encoding="utf-8"))
        return (payload if isinstance(payload, dict) and payload.get("ranking")
                and payload.get("algorithm") in {"xgboost", "elastic_net"} else {"available": False})
    except (OSError, json.JSONDecodeError):
        return {"available": False}


def latest_backtest():
    try:
        reports = sorted(BACKTEST_DIR.glob("*/summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return {"available": False}
    for path in reports:
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(summary, dict) or "stats" not in summary or "slot" not in summary:
                continue
            return {"available": True, "model_id": path.parent.name,
                    "generated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    **summary}
        except (OSError, json.JSONDecodeError):
            continue
    return {"available": False}


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
    view = {key: snapshot.get(key) for key in ("global_markets", "sectors", "market_panorama", "horizon_recommendations", "intraday_review") if key in snapshot}
    return {
        "available": True, **report_metadata(path.stem),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "markdown": path.read_text(encoding="utf-8"), "view": view,
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
                return self.send_json(200, latest_ranking())
            if path == "/api/backtest/latest":
                return self.send_json(200, latest_backtest())
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

            asset = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            if asset not in {"index.html", "styles.css", "app.js"}:
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
        if path == "/api/portfolio-analysis":
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self.send_json(403, {"error": "同源页面才可请求持仓分析。"})
            market = str(self.headers.get("X-Report-Market", "a"))
            if market not in {"a", "hk"}:
                return self.send_json(400, {"error": "市场参数无效。"})
            try:
                request = Request(f"http://127.0.0.1:8765/v1/portfolio-analysis?market={market}",
                                  headers={"Accept": "application/json"})
                with urlopen(request, timeout=180) as response:
                    return self.send_json(200, json.loads(response.read().decode("utf-8")))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return self.send_json(502, {"error": f"持仓分析暂不可用：{exc}"})
        if path == "/api/publish-ranking":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return self.send_json(403, {"error": "Local publishing only"})
            if not latest_ranking().get("ranking"):
                return self.send_json(400, {"error": "No ranking is available"})
            publish_ranking_event()
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
                    event_name = "ranking" if json.loads(message).get("type") == "ranking" else "report"
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
