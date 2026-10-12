import io
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import factor_data_prep as prep


class BaoStockTransportTests(unittest.TestCase):
    def test_closed_socket_cannot_spin_waiting_for_more_bytes(self):
        connection = Mock()
        connection.recv.return_value = b""
        bounded = prep._BoundedBaoSocket(connection)
        bounded.deadline = 60
        with patch.object(prep.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(ConnectionError, "关闭了连接"):
                bounded.recv(8192)
        self.assertIsNotNone(bounded.failure)
        connection.recv.assert_called_once()

    def test_slow_partial_response_is_bounded_by_total_message_deadline(self):
        connection = Mock()
        connection.recv.return_value = b"partial"
        bounded = prep._BoundedBaoSocket(connection)
        bounded.deadline = 60
        with patch.object(prep.time, "monotonic", side_effect=[0, 59, 61]):
            self.assertEqual(bounded.recv(8192), b"partial")
            self.assertEqual(bounded.recv(8192), b"partial")
            with self.assertRaises(TimeoutError):
                bounded.recv(8192)
        self.assertEqual(connection.settimeout.call_args_list[0].args, (30,))
        self.assertEqual(connection.settimeout.call_args_list[1].args, (1,))

    def test_sdk_swallowed_transport_failure_is_raised_instead_of_partial_data(self):
        connection = Mock()
        connection.recv.side_effect = [b"partial", b""]
        context = SimpleNamespace(default_socket=connection)

        def sdk_send(message):
            try:
                context.default_socket.send(message.encode())
                while True:
                    context.default_socket.recv(8192)
            except OSError:
                return None

        transport = SimpleNamespace(send_msg=sdk_send)
        with patch.dict(sys.modules, {"baostock.common.context": context, "baostock.util.socketutil": transport}):
            prep._install_baostock_transport()
            installed = transport.send_msg
            prep._install_baostock_transport()
            self.assertIs(transport.send_msg, installed)
            with self.assertRaisesRegex(prep.BaoStockNetworkError, "响应未接收完整"):
                transport.send_msg("request")
        connection.sendall.assert_called_once_with(b"request")

    def test_login_connection_timeout_does_not_change_other_socket_defaults(self):
        previous = socket.getdefaulttimeout()

        def login():
            self.assertEqual(socket.getdefaulttimeout(), prep.BAOSTOCK_SOCKET_TIMEOUT)
            raise OSError("connection timeout")

        with (patch.object(prep, "_install_baostock_transport"), patch.object(prep, "_close_baostock_connection")):
            with self.assertRaises(prep.BaoStockNetworkError):
                prep._baostock_login(SimpleNamespace(login=login))
        self.assertEqual(socket.getdefaulttimeout(), previous)

    def test_pagination_error_cannot_publish_first_page_as_success(self):
        result = SimpleNamespace(error_code="0", fields=["value"], get_row_data=lambda: ["first page"])

        def next_page():
            if not hasattr(result, "visited"):
                result.visited = True
                return True
            result.error_code = "1"
            result.error_msg = "page failed"
            return False

        result.next = next_page
        with self.assertRaisesRegex(RuntimeError, "分页失败"):
            prep._bs_rows(result, "分钟线")

    def test_failed_request_reconnects_once_then_returns_complete_result(self):
        vendor = SimpleNamespace(query_history_k_data_plus=Mock(side_effect=[prep.BaoStockNetworkError("timeout"), object()]))
        complete = pd.DataFrame({"value": [1]})
        with (patch.dict(sys.modules, {"baostock.common.context": None}),
              patch.object(prep, "_baostock_login") as login,
              patch.object(prep, "_close_baostock_connection") as close,
              patch.object(prep, "_bs_rows", return_value=complete), redirect_stdout(io.StringIO())):
            self.assertIs(prep._minute_history(vendor, "600001", "2026-10-09", "2026-10-12"), complete)
        self.assertEqual(vendor.query_history_k_data_plus.call_count, 2)
        login.assert_called_once_with(vendor)
        close.assert_called_once()

    def test_permanent_network_failure_preserves_cache_and_reports_symbol(self):
        vendor = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0"), logout=lambda: None,
            query_history_k_data_plus=Mock(side_effect=prep.BaoStockNetworkError("timeout")),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bars/600001.csv"
            day = pd.DataFrame([{"time": t, "open": 10, "high": 10, "low": 10,
                                 "close": 10, "volume": 1000, "amount": 10000}
                                for t in prep._minute_times("2026-10-12")])
            prep._atomic_csv(day.iloc[:20], path)
            before = path.read_bytes()
            output = io.StringIO()
            with (patch.dict(sys.modules, {"baostock": vendor, "baostock.common.context": None}), redirect_stdout(output)):
                processed, failures = prep._fetch_minute_chunk([("600001", "2026-10-12"), ("600002", "2026-10-12")], "2026-10-12", root / "bars",
                    {"600001": ["2026-10-12"]}, root / "raw")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(vendor.query_history_k_data_plus.call_count, 2)
            self.assertEqual(len(failures), 1)
            self.assertIn("600001", failures[0])
            self.assertEqual(processed, 1)
            self.assertIn("已检查 1/2", output.getvalue())
            self.assertIn("耗时", output.getvalue())

    def test_complete_cache_does_not_need_a_network_login(self):
        vendor = SimpleNamespace(login=Mock(side_effect=AssertionError("network is unavailable")), logout=Mock())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = pd.DataFrame([{"time": t, "open": 10, "high": 10, "low": 10,
                                 "close": 10, "volume": 1000, "amount": 10000}
                                for t in prep._minute_times("2026-10-12")])
            prep._atomic_csv(day, root / "bars/600001.csv")
            with patch.dict(sys.modules, {"baostock": vendor}), redirect_stdout(io.StringIO()):
                processed, failures = prep._fetch_minute_chunk([("600001", "2026-10-12")], "2026-10-12", root / "bars",
                    {"600001": ["2026-10-12"]}, root / "raw")
            self.assertEqual(processed, 1)
            self.assertFalse(failures)
            vendor.login.assert_not_called()
            vendor.logout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
