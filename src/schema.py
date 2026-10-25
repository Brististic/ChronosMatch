import struct
from dataclasses import dataclass

ORDER_STRUCT_FORMAT = "<QcdIQ"
ORDER_STRUCT_SIZE = struct.calcsize(ORDER_STRUCT_FORMAT)
ORDER_SIZE = ORDER_STRUCT_SIZE

FILL_STRUCT_FORMAT = "<QQcdIQ"
FILL_STRUCT_SIZE = struct.calcsize(FILL_STRUCT_FORMAT)
FILL_SIZE = FILL_STRUCT_SIZE


@dataclass(slots=True, frozen=True)
class Order:
    order_id: int
    side: bytes
    price: float
    quantity: int
    timestamp_ns: int


@dataclass(slots=True, frozen=True)
class ExecutionFill:
    order_id: int
    fill_id: int
    side: bytes
    price: float
    quantity: int
    exec_timestamp_ns: int


def pack_order(order_id: int, side: bytes, price: float, quantity: int, timestamp_ns: int) -> bytes:
    if side not in (b"B", b"S"):
        raise ValueError(f"Invalid side {side}")
    return struct.pack(ORDER_STRUCT_FORMAT, order_id, side, float(price), int(quantity), timestamp_ns)


def unpack_order(raw_bytes: bytes) -> Order:
    if len(raw_bytes) != ORDER_STRUCT_SIZE:
        raise ValueError(f"Expected {ORDER_STRUCT_SIZE} bytes, got {len(raw_bytes)}")
    return Order(*struct.unpack(ORDER_STRUCT_FORMAT, raw_bytes))


def pack_fill(order_id: int, fill_id: int, side: bytes, price: float, quantity: int, timestamp_ns: int) -> bytes:
    if side not in (b"B", b"S"):
        raise ValueError(f"Invalid side {side}")
    return struct.pack(FILL_STRUCT_FORMAT, order_id, fill_id, side, float(price), int(quantity), timestamp_ns)


def unpack_fill(raw_bytes: bytes) -> ExecutionFill:
    if len(raw_bytes) != FILL_STRUCT_SIZE:
        raise ValueError(f"Expected {FILL_STRUCT_SIZE} bytes for execution fill, got {len(raw_bytes)}")
    return ExecutionFill(*struct.unpack(FILL_STRUCT_FORMAT, raw_bytes))
