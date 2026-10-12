import struct
from dataclasses import dataclass

ORDER_FORMAT = "<QcdIQ"
ORDER_SIZE = struct.calcsize(ORDER_FORMAT)  
VALID_SIDES = {b"B", b"S", b"C"}  


@dataclass(slots=True, frozen=True)
class Order:
    order_id: int
    side: bytes
    price: float
    quantity: float
    timestamp_ns: int


def pack_order(order_id: int, side: bytes, price: float, quantity: float, timestamp_ns: int) -> bytes:
    if side not in VALID_SIDES:
        raise ValueError(f"Invalid side {side!r}. Must be one of {VALID_SIDES}")
    return struct.pack(ORDER_FORMAT, order_id, side, price, quantity, timestamp_ns)


def unpack_order(data: bytes) -> Order:
    if len(data) != ORDER_SIZE:
        raise ValueError(f"Invalid packet size {len(data)}. Expected {ORDER_SIZE} bytes.")
    order_id, side, price, quantity, timestamp_ns = struct.unpack(ORDER_FORMAT, data)
    return Order(order_id, side, price, quantity, timestamp_ns)