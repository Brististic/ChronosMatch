import struct
from typing import NamedTuple

ORDER_FORMAT = "=QcdIQ"
ORDER_STRUCT = struct.Struct(ORDER_FORMAT)
ORDER_SIZE = ORDER_STRUCT.size 


class Order(NamedTuple):
    order_id: int
    side: bytes
    price: float
    quantity: int
    timestamp_ns: int


def pack_order(order_id: int, side: bytes, price: float, quantity: int, timestamp_ns: int) -> bytes:
    if side not in (b"B", b"S"):
        raise ValueError(f"Invalid side {side!r}. Must be b'B' or b'S'.")
    return ORDER_STRUCT.pack(order_id, side, price, quantity, timestamp_ns)


def unpack_order(buffer: bytes | bytearray | memoryview, offset: int = 0) -> Order:
    return Order(*ORDER_STRUCT.unpack_from(buffer, offset))