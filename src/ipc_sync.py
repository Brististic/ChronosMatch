import mmap
import os
import struct
import time

LOCK_FORMAT = "<II"
LOCK_SIZE = struct.calcsize(LOCK_FORMAT)  


class MmapProcessLock:
    """Inter-process spinlock backed by a shared memory-mapped binary flag."""

    def __init__(self, filename: str, spin_timeout_sec: float = 2.0, create: bool = True):
        self.filename = filename
        self.spin_timeout_sec = spin_timeout_sec
        self.file_obj = None
        self.buf = None

        if create:
            self.file_obj = open(self.filename, "w+b")
            self.file_obj.seek(LOCK_SIZE - 1)
            self.file_obj.write(b"\x00")
            self.file_obj.flush()
            self.buf = mmap.mmap(self.file_obj.fileno(), LOCK_SIZE)
            self._write_state(0, 0)
        else:
            if not os.path.exists(self.filename):
                raise FileNotFoundError(f"Lock file {self.filename} does not exist.")
            self.file_obj = open(self.filename, "r+b")
            self.buf = mmap.mmap(self.file_obj.fileno(), LOCK_SIZE)

    def _read_state(self) -> tuple[int, int]:
        self.buf.seek(0)
        raw = self.buf.read(LOCK_SIZE)
        return struct.unpack(LOCK_FORMAT, raw)

    def _write_state(self, locked: int, pid: int):
        self.buf.seek(0)
        self.buf.write(struct.pack(LOCK_FORMAT, locked, pid))

    def acquire(self) -> bool:
        """Acquires lock via spin-wait until acquired or timeout occurs."""
        my_pid = os.getpid()
        start = time.perf_counter()

        while True:
            locked, pid = self._read_state()
            if locked == 0:
                # Claim lock
                self._write_state(1, my_pid)
                # Verify ownership (primitive mutual exclusion barrier)
                chk_locked, chk_pid = self._read_state()
                if chk_locked == 1 and chk_pid == my_pid:
                    return True

            if time.perf_counter() - start > self.spin_timeout_sec:
                return False

            time.sleep(0.0001)

    def release(self):
        """Releases the lock."""
        self._write_state(0, 0)

    def __enter__(self):
        acquired = self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire IPC process lock within {self.spin_timeout_sec}s")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def close(self):
        if self.buf and not self.buf.closed:
            self.buf.close()
        if self.file_obj and not self.file_obj.closed:
            self.file_obj.close()