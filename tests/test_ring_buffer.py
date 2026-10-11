import os
import pytest
from src.schema import pack_order, unpack_order
from src.ring_buffer import MmapRingBuffer

BUFFER_TEST_FILE = "test_chronos_ring.bin"


@pytest.fixture
def clean_ring_buffer():
    if os.path.exists(BUFFER_TEST_FILE):
        os.remove(BUFFER_TEST_FILE)
    rb = MmapRingBuffer(BUFFER_TEST_FILE, capacity=5, create=True)
    yield rb
    rb.close()
    if os.path.exists(BUFFER_TEST_FILE):
        os.remove(BUFFER_TEST_FILE)


def test_ring_buffer_write_and_read(clean_ring_buffer):
    rb = clean_ring_buffer
    pkt = pack_order(101, b"B", 152.25, 50, 1000)

    assert rb.write(pkt) is True

    retrieved = rb.read()
    assert retrieved == pkt

    order = unpack_order(retrieved)
    assert order.order_id == 101
    assert order.side == b"B"
    assert order.price == 152.25

    assert rb.read() is None


def test_ring_buffer_capacity_overflow(clean_ring_buffer):
    rb = clean_ring_buffer
    pkt = pack_order(1, b"S", 150.0, 10, 100)

    for _ in range(5):
        assert rb.write(pkt) is True

    assert rb.write(pkt) is False

    assert rb.read() is not None
    assert rb.write(pkt) is True