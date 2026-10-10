#!/usr/bin/env python3
"""User-session service manager for the Linux control panel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "linux-services"
PYTHON = ROOT / ".venv" / "bin" / "python"
LOCAL_BIN = ROOT / ".local" / "node_modules" / ".bin"
SERVICES = {
    "data": ([str(PYTHON), str(ROOT / "scripts" / "stock_data_server.py")], 8765),
    "report": ([str(PYTHON), str(ROOT / "scripts" / "report_web_server.py")], 8766),
    "daily": ([str(PYTHON), str(ROOT / "scripts" / "daily_push.py")], None),
    "factor": ([str(PYTHON), str(ROOT / "scripts" / "factor_scheduler.py")], None),
    "dsh": ([], 3080),
}


def emit(value: str):
    print(value, flush=True)


def runtime_env() -> dict:
    environment = os.environ.copy()
    environment["PATH"] = str(LOCAL_BIN) + os.pathsep + environment.get("PATH", "")
    return environment


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def process_start(pid: int) -> str | None:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().split()
        return fields[21] if fields[2] != "Z" else None
    except (OSError, IndexError):
        return None


def owned_process(name: str) -> dict | None:
    try:
        info = json.loads((STATE / f"{name}.json").read_text())
        pid = int(info["pid"])
        if process_start(pid) == info["start"]:
            return info
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def dsh_path() -> str | None:
    return shutil.which("dsh", path=runtime_env()["PATH"])


def start(name: str):
    if owned_process(name):
        emit(f"{name}: already running")
        return
    if not PYTHON.exists():
        raise RuntimeError("Python virtual environment is missing. Run ./control_panel.sh first.")
    args, port = SERVICES[name]
    if port and port_listening(port):
        raise RuntimeError(f"Port {port} is already occupied by a process not managed by this project.")
    if name == "dsh":
        command = dsh_path()
        if not command:
            raise RuntimeError("dsh is missing. Run ./control_panel.sh first.")
        args = [command, "web", "--no-open"]
    STATE.mkdir(parents=True, exist_ok=True)
    stdout = (STATE / f"{name}.stdout.log").open("a", encoding="utf-8")
    stderr = (STATE / f"{name}.stderr.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(args, cwd=ROOT, env=runtime_env(), stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
    finally:
        stdout.close()
        stderr.close()
    start_id = process_start(process.pid)
    if not start_id:
        raise RuntimeError(f"{name} exited immediately; see data/linux-services/{name}.stderr.log")
    (STATE / f"{name}.json").write_text(json.dumps({"pid": process.pid, "start": start_id}))
    if port:
        for _ in range(60):
            if port_listening(port):
                emit(f"{name}: running on port {port}")
                return
            if process.poll() is not None:
                break
            time.sleep(0.5)
        stop(name)
        raise RuntimeError(f"{name} did not start on port {port}; see data/linux-services/{name}.stderr.log")
    emit(f"{name}: running (PID {process.pid})")


def stop(name: str):
    info = owned_process(name)
    if not info:
        emit(f"{name}: already stopped")
    else:
        pid = int(info["pid"])
        try:
            os.killpg(pid, signal.SIGTERM)
            emit(f"{name}: stopped")
        except ProcessLookupError:
            pass
    (STATE / f"{name}.json").unlink(missing_ok=True)


def status() -> dict:
    running = {name: bool(owned_process(name)) and (port is None or port_listening(port))
               for name, (_, port) in SERVICES.items()}
    manifest = Path.home() / ".dsh" / "profiles" / "web" / "package.json"
    try:
        plugin = "dsh-finance-agent" in json.loads(manifest.read_text()).get("dependencies", {})
    except (OSError, ValueError, AttributeError):
        plugin = False
    return {"agent_task": "Ready" if running["daily"] else "Disabled",
            "factor_task": "Ready" if running["factor"] else "Disabled", "data_service": running["data"],
            "report_site": running["report"], "dsh_web": running["dsh"],
            "node_available": bool(shutil.which("node")), "python_available": bool(shutil.which("python3")),
            "git_available": bool(shutil.which("git")), "git_checkout": (ROOT / ".git").exists(),
            "venv_ready": PYTHON.exists(), "dsh_available": bool(dsh_path()), "plugin_installed": plugin}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("EnableAgent", "EnableDshWeb", "DisableAgent", "ClosePanel", "DisableDshWeb", "Status"))
    action = parser.parse_args().action
    try:
        if action == "EnableAgent":
            config = ROOT / "config" / "daily-push.json"
            if not config.exists():
                config.write_bytes((ROOT / "config" / "daily-push.example.json").read_bytes())
            for name in ("data", "report", "daily", "factor"):
                start(name)
        elif action == "EnableDshWeb":
            start("data")
            start("dsh")
        elif action == "DisableDshWeb":
            stop("dsh")
            if not owned_process("daily"):
                stop("data")
        elif action == "DisableAgent":
            for name in ("factor", "daily", "report"):
                stop(name)
            if not owned_process("dsh"):
                stop("data")
        elif action == "ClosePanel":
            for name in ("daily", "report"):
                stop(name)
            if not owned_process("dsh"):
                stop("data")
        else:
            for line in ("[Check 1/4] Checking user-session services...", "[Check 2/4] Checking ports...",
                         "[Check 3/4] Checking Node.js, Python, Git and .venv...", "[Check 4/4] Checking DSH plugin..."):
                emit(line)
            emit(json.dumps(status(), ensure_ascii=False))
    except Exception as exc:
        emit("Error: " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
