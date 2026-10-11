"""Day 2 tests: C struct layout and wire compatibility with Member 1's schema.

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import struct
import unittest

import chronos_engine

# Same format string as Member 1's src/schema.py (ORDER_FORMAT)
WIRE_FORMAT = "=QcdIQ"


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.info = chronos_engine.layout_info()

    def test_wire_order_matches_struct_module(self):
        self.assertEqual(self.info["sizeof_wire_order"], struct.calcsize(WIRE_FORMAT))
        self.assertEqual(self.info["sizeof_wire_order"], 29)

    def test_wire_offsets_have_no_padding(self):
        off = self.info["wire_offsets"]
        self.assertEqual(
            [off[k] for k in ("order_id", "side", "price", "quantity", "timestamp_ns")],
            [0, 8, 9, 17, 21],
        )

    def test_order_is_32_bytes_and_aligned(self):
        self.assertEqual(self.info["sizeof_order"], 32)
        off = self.info["order_offsets"]
        self.assertEqual(off["order_id"], 0)
        self.assertEqual(off["timestamp_ns"], 8)
        self.assertEqual(off["price"], 16)
        self.assertEqual(off["price"] % 8, 0)

    def test_node_embeds_order_and_two_pointers(self):
        import ctypes
        ptr = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(self.info["sizeof_order_node"], 32 + 2 * ptr)
        off = self.info["node_offsets"]
        self.assertEqual((off["order"], off["prev"], off["next"]), (0, 32, 32 + ptr))


class TestDecode(unittest.TestCase):
    def test_roundtrip_with_struct_pack(self):
        raw = struct.pack(WIRE_FORMAT, 42, b"B", 101.25, 500, 1_700_000_000_123)
        self.assertEqual(
            chronos_engine.decode_wire(raw),
            (42, b"B", 101.25, 500, 1_700_000_000_123),
        )

    def test_sell_side_and_extreme_values(self):
        raw = struct.pack(WIRE_FORMAT, 2**64 - 1, b"S", 0.01, 2**32 - 1, 2**64 - 1)
        out = chronos_engine.decode_wire(raw)
        self.assertEqual((out[0], out[1], out[3], out[4]),
                         (2**64 - 1, b"S", 2**32 - 1, 2**64 - 1))

    def test_decodes_at_buffer_offset(self):
        raw = b"\x00" * 7 + struct.pack(WIRE_FORMAT, 7, b"S", 99.5, 10, 5)
        self.assertEqual(chronos_engine.decode_wire(raw[7:])[0], 7)

    def test_short_buffer_rejected(self):
        with self.assertRaises(ValueError):
            chronos_engine.decode_wire(b"\x00" * 10)

    def test_invalid_side_rejected(self):
        raw = struct.pack(WIRE_FORMAT, 1, b"X", 1.0, 1, 1)
        with self.assertRaises(ValueError):
            chronos_engine.decode_wire(raw)


class TestLinkedNodes(unittest.TestCase):
    def test_forward_and_backward_walk_agree(self):
        fwd, bwd = chronos_engine.selftest_linked_nodes(3)
        self.assertEqual((fwd, bwd), (600, 600))

    def test_many_nodes_and_empty_chain(self):
        self.assertEqual(chronos_engine.selftest_linked_nodes(1000), (500500 * 100, 500500 * 100))
        self.assertEqual(chronos_engine.selftest_linked_nodes(0), (0, 0))


if __name__ == "__main__":
    unittest.main()
