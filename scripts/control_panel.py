#!/usr/bin/env python
"""Compact Windows/Linux control panel for the daily agent and factor lab."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_SCRIPT = PROJECT_ROOT / "scripts" / "agent-service.ps1"
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
CONTROL_PANEL_LAUNCHER = PROJECT_ROOT / ("control_panel.cmd" if sys.platform == "win32" else "control_panel.sh")
FACTOR_SCRIPT = PROJECT_ROOT / "scripts" / "factor_lab.py"
LINUX_SERVICE_SCRIPT = PROJECT_ROOT / "scripts" / "linux_service.py"
LINUX_UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "linux_update.sh"
WINDOWS_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


def stream_command(args: list[str], on_output=None, on_process=None) -> str:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        args,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        bufsize=1,
        creationflags=WINDOWS_CREATION_FLAGS if sys.platform == "win32" else 0,
    )
    if on_process:
        on_process(process)
    try:
        lines = []
        if process.stdout:
            for raw_line in process.stdout:
                line = ANSI_ESCAPE.sub("", raw_line.rstrip())
                if not line:
                    continue
                lines.append(line)
                if on_output:
                    on_output(line)
        return_code = process.wait()
        output = "\n".join(lines).strip()
        if return_code:
            detail = "\n".join(lines[-40:]).strip()
            raise RuntimeError(detail or f"操作失败（退出码 {return_code}）。")
        return output
    finally:
        if process.stdout:
            process.stdout.close()
        if on_process:
            on_process(None)


def service_command(action: str, on_output=None, on_process=None) -> str:
    if sys.platform == "win32":
        args = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SERVICE_SCRIPT), "-Action", action]
    elif action == "Update":
        args = ["bash", str(LINUX_UPDATE_SCRIPT)]
    else:
        args = [str(VENV_PYTHON), str(LINUX_SERVICE_SCRIPT), action]
    return stream_command(args, on_output, on_process)


def service_label(action: str) -> str:
    return (f"powershell.exe agent-service.ps1 -Action {action}" if sys.platform == "win32"
            else ("bash linux_update.sh" if action == "Update" else f"python linux_service.py {action}"))


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
        self.geometry("760x860")
        self.resizable(False, False)
        self.configure(bg=self.BG)
        self.option_add("*Font", ("Microsoft YaHei UI", 9))
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
        self.busy = False
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

    def _build_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Primary.TButton", background=self.ACCENT, foreground="#ffffff", borderwidth=0, padding=(14, 8), font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Primary.TButton", background=[("active", "#86a0ff"), ("disabled", "#40506e")], foreground=[("disabled", "#9ba7bb")])
        style.configure("Secondary.TButton", background="#243451", foreground="#e9efff", borderwidth=0, padding=(14, 8))
        style.map("Secondary.TButton", background=[("active", "#324768"), ("disabled", "#1c283e")], foreground=[("disabled", "#77849a")])

    def _build(self):
        header = tk.Frame(self, bg="#0e1729", height=78)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = tk.Frame(header, bg="#0e1729")
        title_box.pack(side="left", padx=28, pady=15)
        tk.Label(title_box, text="FINANCE AGENT", bg="#0e1729", fg="#91a8ff", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(title_box, text="财务研究智能体", bg="#0e1729", fg=self.TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        operation = tk.Frame(header, bg=self.PANEL_ALT, highlightbackground=self.LINE, highlightthickness=1)
        operation.pack(side="right", padx=28)
        tk.Label(operation, text="当前操作", bg=self.PANEL_ALT, fg=self.MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(10, 5), pady=7)
        tk.Label(operation, textvariable=self.operation_text, bg=self.PANEL_ALT, fg=self.GREEN, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 10))

        body = tk.Frame(self, bg=self.BG)
        body.pack(fill="both", expand=True, padx=28, pady=20)
        tk.Label(body, text="运行状态", bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(0, 8))
        status_row = tk.Frame(body, bg=self.BG)
        status_row.pack(fill="x")
        for index, name in enumerate(self.service_vars):
            tile = tk.Frame(status_row, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1, width=164, height=62)
            tile.grid(row=0, column=index, sticky="nsew", padx=(0, 8) if index < 3 else 0)
            tile.grid_propagate(False)
            status_row.grid_columnconfigure(index, weight=1)
            tk.Label(tile, text=name, bg=self.PANEL, fg=self.MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=12, pady=(9, 1))
            tk.Label(tile, textvariable=self.service_vars[name], bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12)

        controls = tk.Frame(body, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1)
        controls.pack(fill="x", pady=14)
        access_info = tk.Frame(controls, bg=self.PANEL)
        access_info.pack(side="left", fill="both", expand=True, padx=18, pady=13)
        tk.Label(access_info, text="网页入口", bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(access_info, text="服务随控制台启停；关闭网页后可在这里重新打开", bg=self.PANEL, fg=self.MUTED).pack(anchor="w", pady=(3, 0))
        tk.Label(access_info, textvariable=self.lan_report_text, bg=self.PANEL, fg="#7f95bd", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 0))
        self._button(controls, "打开 DSH Web", "Primary.TButton", self.open_dsh_web).pack(side="left", padx=5, pady=14)
        self._button(controls, "打开日报", "Secondary.TButton", lambda: self.open_url("http://127.0.0.1:8766", "日报页面")).pack(side="left", padx=(5, 16), pady=14)

        maintenance = tk.Frame(body, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1)
        factor_box = tk.Frame(body, bg=self.PANEL, highlightbackground=self.LINE, highlightthickness=1)
        factor_box.pack(fill="x", pady=(0, 14))
        factor_info = tk.Frame(factor_box, bg=self.PANEL)
        factor_info.pack(fill="x", padx=18, pady=(12, 4))
        tk.Label(factor_info, text="AI 因子研究 · A 股", bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(factor_info, textvariable=self.factor_status, bg=self.PANEL, fg=self.MUTED, anchor="w").pack(anchor="w", pady=(3, 0))
        tk.Label(factor_info, textvariable=self.factor_schedule_status, bg=self.PANEL, fg="#7f95bd",
                 anchor="w", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 0))
        tk.Label(factor_info, text="用法：每周自动训练；交易日选定时点后 5 分钟内点击“一键排序”；回测需逐日历史数据。",
                 bg=self.PANEL, fg=self.MUTED, anchor="w", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(6, 0))
        slot_row = tk.Frame(factor_box, bg=self.PANEL)
        slot_row.pack(fill="x", padx=18, pady=(3, 8))
        tk.Label(slot_row, text="回测信号时点", bg=self.PANEL, fg=self.MUTED).pack(side="left")
        slot_box = ttk.Combobox(slot_row, textvariable=self.factor_slot, values=("09:35", "10:30", "11:30", "13:30", "14:00", "14:30", "14:50"),
                                width=7, state="readonly")
        slot_box.pack(side="left", padx=(8, 10))
        slot_box.bind("<<ComboboxSelected>>", self.save_factor_slot)
        tk.Label(slot_row, text="默认 14:00；预留一根完整 K 线后，于后一根按 VWAP 模拟成交。",
                 bg=self.PANEL, fg="#7f95bd", font=("Microsoft YaHei UI", 8)).pack(side="left")
        factor_buttons = tk.Frame(factor_box, bg=self.PANEL)
        factor_buttons.pack(fill="x", padx=13, pady=(0, 11))
        self._button(factor_buttons, "一键训练", "Secondary.TButton", lambda: self.run_factor_action("train", "训练中")).pack(side="left", padx=5)
        self._button(factor_buttons, "一键回测", "Secondary.TButton", lambda: self.run_factor_action("backtest", "回测中")).pack(side="left", padx=5)
        self._button(factor_buttons, "一键排序", "Primary.TButton", lambda: self.run_factor_action("rank", "获取排名中")).pack(side="left", padx=5)
        tk.Label(factor_buttons, text="成功排名会即时显示在日报网页", bg=self.PANEL, fg="#7f95bd", font=("Microsoft YaHei UI", 8)).pack(side="right", padx=5)
        maintenance.pack(fill="x")
        info = tk.Frame(maintenance, bg=self.PANEL)
        info.pack(side="left", fill="both", expand=True, padx=18, pady=13)
        tk.Label(info, text="安装与维护", bg=self.PANEL, fg=self.TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(info, textvariable=self.environment_text, bg=self.PANEL, fg=self.MUTED, anchor="w").pack(anchor="w", pady=(3, 0))
        self._button(maintenance, "安装 / 修复", "Secondary.TButton", self.confirm_restart_install).pack(side="left", padx=5, pady=14)
        self._button(maintenance, "更新项目", "Primary.TButton", lambda: self.confirm_maintenance("Update", "更新中", "更新会自动关闭所有服务，随后拉取最新代码、更新 .venv 中的 Python 依赖，并重新注册本地插件；不会更新 dsh 或 pnpm。完成后不会自动启用服务。是否继续？")).pack(side="left", padx=(5, 16), pady=14)

        log_header = tk.Frame(body, bg=self.BG)
        log_header.pack(fill="x", pady=(14, 6))
        tk.Label(log_header, text="运行日志", bg=self.BG, fg=self.MUTED, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Button(log_header, text="清空", command=self.clear_log, bg=self.BG, fg=self.MUTED, activebackground=self.BG, activeforeground=self.TEXT, borderwidth=0, cursor="hand2").pack(side="right")
        log_box = tk.Frame(body, bg="#070c16", highlightbackground=self.LINE, highlightthickness=1)
        log_box.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_box, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(log_box, height=8, bg="#070c16", fg="#b8c7df", insertbackground=self.TEXT, selectbackground="#29426b", relief="flat", borderwidth=0, padx=12, pady=9, font=("Cascadia Mono", 8), wrap="word", yscrollcommand=scrollbar.set, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        scrollbar.configure(command=self.log_text.yview)

        footer = tk.Frame(self, bg="#0e1729", height=54)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        tk.Label(footer, textvariable=self.status_text, bg="#0e1729", fg=self.MUTED, anchor="w").pack(side="left", fill="x", expand=True, padx=28)
        ttk.Button(footer, text="刷新", style="Secondary.TButton", command=self.refresh_status).pack(side="right", padx=(6, 20), pady=10)
        ttk.Button(footer, text="最小化", style="Secondary.TButton", command=self.iconify).pack(side="right", padx=6, pady=10)

    def _button(self, parent, text, style, command):
        button = ttk.Button(parent, text=text, style=style, command=command)
        self.action_buttons.append(button)
        return button

    def confirm_maintenance(self, action: str, state_text: str, prompt: str):
        if messagebox.askyesno("请确认", prompt):
            self.run_maintenance(action, state_text)

    def confirm_restart_install(self):
        prompt = "控制面板将停止全部服务并退出，然后在命令行窗口中重新检测和安装缺失组件。完成后会自动重新打开控制面板。是否继续？"
        if messagebox.askyesno("安装 / 修复", prompt):
            self.restart_through_launcher()

    def set_busy(self, busy: bool, operation: str = "空闲"):
        self.busy = busy
        self.operation_text.set(operation)
        for button in self.action_buttons:
            button.configure(state="disabled" if busy else "normal")

    def start_all_services(self):
        if self.busy or self.starting_services or self.closing:
            return
        if not VENV_PYTHON.exists():
            self.status_text.set("未检测到虚拟环境，请点击“安装 / 修复”。")
            self.operation_text.set("等待安装")
            return
        self.starting_services = True
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

        self.startup_thread = threading.Thread(target=worker, daemon=True)
        self.startup_thread.start()

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
        self.operation_text.set("空闲")
        self.status_text.set(output.splitlines()[-1] if output else "全部服务已启动。")
        self.append_log("✓ 日报智能体与 DSH Web 已在后台启动；未自动打开网页。")
        self.refresh_status()

    def startup_failed(self, detail: str):
        if self.closing:
            return
        self.starting_services = False
        self.operation_text.set("启动未完成")
        self.status_text.set("服务未能全部启动，请查看日志或先执行安装 / 修复。")
        self.append_log("✕ 自动启动未完成：" + detail)
        self.operation_text.set("空闲")
        self.refresh_status()

    def open_url(self, url: str, label: str):
        self.append_log(f"打开 {label}：{url}")
        if not webbrowser.open_new_tab(url):
            messagebox.showerror("无法打开网页", f"未能调用默认浏览器。请手动访问：\n{url}")

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
            factor_module = __import__("factor_lab")
            state = factor_module.status()
            self.factor_slot.set(state["backtest_slot"])
            if state["model_id"]:
                if state.get("algorithm") not in ("xgboost", "elastic_net") or state.get("target_version") != factor_module.ML_TARGET_VERSION:
                    self.factor_status.set("旧版因子模型已停用 · 请点击一键训练")
                    return
                quality = "探索性模拟" if state.get("quality") == "exploratory" else "已验证"
                label = {"xgboost": "XGBoost", "elastic_net": "Elastic Net"}[state["algorithm"]]
                self.factor_status.set(f"{quality}模型：{label} · 最近排名：{state['latest_ranking_at'] or '暂无'}")
            else:
                self.factor_status.set("尚未训练 · 首次训练需回填历史5分钟行情")
        except Exception:
            self.factor_status.set("因子状态暂不可读")

    def save_factor_slot(self, _event=None):
        try:
            factor_module = __import__("factor_lab")
            current = factor_module.load_settings()
            factor_module.save_settings(self.factor_slot.get(), current["weekly_training_enabled"])
            self.append_log(f"✓ 回测信号时点已设为 {self.factor_slot.get()}；下次训练或回测生效。")
        except Exception as error:
            messagebox.showerror("设置失败", str(error))

    def run_factor_action(self, action: str, operation: str):
        if self.busy or self.closing:
            return
        self.set_busy(True, operation)
        self.status_text.set(f"{operation}，详情见运行日志…")
        self.append_log(f"> {VENV_PYTHON.name} factor_lab.py {action}")

        def worker():
            try:
                output = stream_command([str(VENV_PYTHON), str(FACTOR_SCRIPT), action],
                                        lambda line: self.after(0, lambda value=line: self.append_log(value)),
                                        lambda process: setattr(self, "factor_process", process))
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
            factor_module = __import__("factor_lab")
            model = factor_module.read_json(factor_module.MODEL)
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
            messagebox.showinfo("双模型训练结果", "\n".join(lines))
        if action == "backtest":
            factor_module = __import__("factor_lab")
            reports = sorted(factor_module.BACKTESTS.glob("*/summary.json"))
            reports = [path for path in reports
                       if factor_module.read_json(path).get("schema_version") == 2]
            if reports:
                report = factor_module.read_json(reports[-1])
                stats = report.get("stats", {})
                messagebox.showinfo("滚动回测结果", "\n".join([
                    f"回测 {report.get('days', '未知')} 个交易日 · {report.get('slot', '14:00')} 信号，{report.get('execution', '延后VWAP')} 模拟成交",
                    f"收益：{stats.get('total_return', 0):.2%} · 最大回撤：{stats.get('max_drawdown', 0):.2%}",
                    f"交易：{stats.get('trades', 0)} 笔 · 显式费用：{stats.get('costs', 0):,.2f} 元 · 估计滑点：{stats.get('slippage_costs', 0):,.2f} 元",
                    "已使用逐日历史股票池和交易状态；成交仍是5分钟数据模拟，仅供研究。",
                    f"净值与成交文件：{reports[-1].parent}",
                ]))
        if action == "rank":
            self.append_log("✓ 最新因子排名已推送到日报网页。")

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
        if self.busy or self.closing:
            return
        if verbose:
            self.append_log("检测中：日报调度、服务端口、Node.js、Python、Git、虚拟环境与 DSH 插件…")

        def worker():
            try:
                output = service_command("Status", (lambda line: self.after(0, lambda value=line: self.append_log(value)) if not line.lstrip().startswith("{") else None) if verbose else None)
                payload = json.loads(output.splitlines()[-1])
                self.after(0, lambda: self.show_status(payload, verbose))
            except Exception as exc:
                self.after(0, lambda detail=str(exc): self.status_failed(detail))

        threading.Thread(target=worker, daemon=True).start()

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
        self.factor_schedule_status.set("每周训练：已启用 · 工作日 12:00 检查，本周成功后跳过" if factor_on
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
