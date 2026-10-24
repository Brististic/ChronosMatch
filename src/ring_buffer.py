import os
import mmap
import struct
import time
from typing import List, Tuple, Optional

# Binary Header Format:
# Head: uint64 (8 bytes) | Tail: uint64 (8 bytes) | Capacity: uint64 (8 bytes)
# Slot Size: uint32 (4 bytes) | Drops: uint64 (8 bytes)
HEADER_FORMAT = "<QQQIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
DEFAULT_SLOT_SIZE = 29


class MmapRingBuffer:
    """High-performance zero-copy shared memory circular ring buffer."""

    def __init__(self, filepath: str, capacity: int = 1024, slot_size: int = DEFAULT_SLOT_SIZE, create: bool = True):
        self.filepath = filepath
        self.capacity = capacity
        self.slot_size = slot_size
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

        if create:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            with open(filepath, "wb") as f:
                f.write(b"\x00" * self.total_size)

        self._f = open(filepath, "r+b")
        self._buf = mmap.mmap(self._f.fileno(), self.total_size)

        if create:
            self._write_header(0, 0, self.capacity, self.slot_size, 0)

    def _read_header(self) -> Tuple[int, int, int, int, int]:
        return struct.unpack_from(HEADER_FORMAT, self._buf, 0)

    def _write_header(self, head: int, tail: int, capacity: int, slot_size: int, drops: int) -> None:
        struct.pack_into(HEADER_FORMAT, self._buf, 0, head, tail, capacity, slot_size, drops)

    def write_batch(self, packets: List[bytes]) -> int:
        """Writes a batch of raw fixed-size packets. Drops if capacity is exceeded."""
        head, tail, capacity, slot_size, drops = self._read_header()
        unconsumed = (head - tail) % capacity if head >= tail else (capacity - (tail - head))
        available = capacity - unconsumed - 1

        to_write = min(len(packets), available)
        dropped = len(packets) - to_write

        for i in range(to_write):
            slot_idx = (head + i) % capacity
            offset = HEADER_SIZE + (slot_idx * slot_size)
            self._buf[offset : offset + slot_size] = packets[i]

        new_head = (head + to_write) % capacity
        new_drops = drops + dropped
        self._write_header(new_head, tail, capacity, slot_size, new_drops)
        return to_write

    def read_batch(self, max_count: int = 128) -> List[bytes]:
        """Reads up to max_count raw packets from the buffer."""
        head, tail, capacity, slot_size, drops = self._read_header()
        if head == tail:
            return []

        unconsumed = (head - tail) % capacity if head >= tail else (capacity - (tail - head))
        to_read = min(max_count, unconsumed)

        packets = []
        for i in range(to_read):
            slot_idx = (tail + i) % capacity
            offset = HEADER_SIZE + (slot_idx * slot_size)
            packets.append(bytes(self._buf[offset : offset + slot_size]))

        new_tail = (tail + to_read) % capacity
        self._write_header(head, new_tail, capacity, slot_size, drops)
        return packets

    def compact_ring_buffer(self, lock) -> int:
        """Day 14 Member 1: Compacts active unconsumed messages to index 0 and realigns head/tail counters."""
        with lock:
            head, tail, capacity, slot_size, drops = self._read_header()
            unconsumed = (head - tail) % capacity
            if unconsumed == 0:
                self._write_header(0, 0, capacity, slot_size, 0)
                return 0

            compacted_data = bytearray()
            for i in range(unconsumed):
                idx = (tail + i) % capacity
                offset = HEADER_SIZE + (idx * slot_size)
                compacted_data.extend(self._buf[offset : offset + slot_size])

            dest_offset = HEADER_SIZE
            self._buf[dest_offset : dest_offset + len(compacted_data)] = compacted_data
            self._write_header(unconsumed, 0, capacity, slot_size, 0)
            return unconsumed

    def resize_ring_buffer(self, new_capacity: int, lock) -> bool:
        """Day 15 Member 1: Dynamically expands buffer capacity without dropping unconsumed packets."""
        if new_capacity <= self.capacity:
            return False

        with lock:
            head, tail, old_capacity, slot_size, drops = self._read_header()
            unconsumed = (head - tail) % old_capacity

            # Extract unconsumed items in order
            active_items = []
            for i in range(unconsumed):
                idx = (tail + i) % old_capacity
                offset = HEADER_SIZE + (idx * slot_size)
                active_items.append(bytes(self._buf[offset : offset + slot_size]))

            # On Windows, close mmap and file before resizing
            new_total_size = HEADER_SIZE + (new_capacity * slot_size)
            self._buf.close()
            self._f.close()

            # Resize the underlying file on disk
            with open(self.filepath, "a+b") as f:
                current_len = f.tell()
                if new_total_size > current_len:
                    f.write(b"\x00" * (new_total_size - current_len))

            # Re-open file and re-map
            self._f = open(self.filepath, "r+b")
            self._buf = mmap.mmap(self._f.fileno(), new_total_size)
            self.capacity = new_capacity
            self.total_size = new_total_size

            # Write active items sequentially starting from slot 0
            for i, item in enumerate(active_items):
                dest_offset = HEADER_SIZE + (i * slot_size)
                self._buf[dest_offset : dest_offset + slot_size] = item

            # Update header: head = unconsumed, tail = 0, new capacity
            self._write_header(unconsumed, 0, new_capacity, slot_size, drops)
            return True

    def close(self):
        if hasattr(self, "_buf") and not self._buf.closed:
            self._buf.close()
        if hasattr(self, "_f") and not self._f.closed:
            self._f.close()
