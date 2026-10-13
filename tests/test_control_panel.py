import importlib.util
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("control_panel", ROOT / "scripts" / "control_panel.py")
control_panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control_panel)


class StreamCommandTests(unittest.TestCase):
    def test_chinese_error_keeps_its_text_in_log_and_exception(self):
        message = "错误：仅在 A 股交易时段 09:35–11:30、13:00–15:00 生成排名。"
        received = []
        with patch.dict(os.environ, {"PYTHONIOENCODING": "gbk"}):
            with self.assertRaises(RuntimeError) as caught:
                control_panel.stream_command(
                    [sys.executable, "-c", f"print({message!r}); raise SystemExit(1)"],
                    received.append,
                )
        self.assertEqual(received, [message])
        self.assertEqual(str(caught.exception), message)

    def test_launcher_completion_does_not_wait_for_inherited_stdout(self):
        # The service keeps the same output handle open after its launcher exits.
        command = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], "
            "stdout=sys.stdout, stderr=sys.stderr); "
            "print('launcher finished', flush=True)"
        )
        tracked = []
        started = time.monotonic()
        output = control_panel.stream_command(
            [sys.executable, "-c", command], on_process=tracked.append, timeout=3,
        )
        self.assertEqual(output, "launcher finished")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(tracked[0].returncode, 0)
        self.assertIsNone(tracked[-1])

    def test_timeout_ends_launcher_and_keeps_last_progress(self):
        tracked = []
        received = []
        with self.assertRaisesRegex(RuntimeError, "操作超时") as caught:
            control_panel.stream_command(
                [sys.executable, "-c", "import time; print('正在启动数据服务', flush=True); time.sleep(30)"],
                received.append, tracked.append, timeout=0.5,
            )
        self.assertIn("正在启动数据服务", str(caught.exception))
        self.assertEqual(received, ["正在启动数据服务"])
        self.assertIsNotNone(tracked[0].poll())
        self.assertIsNone(tracked[-1])

    def test_split_utf8_and_final_line_without_newline(self):
        command = (
            "import sys, time; value='启动完成'.encode('utf-8'); "
            "sys.stdout.buffer.write(value[:2]); sys.stdout.buffer.flush(); time.sleep(0.1); "
            "sys.stdout.buffer.write(value[2:]); sys.stdout.buffer.flush()"
        )
        received = []
        output = control_panel.stream_command([sys.executable, "-c", command], received.append)
        self.assertEqual(output, "启动完成")
        self.assertEqual(received, ["启动完成"])

    def test_service_commands_bound_startup_and_status_but_allow_long_install(self):
        for action, timeout in (("Status", 20), ("EnableAgent", 90), ("EnableDshWeb", 60), ("Install", None)):
            with self.subTest(action=action), patch.object(control_panel, "stream_command", return_value="ok") as run:
                self.assertEqual(control_panel.service_command(action), "ok")
                self.assertEqual(run.call_args.kwargs["timeout"], timeout)


@unittest.skipUnless(sys.platform == "win32", "Windows service launcher test")
class WindowsServiceTests(unittest.TestCase):
    def test_python_start_failure_reports_stderr_from_script_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="service test ") as directory:
            project = Path(directory)
            script = project / "failing service.py"
            script.write_text("import sys\nprint('synthetic startup failure：启动失败', file=sys.stderr)\nraise SystemExit(7)\n", encoding="utf-8")
            # Load only the real helper functions; do not register tasks or launch
            # the user's services. Override the port check for this failing child.
            helpers = (ROOT / "scripts" / "agent-service.ps1").read_text(encoding="utf-8").split("switch ($Action)")[0]
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            harness = project / "test-service.ps1"
            harness.write_text(
                helpers + f"\n$projectRoot = {quote(project)}\n$pythonExe = {quote(sys.executable)}\n"
                + "function Test-PortListening([int]$Port) { return $false }\n"
                + f"Start-ProjectPythonService {quote(script)} 18765 'Synthetic service'\n",
                encoding="utf-8-sig",
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                creationflags=control_panel.WINDOWS_CREATION_FLAGS,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("synthetic startup failure", result.stdout)
            self.assertIn("启动失败", result.stdout)
            self.assertIn("exited before listening", result.stderr)
            self.assertIn("exit code 7", result.stderr)


class ResponsiveLayoutTests(unittest.TestCase):
    """Real Tk geometry checks; never start services or modify saved settings."""

    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for method in ("start_all_services", "periodic_refresh", "refresh_status", "update_factor_status"):
            self.patches.enter_context(patch.object(control_panel.ControlPanel, method))
        self.patches.enter_context(patch.object(control_panel, "detect_lan_ip", return_value="192.168.1.100"))
        try:
            self.panel = control_panel.ControlPanel()
        except control_panel.tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(self.close_panel)
        self.panel.overrideredirect(True)

    def close_panel(self):
        for callback in self.panel.tk.call("after", "info"):
            self.panel.after_cancel(callback)
        self.panel.destroy()

    def resize(self, width, height):
        self.panel.geometry(f"{width}x{height}+10000+10000")
        self.panel.update()
        self.panel.update_idletasks()

    def assert_controls_fit(self):
        for flow in (self.panel.access_buttons, self.panel.factor_buttons, self.panel.factor_fields,
                     self.panel.maintenance_buttons, self.panel.status_grid):
            for widget in flow.items:
                self.assertGreaterEqual(widget.winfo_x(), 0)
                self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), flow.winfo_width())
        self.assertGreaterEqual(self.panel.body.winfo_height(), self.panel.body.winfo_reqheight())
        self.assertGreaterEqual(self.panel.log_box.winfo_height(), self.panel.log_box.winfo_reqheight())
        self.assertLessEqual(self.panel.footer.winfo_y() + self.panel.footer.winfo_height(), self.panel.winfo_height())

    def test_resize_reflows_without_clipping_and_bottom_remains_reachable(self):
        for width, height in ((1000, 900), (520, 480), (760, 700), (1280, 1000), (520, 600)):
            with self.subTest(width=width, height=height):
                self.resize(width, height)
                self.assert_controls_fit()
                self.assertEqual(self.panel._main_columns, 2 if width - self.panel.content_scrollbar.winfo_width() >= 900 else 1)
                self.panel.content_canvas.yview_moveto(1)
                self.assertAlmostEqual(self.panel.content_canvas.yview()[1], 1)
        self.assertLess(self.panel.access_buttons.columns, 4)
        self.assertEqual(len(self.panel.web_buttons), 3)

    def test_desktop_layout_shows_every_section_and_adapts_log_height(self):
        for width in (1000, 1120, 1280):
            with self.subTest(width=width):
                self.resize(width, 800)
                self.assert_controls_fit()
                self.assertLessEqual(self.panel.body.winfo_reqheight(), self.panel.content_canvas.winfo_height())
                self.assertEqual(self.panel.factor_fields.columns, 2)
                self.assertLess(self.panel.access_section.winfo_x(), self.panel.factor_section.winfo_x())
                self.assertLess(self.panel.access_section.winfo_y(), self.panel.maintenance_section.winfo_y())
                for section in (self.panel.access_section, self.panel.factor_section,
                                self.panel.maintenance_section, self.panel.log_box):
                    self.assertLessEqual(section.winfo_y() + section.winfo_height(),
                                         self.panel.content_canvas.winfo_height())
        self.resize(1000, 800)
        compact_height = self.panel.log_box.winfo_height()
        self.resize(1000, 1000)
        self.assertGreater(self.panel.log_box.winfo_height(), compact_height)

    def test_initial_window_fits_measured_content_within_screen(self):
        self.panel.update()
        self.assertLessEqual(self.panel.winfo_height(), max(320, self.panel.winfo_screenheight() - 100))
        required_height = (self.panel.header.winfo_reqheight() + self.panel.body.winfo_reqheight()
                           + self.panel.footer.winfo_reqheight())
        if required_height <= self.panel.winfo_screenheight() - 100:
            self.assertGreaterEqual(self.panel.winfo_height(), required_height)
            self.assertLessEqual(self.panel.winfo_height(), required_height + 24)

    def test_long_status_and_strategy_text_expand_scroll_region_without_resize(self):
        self.resize(520, 600)
        original_height = self.panel.body.winfo_height()
        self.panel.strategy_description.set("测试长策略说明，换行后仍需显示完整。" * 30)
        self.panel.environment_text.set("测试环境与安装状态说明。" * 12)
        self.panel.update()
        self.assertGreater(self.panel.body.winfo_height(), original_height)
        self.assert_controls_fit()

    def test_larger_fonts_are_measured_before_wrapping_buttons(self):
        self.resize(520, 600)
        previous_columns = self.panel.access_buttons.columns
        for font in self.panel._fonts.values():
            font.configure(size=round(int(font.cget("size")) * 1.5))
        control_panel.tkfont.nametofont("TkDefaultFont", root=self.panel).configure(size=14)
        self.panel.update()
        self.assertLessEqual(self.panel.access_buttons.columns, previous_columns)
        self.assert_controls_fit()

    def test_log_wheel_is_independent_and_keyboard_focus_reveals_controls(self):
        self.resize(520, 480)
        self.panel.content_canvas.yview_moveto(0)
        before = self.panel.content_canvas.yview()
        result = self.panel._scroll_content(SimpleNamespace(widget=self.panel.log_text, num=5, delta=0))
        self.assertIsNone(result)
        self.assertEqual(self.panel.content_canvas.yview(), before)
        self.assertEqual(self.panel._scroll_content(SimpleNamespace(widget=self.panel.access_section, num=5, delta=0)), "break")
        self.panel._reveal_control(SimpleNamespace(widget=self.panel.maintenance_buttons.items[-1]))
        widget = self.panel.maintenance_buttons.items[-1]
        logical_bottom = widget.winfo_rooty() - self.panel.body.winfo_rooty() + widget.winfo_height()
        self.assertLessEqual(logical_bottom, self.panel.content_canvas.canvasy(0) + self.panel.content_canvas.winfo_height())


class ManualDailyReportTests(unittest.TestCase):
    def test_open_backtests_uses_default_browser_and_existing_web_error_handling(self):
        panel = SimpleNamespace(append_log=Mock())
        panel.open_url = lambda url, label: control_panel.ControlPanel.open_url(panel, url, label)
        with patch.object(control_panel.webbrowser, "open_new_tab", return_value=True) as browser:
            control_panel.ControlPanel.open_backtests(panel)
        browser.assert_called_once_with("http://127.0.0.1:8766/backtests")
        with (patch.object(control_panel.webbrowser, "open_new_tab", return_value=False),
              patch.object(control_panel.messagebox, "showerror") as error):
            control_panel.ControlPanel.open_backtests(panel)
        self.assertIn("http://127.0.0.1:8766/backtests", error.call_args.args[1])

    def panel(self):
        class Control:
            def __init__(self):
                self.state = "normal"
            def configure(self, state):
                self.state = state
        class Panel:
            busy = False
            factor_busy = False
            report_busy = False
            status_checking = False
            starting_services = False
            closing = False
            factor_process = None
            def __init__(self):
                self.action_buttons = [Control(), Control()]
                self.web_buttons = [Control(), Control(), Control()]
                self.utility_buttons = [Control()]
                self.factor_slot_box = Control()
                self.logs = []
                self.status = []
                self.error = None
                self.operation_text = SimpleNamespace(set=lambda value: None)
                self.status_text = SimpleNamespace(set=self.status.append)
            def _update_controls(self):
                control_panel.ControlPanel._update_controls(self)
            def set_busy(self, busy, operation="空闲"):
                control_panel.ControlPanel.set_busy(self, busy, operation)
            def append_log(self, message):
                self.logs.append(message)
            def after(self, delay, callback):
                callback()
            def manual_report_done(self, output):
                control_panel.ControlPanel.manual_report_done(self, output)
            def action_failed(self, detail):
                self.error = detail
                self.set_busy(False)
            def refresh_status(self, verbose):
                pass
        return Panel()

    def test_manual_report_disables_actions_but_allows_web_and_restores_controls(self):
        pending = []
        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                pending.append(self.target)
        panel = self.panel()
        panel.status_checking = True
        with (patch.object(control_panel.threading, "Thread", DeferredThread),
              patch.object(control_panel, "stream_command", return_value="日报2（收盘前复盘）已生成：test.md") as run):
            control_panel.ControlPanel.run_manual_report(panel)
            self.assertTrue(panel.report_busy)
            self.assertTrue(all(button.state == "disabled" for button in panel.action_buttons + panel.utility_buttons))
            self.assertTrue(all(button.state == "normal" for button in panel.web_buttons))
            control_panel.ControlPanel.run_manual_report(panel)
            self.assertEqual(len(pending), 1)
            pending.pop()()
        self.assertEqual(run.call_args.args[0], [str(control_panel.VENV_PYTHON), str(control_panel.DAILY_REPORT_SCRIPT), "--manual"])
        self.assertFalse(panel.busy)
        self.assertFalse(panel.report_busy)
        self.assertTrue(all(button.state == "normal" for button in panel.action_buttons + panel.web_buttons + panel.utility_buttons))
        self.assertIn("已生成", panel.status[-1])

    def test_manual_report_rejects_busy_starting_and_closing_states(self):
        for flag in ("busy", "starting_services", "closing"):
            panel = self.panel(); setattr(panel, flag, True)
            with patch.object(control_panel.threading, "Thread") as thread:
                control_panel.ControlPanel.run_manual_report(panel)
            thread.assert_not_called()
        panel = self.panel(); panel.busy = True; panel.report_busy = True; panel.status_checking = True
        panel._update_controls()
        self.assertTrue(all(button.state == "disabled" for button in panel.action_buttons + panel.utility_buttons))
        self.assertTrue(all(button.state == "normal" for button in panel.web_buttons))

    def test_manual_report_failure_and_holiday_release_busy_state(self):
        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        panel = self.panel()
        with (patch.object(control_panel.threading, "Thread", ImmediateThread),
              patch.object(control_panel, "stream_command", side_effect=RuntimeError("错误：行情服务未启动"))):
            control_panel.ControlPanel.run_manual_report(panel)
        self.assertEqual(panel.error, "错误：行情服务未启动")
        self.assertFalse(panel.busy)
        self.assertFalse(panel.report_busy)
        panel.set_busy(True)
        panel.manual_report_done("今日不是交易日，不生成或推送日报1。")
        self.assertFalse(panel.busy)
        self.assertTrue(any("已跳过" in line for line in panel.logs))


class BacktestPreparationTests(unittest.TestCase):
    def test_failed_preparation_does_not_start_backtest(self):
        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        class Panel:
            busy = False
            factor_busy = False
            closing = False
            status_text = SimpleNamespace(set=lambda value: None)
            def __init__(self):
                self.error = None
            def set_busy(self, busy, operation):
                self.busy = busy
            def append_log(self, message):
                pass
            def after(self, delay, callback):
                callback()
            def action_failed(self, detail):
                self.error = detail
                self.busy = False
                self.factor_busy = False
        for action in ("backtest", "backtest-all"):
            with tempfile.TemporaryDirectory() as directory:
                panel = Panel()
                with (patch.object(control_panel, "FACTOR_SETTINGS", Path(directory) / "factor.json"),
                      patch.object(control_panel, "backtest_data_missing", return_value=True),
                      patch.object(control_panel.messagebox, "showinfo"),
                      patch.object(control_panel, "stream_command", side_effect=RuntimeError("数据准备失败：预热缺失")) as run,
                      patch.object(control_panel.threading, "Thread", ImmediateThread)):
                    control_panel.write_json(control_panel.FACTOR_SETTINGS, {"strategy": "momentum"})
                    control_panel.ControlPanel.run_factor_action(panel, action, "回测中")
                self.assertEqual(run.call_count, 1)
                self.assertIn("factor_data_prep.py", run.call_args.args[0][1])
                self.assertIn("预热缺失", panel.error)
                self.assertFalse(panel.busy)

    def test_missing_inputs_trigger_preparation_and_validated_inputs_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            universe = store / "universe-history.csv"
            state = store / "market-state.csv"
            manifest = store / "source-manifest.json"
            with (patch.object(control_panel, "FACTOR_STORE", store),
                  patch.object(control_panel, "FACTOR_UNIVERSE", universe),
                  patch.object(control_panel, "FACTOR_MARKET_STATE", state),
                  patch.object(control_panel, "FACTOR_MANIFEST", manifest)):
                self.assertTrue(control_panel.backtest_data_missing())
                universe.write_text("day,symbol,known_at\n", encoding="utf-8")
                state.write_text("day,symbol,is_st\n", encoding="utf-8")
                (store / "bars").mkdir()
                (store / "bars" / "600001.csv").write_text("time,close\n", encoding="utf-8")
                control_panel.write_json(manifest, {
                    "validated": True,
                    "feature_warmup_validated": True,
                    "data_schema_version": 2,
                    "source_scope": "historical_vendor_reconstruction",
                    "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (universe, state)},
                    "bars": {"600001": hashlib.sha256((store / "bars" / "600001.csv").read_bytes()).hexdigest()},
                })
                self.assertFalse(control_panel.backtest_data_missing())
                bars_path = store / "bars" / "600001.csv"
                original = bars_path.read_bytes()
                bars_path.write_bytes(original + b"invalid\n")
                self.assertTrue(control_panel.backtest_data_missing())
                bars_path.write_bytes(original)
                self.assertFalse(control_panel.backtest_data_missing())
                old = control_panel.read_json(manifest)
                old.pop("feature_warmup_validated")
                control_panel.write_json(manifest, old)
                self.assertTrue(control_panel.backtest_data_missing())
                old["feature_warmup_validated"] = True
                old["data_schema_version"] = 1
                control_panel.write_json(manifest, old)
                self.assertTrue(control_panel.backtest_data_missing())

    def test_backtest_downloads_after_notice_then_runs(self):
        for action in ("backtest", "backtest-all", "rank"):
            commands = []

            class ImmediateThread:
                def __init__(self, target, daemon):
                    self.target = target

                def start(self):
                    self.target()

            class Panel:
                busy = False
                factor_busy = False
                closing = False
                status_text = SimpleNamespace(set=lambda value: None)

                def set_busy(self, busy, operation):
                    self.busy = busy

                def append_log(self, message):
                    pass

                def after(self, delay, callback):
                    callback()

                def factor_action_done(self, action, output):
                    commands.append((action, output))

            def run(args, on_output, on_process):
                commands.append(args)
                return "completed"

            with (tempfile.TemporaryDirectory() as directory,
                  patch.object(control_panel, "FACTOR_SETTINGS", Path(directory) / "factor.json"),
                  patch.object(control_panel, "backtest_data_missing", return_value=True),
                  patch.object(control_panel, "backtest_preparation_dates", return_value=("2025-09-04", "2026-10-09")),
                  patch.object(control_panel.messagebox, "showinfo") as notice,
                  patch.object(control_panel, "stream_command", side_effect=run),
                  patch.object(control_panel.threading, "Thread", ImmediateThread)):
                control_panel.write_json(control_panel.FACTOR_SETTINGS, {"strategy": "momentum"})
                control_panel.ControlPanel.run_factor_action(Panel(), action, "回测中")

            if action == "rank":
                notice.assert_not_called()
                self.assertEqual(commands[0], [str(control_panel.VENV_PYTHON), str(control_panel.FACTOR_SCRIPT), action, "--strategy", "momentum", "--scope", "full_market"])
                self.assertEqual(commands[1], (action, "completed"))
                continue
            notice.assert_called_once()
            self.assertEqual(commands[0], [str(control_panel.VENV_PYTHON), str(control_panel.FACTOR_PREP_SCRIPT),
                                           "--start", "2025-09-04", "--end", "2026-10-09"])
            self.assertEqual(commands[1], [str(control_panel.VENV_PYTHON), str(control_panel.FACTOR_SCRIPT), action, "--strategy", "momentum"])
            self.assertEqual(commands[2], (action, "completed"))


class StatusControlTests(unittest.TestCase):
    def test_startup_failure_releases_controls_and_stops_remaining_actions(self):
        pending = []

        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                pending.append(self.target)

        panel = SimpleNamespace(
            busy=False, starting_services=False, closing=False,
            operation_text=SimpleNamespace(set=Mock()), status_text=SimpleNamespace(set=Mock()),
            _update_controls=Mock(), append_log=Mock(), refresh_status=Mock(), track_startup_process=Mock(),
            after=lambda delay, callback: callback(),
        )
        panel.startup_failed = lambda detail: control_panel.ControlPanel.startup_failed(panel, detail)
        with (patch.object(control_panel.threading, "Thread", DeferredThread),
              patch.object(control_panel, "service_command", side_effect=RuntimeError("操作超时")) as run,
              patch.object(control_panel, "VENV_PYTHON", SimpleNamespace(exists=lambda: True))):
            control_panel.ControlPanel.start_all_services(panel)
            self.assertTrue(panel.starting_services)
            pending.pop()()
        self.assertFalse(panel.starting_services)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(panel._update_controls.call_count, 2)
        panel.refresh_status.assert_called_once()
        self.assertIn("操作超时", panel.append_log.call_args.args[0])

    def test_periodic_status_does_not_overlap_service_startup(self):
        panel = SimpleNamespace(busy=False, closing=False, status_checking=False, starting_services=True)
        with patch.object(control_panel.threading, "Thread") as thread:
            control_panel.ControlPanel.refresh_status(panel, False)
        thread.assert_not_called()
        self.assertFalse(panel.status_checking)

    def test_strategy_selection_and_slot_changes_preserve_settings(self):
        panel = SimpleNamespace(
            strategy_choice=SimpleNamespace(get=lambda: "Elastic Net 因子模型"),
            factor_slot=SimpleNamespace(get=lambda: "10:30"),
            update_factor_status=lambda: None, append_log=lambda message: None,
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(control_panel, "FACTOR_SETTINGS", Path(directory) / "settings.json"):
            control_panel.write_json(control_panel.FACTOR_SETTINGS, {"weekly_training_enabled": False, "backtest_slot": "14:00"})
            control_panel.ControlPanel.save_strategy(panel)
            control_panel.ControlPanel.save_factor_slot(panel)
            settings = control_panel.read_json(control_panel.FACTOR_SETTINGS)
            self.assertEqual(settings, {"strategy": "elastic_net", "backtest_slot": "10:30", "weekly_training_enabled": False})

    def test_status_checks_keep_controls_available_and_prevent_duplicate_checks(self):
        class Control:
            def __init__(self):
                self.state = "normal"

            def configure(self, state):
                self.state = state

        class Panel:
            busy = False
            factor_busy = False
            closing = False
            starting_services = False
            status_checking = False

            def __init__(self):
                self.action_buttons = [Control(), Control()]
                self.web_buttons = [Control(), Control()]
                self.utility_buttons = [Control(), Control(), Control()]
                self.factor_slot_box = Control()
                self.strategy_box = Control()
                self.results = []

            def _update_controls(self):
                control_panel.ControlPanel._update_controls(self)

            def _finish_status(self, **kwargs):
                control_panel.ControlPanel._finish_status(self, **kwargs)

            def after(self, delay, callback):
                callback()

            def append_log(self, message):
                pass

            def show_status(self, payload, verbose):
                self.results.append(payload)

            def status_failed(self, detail):
                self.results.append(detail)

        pending = []

        class DeferredThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                pending.append(self.target)

        panel = Panel()
        panel._update_controls()
        with (patch.object(control_panel.threading, "Thread", DeferredThread),
              patch.object(control_panel, "service_command", return_value='{"agent_task":"Ready"}')):
            control_panel.ControlPanel.refresh_status(panel, False)
            self.assertTrue(panel.status_checking)
            self.assertTrue(all(button.state == "normal" for button in panel.action_buttons + panel.web_buttons + panel.utility_buttons))
            self.assertEqual(panel.factor_slot_box.state, "readonly")
            self.assertEqual(panel.strategy_box.state, "readonly")
            control_panel.ControlPanel.refresh_status(panel, False)
            self.assertEqual(len(pending), 1)
            pending.pop()()

        self.assertEqual(panel.results, [{"agent_task": "Ready"}])
        self.assertFalse(panel.status_checking)
        self.assertTrue(all(button.state == "normal" for button in panel.action_buttons + panel.web_buttons + panel.utility_buttons))
        self.assertEqual(panel.factor_slot_box.state, "readonly")
        self.assertEqual(panel.strategy_box.state, "readonly")

        with (patch.object(control_panel.threading, "Thread", DeferredThread),
              patch.object(control_panel, "service_command", side_effect=RuntimeError("状态服务不可用"))):
            control_panel.ControlPanel.refresh_status(panel, True)
            self.assertTrue(all(button.state == "normal" for button in panel.action_buttons + panel.web_buttons + panel.utility_buttons))
            pending.pop()()
        self.assertEqual(panel.results[-1], "状态服务不可用")
        self.assertFalse(panel.status_checking)
        self.assertTrue(all(button.state == "normal" for button in panel.action_buttons + panel.web_buttons + panel.utility_buttons))

    def test_late_status_result_cannot_overwrite_a_newer_operation(self):
        for detail in (None, "状态检查超时"):
            with self.subTest(detail=detail):
                panel = SimpleNamespace(
                    busy=False, starting_services=False, closing=False, status_checking=True,
                    factor_busy=False, report_busy=False, operation_revision=0,
                    operation_text=SimpleNamespace(set=Mock()), _update_controls=Mock(),
                    show_status=Mock(), status_failed=Mock(),
                )
                # A check started at revision 0. The user finishes an operation
                # before the check's queued result reaches Tk's main thread.
                control_panel.ControlPanel.set_busy(panel, True, "生成日报中")
                control_panel.ControlPanel.set_busy(panel, False)
                control_panel.ControlPanel._finish_status(panel, payload={"report_site": False}, detail=detail, revision=0)
                panel.show_status.assert_not_called()
                panel.status_failed.assert_not_called()
                self.assertFalse(panel.status_checking)
                control_panel.ControlPanel._finish_status(panel, payload={"report_site": True}, revision=panel.operation_revision)
                panel.show_status.assert_called_once_with({"report_site": True}, False)

    def test_status_result_during_operation_preserves_busy_controls(self):
        for flag in ("busy", "starting_services", "closing"):
            panel = SimpleNamespace(
                busy=False, starting_services=False, closing=False, status_checking=True,
                _update_controls=Mock(), show_status=Mock(), status_failed=Mock(),
            )
            setattr(panel, flag, True)
            control_panel.ControlPanel._finish_status(panel, payload={"report_site": True})
            panel.show_status.assert_not_called()
            self.assertTrue(getattr(panel, flag))
            self.assertFalse(panel.status_checking)

    def test_web_buttons_remain_available_during_factor_work(self):
        class Control:
            def __init__(self):
                self.state = "normal"

            def configure(self, state):
                self.state = state

        panel = SimpleNamespace(
            action_buttons=[Control() for _ in range(3)],
            web_buttons=[Control() for _ in range(3)],
            utility_buttons=[Control()], factor_slot_box=Control(),
            busy=True, factor_busy=True, status_checking=False,
            starting_services=False, closing=False,
        )
        control_panel.ControlPanel._update_controls(panel)
        self.assertTrue(all(button.state == "normal" for button in panel.web_buttons))
        self.assertTrue(all(button.state == "disabled" for button in panel.action_buttons + panel.utility_buttons))
        self.assertEqual(panel.factor_slot_box.state, "disabled")

        panel.status_checking = True
        control_panel.ControlPanel._update_controls(panel)
        self.assertTrue(all(button.state == "normal" for button in panel.web_buttons))

        panel.status_checking = False
        panel.factor_busy = False
        control_panel.ControlPanel._update_controls(panel)
        self.assertTrue(all(button.state == "disabled" for button in panel.web_buttons))


if __name__ == "__main__":
    unittest.main()
