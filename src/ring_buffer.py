import os
import mmap
import struct
from typing import List, Tuple

HEADER_FORMAT = "<IIIIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
DEFAULT_SLOT_SIZE = 29


class MmapRingBuffer:
    def __init__(
        self,
        filepath: str,
        capacity: int = 1024,
        slot_size: int = DEFAULT_SLOT_SIZE,
        create: bool = True,
    ):
        self.filepath = filepath
        self.capacity = capacity
        self.slot_size = slot_size
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

        if create:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            with open(self.filepath, "a+b") as f:
                f.seek(0, os.SEEK_END)
                current_len = f.tell()
                if current_len < self.total_size:
                    f.write(b"\x00" * (self.total_size - current_len))

        self._f = open(self.filepath, "r+b")
        self._buf = mmap.mmap(self._f.fileno(), self.total_size)

        if create:
            head, tail, cap, sz, drops = self._read_header()
            if cap == 0 or sz == 0:
                self._write_header(0, 0, self.capacity, self.slot_size, 0)
        else:
            _, _, self.capacity, self.slot_size, _ = self._read_header()

    def _read_header(self) -> Tuple[int, int, int, int, int]:
        return struct.unpack(HEADER_FORMAT, self._buf[:HEADER_SIZE])

    def _write_header(self, head: int, tail: int, capacity: int, slot_size: int, drops: int):
        self._buf[:HEADER_SIZE] = struct.pack(HEADER_FORMAT, head, tail, capacity, slot_size, drops)

    def write_batch(self, packets: List[bytes]) -> int:
        if not packets:
            return 0

        head, tail, capacity, slot_size, drops = self._read_header()
        free_slots = capacity - ((head - tail) % capacity) - 1

        written = 0
        new_drops = drops
        for pkt in packets:
            if len(pkt) != slot_size:
                continue
            if free_slots <= 0:
                new_drops += 1
                continue

            offset = HEADER_SIZE + ((head % capacity) * slot_size)
            self._buf[offset : offset + slot_size] = pkt
            head = (head + 1) % (2 * capacity)
            free_slots -= 1
            written += 1

        self._write_header(head, tail, capacity, slot_size, new_drops)
        return written

    def read_batch(self, max_count: int = 64) -> List[bytes]:
        head, tail, capacity, slot_size, drops = self._read_header()
        available = (head - tail) % capacity
        if available == 0:
            return []

        count = min(available, max_count)
        read_items = []
        for _ in range(count):
            offset = HEADER_SIZE + ((tail % capacity) * slot_size)
            read_items.append(bytes(self._buf[offset : offset + slot_size]))
            tail = (tail + 1) % (2 * capacity)

        self._write_header(head, tail, capacity, slot_size, drops)
        return read_items

    def compact_ring_buffer(self, lock) -> int:
        """Day 14 Member 1: In-place zero-copy defragmentation shifting unconsumed items to index 0."""
        with lock:
            head, tail, capacity, slot_size, drops = self._read_header()
            unconsumed = (head - tail) % capacity
            if unconsumed == 0:
                self._write_header(0, 0, capacity, slot_size, 0)
                return 0

            active_items = []
            for i in range(unconsumed):
                idx = (tail + i) % capacity
                offset = HEADER_SIZE + (idx * slot_size)
                active_items.append(bytes(self._buf[offset : offset + slot_size]))

            for i, item in enumerate(active_items):
                dest_offset = HEADER_SIZE + (i * slot_size)
                self._buf[dest_offset : dest_offset + slot_size] = item

            self._write_header(unconsumed, 0, capacity, slot_size, 0)
            return unconsumed

    def resize_ring_buffer(self, new_capacity: int, lock) -> bool:
        """Day 15 Member 1: Dynamically expands capacity on the fly without dropping unconsumed packets."""
        if new_capacity <= self.capacity:
            return False

        with lock:
            head, tail, old_capacity, slot_size, drops = self._read_header()
            unconsumed = (head - tail) % old_capacity

            active_items = []
            for i in range(unconsumed):
                idx = (tail + i) % old_capacity
                offset = HEADER_SIZE + (idx * slot_size)
                active_items.append(bytes(self._buf[offset : offset + slot_size]))

            new_total_size = HEADER_SIZE + (new_capacity * slot_size)

            self._buf.close()
            self._f.close()

            with open(self.filepath, "a+b") as f:
                current_len = f.tell()
                if new_total_size > current_len:
                    f.write(b"\x00" * (new_total_size - current_len))

            self._f = open(self.filepath, "r+b")
            self._buf = mmap.mmap(self._f.fileno(), new_total_size)
            self.capacity = new_capacity
            self.total_size = new_total_size

            for i, item in enumerate(active_items):
                dest_offset = HEADER_SIZE + (i * slot_size)
                self._buf[dest_offset : dest_offset + slot_size] = item

            self._write_header(unconsumed, 0, new_capacity, slot_size, drops)
            return True

    def close(self):
        if hasattr(self, "_buf") and not self._buf.closed:
            self._buf.close()
        if hasattr(self, "_f") and not self._f.closed:
            self._f.close()
