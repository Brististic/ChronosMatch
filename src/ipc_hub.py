import os
import time
from typing import List, Dict
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.schema import ORDER_STRUCT_SIZE, FILL_STRUCT_SIZE


class MultiRingIPCHub:
    def __init__(
        self,
        symbols: List[str],
        base_dir: str = "shared_mem",
        capacity_per_ring: int = 1024,
        create: bool = True,
        high_watermark: float = 0.85,
    ):
        self.symbols = symbols
        self.base_dir = base_dir
        self.capacity_per_ring = capacity_per_ring
        self.create = create
        self.high_watermark = high_watermark

        self.ring_buffers: Dict[str, MmapRingBuffer] = {}
        self.locks: Dict[str, MmapProcessLock] = {}

        self.fill_rings: Dict[str, MmapRingBuffer] = {}
        self.fill_locks: Dict[str, MmapProcessLock] = {}

        os.makedirs(self.base_dir, exist_ok=True)

        for sym in self.symbols:
            buf_path = os.path.join(self.base_dir, f"{sym}_ring.bin")
            lock_path = os.path.join(self.base_dir, f"{sym}_lock.bin")
            self.ring_buffers[sym] = MmapRingBuffer(
                buf_path, capacity=self.capacity_per_ring, slot_size=ORDER_STRUCT_SIZE, create=self.create
            )
            self.locks[sym] = MmapProcessLock(lock_path, create=self.create)

    def init_execution_ring(self, symbol: str, capacity: int = 1024):
        buf_path = os.path.join(self.base_dir, f"{symbol}_fills.bin")
        lock_path = os.path.join(self.base_dir, f"{symbol}_fills.lock")

        self.fill_rings[symbol] = MmapRingBuffer(
            buf_path, capacity=capacity, slot_size=FILL_STRUCT_SIZE, create=self.create
        )
        self.fill_locks[symbol] = MmapProcessLock(lock_path, create=self.create)

    def write_fill(self, symbol: str, fill_packet: bytes) -> bool:
        if symbol not in self.fill_rings:
            self.init_execution_ring(symbol)

        with self.fill_locks[symbol]:
            written = self.fill_rings[symbol].write_batch([fill_packet])
            return written > 0

    def read_fills(self, symbol: str, max_count: int = 64) -> List[bytes]:
        if symbol not in self.fill_rings:
            return []

        with self.fill_locks[symbol]:
            return self.fill_rings[symbol].read_batch(max_count=max_count)

    def get_ring_occupancy(self, symbol: str) -> float:
        rb = self.ring_buffers[symbol]
        head, tail, capacity, _, _ = rb._read_header()
        unconsumed = (head - tail) % capacity
        return unconsumed / float(capacity)

    def write_symbol_batch(self, symbol: str, packets: List[bytes]) -> int:
        with self.locks[symbol]:
            return self.ring_buffers[symbol].write_batch(packets)

    def write_symbol_batch_with_backpressure(self, symbol: str, packets: List[bytes], max_retries: int = 5) -> int:
        for _ in range(max_retries):
            occupancy = self.get_ring_occupancy(symbol)
            if occupancy < self.high_watermark:
                return self.write_symbol_batch(symbol, packets)
            time.sleep(0.0001)
        return self.write_symbol_batch(symbol, packets)

    def read_symbol_batch(self, symbol: str, max_count: int = 64) -> List[bytes]:
        with self.locks[symbol]:
            return self.ring_buffers[symbol].read_batch(max_count=max_count)

    def round_robin_read(self, max_count_per_symbol: int = 64) -> Dict[str, List[bytes]]:
        result = {}
        for sym in self.symbols:
            result[sym] = self.read_symbol_batch(sym, max_count=max_count_per_symbol)
        return result

    def close(self):
        for rb in self.ring_buffers.values():
            rb.close()
        for lk in self.locks.values():
            lk.close()
        for fr in self.fill_rings.values():
            fr.close()
        for fl in self.fill_locks.values():
            fl.close()

    def cleanup_files(self):
        for sym in self.symbols:
            for ext in [f"{sym}_ring.bin", f"{sym}_lock.bin", f"{sym}_fills.bin", f"{sym}_fills.lock"]:
                p = os.path.join(self.base_dir, ext)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
