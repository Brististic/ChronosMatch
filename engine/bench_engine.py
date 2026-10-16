import os
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.abspath("engine"))
from chronos_engine import OrderBook


def run_engine_benchmark(order_count: int = 100_000) -> Dict[str, float]:
    """Day 7 Member 2 Deliverable: High-throughput Cython OrderBook matching benchmark."""
    print(f"[*] Benchmarking Cython OrderBook with {order_count:,} orders...")
    book = OrderBook()

    # Pre-generate paired limit orders: rest a sell, cross with a buy
    orders = []
    mid = 180.00
    for i in range(1, order_count + 1):
        if i % 2 == 1:
            # Maker: resting ask
            orders.append((i, b"S", mid, 25))
        else:
            # Taker: crossing bid hitting the resting ask
            orders.append((i, b"B", mid, 25))

    total_fills = 0
    t0 = time.perf_counter()
    for oid, side, price, qty in orders:
        fills = book.submit(oid, side, price, qty)
        total_fills += len(fills)
    elapsed = time.perf_counter() - t0

    throughput = order_count / elapsed if elapsed > 0 else 0.0
    avg_latency_us = (elapsed / order_count) * 1_000_000.0

    assert book.check_integrity(), "Book integrity violated after benchmark!"

    print(f"    [+] Total Orders Submitted: {order_count:,}")
    print(f"    [+] Total Executed Matches: {total_fills:,}")
    print(f"    [+] Total Elapsed Time:     {elapsed:.4f} s")
    print(f"    [+] Engine Throughput:      {throughput:,.0f} orders/sec")
    print(f"    [+] Avg Match Latency:      {avg_latency_us:.3f} us/order")
    print(f"    [+] Final Book Integrity:   VERIFIED")

    return {
        "order_count": order_count,
        "total_fills": total_fills,
        "elapsed_sec": round(elapsed, 4),
        "throughput_ops_sec": round(throughput, 1),
        "avg_latency_us": round(avg_latency_us, 3),
    }


if __name__ == "__main__":
    run_engine_benchmark()
