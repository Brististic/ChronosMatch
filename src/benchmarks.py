import time
import statistics
from typing import List, Dict, Any
from src.schema import pack_order
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock

class IPCLatencyBenchmark:
    """Day 20 Member 1: High-resolution latency profiler and throughput measurement harness."""

    def __init__(self, ring: MmapRingBuffer, lock: MmapProcessLock):
        self.ring = ring
        self.lock = lock

    def measure_round_trip_latencies(self, num_orders: int = 1000) -> Dict[str, Any]:
        """Measures write-to-read transit latency in nanoseconds using hardware-level timestamps."""
        latencies_ns: List[int] = []

        for i in range(num_orders):
            t_write_ns = time.perf_counter_ns()
            pkt = pack_order(
                order_id=i + 1,
                side=b"B" if i % 2 == 0 else b"S",
                price=100.0 + (i % 10) * 0.1,
                quantity=100,
                timestamp_ns=t_write_ns
            )

            with self.lock:
                self.ring.write_batch([pkt])

            with self.lock:
                # Positional call: avoids keyword name mismatch (max_count vs batch_size)
                packets = self.ring.read_batch(1)

            t_read_ns = time.perf_counter_ns()
            if packets:
                latencies_ns.append(max(0, t_read_ns - t_write_ns))

        if not latencies_ns:
            return {"count": 0, "p50_us": 0.0, "p99_us": 0.0, "p999_us": 0.0, "mean_us": 0.0}

        latencies_ns.sort()
        n = len(latencies_ns)
        return {
            "count": n,
            "mean_us": round(statistics.mean(latencies_ns) / 1000.0, 3),
            "p50_us": round(latencies_ns[int(n * 0.50)] / 1000.0, 3),
            "p90_us": round(latencies_ns[int(n * 0.90)] / 1000.0, 3),
            "p99_us": round(latencies_ns[min(n - 1, int(n * 0.99))] / 1000.0, 3),
            "p999_us": round(latencies_ns[min(n - 1, int(n * 0.999))] / 1000.0, 3),
            "min_us": round(latencies_ns[0] / 1000.0, 3),
            "max_us": round(latencies_ns[-1] / 1000.0, 3),
        }
