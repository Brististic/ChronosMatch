import struct

import pytest

from chronos_engine import OrderBook
from chronos_engine.fill_wire import HEADER_FORMAT, HEADER_SIZE, FILL_FORMAT, FILL_SIZE, decode_fill


def make_ring(capacity=8):
    slot_size = FILL_SIZE
    ring = bytearray(HEADER_SIZE + capacity * slot_size)
    struct.pack_into(HEADER_FORMAT, ring, 0, 0, 0, capacity, slot_size, 0)
    return ring


def ring_header(ring):
    return struct.unpack_from(HEADER_FORMAT, ring, 0)


def read_slots(ring):
    head, tail, capacity, slot_size, drops = ring_header(ring)
    records = []
    while tail != head:
        offset = HEADER_SIZE + tail * slot_size
        records.append(decode_fill(bytes(ring[offset:offset + slot_size])))
        tail = (tail + 1) % capacity
    return records


def test_outbound_ring_emits_full_audit_record_for_partial_maker_fill():
    book = OrderBook()
    book.insert(101, b"S", 100.00, 5, 111)
    ring = make_ring()

    result = book.submit_to_outbound_ring(202, b"B", 100.00, 2, 999, ring)

    assert result["fill_count"] == 1
    assert result["filled_quantity"] == 2
    assert result["resting_quantity"] == 0
    assert ring_header(ring)[0] == 1
    fill = read_slots(ring)[0]
    assert fill == (1, 202, 101, 100.0, 2, 0, 3, 999, ord("B"))
    assert book.remaining(101) == 3


def test_outbound_ring_full_rejects_before_mutating_book():
    book = OrderBook()
    book.insert(1, b"S", 10.0, 2, 10)
    ring = make_ring(capacity=2)  # one usable slot in this ring

    book.submit_to_outbound_ring(2, b"B", 10.0, 1, 20, ring)
    assert book.remaining(1) == 1

    with pytest.raises(BufferError, match="insufficient free slots"):
        book.submit_to_outbound_ring(3, b"B", 10.0, 1, 30, ring)

    # Rejected because the ring was full: resting maker and head remain unchanged.
    assert book.remaining(1) == 1
    assert ring_header(ring)[0] == 1
    assert len(read_slots(ring)) == 1


def test_ring_requires_member1_header_and_53_byte_fill_slots():
    book = OrderBook()
    with pytest.raises(ValueError, match="slot_size=53"):
        book.submit_to_outbound_ring(1, b"B", 1.0, 1, 0, bytearray(24 + 4 * 29))
