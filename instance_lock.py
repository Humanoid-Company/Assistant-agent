"""Single-instance PID lock for main.py — see config.PID_FILE for why this exists."""
from __future__ import annotations

import os
import sys
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """Raised by SingleInstanceLock.acquire() when another live process holds the lock."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"Another instance is already running (PID {pid}).")
        self.pid = pid


def _pid_is_alive(pid: int) -> bool:
    """True if `pid` belongs to a live process.

    On Windows, ``os.kill(huge_pid, 0)`` can hang — use OpenProcess instead and reject
    out-of-range PIDs early.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    # Windows process IDs are 32-bit; absurd values must not call into the kernel.
    if pid >= 2**28:
        return False

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # 5 = ERROR_ACCESS_DENIED → process exists but we can't open it
        return kernel32.GetLastError() == 5

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SingleInstanceLock:
    """Claims `path` as a PID file for the lifetime of this process. A leftover file from a
    process that's no longer alive (crash, `kill -9`, power loss — anything that skips the
    normal release()) is treated as stale and silently reclaimed rather than blocking forever."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._acquired = False

    def acquire(self) -> None:
        if self._path.exists():
            try:
                existing_pid = int(self._path.read_text().strip())
            except (ValueError, OSError):
                existing_pid = None
            if existing_pid is not None and _pid_is_alive(existing_pid):
                raise AlreadyRunningError(existing_pid)
        self._path.write_text(str(os.getpid()))
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False
