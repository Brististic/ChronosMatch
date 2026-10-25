import os
import mmap
import time
import struct


class MmapProcessLock:
    def __init__(self, filepath: str, create: bool = True):
        self.filepath = filepath
        self.create = create

        if create:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            with open(self.filepath, "a+b") as f:
                if f.tell() < 1:
                    f.write(b"\x00")

        self._f = open(self.filepath, "r+b")
        self._buf = mmap.mmap(self._f.fileno(), 1)

    def acquire(self, timeout_sec: float = 1.0) -> bool:
        start = time.perf_counter()
        while time.perf_counter() - start < timeout_sec:
            if self._buf[0] == 0:
                self._buf[0] = 1
                return True
            time.sleep(0.0001)
        return False

    def release(self):
        if self._buf and not self._buf.closed:
            self._buf[0] = 0

    def is_locked(self) -> bool:
        if self._buf is None or self._buf.closed:
            return False
        return self._buf[0] == 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def close(self):
        if hasattr(self, "_buf") and self._buf is not None and not self._buf.closed:
            self._buf.close()
        if hasattr(self, "_f") and self._f is not None and not self._f.closed:
            self._f.close()
