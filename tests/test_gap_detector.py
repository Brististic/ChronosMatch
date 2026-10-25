import pytest
import time
from src.gap_detector import SequenceGapDetector
from src.schema import pack_order, unpack_order


def test_in_order_sequence_flow():
    detector = SequenceGapDetector(expected_start_seq=1)
    now = time.perf_counter_ns()

    pkt1 = pack_order(1, b"B", 100.0, 10, now)
    pkt2 = pack_order(2, b"S", 101.0, 20, now)

    r1 = detector.process_packet(1, pkt1)
    r2 = detector.process_packet(2, pkt2)

    assert len(r1) == 1
    assert len(r2) == 1
    assert detector.get_gap_count() == 0
    assert detector.expected_seq == 3


def test_out_of_order_and_gap_resolution():
    detector = SequenceGapDetector(expected_start_seq=1)
    now = time.perf_counter_ns()

    pkt1 = pack_order(1, b"B", 100.0, 10, now)
    pkt2 = pack_order(2, b"B", 101.0, 10, now)
    pkt3 = pack_order(3, b"B", 102.0, 10, now)

    r3 = detector.process_packet(3, pkt3)
    assert len(r3) == 0
    assert detector.get_gap_count() == 1
    assert detector.detected_gaps[0] == (1, 2)

    r1 = detector.process_packet(1, pkt1)
    assert len(r1) == 1
    assert unpack_order(r1[0]).order_id == 1
    assert detector.expected_seq == 2

    r2 = detector.process_packet(2, pkt2)
    assert len(r2) == 2
    assert unpack_order(r2[0]).order_id == 2
    assert unpack_order(r2[1]).order_id == 3
    assert detector.expected_seq == 4
