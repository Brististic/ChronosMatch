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
