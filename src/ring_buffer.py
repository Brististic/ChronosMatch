import mmap
import os
import struct
from typing import Optional, List

HEADER_FORMAT = "<IIIIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class MmapRingBuffer:
    """Zero-copy memory-mapped circular ring buffer with batching and overrun telemetry."""

    def __init__(
        self,
        filepath: str,
        slot_size: int = 29,
        capacity: int = 1024,
        create: bool = False,
    ):
        self.filepath = filepath
        self.slot_size = slot_size
        self.capacity = capacity
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

        if create:
            with open(self.filepath, "wb") as f:
                f.write(b"\x00" * self.total_size)
            self._fd = os.open(self.filepath, os.O_RDWR)
            self._buf = mmap.mmap(self._fd, self.total_size, access=mmap.ACCESS_WRITE)
            self._write_header(0, 0, self.capacity, self.slot_size, 0)
        else:
            if not os.path.exists(self.filepath):
                raise FileNotFoundError(f"Buffer file '{self.filepath}' not found.")
            self._fd = os.open(self.filepath, os.O_RDWR)
            self._buf = mmap.mmap(self._fd, 0, access=mmap.ACCESS_WRITE)
            head, tail, cap, size, drops = self._read_header()
            self.capacity = cap
            self.slot_size = size
            self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

    def _read_header(self) -> tuple[int, int, int, int, int]:
        return struct.unpack_from(HEADER_FORMAT, self._buf, 0)

    def _write_header(self, head: int, tail: int, capacity: int, slot_size: int, drops: int) -> None:
        struct.pack_into(HEADER_FORMAT, self._buf, 0, head, tail, capacity, slot_size, drops)

    def _get_occupied_slots(self, head: int, tail: int) -> int:
        if head >= tail:
            return head - tail
        return self.capacity - (tail - head)

    def _get_free_slots(self, head: int, tail: int) -> int:
        return (self.capacity - 1) - self._get_occupied_slots(head, tail)

    def write(self, data: bytes) -> bool:
        """Write single slot."""
        if len(data) != self.slot_size:
            raise ValueError(f"Packet size mismatch. Expected {self.slot_size}, got {len(data)}.")

        head, tail, cap, size, drops = self._read_header()
        if self._get_free_slots(head, tail) == 0:
            # Overrun drop
            self._write_header(head, tail, cap, size, drops + 1)
            return False

        offset = HEADER_SIZE + (head * self.slot_size)
        self._buf[offset : offset + self.slot_size] = data
        new_head = (head + 1) % self.capacity
        self._write_header(new_head, tail, cap, size, drops)
        return True

    def write_batch(self, packets: List[bytes]) -> int:
        """Batch write. Returns count of successfully written packets."""
        head, tail, cap, size, drops = self._read_header()
        free_slots = self._get_free_slots(head, tail)
        to_write = min(len(packets), free_slots)
        dropped = len(packets) - to_write

        current_head = head
        for i in range(to_write):
            pkt = packets[i]
            if len(pkt) != self.slot_size:
                raise ValueError(f"Packet {i} invalid length {len(pkt)}. Expected {self.slot_size}.")
            offset = HEADER_SIZE + (current_head * self.slot_size)
            self._buf[offset : offset + self.slot_size] = pkt
            current_head = (current_head + 1) % self.capacity

        self._write_header(current_head, tail, cap, size, drops + dropped)
        return to_write

    def read(self) -> Optional[bytes]:
        """Read single slot."""
        head, tail, cap, size, drops = self._read_header()
        if head == tail:
            return None

        offset = HEADER_SIZE + (tail * self.slot_size)
        data = bytes(self._buf[offset : offset + self.slot_size])
        new_tail = (tail + 1) % self.capacity
        self._write_header(head, new_tail, cap, size, drops)
        return data

    def read_batch(self, max_count: int = 100) -> List[bytes]:
        """Batch read up to max_count items."""
        head, tail, cap, size, drops = self._read_header()
        occupied = self._get_occupied_slots(head, tail)
        to_read = min(max_count, occupied)
        if to_read == 0:
            return []

        results = []
        current_tail = tail
        for _ in range(to_read):
            offset = HEADER_SIZE + (current_tail * self.slot_size)
            results.append(bytes(self._buf[offset : offset + self.slot_size]))
            current_tail = (current_tail + 1) % self.capacity

        self._write_header(head, current_tail, cap, size, drops)
        return results

    def get_metrics(self) -> dict:
        """Returns buffer state: head, tail, occupied, drops."""
        head, tail, cap, size, drops = self._read_header()
        return {
            "head": head,
            "tail": tail,
            "capacity": cap,
            "occupied": self._get_occupied_slots(head, tail),
            "free": self._get_free_slots(head, tail),
            "drops": drops,
        }

    def close(self) -> None:
        if hasattr(self, "_buf") and self._buf:
            self._buf.close()
        if hasattr(self, "_fd") and self._fd:
            os.close(self._fd)