import os
import time
import pytest
from src.ipc_sync import MmapProcessLock
from src.ipc_hub import MultiRingIPCHub
from src.schema import pack_order, unpack_order, pack_fill, unpack_fill


def test_lock_acquire_release(tmp_path):
    lock_path = str(tmp_path / "test.lock")
    lock = MmapProcessLock(lock_path, create=True)

    assert lock.acquire() is True
    assert lock.is_locked() is True

    lock.release()
    assert lock.is_locked() is False
    lock.close()


def test_lock_context_manager(tmp_path):
    lock_path = str(tmp_path / "ctx.lock")
    lock = MmapProcessLock(lock_path, create=True)

    with lock:
        assert lock.is_locked() is True

    assert lock.is_locked() is False
    lock.close()


def test_multi_ring_ipc_hub_round_robin(tmp_path):
    base_dir = str(tmp_path / "hub_ipc")
    symbols = ["AAPL", "MSFT"]
    hub = MultiRingIPCHub(symbols=symbols, base_dir=base_dir, capacity_per_ring=16, create=True)

    now = time.perf_counter_ns()
    aapl_orders = [pack_order(1, b"B", 180.0, 10, now), pack_order(2, b"S", 181.0, 20, now)]
    msft_orders = [pack_order(3, b"B", 410.0, 15, now)]

    hub.write_symbol_batch("AAPL", aapl_orders)
    hub.write_symbol_batch("MSFT", msft_orders)

    drained = hub.round_robin_read(max_count_per_symbol=10)
    assert len(drained["AAPL"]) == 2
    assert len(drained["MSFT"]) == 1

    hub.close()
    hub.cleanup_files()


def test_hub_backpressure_and_occupancy(tmp_path):
    base_dir = str(tmp_path / "backpressure_ipc")
    hub = MultiRingIPCHub(symbols=["TSLA"], base_dir=base_dir, capacity_per_ring=10, create=True, high_watermark=0.5)

    now = time.perf_counter_ns()
    pkts = [pack_order(i, b"B", 240.0, 10, now) for i in range(1, 8)]

    written = hub.write_symbol_batch_with_backpressure("TSLA", pkts)
    assert written > 0

    occupancy = hub.get_ring_occupancy("TSLA")
    assert occupancy > 0.0

    hub.close()
    hub.cleanup_files()


def test_execution_fills_channel(tmp_path):
    base_dir = str(tmp_path / "fills_ipc")
    hub = MultiRingIPCHub(symbols=["AAPL"], base_dir=base_dir, capacity_per_ring=32, create=True)

    try:
        hub.init_execution_ring("AAPL", capacity=32)
        now = time.perf_counter_ns()
        fill_pkt = pack_fill(order_id=101, fill_id=1, side=b"B", price=182.50, quantity=50, timestamp_ns=now)

        success = hub.write_fill("AAPL", fill_pkt)
        assert success is True

        drained = hub.read_fills("AAPL", max_count=10)
        assert len(drained) == 1

        unpacked = unpack_fill(drained[0])
        assert unpacked.order_id == 101
        assert unpacked.fill_id == 1
        assert unpacked.side == b"B"
        assert unpacked.price == 182.50
        assert unpacked.quantity == 50
    finally:
        hub.close()
        hub.cleanup_files()
