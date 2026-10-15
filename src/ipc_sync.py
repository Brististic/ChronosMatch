import mmap
import os
import struct
import sys
import time
from typing import Optional

# Layout: [lock_flag (uint32), owner_pid (uint32)]
LOCK_FORMAT = "<II"
LOCK_SIZE = struct.calcsize(LOCK_FORMAT)


class MmapProcessLock:
    """Inter-process spinlock with PID tracking and stale lock recovery."""

    def __init__(
        self,
        filepath: str,
        spin_timeout_sec: float = 1.0,
        stale_timeout_sec: float = 2.0,
        create: bool = False,
    ):
        self.filepath = filepath
        self.spin_timeout_sec = spin_timeout_sec
        self.stale_timeout_sec = stale_timeout_sec

        if create:
            with open(self.filepath, "wb") as f:
                f.write(b"\x00" * LOCK_SIZE)
            self._fd = os.open(self.filepath, os.O_RDWR)
            self._buf = mmap.mmap(self._fd, LOCK_SIZE, access=mmap.ACCESS_WRITE)
            self._write_state(0, 0)
        else:
            if not os.path.exists(self.filepath):
                raise FileNotFoundError(f"Lock file '{self.filepath}' not found.")
            self._fd = os.open(self.filepath, os.O_RDWR)
            self._buf = mmap.mmap(self._fd, LOCK_SIZE, access=mmap.ACCESS_WRITE)

    def _read_state(self) -> tuple[int, int]:
        return struct.unpack_from(LOCK_FORMAT, self._buf, 0)

    def _write_state(self, flag: int, pid: int) -> None:
        struct.pack_into(LOCK_FORMAT, self._buf, 0, flag, pid)

    def _is_pid_alive(self, pid: int) -> bool:
        """Cross-platform check if PID is alive without sending termination signals."""
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True

        if sys.platform == "win32":
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000, SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle == 0:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def acquire(self, timeout_sec: Optional[float] = None) -> bool:
        timeout = timeout_sec if timeout_sec is not None else self.spin_timeout_sec
        start = time.perf_counter()
        my_pid = os.getpid()

        while True:
            flag, owner_pid = self._read_state()

            # Free to take
            if flag == 0:
                self._write_state(1, my_pid)
                f, p = self._read_state()
                if f == 1 and p == my_pid:
                    return True

            # If locked by ourselves, don't re-acquire or deadlock
            elif owner_pid == my_pid:
                pass

            # Stale lock recovery if holding PID is dead
            elif not self._is_pid_alive(owner_pid):
                self._write_state(1, my_pid)
                f, p = self._read_state()
                if f == 1 and p == my_pid:
                    return True

            if (time.perf_counter() - start) >= timeout:
                return False

            time.sleep(0.0005)

    def release(self) -> None:
        self._write_state(0, 0)

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("Failed to acquire process lock.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def close(self) -> None:
        if hasattr(self, "_buf") and self._buf:
            self._buf.close()
        if hasattr(self, "_fd") and self._fd:
            os.close(self._fd)
