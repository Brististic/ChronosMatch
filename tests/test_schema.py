import pytest
from src.schema import ORDER_SIZE, pack_order, unpack_order


def test_order_packing_and_unpacking():
    order_id = 1001
    side = b"B"
    price = 145.50
    quantity = 250
    timestamp_ns = 1728556800000000000

    raw_bytes = pack_order(order_id, side, price, quantity, timestamp_ns)

    assert len(raw_bytes) == ORDER_SIZE
    assert len(raw_bytes) == 29

    unpacked = unpack_order(raw_bytes)
    assert unpacked.order_id == order_id
    assert unpacked.side == side
    assert pytest.approx(unpacked.price) == price
    assert unpacked.quantity == quantity
    assert unpacked.timestamp_ns == timestamp_ns


def test_invalid_side_raises_error():
    with pytest.raises(ValueError):
        pack_order(1002, b"X", 100.0, 10, 1000)