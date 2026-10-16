import os
import tempfile
from typing import Dict, List, Optional
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock


class MultiRingIPCHub:
    """Manages dedicated ring buffer and lock pairs per trading symbol to eliminate cross-symbol lock contention."""

    def __init__(self, symbols: List[str], base_dir: Optional[str] = None, capacity_per_ring: int = 2048, create: bool = True):
        self.symbols = symbols
        self.capacity = capacity_per_ring
        self.base_dir = base_dir or tempfile.gettempdir()
        self.create = create

        self.ring_buffers: Dict[str, MmapRingBuffer] = {}
        self.locks: Dict[str, MmapProcessLock] = {}
        self.paths: Dict[str, Dict[str, str]] = {}

        self._initialize_channels()

    def _initialize_channels(self):
        for sym in self.symbols:
            buf_path = os.path.join(self.base_dir, f"chronos_{sym}.bin")
            lock_path = os.path.join(self.base_dir, f"chronos_{sym}.lock")
            self.paths[sym] = {"buf": buf_path, "lock": lock_path}

            self.ring_buffers[sym] = MmapRingBuffer(buf_path, capacity=self.capacity, create=self.create)
            self.locks[sym] = MmapProcessLock(lock_path, create=self.create)

    def write_symbol_batch(self, symbol: str, raw_orders: List[bytes]) -> int:
        """Writes a batch of orders directly into the symbol-specific ring buffer."""
        if symbol not in self.ring_buffers:
            raise KeyError(f"Unknown symbol: {symbol}")

        with self.locks[symbol]:
            return self.ring_buffers[symbol].write_batch(raw_orders)

    def read_symbol_batch(self, symbol: str, max_count: int = 64) -> List[bytes]:
        """Reads a batch of orders from the symbol-specific ring buffer."""
        if symbol not in self.ring_buffers:
            raise KeyError(f"Unknown symbol: {symbol}")

        with self.locks[symbol]:
            return self.ring_buffers[symbol].read_batch(max_count=max_count)

    def round_robin_read(self, max_count_per_symbol: int = 32) -> Dict[str, List[bytes]]:
        """Fair round-robin drain across all registered symbol channels."""
        drained: Dict[str, List[bytes]] = {}
        for sym in self.symbols:
            drained[sym] = self.read_symbol_batch(sym, max_count=max_count_per_symbol)
        return drained

    def close(self):
        """Closes all mapped file handles and spinlocks."""
        for sym in self.symbols:
            self.ring_buffers[sym].close()
            self.locks[sym].close()

    def cleanup_files(self):
        """Removes backing mmap files."""
        for sym in self.symbols:
            for p in self.paths[sym].values():
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
