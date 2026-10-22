"""Day 13 outbound execution-record wire format.

Header is Member 1's 24-byte little-endian ``<IIIIQ`` ring header.
Each fill slot is 53 bytes, little-endian ``<QQQdIIIQB``:
trade_id, taker_id, maker_id, price, quantity, taker_remaining,
maker_remaining, timestamp_ns, taker_side (ASCII B/S).

The Cython matching path writes these fields directly into the writable ring
mapping. This module is only a convenience decoder for tests/consumers.
"""
import struct

HEADER_FORMAT = "<IIIIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
FILL_FORMAT = "<QQQdIIIQB"
FILL_SIZE = struct.calcsize(FILL_FORMAT)


def decode_fill(slot: bytes):
    """Decode one fill slot to a tuple; not used by the engine hot path."""
    if len(slot) != FILL_SIZE:
        raise ValueError(f"fill slot must be {FILL_SIZE} bytes, got {len(slot)}")
    return struct.unpack(FILL_FORMAT, slot)
