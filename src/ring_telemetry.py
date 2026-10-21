import time
from typing import Dict, Any, List
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock


class RingTelemetryMonitor:
    """Non-intrusive runtime diagnostic telemetry for circular ring buffers."""

    def __init__(self, ring_buffer: MmapRingBuffer, lock: MmapProcessLock, sample_window: int = 100):
        self.ring_buffer = ring_buffer
        self.lock = lock
        self.sample_window = sample_window
        self.history: List[float] = []

    def sample_health(self) -> Dict[str, Any]:
        """Extracts occupancy ratio, head/tail offset drift, and drop count."""
        with self.lock:
            header = self.ring_buffer._read_header()
            head, tail, capacity = header[0], header[1], header[2]
            slot_size = header[3] if len(header) > 3 else getattr(self.ring_buffer, "slot_size", 29)
            drops = header[4] if len(header) > 4 else 0

        unconsumed = (head - tail) % capacity
        occupancy = unconsumed / float(capacity)
        self.history.append(occupancy)
        if len(self.history) > self.sample_window:
            self.history.pop(0)

        moving_avg_occ = sum(self.history) / len(self.history) if self.history else 0.0

        return {
            "head": head,
            "tail": tail,
            "capacity": capacity,
            "slot_size": slot_size,
            "drops": drops,
            "unconsumed": unconsumed,
            "occupancy_pct": round(occupancy * 100.0, 2),
            "moving_avg_occupancy_pct": round(moving_avg_occ * 100.0, 2),
            "healthy": occupancy < 0.90,
        }
