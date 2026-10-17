"""Exit with the owning web process without blocking Python's standard IO."""
import os
import threading
import time

def watch_owner():
    owners = {int(os.getenv("FINANCE_OWNER_PID") or os.getppid()), os.getppid()}
    if any(owner <= 0 for owner in owners):
        raise RuntimeError("网页进程标识无效。")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        kernel.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []
        for owner in owners:
            handle = kernel.OpenProcess(0x00100000, False, owner)  # SYNCHRONIZE only.
            if not handle:
                for opened in handles:
                    kernel.CloseHandle(opened)
                raise RuntimeError(f"无法监控所属网页进程 {owner}。")
            handles.append(handle)
        array = (wintypes.HANDLE * len(handles))(*handles)
        def monitor():
            try:
                if kernel.WaitForMultipleObjects(len(handles), array, False, 0xFFFFFFFF) < len(handles):
                    os._exit(0)
            finally:
                for handle in handles:
                    kernel.CloseHandle(handle)
    else:
        def monitor():
            while True:
                try:
                    for owner in owners:
                        os.kill(owner, 0)
                except ProcessLookupError:
                    os._exit(0)
                time.sleep(1)
    thread = threading.Thread(target=monitor, name="web-process-owner", daemon=True)
    thread.start()
    return thread
