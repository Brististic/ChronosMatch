import os
import json
import time
from typing import Dict, Any, Optional
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock


class SnapshotManager:
    """Manages atomic point-in-time ring state checkpoints and recovery metadata."""

    def __init__(self, snapshot_dir: str):
        self.snapshot_dir = snapshot_dir
        os.makedirs(snapshot_dir, exist_ok=True)

    def _meta_path(self, symbol: str) -> str:
        return os.path.join(self.snapshot_dir, f"{symbol}_snapshot.json")

    def capture_channel_snapshot(
        self,
        symbol: str,
        ring_buffer: MmapRingBuffer,
        lock: MmapProcessLock,
        engine_stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Atomically captures ring buffer write/read offsets and execution metrics under lock."""
        with lock:
            header = ring_buffer._read_header()
            # head, tail, capacity, slot_size, drops
            head, tail, capacity = header[0], header[1], header[2]
            slot_size = header[3] if len(header) > 3 else ring_buffer.slot_size
            drops = header[4] if len(header) > 4 else 0

        snapshot_payload = {
            "symbol": symbol,
            "timestamp_ns": time.perf_counter_ns(),
            "write_idx": head,
            "read_idx": tail,
            "capacity": capacity,
            "slot_size": slot_size,
            "dropped_count": drops,
            "unconsumed_count": (head - tail) % capacity,
            "engine_stats": engine_stats or {},
        }

        tmp_path = self._meta_path(symbol) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_payload, f, indent=2)
        os.replace(tmp_path, self._meta_path(symbol))

        return snapshot_payload

    def load_latest_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Loads the most recent validated state snapshot for a symbol."""
        path = self._meta_path(symbol)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def recover_ring_offsets(
        self,
        symbol: str,
        ring_buffer: MmapRingBuffer,
        lock: MmapProcessLock,
    ) -> bool:
        """Restores committed read/write indices into an active ring buffer header."""
        meta = self.load_latest_snapshot(symbol)
        if not meta:
            return False

        with lock:
            slot_sz = meta.get("slot_size", getattr(ring_buffer, "slot_size", 29))
            ring_buffer._write_header(
                meta["write_idx"],
                meta["read_idx"],
                meta["capacity"],
                slot_sz,
                meta["dropped_count"],
            )
        return True
