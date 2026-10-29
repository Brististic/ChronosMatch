import os
import pytest
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.benchmarks import IPCLatencyBenchmark
from src.market_feed import MarketSimulator
from src.schema import unpack_order

def test_day20_ipc_latency_profiler(tmp_path):
    ring_path = str(tmp_path / "bench_ring.bin")
    lock_path = str(tmp_path / "bench_lock.bin")

    # Order wire packets are 29 bytes (ORDER_STRUCT_SIZE)
    ring = MmapRingBuffer(ring_path, capacity=512, slot_size=29, create=True)
    lock = MmapProcessLock(lock_path, create=True)
    bench = IPCLatencyBenchmark(ring, lock)

    try:
        stats = bench.measure_round_trip_latencies(num_orders=100)
        assert stats["count"] == 100
        assert stats["p50_us"] >= 0.0
        assert stats["p99_us"] >= stats["p50_us"]
        assert "mean_us" in stats
    finally:
        ring.close()
        lock.close()

def test_day20_systemic_crisis_burst():
    symbols = ["AAPL", "MSFT", "TSLA"]
    sim = MarketSimulator(symbols=symbols)
    initial_prices = {s: sim.asset_prices[s] for s in symbols}

    crisis = sim.generate_systemic_crisis_burst(volatility_multiplier=3.0, orders_per_symbol=25)

    for sym in symbols:
        assert sym in crisis
        assert len(crisis[sym]) == 25
        orders = [unpack_order(b) for b in crisis[sym]]
        sells = sum(1 for o in orders if o.side == b"S")
        assert sells > len(orders) // 2
        assert sim.asset_prices[sym] < initial_prices[sym]
