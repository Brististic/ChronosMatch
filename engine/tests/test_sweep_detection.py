
from chronos_engine import OrderBook


def test_multi_level_sweep_is_detected():
    book = OrderBook()
    book.insert(1, b"S", 100.0, 5)
    book.insert(2, b"S", 101.0, 5)

    result = book.submit_traced(3, b"B", 101.0, 10)

    assert result["levels_swept"] == 2
    assert result["sweep_detected"] is True
    assert result["filled_quantity"] == 10


def test_single_level_fill_is_not_a_sweep():
    book = OrderBook()
    book.insert(1, b"S", 100.0, 10)

    result = book.submit_traced(2, b"B", 100.0, 5)

    assert result["levels_swept"] == 1
    assert result["sweep_detected"] is False
    assert result["filled_quantity"] == 5


def test_multiple_orders_at_same_price_count_as_one_level():
    book = OrderBook()
    book.insert(1, b"S", 100.0, 3)
    book.insert(2, b"S", 100.0, 5)

    result = book.submit_traced(3, b"B", 100.0, 8)

    assert result["levels_swept"] == 1
    assert result["sweep_detected"] is False
    assert result["filled_quantity"] == 8
