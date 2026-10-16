import math
import os
import time
from typing import Dict, List
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.schema import pack_order, unpack_order


def calculate_percentiles(latencies_us: List[float]) -> Dict[str, float]:
    """Calculates summary percentiles (p50, p90, p99, p99.9, max, avg) for latency arrays."""
    if not latencies_us:
        return {"avg": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "p99_9": 0.0, "max": 0.0, "min": 0.0}

    sorted_vals = sorted(latencies_us)
    n = len(sorted_vals)

    def pct(p: float) -> float:
        k = (n - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return round(sorted_vals[int(k)], 3)
        d0 = sorted_vals[int(f)] * (c - k)
        d1 = sorted_vals[int(c)] * (k - f)
        return round(d0 + d1, 3)

    return {
        "avg": round(sum(sorted_vals) / n, 3),
        "min": round(sorted_vals[0], 3),
        "p50": pct(50.0),
        "p90": pct(90.0),
        "p99": pct(99.0),
        "p99_9": pct(99.9),
        "max": round(sorted_vals[-1], 3),
    }


def benchmark_ring_buffer_throughput(
    buffer_path: str,
    lock_path: str,
    total_orders: int = 50_000,
    batch_size: int = 64,
) -> Dict[str, float]:
    """Benchmarks direct zero-copy write_batch and read_batch throughput under spinlock."""
    rb = MmapRingBuffer(buffer_path, capacity=4096, create=True)
    lock = MmapProcessLock(lock_path, create=True)

    dummy_packets = [
        pack_order(i, b"B", 150.0 + (i % 10), 100, time.perf_counter_ns())
        for i in range(total_orders)
    ]

    # Write benchmark
    t0 = time.perf_counter()
    written = 0
    for i in range(0, total_orders, batch_size):
        batch = dummy_packets[i : i + batch_size]
        with lock:
            written += rb.write_batch(batch)
    write_time = time.perf_counter() - t0
    write_throughput = written / write_time if write_time > 0 else 0.0

    # Read benchmark
    t1 = time.perf_counter()
    read_count = 0
    while read_count < written:
        with lock:
            batch = rb.read_batch(max_count=batch_size)
        if not batch:
            break
        read_count += len(batch)
    read_time = time.perf_counter() - t1
    read_throughput = read_count / read_time if read_time > 0 else 0.0

    rb.close()
    lock.close()

    return {
        "total_orders": written,
        "write_throughput_ops_sec": round(write_throughput, 1),
        "read_throughput_ops_sec": round(read_throughput, 1),
        "write_time_sec": round(write_time, 4),
        "read_time_sec": round(read_time, 4),
    }
