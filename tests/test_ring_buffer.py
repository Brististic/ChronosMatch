import os
import time
import pytest
from src.ring_buffer import MmapRingBuffer
from src.schema import pack_order, unpack_order


def test_basic_write_read(tmp_path):
    buf_path = str(tmp_path / "test_ring.bin")
    rb = MmapRingBuffer(buf_path, capacity=10, create=True)

    now = time.perf_counter_ns()
    pkt = pack_order(1, b"B", 150.0, 100, now)
    rb.write_batch([pkt])

    read_pkts = rb.read_batch(max_count=5)
    assert len(read_pkts) == 1

    order = unpack_order(read_pkts[0])
    assert order.order_id == 1
    assert order.side == b"B"
    assert order.price == 150.0
    assert order.quantity == 100

    rb.close()


def test_batch_write_read_and_drops(tmp_path):
    buf_path = str(tmp_path / "test_drops.bin")
    rb = MmapRingBuffer(buf_path, capacity=5, create=True)

    now = time.perf_counter_ns()
    pkts = [pack_order(i, b"B", 100.0 + i, 10, now) for i in range(1, 9)]

    # Writing 8 items to a buffer with capacity 5 will drop excess
    written = rb.write_batch(pkts)
    assert written <= 5

    head, tail, capacity, slot_size, drops = rb._read_header()
    assert drops > 0

    drained = rb.read_batch(max_count=10)
    assert len(drained) == written

    rb.close()


def test_ring_buffer_compaction(tmp_path):
    from src.ipc_sync import MmapProcessLock

    buf_path = str(tmp_path / "compact_test.bin")
    lock_path = str(tmp_path / "compact_test.lock")

    rb = MmapRingBuffer(buf_path, capacity=16, create=True)
    lock = MmapProcessLock(lock_path, create=True)

    now = time.perf_counter_ns()
    # Write 6 orders, read 3, write 5 more to advance indices
    rb.write_batch([pack_order(i, b"B", 100.0, 10, now) for i in range(1, 7)])
    drained = rb.read_batch(max_count=3)
    assert len(drained) == 3

    rb.write_batch([pack_order(i, b"B", 100.0, 10, now) for i in range(7, 12)])
    head, tail, capacity, slot_size, drops = rb._read_header()
    assert tail == 3

    # Compact buffer
    unconsumed = rb.compact_ring_buffer(lock)
    assert unconsumed == 8

    head_after, tail_after, _, _, _ = rb._read_header()
    assert tail_after == 0
    assert head_after == 8

    # Ensure remaining 8 items read out in exact order (IDs 4 through 11)
    remaining = rb.read_batch(max_count=10)
    assert len(remaining) == 8
    first_order = unpack_order(remaining[0])
    last_order = unpack_order(remaining[-1])
    assert first_order.order_id == 4
    assert last_order.order_id == 11

    rb.close()
    lock.close()


def test_dynamic_buffer_resizing(tmp_path):
    from src.ipc_sync import MmapProcessLock

    buf_path = str(tmp_path / "resize_test.bin")
    lock_path = str(tmp_path / "resize_test.lock")

    rb = MmapRingBuffer(buf_path, capacity=8, create=True)
    lock = MmapProcessLock(lock_path, create=True)

    now = time.perf_counter_ns()
    # Write 6 orders into capacity 8
    rb.write_batch([pack_order(i, b"B", 100.0 + i, 10, now) for i in range(1, 7)])

    # Expand buffer to capacity 32
    success = rb.resize_ring_buffer(32, lock)
    assert success is True

    head, tail, capacity, slot_size, drops = rb._read_header()
    assert capacity == 32
    assert head == 6
    assert tail == 0

    # Write additional items into expanded space without drops
    rb.write_batch([pack_order(i, b"S", 200.0, 10, now) for i in range(7, 20)])
    head_new, _, _, _, drops_new = rb._read_header()
    assert head_new == 19
    assert drops_new == 0

    drained = rb.read_batch(max_count=25)
    assert len(drained) == 19
    assert unpack_order(drained[0]).order_id == 1
    assert unpack_order(drained[-1]).order_id == 19

    rb.close()
    lock.close()
