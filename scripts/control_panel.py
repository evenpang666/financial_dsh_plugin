#!/usr/bin/env python
"""Compact Windows/Linux control panel for the daily agent and factor lab."""

from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk


sys.path.insert(0, str(Path(__file__).resolve().parent))
from strategy_catalog import MODEL_STRATEGIES, STRATEGIES

STRATEGY_LABELS = {value["name"]: key for key, value in STRATEGIES.items()}


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_SCRIPT = PROJECT_ROOT / "scripts" / "agent-service.ps1"
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
CONTROL_PANEL_LAUNCHER = PROJECT_ROOT / ("control_panel.cmd" if sys.platform == "win32" else "control_panel.sh")
FACTOR_SCRIPT = PROJECT_ROOT / "scripts" / "factor_lab.py"
FACTOR_STORE = PROJECT_ROOT / "data" / "factor-lab"
FACTOR_MODEL = FACTOR_STORE / "model.json"
FACTOR_LATEST = FACTOR_STORE / "latest-ranking.json"
FACTOR_BACKTESTS = FACTOR_STORE / "backtests"
FACTOR_PREP_SCRIPT = PROJECT_ROOT / "scripts" / "factor_data_prep.py"
FACTOR_UNIVERSE = FACTOR_STORE / "universe-history.csv"
FACTOR_MARKET_STATE = FACTOR_STORE / "market-state.csv"
FACTOR_MANIFEST = FACTOR_STORE / "source-manifest.json"
FACTOR_SETTINGS = PROJECT_ROOT / "config" / "factor-lab.json"
DAILY_REPORT_SCRIPT = PROJECT_ROOT / "scripts" / "daily_push.py"
FACTOR_TARGET_VERSION = 3
FACTOR_SLOTS = ("09:35", "10:30", "11:30", "13:30", "14:00", "14:30", "14:50")
LINUX_SERVICE_SCRIPT = PROJECT_ROOT / "scripts" / "linux_service.py"
LINUX_UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "linux_update.sh"
WINDOWS_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

WINDOWS_CJK_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei")
LINUX_CJK_FONTS = (
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
    "Noto Sans SC", "Noto Sans Mono CJK SC", "Droid Sans Fallback",
)


def installed_font(available: tuple[str, ...], preferred: tuple[str, ...]) -> str | None:
    """Choose an installed font instead of letting Tk substitute a missing family."""
    by_name = {name.casefold(): name for name in available}
    return next((by_name[name.casefold()] for name in preferred if name.casefold() in by_name), None)


def linux_system_font(pattern: str) -> str | None:
    """Read the actual installed family selected by the user's fontconfig settings."""
    if not shutil.which("fc-match"):
        return None
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}\n", pattern],
            capture_output=True, text=True, check=True, timeout=3,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.splitlines()[0].split(",")[0].strip() if result.stdout.strip() else None


def linux_installed_fonts() -> tuple[str, ...]:
    """List Chinese capable families in fontconfig, independently of Tk aliases."""
    if not shutil.which("fc-list"):
        return ()
    try:
        result = subprocess.run(
            ["fc-list", "-f", "%{family}\n", ":lang=zh-cn"],
            capture_output=True, text=True, check=True, timeout=3,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ()
    return tuple(name.strip() for line in result.stdout.splitlines()
                 for name in line.split(",") if name.strip())


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def backtest_data_missing() -> bool:
    """Check whether the validated local inputs needed by backtest exist."""
    if not FACTOR_UNIVERSE.is_file() or not FACTOR_MARKET_STATE.is_file():
        return True
    manifest = read_json(FACTOR_MANIFEST)
    files = manifest.get("files")
    if (manifest.get("validated") is not True
            or manifest.get("feature_warmup_validated") is not True
            or manifest.get("data_schema_version") != 2
            or manifest.get("source_scope") != "historical_vendor_reconstruction"
            or not isinstance(files, dict)
            or set(files) != {FACTOR_UNIVERSE.name, FACTOR_MARKET_STATE.name}):
        return True
    def matches(path, digest):
        if not isinstance(digest, str):
            return False
        try:
            value = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    value.update(chunk)
            return value.hexdigest() == digest
        except OSError:
            return False
    if any(not matches(FACTOR_STORE / name, digest) for name, digest in files.items()):
        return True
    bars = manifest.get("bars")
    return not isinstance(bars, dict) or not bars or any(
        not matches(FACTOR_STORE / "bars" / f"{symbol}.csv", digest) for symbol, digest in bars.items()
    )


def backtest_preparation_dates(today: datetime | None = None) -> tuple[str, str]:
    """End at the last completed weekday and keep over 140 trading days."""
    today = today or datetime.now(timezone(timedelta(hours=8)))
    end = today.date() - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start = end - timedelta(days=400)
    return start.isoformat(), end.isoformat()


def detect_lan_ip() -> str | None:
    """Return the preferred private IPv4 address from the current routing table."""
    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for value in candidates:
        try:
            address = ipaddress.ip_address(value)
            if address.version == 4 and address.is_private and not address.is_loopback:
                return value
        except ValueError:
            continue
    return None


def stream_command(args: list[str], on_output=None, on_process=None, *, timeout: float | None = None) -> str:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    # A background service can inherit the launcher's stdout on Windows. A pipe
    # then never reaches EOF, even after the launcher exits. Follow a file and
    # the launcher's lifetime instead, so descendants cannot hold up the UI.
    output_file = tempfile.NamedTemporaryFile(mode="w+b", delete=False)
    process = None
    try:
        process = subprocess.Popen(
            args,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=WINDOWS_CREATION_FLAGS if sys.platform == "win32" else 0,
        )
        if on_process:
            on_process(process)
        started = time.monotonic()
        lines = []
        pending = b""

        def collect(source, final=False):
            nonlocal pending
            pending += source.read()
            parts = pending.split(b"\n")
            pending = parts.pop()
            if final and pending:
                parts.append(pending)
                pending = b""
            for raw_line in parts:
                line = ANSI_ESCAPE.sub("", raw_line.decode("utf-8", errors="replace").rstrip())
                if not line:
                    continue
                lines.append(line)
                if on_output:
                    on_output(line)

        with open(output_file.name, "rb") as source:
            while process.poll() is None:
                collect(source)
                if timeout is not None and time.monotonic() - started >= timeout:
                    process.kill()
                    process.wait(timeout=5)
                    collect(source, final=True)
                    detail = "\n".join(lines[-40:])
                    raise RuntimeError(f"操作超时（{timeout:g} 秒），已结束当前命令。" + (f"\n{detail}" if detail else ""))
                time.sleep(0.05)
            collect(source, final=True)
        return_code = process.wait()
        output = "\n".join(lines).strip()
        if return_code:
            detail = "\n".join(lines[-40:]).strip()
            raise RuntimeError(detail or f"操作失败（退出码 {return_code}）。")
        return output
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        output_file.close()
        try:
            os.unlink(output_file.name)
        except OSError:
            # An unexpected descendant may still hold the log handle on Windows.
            pass
        if on_process:
            on_process(None)


def service_command(action: str, on_output=None, on_process=None) -> str:
    if sys.platform == "win32":
        args = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SERVICE_SCRIPT), "-Action", action]
    elif action == "Update":
        args = ["bash", str(LINUX_UPDATE_SCRIPT)]
    else:
        args = [str(VENV_PYTHON), str(LINUX_SERVICE_SCRIPT), action]
    timeout = {"Status": 20, "EnableAgent": 90, "EnableDshWeb": 60,
               "DisableAgent": 60, "DisableDshWeb": 60, "ClosePanel": 60}.get(action)
    return stream_command(args, on_output, on_process, timeout=timeout)


def service_label(action: str) -> str:
    return (f"powershell.exe agent-service.ps1 -Action {action}" if sys.platform == "win32"
            else ("bash linux_update.sh" if action == "Update" else f"python linux_service.py {action}"))


class FlowFrame(tk.Frame):
    """Wrap controls using their measured Tk sizes, including DPI/font scaling."""

    def __init__(self, parent, *, gap=8, **kwargs):
        super().__init__(parent, **kwargs)
        self.gap = gap
        self.items = []
        self.columns = 0
        self.pending_layout = None
        self.bind("<Configure>", self.schedule_layout)

    def add(self, widget):
        self.items.append(widget)
        self.schedule_layout()
        return widget

    def schedule_layout(self, event=None):
        if self.pending_layout is None:
            self.pending_layout = self.after_idle(self.reflow)

    def reflow(self):
        self.pending_layout = None
        if not self.items:
            return
        cell_width = max(widget.winfo_reqwidth() for widget in self.items)
        columns = max(1, min(len(self.items), (self.winfo_width() + self.gap) // (cell_width + self.gap)))
        if columns == self.columns:
            return
        for column in range(max(self.columns, columns)):
            self.grid_columnconfigure(column, weight=1 if column < columns else 0, uniform="flow" if column < columns else "")
        self.columns = columns
        for index, widget in enumerate(self.items):
            row, column = divmod(index, columns)
            widget.grid(row=row, column=column, sticky="ew",
                        padx=(0, self.gap if column < columns - 1 else 0),
                        pady=(0, self.gap if index + columns < len(self.items) else 0))


class ControlPanel(tk.Tk):
    BG = "#0a1020"
    PANEL = "#111a2c"
    PANEL_ALT = "#162238"
    LINE = "#263552"
    TEXT = "#f5f7ff"
    MUTED = "#98a7be"
    ACCENT = "#6d8cff"
    GREEN = "#5bd6a2"

    def __init__(self):
        super().__init__()
        self.title("财务研究智能体")
        width = min(1000, max(320, self.winfo_screenwidth() - 80))
        height = min(900, max(320, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(520, width), min(480, height))
        self.resizable(True, True)
        self.configure(bg=self.BG)
        default_font = tkfont.nametofont("TkDefaultFont", root=self).actual("family")
        if sys.platform == "win32":
            available_fonts = tuple(tkfont.families(self))
            self.ui_font = installed_font(available_fonts, WINDOWS_CJK_FONTS) or default_font
            self.log_font = installed_font(available_fonts, ("Cascadia Mono",)) or self.ui_font
        else:
            available_fonts = linux_installed_fonts()
            self.ui_font = installed_font(available_fonts, LINUX_CJK_FONTS) or linux_system_font("sans-serif:lang=zh-cn") or default_font
            self.log_font = installed_font(available_fonts, ("Noto Sans Mono CJK SC", "Source Han Mono SC")) or self.ui_font
        self._fonts: dict[tuple[int, bool, bool], tkfont.Font] = {}
        self.option_add("*Font", self._font(9))
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont"):
            try:
                tkfont.nametofont(name, root=self).configure(family=self.ui_font)
            except tk.TclError:
                pass
        self.status_text = tk.StringVar(value="正在读取运行状态…")
        self.operation_text = tk.StringVar(value="空闲")
        self.environment_text = tk.StringVar(value="环境检测中…")
        lan_ip = detect_lan_ip()
        self.lan_report_text = tk.StringVar(
            value=f"其他设备可访问日报：http://{lan_ip}:8766（同一局域网）"
            if lan_ip else "其他设备日报地址：未识别到局域网 IPv4"
        )
        self.service_vars = {name: tk.StringVar(value="检测中") for name in ("日报任务", "数据服务", "日报页面", "DSH Web")}
        self.action_buttons: list[ttk.Button] = []
        self.web_buttons: list[ttk.Button] = []
        self.utility_buttons: list[ttk.Button | tk.Button] = []
        self.factor_slot_box: ttk.Combobox | None = None
        self.strategy_box: ttk.Combobox | None = None
        self.factor_train_button: ttk.Button | None = None
        self.busy = False
        self.factor_busy = False
        self.report_busy = False
        self.status_checking = False
        self.operation_revision = 0
        self.starting_services = False
        self.startup_thread: threading.Thread | None = None
        self.startup_process: subprocess.Popen | None = None
        self.startup_process_lock = threading.Lock()
        self.startup_cancelled = False
        self.closing = False
        self.last_status_summary = ""
        self.last_status_error = ""
        self.factor_process: subprocess.Popen | None = None
        self.factor_status = tk.StringVar(value="因子模型：未训练")
        self.factor_schedule_status = tk.StringVar(value="每周训练：状态检测中")
        self.factor_slot = tk.StringVar(value="14:00")
        self.strategy_choice = tk.StringVar(value=STRATEGIES["xgboost"]["name"])
        self.strategy_description = tk.StringVar(value=STRATEGIES["xgboost"]["description"])
        self._build_styles()
        self._build()
        self.update_factor_status()
        self.protocol("WM_DELETE_WINDOW", self.close_panel)
        if VENV_PYTHON.exists():
            self.append_log("控制台已启动，正在静默启用日报智能体与 DSH Web。")
            self.after(150, self.start_all_services)
        else:
            self.status_text.set("首次使用：未检测到虚拟环境，请点击“安装 / 修复”。")
            self.operation_text.set("等待安装")
            self.append_log("未检测到 .venv；控制台已就绪。请点击“安装 / 修复”完成首次安装。")
            self.after(150, self.refresh_status)
        self.after(6000, self.periodic_refresh)

    def _font(self, size: int, bold: bool = False, mono: bool = False) -> tkfont.Font:
        key = (size, bold, mono)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(
                root=self, family=self.log_font if mono else self.ui_font,
                size=size, weight="bold" if bold else "normal",
            )
        return self._fonts[key]

    def _build_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Primary.TButton", background=self.ACCENT, foreground="#ffffff", borderwidth=0, padding=(14, 8), font=self._font(9, bold=True))
        style.map("Primary.TButton", background=[("active", "#86a0ff"), ("disabled", "#40506e")], foreground=[("disabled", "#9ba7bb")])
        style.configure("Secondary.TButton", background="#243451", foreground="#e9efff", borderwidth=0, padding=(14, 8))
        style.map("Secondary.TButton", background=[("active", "#324768"), ("disabled", "#1c283e")], foreground=[("disabled", "#77849a")])

    def _wrapped_label(self, parent, **kwargs):
        label = tk.Label(parent, width=1, justify="left", anchor="w", **kwargs)
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width - 2)))
        return label

    def _section(self, parent, title):
        frame = tk.Frame(parent, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1)
        tk.Label(frame, text=title, bg=self.PANEL, fg=self.TEXT, font=self._font(11, bold=True)).pack(
            fill="x", padx=16, pady=(14, 8), anchor="w")
        return frame

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.header = tk.Frame(self, bg="#0e1729")
        self.header.grid(row=0, column=0, sticky="ew")
        self.title_box = tk.Frame(self.header, bg="#0e1729")
        tk.Label(self.title_box, text="FINANCE AGENT", bg="#0e1729", fg="#91a8ff", font=self._font(8, bold=True)).pack(anchor="w")
        tk.Label(self.title_box, text="财务研究智能体", bg="#0e1729", fg=self.TEXT, font=self._font(18, bold=True)).pack(anchor="w")
        self.operation_box = tk.Frame(self.header, bg=self.PANEL_ALT, highlightbackground=self.LINE, highlightthickness=1)
        tk.Label(self.operation_box, text="当前操作", bg=self.PANEL_ALT, fg=self.MUTED, font=self._font(8)).pack(anchor="w", padx=10, pady=(7, 0))
        self._wrapped_label(self.operation_box, textvariable=self.operation_text, bg=self.PANEL_ALT,
                            fg=self.GREEN, font=self._font(9, bold=True)).pack(fill="x", padx=10, pady=(0, 7))

        viewport = tk.Frame(self, bg=self.BG)
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.grid_columnconfigure(0, weight=1)
        viewport.grid_rowconfigure(0, weight=1)
        self.content_canvas = tk.Canvas(viewport, bg=self.BG, highlightthickness=0, yscrollincrement=24)
        self.content_canvas.grid(row=0, column=0, sticky="nsew")
        self.content_scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.content_canvas.yview)
        self.content_scrollbar.grid(row=0, column=1, sticky="ns")
        self.content_canvas.configure(yscrollcommand=self.content_scrollbar.set)
        self.body = tk.Frame(self.content_canvas, bg=self.BG)
        self.body_window = self.content_canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._main_columns = 0
        self._chrome_compact = None
        self._pending_body_sync = None
        self.content_canvas.bind("<Configure>", self._resize_viewport)
        self.body.bind("<Configure>", self._schedule_body_sync)
        self.bind("<Configure>", self._resize_chrome)
        self.bind("<MouseWheel>", self._scroll_content)
        self.bind("<Button-4>", self._scroll_content)
        self.bind("<Button-5>", self._scroll_content)
        self.bind("<FocusIn>", self._reveal_control)

        self.status_heading = tk.Label(self.body, text="运行状态", bg=self.BG, fg=self.MUTED, font=self._font(9, bold=True))
        self.status_grid = FlowFrame(self.body, bg=self.BG)
        for name, value in self.service_vars.items():
            tile = tk.Frame(self.status_grid, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1)
            tk.Label(tile, text=name, bg=self.PANEL, fg=self.MUTED, font=self._font(8)).pack(anchor="w", padx=12, pady=(9, 1))
            tk.Label(tile, textvariable=value, bg=self.PANEL, fg=self.TEXT, font=self._font(10, bold=True)).pack(anchor="w", padx=12, pady=(0, 9))
            self.status_grid.add(tile)

        self.access_section = self._section(self.body, "网页入口")
        self._wrapped_label(self.access_section, text="服务随控制台启停；关闭网页后可在这里重新打开",
                            bg=self.PANEL, fg=self.MUTED).pack(fill="x", padx=16)
        self._wrapped_label(self.access_section, textvariable=self.lan_report_text, bg=self.PANEL,
                            fg="#7f95bd", font=self._font(8)).pack(fill="x", padx=16, pady=(4, 12))
        self.access_buttons = FlowFrame(self.access_section, bg=self.PANEL)
        self.access_buttons.pack(fill="x", padx=16, pady=(0, 12))
        self.access_buttons.add(self._button(self.access_buttons, "打开 DSH Web", "Primary.TButton", self.open_dsh_web, web=True))
        self.access_buttons.add(self._button(self.access_buttons, "打开日报", "Secondary.TButton",
                                            lambda: self.open_url("http://127.0.0.1:8766", "日报页面"), web=True))
        self.access_buttons.add(self._button(self.access_buttons, "打开回测", "Secondary.TButton", self.open_backtests, web=True))
        self.manual_report_button = self._button(self.access_buttons, "手动生成日报", "Secondary.TButton", self.run_manual_report)
        self.access_buttons.add(self.manual_report_button)
        self._wrapped_label(self.access_section, text="手动生成按当前时间更新日报1或日报2。", bg=self.PANEL,
                            fg=self.MUTED, font=self._font(8)).pack(fill="x", padx=16, pady=(0, 14))

        self.factor_section = self._section(self.body, "策略研究 · A 股")
        for variable, color in ((self.factor_status, self.MUTED), (self.factor_schedule_status, "#7f95bd"), (self.strategy_description, self.MUTED)):
            self._wrapped_label(self.factor_section, textvariable=variable, bg=self.PANEL, fg=color,
                                font=self._font(8)).pack(fill="x", padx=16, pady=(0, 6))
        self.factor_fields = FlowFrame(self.factor_section, gap=12, bg=self.PANEL)
        self.factor_fields.pack(fill="x", padx=16, pady=(4, 12))
        strategy_field = tk.Frame(self.factor_fields, bg=self.PANEL)
        tk.Label(strategy_field, text="策略 / 模型", bg=self.PANEL, fg=self.MUTED).pack(anchor="w", pady=(0, 4))
        self.strategy_box = ttk.Combobox(strategy_field, textvariable=self.strategy_choice,
                                        values=tuple(STRATEGY_LABELS), width=22, state="readonly")
        self.strategy_box.pack(fill="x")
        self.strategy_box.bind("<<ComboboxSelected>>", self.save_strategy)
        self.factor_fields.add(strategy_field)
        slot_field = tk.Frame(self.factor_fields, bg=self.PANEL)
        tk.Label(slot_field, text="信号时点", bg=self.PANEL, fg=self.MUTED).pack(anchor="w", pady=(0, 4))
        self.factor_slot_box = ttk.Combobox(slot_field, textvariable=self.factor_slot, values=FACTOR_SLOTS,
                                          width=7, state="readonly")
        self.factor_slot_box.pack(fill="x")
        self.factor_slot_box.bind("<<ComboboxSelected>>", self.save_factor_slot)
        self.factor_fields.add(slot_field)
        self.factor_buttons = FlowFrame(self.factor_section, bg=self.PANEL)
        self.factor_buttons.pack(fill="x", padx=16, pady=(0, 12))
        self.factor_train_button = self._button(self.factor_buttons, "一键训练", "Secondary.TButton", lambda: self.run_factor_action("train", "训练中"))
        self.factor_buttons.add(self.factor_train_button)
        self.factor_buttons.add(self._button(self.factor_buttons, "一键回测", "Secondary.TButton", lambda: self.run_factor_action("backtest", "回测中")))
        self.factor_buttons.add(self._button(self.factor_buttons, "一键排序", "Primary.TButton", lambda: self.run_factor_action("rank", "获取排名中")))
        self.factor_buttons.add(self._button(self.factor_buttons, "回测全部策略", "Secondary.TButton", lambda: self.run_factor_action("backtest-all", "回测全部策略中")))
        self._wrapped_label(self.factor_section, text="回测使用延后 VWAP 模拟成交。", bg=self.PANEL, fg="#7f95bd",
                            font=self._font(8)).pack(fill="x", padx=16, pady=(0, 14))

        self.maintenance_section = self._section(self.body, "安装与维护")
        self._wrapped_label(self.maintenance_section, textvariable=self.environment_text, bg=self.PANEL,
                            fg=self.MUTED).pack(fill="x", padx=16, pady=(0, 10))
        self.maintenance_buttons = FlowFrame(self.maintenance_section, bg=self.PANEL)
        self.maintenance_buttons.pack(fill="x", padx=16, pady=(0, 14))
        self.maintenance_buttons.add(self._button(self.maintenance_buttons, "安装 / 修复", "Secondary.TButton", self.confirm_restart_install))
        self.maintenance_buttons.add(self._button(self.maintenance_buttons, "更新项目", "Primary.TButton",
            lambda: self.confirm_maintenance("Update", "更新中", "更新会自动关闭所有服务，随后拉取最新代码、更新 .venv 中的 Python 依赖，并重新注册本地插件；不会更新 dsh 或 pnpm。完成后不会自动启用服务。是否继续？")))

        self.log_header = tk.Frame(self.body, bg=self.BG)
        tk.Label(self.log_header, text="运行日志", bg=self.BG, fg=self.MUTED, font=self._font(9, bold=True)).pack(side="left")
        clear_button = tk.Button(self.log_header, text="清空", command=self.clear_log, bg=self.BG, fg=self.MUTED,
                                 activebackground=self.BG, activeforeground=self.TEXT, borderwidth=0, cursor="hand2")
        clear_button.pack(side="right")
        self.utility_buttons.append(clear_button)
        self.log_box = tk.Frame(self.body, bg="#070c16", highlightbackground=self.LINE, highlightthickness=1)
        log_scrollbar = ttk.Scrollbar(self.log_box, orient="vertical")
        log_scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(self.log_box, width=1, height=9, bg="#070c16", fg="#b8c7df", insertbackground=self.TEXT,
                               selectbackground="#29426b", relief="flat", borderwidth=0, padx=12, pady=9,
                               font=self._font(8, mono=True), wrap="word", yscrollcommand=log_scrollbar.set, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        log_scrollbar.configure(command=self.log_text.yview)

        self.footer = tk.Frame(self, bg="#0e1729")
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer_status = self._wrapped_label(self.footer, textvariable=self.status_text, bg="#0e1729", fg=self.MUTED)
        self.footer_buttons = tk.Frame(self.footer, bg="#0e1729")
        refresh_button = ttk.Button(self.footer_buttons, text="刷新", style="Secondary.TButton", command=self.refresh_status)
        refresh_button.pack(side="right", padx=(6, 0))
        minimize_button = ttk.Button(self.footer_buttons, text="最小化", style="Secondary.TButton", command=self.iconify)
        minimize_button.pack(side="right")
        self.utility_buttons.extend((refresh_button, minimize_button))
        self._layout_main(900)
        self._layout_chrome(1000)

    def _layout_main(self, width):
        columns = 2 if width >= 900 else 1
        if columns == self._main_columns:
            return
        self._main_columns = columns
        for column in range(2):
            self.body.grid_columnconfigure(column, weight=1 if column < columns else 0, uniform="sections" if column < columns else "")
        for row in range(7):
            self.body.grid_rowconfigure(row, weight=0)
        for widget in (self.status_heading, self.status_grid, self.access_section, self.factor_section,
                       self.maintenance_section, self.log_header, self.log_box):
            widget.grid_forget()
        padding = 18 if width < 680 else 24
        self.status_heading.grid(row=0, column=0, columnspan=columns, sticky="w", padx=padding, pady=(18, 8))
        self.status_grid.grid(row=1, column=0, columnspan=columns, sticky="ew", padx=padding, pady=(0, 14))
        if columns == 2:
            self.access_section.grid(row=2, column=0, sticky="new", padx=(padding, 7), pady=(0, 14))
            self.factor_section.grid(row=2, column=1, sticky="new", padx=(7, padding), pady=(0, 14))
            next_row = 3
        else:
            self.access_section.grid(row=2, column=0, sticky="ew", padx=padding, pady=(0, 14))
            self.factor_section.grid(row=3, column=0, sticky="ew", padx=padding, pady=(0, 14))
            next_row = 4
        self.maintenance_section.grid(row=next_row, column=0, columnspan=columns, sticky="ew", padx=padding, pady=(0, 14))
        self.log_header.grid(row=next_row + 1, column=0, columnspan=columns, sticky="ew", padx=padding, pady=(0, 6))
        self.log_box.grid(row=next_row + 2, column=0, columnspan=columns, sticky="nsew", padx=padding, pady=(0, 18))
        self.body.grid_rowconfigure(next_row + 2, weight=1)

    def _layout_chrome(self, width):
        compact = width < 680
        if compact == self._chrome_compact:
            return
        self._chrome_compact = compact
        self.header.grid_columnconfigure(0, weight=1)
        self.header.grid_columnconfigure(1, weight=0, minsize=0 if compact else 260)
        self.title_box.grid(row=0, column=0, sticky="w", padx=20, pady=14)
        self.operation_box.grid(row=1 if compact else 0, column=0 if compact else 1,
                                sticky="ew", padx=20, pady=(0, 12) if compact else 14)
        self.operation_box.configure(width=240)
        self.footer.grid_columnconfigure(0, weight=1)
        self.footer.grid_columnconfigure(1, weight=0)
        self.footer_status.grid(row=0, column=0, columnspan=2 if compact else 1,
                                sticky="ew", padx=20, pady=(10, 6) if compact else 12)
        self.footer_buttons.grid(row=1 if compact else 0, column=0 if compact else 1,
                                 sticky="e", padx=20, pady=(0, 10) if compact else 10)

    def _resize_chrome(self, event):
        if event.widget is self:
            self._layout_chrome(event.width)
        # Wrapped text/controls can change the requested height without resizing
        # the canvas window itself. Observe descendant geometry changes too.
        self._schedule_body_sync()

    def _resize_viewport(self, event):
        self.content_canvas.itemconfigure(self.body_window, width=event.width)
        self._layout_main(event.width)
        self._schedule_body_sync()

    def _schedule_body_sync(self, event=None):
        if self._pending_body_sync is None:
            self._pending_body_sync = self.after_idle(self._sync_body)

    def _sync_body(self):
        self._pending_body_sync = None
        height = max(self.body.winfo_reqheight(), self.content_canvas.winfo_height())
        self.content_canvas.itemconfigure(self.body_window, height=height)
        self.content_canvas.configure(scrollregion=self.content_canvas.bbox(self.body_window))
        if height <= self.content_canvas.winfo_height():
            self.content_canvas.yview_moveto(0)

    def _scroll_content(self, event):
        widget = event.widget
        while widget is not self and widget is not None:
            if isinstance(widget, (tk.Text, ttk.Combobox, ttk.Scrollbar)):
                return
            widget = getattr(widget, "master", None)
        if self.body.winfo_height() <= self.content_canvas.winfo_height():
            return
        if getattr(event, "num", None) not in (4, 5) and not getattr(event, "delta", 0):
            return
        direction = -1 if getattr(event, "num", None) == 4 else 1 if getattr(event, "num", None) == 5 else (-1 if event.delta > 0 else 1)
        self.content_canvas.yview_scroll(direction * 3, "units")
        return "break"

    def _reveal_control(self, event):
        ancestor = event.widget
        while ancestor is not self.body and ancestor is not None:
            ancestor = getattr(ancestor, "master", None)
        if ancestor is None:
            return
        top = event.widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + event.widget.winfo_height()
        visible_top = self.content_canvas.canvasy(0)
        visible_height = self.content_canvas.winfo_height()
        increment = int(self.content_canvas.cget("yscrollincrement"))
        if top < visible_top:
            self.content_canvas.yview_moveto(max(0, top - increment) / max(1, self.body.winfo_height()))
        elif bottom > visible_top + visible_height:
            self.content_canvas.yview_moveto((bottom - visible_height + increment) / max(1, self.body.winfo_height()))

    def _button(self, parent, text, style, command, web: bool = False):
        button = ttk.Button(parent, text=text, style=style, command=command)
        (self.web_buttons if web else self.action_buttons).append(button)
        return button

    def confirm_maintenance(self, action: str, state_text: str, prompt: str):
        if messagebox.askyesno("请确认", prompt):
            self.run_maintenance(action, state_text)

    def confirm_restart_install(self):
        prompt = "控制面板将停止全部服务并退出，然后在命令行窗口中重新检测和安装缺失组件。完成后会自动重新打开控制面板。是否继续？"
        if messagebox.askyesno("安装 / 修复", prompt):
            self.restart_through_launcher()

    def set_busy(self, busy: bool, operation: str = "空闲"):
        self.operation_revision = getattr(self, "operation_revision", 0) + 1
        self.busy = busy
        if not busy:
            self.factor_busy = False
            self.report_busy = False
        self.operation_text.set(operation)
        self._update_controls()

    def _update_controls(self):
        disabled = self.busy or self.starting_services or self.closing
        for button in (*self.action_buttons, *self.utility_buttons):
            button.configure(state="disabled" if disabled else "normal")
        web_disabled = (self.starting_services or self.closing
                        or (self.busy and not (self.factor_busy or getattr(self, "report_busy", False))))
        for button in self.web_buttons:
            button.configure(state="disabled" if web_disabled else "normal")
        if self.factor_slot_box is not None:
            self.factor_slot_box.configure(state="disabled" if disabled else "readonly")

        if getattr(self, "strategy_box", None) is not None:
            self.strategy_box.configure(state="disabled" if disabled else "readonly")
        if getattr(self, "factor_train_button", None) is not None:
            strategy = STRATEGY_LABELS.get(self.strategy_choice.get(), "xgboost")
            if strategy not in MODEL_STRATEGIES:
                self.factor_train_button.configure(state="disabled")

    def start_all_services(self):
        if self.busy or self.starting_services or self.closing:
            return
        if not VENV_PYTHON.exists():
            self.status_text.set("未检测到虚拟环境，请点击“安装 / 修复”。")
            self.operation_text.set("等待安装")
            return
        self.starting_services = True
        self.operation_revision = getattr(self, "operation_revision", 0) + 1
        self._update_controls()
        self.startup_cancelled = False
        self.operation_text.set("后台启动中")
        self.status_text.set("正在启用日报智能体与 DSH Web…")

        def worker():
            try:
                outputs = []
                for action in ("EnableAgent", "EnableDshWeb"):
                    if self.closing or self.startup_cancelled:
                        break
                    self.after(0, lambda value=action: self.append_log(f"> {service_label(value)}"))
                    outputs.append(service_command(
                        action,
                        lambda line: self.after(0, lambda value=line: self.append_log(value)),
                        self.track_startup_process,
                    ))
                if not self.closing and not self.startup_cancelled:
                    self.after(0, lambda: self.services_started("\n".join(outputs)))
            except Exception as exc:
                if not self.closing and not self.startup_cancelled:
                    self.after(0, lambda detail=str(exc): self.startup_failed(detail))

        try:
            self.startup_thread = threading.Thread(target=worker, daemon=True)
            self.startup_thread.start()
        except Exception as exc:
            self.startup_failed(str(exc))

    def track_startup_process(self, process: subprocess.Popen | None):
        with self.startup_process_lock:
            self.startup_process = process

    def cancel_startup(self):
        self.startup_cancelled = True
        self.starting_services = False
        with self.startup_process_lock:
            process = self.startup_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def services_started(self, output: str):
        if self.closing:
            return
        self.starting_services = False
        self._update_controls()
        self.operation_text.set("空闲")
        self.status_text.set(output.splitlines()[-1] if output else "全部服务已启动。")
        self.append_log("✓ 日报智能体与 DSH Web 已在后台启动；未自动打开网页。")
        self.refresh_status()

    def startup_failed(self, detail: str):
        if self.closing:
            return
        self.starting_services = False
        self._update_controls()
        self.operation_text.set("启动未完成")
        self.status_text.set("服务未能全部启动，请查看日志或先执行安装 / 修复。")
        self.append_log("✕ 自动启动未完成：" + detail)
        self.operation_text.set("空闲")
        self.refresh_status()

    def open_url(self, url: str, label: str):
        self.append_log(f"打开 {label}：{url}")
        if not webbrowser.open_new_tab(url):
            messagebox.showerror("无法打开网页", f"未能调用默认浏览器。请手动访问：\n{url}")

    def open_backtests(self):
        self.open_url("http://127.0.0.1:8766/backtests", "回测页面")

    def open_dsh_web(self):
        log_path = PROJECT_ROOT / "data" / ("dsh-web.stdout.log" if sys.platform == "win32" else "linux-services/dsh.stdout.log")
        url = "http://127.0.0.1:3080"
        try:
            content = ANSI_ESCAPE.sub("", log_path.read_text(encoding="utf-8", errors="replace"))
            candidates = re.findall(r'https?://[^\s<>"\']+', content)
            authenticated = [candidate.rstrip(".,);]") for candidate in candidates if ":3080" in candidate]
            if authenticated:
                url = authenticated[-1]
        except OSError:
            pass
        self.open_url(url, "DSH Web")

    def close_panel(self):
        if self.closing:
            return
        if self.busy and self.factor_process is None:
            messagebox.showinfo("操作进行中", "请等待当前安装、更新或服务操作完成后再关闭控制台。")
            return
        if self.factor_process and self.factor_process.poll() is None:
            self.factor_process.terminate()
        self.closing = True
        self.set_busy(True, "正在退出")
        self.status_text.set("正在关闭网页服务；每周训练继续运行…")
        self.append_log("控制台即将关闭；每周 AI 因子训练计划继续运行。")
        self.withdraw()

        def worker():
            self.cancel_startup()
            errors = []
            for action in ("DisableDshWeb", "ClosePanel"):
                try:
                    self.after(0, lambda value=action: self.append_log(f"> {service_label(value)}"))
                    service_command(action, lambda line: self.after(0, lambda value=line: self.append_log(value)))
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                self.after(0, lambda: self.append_log("✕ 部分服务关闭失败，控制台仍将退出：" + "；".join(errors)))
            self.after(200, self.destroy)

        threading.Thread(target=worker, daemon=True).start()

    def restart_through_launcher(self):
        if self.busy or self.closing:
            return
        self.closing = True
        self.set_busy(True, "准备安装")
        self.status_text.set("正在停止服务并重新启动安装检查…")
        self.append_log("安装 / 修复：停止全部服务后，将在命令行窗口中重新执行启动检查。")

        def worker():
            if self.startup_thread and self.startup_thread.is_alive():
                self.after(0, lambda: self.append_log("正在中止自动启动流程并立即关闭全部服务…"))
                self.cancel_startup()
            errors = []
            for action in ("DisableDshWeb", "DisableAgent"):
                try:
                    service_command(action, lambda line: self.after(0, lambda value=line: self.append_log(value)))
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                self.after(0, lambda: self.install_restart_failed("；".join(errors)))
                return
            try:
                self.launch_bootstrap()
            except Exception as exc:
                self.after(0, lambda detail=str(exc): self.install_restart_failed(detail))
                return
            self.after(0, self.destroy)

        threading.Thread(target=worker, daemon=True).start()

    def install_restart_failed(self, detail: str):
        self.closing = False
        self.set_busy(False, "启动失败")
        self.status_text.set("无法重新启动安装流程，请查看错误信息。")
        self.append_log("✕ 无法重新启动安装流程：" + detail)
        messagebox.showerror("安装 / 修复启动失败", detail)

    def launch_bootstrap(self):
        if sys.platform == "win32":
            args = ["cmd.exe", "/d", "/c", "call", str(CONTROL_PANEL_LAUNCHER)]
            subprocess.Popen(args, cwd=PROJECT_ROOT, creationflags=WINDOWS_NEW_CONSOLE)
            return
        options = (["x-terminal-emulator", "-e", "bash", str(CONTROL_PANEL_LAUNCHER)],
                   ["gnome-terminal", "--", "bash", str(CONTROL_PANEL_LAUNCHER)],
                   ["konsole", "-e", "bash", str(CONTROL_PANEL_LAUNCHER)],
                   ["xterm", "-e", "bash", str(CONTROL_PANEL_LAUNCHER)])
        for args in options:
            if shutil.which(args[0]):
                subprocess.Popen(args, cwd=PROJECT_ROOT)
                return
        raise RuntimeError("未找到终端程序；请在终端运行 ./control_panel.sh 完成安装。")

    def update_factor_status(self):
        try:
            latest = read_json(FACTOR_LATEST)
            settings = read_json(FACTOR_SETTINGS)
            strategy = settings.get("strategy", "xgboost")
            if strategy not in STRATEGIES:
                strategy = "xgboost"
            self.strategy_choice.set(STRATEGIES[strategy]["name"])
            self.strategy_description.set(STRATEGIES[strategy]["description"])
            slot = settings.get("backtest_slot", "14:00")
            self.factor_slot.set(slot if slot in FACTOR_SLOTS else "14:00")
            if strategy not in MODEL_STRATEGIES:
                self.factor_status.set("固定规则策略 · 无需训练，可直接回测与排序")
                self.factor_schedule_status.set("固定规则无需每周训练；切换至因子模型后恢复训练检查")
            else:
                model = read_json(FACTOR_MODEL.with_name(f"model-{strategy}.json")) or read_json(FACTOR_MODEL)
                artifact = FACTOR_STORE / "models" / str(model.get("model_id", "")) / f"{strategy}.json"
                if model.get("target_version") != FACTOR_TARGET_VERSION or not artifact.is_file():
                    self.factor_status.set("所选模型尚未训练 · 请点击一键训练")
                else:
                    quality = "探索性模拟" if model.get("quality") == "exploratory" else "已验证"
                    self.factor_status.set(f"{quality}：{STRATEGIES[strategy]['name']} · 最近排名：{latest.get('generated_at') or '暂无'}")
            self._update_controls()
        except Exception:
            self.factor_status.set("因子状态暂不可读")

    def save_factor_slot(self, _event=None):
        try:
            slot = self.factor_slot.get()
            if slot not in FACTOR_SLOTS:
                raise ValueError(f"回测时点无效：{slot}")
            current = read_json(FACTOR_SETTINGS)
            write_json(FACTOR_SETTINGS, {
                **current, "backtest_slot": slot,
                "weekly_training_enabled": bool(current.get("weekly_training_enabled", True)),
            })
            self.append_log(f"✓ 回测信号时点已设为 {self.factor_slot.get()}；下次训练或回测生效。")
        except Exception as error:
            messagebox.showerror("设置失败", str(error))

    def save_strategy(self, _event=None):
        try:
            strategy = STRATEGY_LABELS[self.strategy_choice.get()]
            current = read_json(FACTOR_SETTINGS)
            write_json(FACTOR_SETTINGS, {**current, "strategy": strategy})
            self.update_factor_status()
            self.append_log(f"✓ 已选择 {STRATEGIES[strategy]['name']}；训练、回测与排序使用此策略。")
        except Exception as error:
            messagebox.showerror("设置失败", str(error))

    def run_manual_report(self):
        if self.busy or self.closing or self.starting_services:
            return
        self.report_busy = True
        self.set_busy(True, "生成日报中")
        self.status_text.set("正在获取行情并生成日报，详情见运行日志…")
        self.append_log("> 手动生成日报：重新获取当前时段数据并更新本地归档。")

        def worker():
            try:
                output = stream_command(
                    [str(VENV_PYTHON), str(DAILY_REPORT_SCRIPT), "--manual"],
                    lambda line: self.after(0, lambda value=line: self.append_log(value)),
                    lambda process: setattr(self, "factor_process", process),
                )
                self.after(0, lambda: self.manual_report_done(output))
            except Exception as exc:
                if not self.closing:
                    self.after(0, lambda detail=str(exc): self.action_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def manual_report_done(self, output: str):
        if self.closing:
            return
        self.set_busy(False)
        result = next((line for line in reversed(output.splitlines())
                       if "已生成：" in line or "今日不是交易日" in line), None)
        self.status_text.set(result or "日报操作完成，请查看运行日志。")
        self.append_log("✓ " + ("今日休市，已跳过日报生成。" if "今日不是交易日" in output
                                 else "手动日报已生成，可在日报页面查看。"))
        self.refresh_status(False)

    def run_factor_action(self, action: str, operation: str):
        if self.busy or self.closing:
            return
        strategy = read_json(FACTOR_SETTINGS).get("strategy", "xgboost")
        if strategy not in STRATEGIES:
            strategy = "xgboost"
        if action == "train" and strategy not in MODEL_STRATEGIES:
            messagebox.showinfo("固定规则策略", "此策略无需训练，可直接回测与排序。")
            return
        needs_preparation = action in ("backtest", "backtest-all") and backtest_data_missing()
        if needs_preparation:
            start, end = backtest_preparation_dates()
            messagebox.showinfo(
                "缺少回测数据",
                "回测需要逐日历史股票池、交易状态（含红股上市日）、首次入池前的预热分钟线和已验收的来源清单。\n"
                f"将自动下载并验收 {start} 至 {end} 的历史数据；完成后继续回测。\n"
                "首次下载可能耗时较长，进度会显示在运行日志中。",
            )
        self.factor_busy = True
        self.set_busy(True, "准备回测数据" if needs_preparation else operation)
        self.status_text.set("正在下载并验收回测数据，详情见运行日志…" if needs_preparation
                             else f"{operation}，详情见运行日志…")

        def worker():
            try:
                on_output = lambda line: self.after(0, lambda value=line: self.append_log(value))
                on_process = lambda process: setattr(self, "factor_process", process)
                if needs_preparation:
                    self.after(0, lambda: self.append_log(
                        f"> {VENV_PYTHON.name} factor_data_prep.py --start {start} --end {end}"))
                    stream_command(
                        [str(VENV_PYTHON), str(FACTOR_PREP_SCRIPT), "--start", start, "--end", end],
                        on_output, on_process,
                    )
                    self.after(0, lambda: self.append_log("✓ 回测历史数据已下载并通过验收，开始回测。"))
                    self.after(0, lambda: self.set_busy(True, operation))
                    self.after(0, lambda: self.status_text.set(f"{operation}，详情见运行日志…"))
                self.after(0, lambda: self.append_log(f"> {VENV_PYTHON.name} factor_lab.py {action} --strategy {strategy}"))
                output = stream_command([str(VENV_PYTHON), str(FACTOR_SCRIPT), action, "--strategy", strategy],
                                        on_output, on_process)
                self.after(0, lambda: self.factor_action_done(action, output))
            except Exception as exc:
                if not self.closing:
                    lines = str(exc).splitlines()
                    summary = next((line for line in reversed(lines) if line.startswith("错误：")), lines[-1] if lines else str(exc))
                    self.after(0, lambda detail=summary[:900]: self.action_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def factor_action_done(self, action: str, output: str):
        if self.closing:
            return
        self.set_busy(False)
        self.update_factor_status()
        self.status_text.set(output.splitlines()[-1] if output else "操作完成。")
        if action == "train":
            model = read_json(FACTOR_MODEL)
            algorithms = model.get("algorithms", {})
            lines = [f"已发布 AI 模型 {model.get('model_id', '')}，当前使用 {model.get('algorithm', '未知')}"]
            for name, metrics in algorithms.items():
                lines.append(f"{name}: 验证 Rank IC={metrics['validation']['rank_ic']}；留出 Rank IC={metrics['holdout']['rank_ic']}")
            backtest = model.get("backtest", {})
            if backtest:
                stats = backtest["stats"]
                lines.append(f"{backtest.get('slot', '14:00')} 信号滚动回测 {backtest['days']} 日：收益 {stats['total_return']:.2%}；最大回撤 {stats['max_drawdown']:.2%}")
                lines.append("详细交易见 backtests 目录；旧版回测结果不再作为有效绩效展示。")
            lines.append(f"历史交易日：{model.get('training', {}).get('days', '未知')}；仅供模拟研究。")
            messagebox.showinfo("所选模型训练结果", "\n".join(lines))
        if action == "backtest-all":
            batches = sorted((FACTOR_BACKTESTS / "batches").glob("*.json"))
            if batches:
                batch = read_json(batches[-1])
                lines = [f"{report['strategy_name']}：收益 {report['stats']['total_return']:.2%}，最大回撤 {report['stats']['max_drawdown']:.2%}"
                         for report in batch.get("completed", [])]
                messagebox.showinfo("全部策略回测结果", "\n".join(lines + ["日报页面可查看各策略的净值、回撤图与历史结果。 "]))
        if action == "backtest":
            reports = sorted(FACTOR_BACKTESTS.glob("*/summary.json"))
            reports = [path for path in reports
                       if read_json(path).get("schema_version") == 2]
            if reports:
                report = read_json(reports[-1])
                stats = report.get("stats", {})
                messagebox.showinfo("滚动回测结果", "\n".join([
                    f"回测 {report.get('days', '未知')} 个交易日 · {report.get('slot', '14:00')} 信号，{report.get('execution', '延后VWAP')} 模拟成交",
                    f"收益：{stats.get('total_return', 0):.2%} · 最大回撤：{stats.get('max_drawdown', 0):.2%}",
                    f"交易：{stats.get('trades', 0)} 笔 · 显式费用：{stats.get('costs', 0):,.2f} 元 · 估计滑点：{stats.get('slippage_costs', 0):,.2f} 元",
                    "已使用逐日历史股票池和交易状态；成交仍是5分钟数据模拟，仅供研究。",
                    f"净值与成交文件：{reports[-1].parent}",
                ]))
        if action == "rank":
            self.append_log("✓ 最新策略排名已推送到日报网页。")

    def run_maintenance(self, action: str, operation: str):
        """Stop all managed services before an installation or an update."""
        if self.busy:
            return
        self.set_busy(True, operation)
        self.status_text.set(f"{operation}：正在关闭全部服务…")
        self.append_log(f"> {operation}前置步骤：关闭 DSH Web、日报任务与本地服务")

        def worker():
            try:
                if self.startup_thread and self.startup_thread.is_alive():
                    self.after(0, lambda: self.append_log("正在中止自动启动流程并立即关闭全部服务…"))
                    self.cancel_startup()
                for stop_action in ("DisableDshWeb", "DisableAgent"):
                    self.after(0, lambda value=stop_action: self.append_log(f"> {service_label(value)}"))
                    service_command(stop_action, lambda line: self.after(0, lambda value=line: self.append_log(value)))
                self.after(0, lambda: self.append_log("✓ 全部服务已停止，开始后续维护操作。"))
                self.after(0, lambda: self.status_text.set(f"{operation}，请稍候…"))
                self.after(0, lambda: self.append_log(f"> {service_label(action)}"))
                output = service_command(action, lambda line: self.after(0, lambda value=line: self.append_log(value)))
                self.after(0, lambda: self.action_done(action, output))
            except Exception as exc:
                self.after(0, lambda detail=str(exc): self.action_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def run_action(self, action: str, operation: str):
        if self.starting_services:
            messagebox.showinfo("服务正在启动", "日报智能体与 DSH Web 正在后台启动，请稍候再执行安装或更新。")
            return
        if self.busy:
            return
        self.set_busy(True, operation)
        self.status_text.set(f"{operation}，请稍候…")
        self.append_log(f"> {service_label(action)}")

        def worker():
            try:
                output = service_command(action, lambda line: self.after(0, lambda value=line: self.append_log(value)))
                self.after(0, lambda: self.action_done(action, output))
            except Exception as exc:
                self.after(0, lambda detail=str(exc): self.action_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

    def action_done(self, action: str, output: str):
        self.set_busy(False)
        self.status_text.set(output.splitlines()[-1] if output else "操作完成。")
        self.append_log("✓ 操作完成。")
        if action == "Update":
            messagebox.showinfo("更新完成", "代码、依赖与插件已更新。所有服务和日报任务现已关闭；请重新启动控制面板以恢复运行。点击“确定”后，本窗口将自动关闭。")
            self.closing = True
            self.destroy()
            return
        self.refresh_status()

    def action_failed(self, detail: str):
        self.set_busy(False, "操作失败")
        self.status_text.set("操作未完成，请查看错误信息。")
        self.append_log("✕ 操作失败，详情见上方输出。")
        messagebox.showerror("操作失败", detail)
        self.operation_text.set("空闲")
        self.refresh_status()

    def refresh_status(self, verbose: bool = True):
        if self.busy or self.closing or self.status_checking or self.starting_services:
            return
        self.status_checking = True
        revision = getattr(self, "operation_revision", 0)
        if verbose:
            self.append_log("检测中：日报调度、服务端口、Node.js、Python、Git、虚拟环境与 DSH 插件…")

        def worker():
            try:
                output = service_command("Status", (lambda line: self.after(0, lambda value=line: self.append_log(value)) if not line.lstrip().startswith("{") else None) if verbose else None)
                payload = json.loads(output.splitlines()[-1])
                self.after(0, lambda: self._finish_status(payload=payload, verbose=verbose, revision=revision))
            except Exception as exc:
                self.after(0, lambda detail=str(exc): self._finish_status(detail=detail, revision=revision))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self._finish_status(detail=str(exc), revision=revision)

    def _finish_status(self, payload: dict | None = None, detail: str | None = None,
                       verbose: bool = False, revision: int | None = None):
        try:
            # A read-only check must neither block actions nor overwrite their
            # progress/results if the user started an operation while it ran.
            if (self.busy or self.starting_services or self.closing
                    or (revision is not None and revision != getattr(self, "operation_revision", 0))):
                return
            if detail is None:
                self.show_status(payload or {}, verbose)
            else:
                self.status_failed(detail)
        finally:
            self.status_checking = False
            self._update_controls()

    def periodic_refresh(self):
        if self.closing:
            return
        self.refresh_status(False)
        self.update_factor_status()
        self.after(6000, self.periodic_refresh)

    def status_failed(self, detail: str):
        if self.closing:
            return
        self.status_text.set(f"无法读取状态：{detail}")
        if detail != self.last_status_error:
            self.append_log("✕ 状态检测失败：" + detail)
            self.last_status_error = detail

    def show_status(self, payload: dict, verbose: bool = False):
        if self.closing:
            return
        self.last_status_error = ""
        agent_on = payload.get("agent_task") in {"Ready", "Running"}
        factor_on = payload.get("factor_task") in {"Ready", "Running"}
        if read_json(FACTOR_SETTINGS).get("strategy", "xgboost") not in MODEL_STRATEGIES:
            self.factor_schedule_status.set("固定规则无需每周训练；切换至因子模型后恢复训练检查")
        else:
            self.factor_schedule_status.set("每周训练：已启用 · 工作日 12:00 检查所选模型" if factor_on
                                            else "每周训练：未启用 · 重新启动控制面板后注册")
        values = {
            "日报任务": agent_on,
            "数据服务": bool(payload.get("data_service")),
            "日报页面": bool(payload.get("report_site")),
            "DSH Web": bool(payload.get("dsh_web")),
        }
        for name, enabled in values.items():
            self.service_vars[name].set("● 运行中" if enabled else "○ 已关闭")
        prerequisites = payload.get("node_available") and payload.get("python_available")
        installed = payload.get("venv_ready") and payload.get("dsh_available") and payload.get("plugin_installed")
        if not payload.get("git_available"):
            git_note = " · 未检测到 Git，更新不可用"
        elif not payload.get("git_checkout"):
            git_note = " · 当前目录不是 Git 仓库，更新不可用"
        else:
            git_note = ""
        if installed:
            self.environment_text.set("环境已安装 · Python 依赖与 DSH 插件就绪" + git_note)
        elif prerequisites:
            self.environment_text.set("Node.js / Python 已就绪 · 可点击“安装 / 修复”" + git_note)
        else:
            missing = [name for name, ready in (("Node.js", payload.get("node_available")), ("Python", payload.get("python_available"))) if not ready]
            self.environment_text.set("请先安装：" + "、".join(missing))
        active = [name for name, enabled in values.items() if enabled]
        self.status_text.set("已启用：" + "、".join(active) if active else "当前没有启用任何服务。")
        environment = "环境就绪" if installed else "环境未完整安装"
        summary = f"服务：{'、'.join(active) if active else '全部关闭'}；{environment}；Node.js={'是' if payload.get('node_available') else '否'}，Python={'是' if payload.get('python_available') else '否'}，Git={'是' if payload.get('git_available') else '否'}，插件={'是' if payload.get('plugin_installed') else '否'}"
        if verbose or summary != self.last_status_summary:
            self.append_log("[状态] " + summary)
            self.last_status_summary = summary

    def append_log(self, message: str):
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log_text.delete("1.0", f"{line_count - 500}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    ControlPanel().mainloop()
