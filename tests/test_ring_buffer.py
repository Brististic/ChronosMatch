import os
import pytest
from src.ring_buffer import MmapRingBuffer


@pytest.fixture
def temp_buffer_file(tmp_path):
    f = tmp_path / "test_ring.bin"
    return str(f)


def test_basic_write_read(temp_buffer_file):
    rb = MmapRingBuffer(temp_buffer_file, slot_size=29, capacity=5, create=True)
    sample_data = b"X" * 29
    assert rb.write(sample_data) is True
    assert rb.read() == sample_data
    assert rb.read() is None
    rb.close()


def test_batch_write_read_and_drops(temp_buffer_file):
    rb = MmapRingBuffer(temp_buffer_file, slot_size=29, capacity=10, create=True)

    packets = [bytes([i % 256] * 29) for i in range(8)]
    written = rb.write_batch(packets)
    assert written == 8

    metrics = rb.get_metrics()
    assert metrics["occupied"] == 8
    assert metrics["free"] == 1  
    assert metrics["drops"] == 0

    overflow_batch = [bytes([99] * 29) for _ in range(5)]
    written_extra = rb.write_batch(overflow_batch)
    assert written_extra == 1

    metrics = rb.get_metrics()
    assert metrics["drops"] == 4
    assert metrics["occupied"] == 9

    drained = rb.read_batch(max_count=20)
    assert len(drained) == 9
    assert drained[0] == packets[0]

    assert rb.get_metrics()["occupied"] == 0
    rb.close()