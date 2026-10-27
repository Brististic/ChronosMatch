import time
import pytest
from src.risk_filter import PreTradeRiskFilter
from src.schema import pack_order


def test_risk_filter_notional_pass_and_reject():
    rf = PreTradeRiskFilter(max_notional=50_000.0)
    now = time.perf_counter_ns()

    valid_pkt = pack_order(1, b"B", 100.0, 100, now)  # 10,000 notional
    ok, reason = rf.validate_order(valid_pkt, reference_price=100.0)
    assert ok is True
    assert reason is None

    fat_finger_pkt = pack_order(2, b"B", 100.0, 600, now)  # 60,000 notional
    ok, reason = rf.validate_order(fat_finger_pkt, reference_price=100.0)
    assert ok is False
    assert "EXCEEDS_MAX_NOTIONAL" in reason


def test_risk_filter_price_band_check():
    rf = PreTradeRiskFilter(max_price_band_pct=0.05)
    now = time.perf_counter_ns()

    # 100.0 reference, 104.0 is within 5%
    ok, _ = rf.validate_order(pack_order(1, b"B", 104.0, 10, now), reference_price=100.0)
    assert ok is True

    # 106.0 is 6% deviation -> rejects
    ok, reason = rf.validate_order(pack_order(2, b"B", 106.0, 10, now), reference_price=100.0)
    assert ok is False
    assert "PRICE_BAND_VIOLATION" in reason


def test_risk_filter_cancellations_bypass():
    rf = PreTradeRiskFilter(max_notional=100.0, max_price_band_pct=0.01)
    now = time.perf_counter_ns()

    # Cancellation with 0 quantity passes regardless of price or notional
    cancel_pkt = pack_order(999, b"B", 50000.0, 0, now)
    ok, reason = rf.validate_order(cancel_pkt, reference_price=100.0)
    assert ok is True
    assert reason is None
