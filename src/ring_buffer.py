import mmap
import os
import struct
from src.schema import ORDER_SIZE


HEADER_FORMAT = "<QQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  


class MmapRingBuffer:
    """Zero-copy circular ring buffer implemented over shared memory via mmap."""

    def __init__(self, filename: str, capacity: int = 10_000, create: bool = True):
        self.filename = filename
        self.capacity = capacity
        self.slot_size = ORDER_SIZE
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)
        self.file_obj = None
        self.buf = None

        if create:
            self.file_obj = open(self.filename, "w+b")
            self.file_obj.seek(self.total_size - 1)
            self.file_obj.write(b"\x00")
            self.file_obj.flush()
            self.buf = mmap.mmap(self.file_obj.fileno(), self.total_size)
            self._write_header(0, 0)
        else:
            if not os.path.exists(self.filename):
                raise FileNotFoundError(f"Buffer file {self.filename} does not exist.")
            self.file_obj = open(self.filename, "r+b")
            self.buf = mmap.mmap(self.file_obj.fileno(), self.total_size)

    def _read_header(self) -> tuple[int, int]:
        self.buf.seek(0)
        raw = self.buf.read(HEADER_SIZE)
        return struct.unpack(HEADER_FORMAT, raw)

    def _write_header(self, write_head: int, read_head: int):
        self.buf.seek(0)
        self.buf.write(struct.pack(HEADER_FORMAT, write_head, read_head))

    def write(self, data: bytes) -> bool:
        """Writes an exact ORDER_SIZE byte slice into the circular ring buffer."""
        if len(data) != self.slot_size:
            raise ValueError(f"Payload size must be {self.slot_size} bytes, got {len(data)}")

        write_head, read_head = self._read_header()

        if (write_head - read_head) >= self.capacity:
            return False

        slot_idx = write_head % self.capacity
        offset = HEADER_SIZE + (slot_idx * self.slot_size)

        self.buf.seek(offset)
        self.buf.write(data)

        self._write_header(write_head + 1, read_head)
        return True

    def read(self) -> bytes | None:
        """Reads the next available binary slot from the circular ring buffer."""
        write_head, read_head = self._read_header()

        if read_head >= write_head:
            return None

        slot_idx = read_head % self.capacity
        offset = HEADER_SIZE + (slot_idx * self.slot_size)

        self.buf.seek(offset)
        data = self.buf.read(self.slot_size)

        self._write_header(write_head, read_head + 1)
        return data

    def close(self):
        if self.buf and not self.buf.closed:
            self.buf.close()
        if self.file_obj and not self.file_obj.closed:
            self.file_obj.close()