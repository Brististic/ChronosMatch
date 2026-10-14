import os
import pytest
from src.ring_buffer import MmapRingBuffer
from src.ipc_sync import MmapProcessLock
from src.schema import pack_order
from src.engine_feeder import EngineFeeder


@pytest.fixture
def feeder_env(tmp_path):
    buf_path = str(tmp_path / "feeder_ring.bin")
    lock_path = str(tmp_path / "feeder.lock")
    rb = MmapRingBuffer(buf_path, capacity=50, create=True)
    lock = MmapProcessLock(lock_path, create=True)

    yield rb, lock

    rb.close()
    lock.close()


def test_engine_feeder_batch_consumption(feeder_env):
    rb, lock = feeder_env
    received_orders = []

    feeder = EngineFeeder(
        ring_buffer=rb,
        lock=lock,
        batch_size=10,
        on_order_callback=lambda order: received_orders.append(order),
    )

    packets = [pack_order(i, b"B", 100.0 + i, 10, i * 1000) for i in range(1, 16)]
    with lock:
        rb.write_batch(packets)

    processed = feeder.process_batch()
    assert processed == 10
    assert len(received_orders) == 10

    drained = feeder.drain_all()
    assert drained == 5
    assert len(received_orders) == 15

    stats = feeder.get_latency_stats()
    assert stats["count"] == 15
    assert stats["avg_us"] >= 0