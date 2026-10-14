import os
import sys
import time
from typing import Callable, List, Optional
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.schema import unpack_order, Order


class EngineFeeder:
    """Consumes orders from MmapRingBuffer and dispatches them to matching engine hooks.
    
    Tracks end-to-end transit latencies and throughput metrics.
    """

    def __init__(
        self,
        ring_buffer: MmapRingBuffer,
        lock: MmapProcessLock,
        batch_size: int = 128,
        on_order_callback: Optional[Callable[[Order], None]] = None,
    ):
        self.ring_buffer = ring_buffer
        self.lock = lock
        self.batch_size = batch_size
        self.on_order_callback = on_order_callback

        self.total_consumed = 0
        self.total_latency_ns = 0
        self.min_latency_ns = float("inf")
        self.max_latency_ns = 0

    def process_batch(self) -> int:
        """Pulls a single batch under lock and dispatches decoded orders.
        
        Returns the number of processed orders.
        """
        with self.lock:
            raw_packets = self.ring_buffer.read_batch(max_count=self.batch_size)

        if not raw_packets:
            return 0

        now_ns = time.perf_counter_ns()
        for pkt in raw_packets:
            order = unpack_order(pkt)
            latency = max(0, now_ns - order.timestamp_ns)

            self.total_consumed += 1
            self.total_latency_ns += latency
            if latency < self.min_latency_ns:
                self.min_latency_ns = latency
            if latency > self.max_latency_ns:
                self.max_latency_ns = latency

            if self.on_order_callback:
                self.on_order_callback(order)

        return len(raw_packets)

    def drain_all(self) -> int:
        """Continuously processes until the buffer is empty."""
        total = 0
        while True:
            count = self.process_batch()
            if count == 0:
                break
            total += count
        return total

    def get_latency_stats(self) -> dict:
        """Returns microsecond summary latency statistics."""
        if self.total_consumed == 0:
            return {"count": 0, "avg_us": 0.0, "min_us": 0.0, "max_us": 0.0}
        return {
            "count": self.total_consumed,
            "avg_us": round((self.total_latency_ns / self.total_consumed) / 1000.0, 3),
            "min_us": round(self.min_latency_ns / 1000.0, 3),
            "max_us": round(self.max_latency_ns / 1000.0, 3),
        }