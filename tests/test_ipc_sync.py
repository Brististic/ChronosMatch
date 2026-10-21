import os
import time
import pytest
from src.ipc_sync import MmapProcessLock


def test_lock_acquire_and_release(tmp_path):
    lock_file = str(tmp_path / "test.lock")
    lock = MmapProcessLock(lock_file, create=True)

    acquired = lock.acquire(timeout_sec=1.0)
    assert acquired is True
    assert lock._read_state() == (1, os.getpid())

    lock.release()
    assert lock._read_state() == (0, 0)
    lock.close()


def test_lock_context_manager(tmp_path):
    lock_file = str(tmp_path / "test_ctx.lock")
    lock = MmapProcessLock(lock_file, create=True)

    with lock:
        assert lock._read_state() == (1, os.getpid())

    assert lock._read_state() == (0, 0)
    lock.close()


def test_lock_timeout_on_contention(tmp_path):
    lock_file = str(tmp_path / "test_timeout.lock")
    lock1 = MmapProcessLock(lock_file, create=True)
    lock2 = MmapProcessLock(lock_file, create=False)

    assert lock1.acquire(timeout_sec=0.5) is True

    # Lock 2 should timeout while Lock 1 holds it
    assert lock2.acquire(timeout_sec=0.1) is False

    lock1.release()
    assert lock2.acquire(timeout_sec=0.5) is True

    lock2.release()
    lock1.close()
    lock2.close()


def test_multi_ring_ipc_hub_routing(tmp_path):
    from src.ipc_hub import MultiRingIPCHub
    from src.schema import pack_order, unpack_order

    symbols = ["AAPL", "MSFT"]
    hub = MultiRingIPCHub(symbols=symbols, base_dir=str(tmp_path), capacity_per_ring=64, create=True)

    now = time.perf_counter_ns()
    aapl_orders = [pack_order(1, b"B", 180.0, 10, now)]
    msft_orders = [pack_order(2, b"S", 410.0, 20, now)]

    hub.write_symbol_batch("AAPL", aapl_orders)
    hub.write_symbol_batch("MSFT", msft_orders)

    read_data = hub.round_robin_read(max_count_per_symbol=10)
    assert len(read_data["AAPL"]) == 1
    assert len(read_data["MSFT"]) == 1

    o1 = unpack_order(read_data["AAPL"][0])
    o2 = unpack_order(read_data["MSFT"][0])
    assert o1.order_id == 1 and o1.price == 180.0
    assert o2.order_id == 2 and o2.price == 410.0

    hub.close()
    hub.cleanup_files()


def test_multiprocess_pipeline_runner(tmp_path):
    from src.pipeline_runner import run_multiprocess_system

    res = run_multiprocess_system(
        symbols=["AAPL", "MSFT"],
        duration_sec=0.5,
        base_dir=str(tmp_path),
    )
    assert res["consumed"] > 0


def test_multi_ring_backpressure_and_occupancy(tmp_path):
    from src.ipc_hub import MultiRingIPCHub
    from src.schema import pack_order

    symbols = ["AAPL"]
    hub = MultiRingIPCHub(
        symbols=symbols,
        base_dir=str(tmp_path),
        capacity_per_ring=32,
        create=True,
        high_watermark=0.50,
    )

    now = time.perf_counter_ns()
    orders = [pack_order(i, b"B", 180.0, 10, now) for i in range(1, 20)]

    written = hub.write_symbol_batch_with_backpressure("AAPL", orders, max_retries=2, backoff_us=10.0)
    assert written > 0

    occupancy = hub.get_ring_occupancy("AAPL")
    assert occupancy > 0.0

    hub.close()
    hub.cleanup_files()


def test_snapshot_checkpoint_and_recovery(tmp_path):
    from src.snapshot_manager import SnapshotManager
    from src.ring_buffer import MmapRingBuffer
    from src.ipc_sync import MmapProcessLock
    from src.schema import pack_order

    buf_path = str(tmp_path / "snap_test.bin")
    lock_path = str(tmp_path / "snap_test.lock")
    snap_dir = str(tmp_path / "snapshots")

    rb = MmapRingBuffer(buf_path, capacity=64, create=True)
    lock = MmapProcessLock(lock_path, create=True)
    mgr = SnapshotManager(snap_dir)

    now = time.perf_counter_ns()
    pkts = [pack_order(i, b"B", 100.0 + i, 10, now) for i in range(1, 11)]
    rb.write_batch(pkts)

    # Capture state snapshot
    snap = mgr.capture_channel_snapshot("AAPL", rb, lock, engine_stats={"total_matches": 5})
    assert snap["write_idx"] == 10
    assert snap["unconsumed_count"] == 10

    # Simulate crash/header corruption by resetting indices with exact 5 arguments: (head, tail, capacity, slot_size, drops)
    with lock:
        rb._write_header(0, 0, 64, rb.slot_size, 0)

    # Recover state from snapshot
    recovered = mgr.recover_ring_offsets("AAPL", rb, lock)
    assert recovered is True

    hdr = rb._read_header()
    assert hdr[0] == 10  # write_idx (head) recovered back to 10

    rb.close()
    lock.close()


def test_ring_telemetry_monitor_health(tmp_path):
    from src.ring_telemetry import RingTelemetryMonitor
    from src.ring_buffer import MmapRingBuffer
    from src.ipc_sync import MmapProcessLock
    from src.schema import pack_order

    buf_path = str(tmp_path / "telem.bin")
    lock_path = str(tmp_path / "telem.lock")

    rb = MmapRingBuffer(buf_path, capacity=64, create=True)
    lock = MmapProcessLock(lock_path, create=True)
    monitor = RingTelemetryMonitor(rb, lock, sample_window=10)

    now = time.perf_counter_ns()
    pkts = [pack_order(i, b"B", 100.0, 10, now) for i in range(1, 16)]
    rb.write_batch(pkts)

    health = monitor.sample_health()
    assert health["unconsumed"] == 15
    assert health["occupancy_pct"] > 0.0
    assert health["healthy"] is True

    rb.close()
    lock.close()
