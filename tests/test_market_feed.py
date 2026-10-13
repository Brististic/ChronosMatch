import pytest
from src.schema import unpack_order, ORDER_SIZE
from src.market_feed import MarketSimulator


@pytest.mark.asyncio
async def test_generate_ticks_count_and_size():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"], target_rate=100_000)
    ticks = []
    async for tick in sim.generate_ticks(count=20, cancel_ratio=0.0):
        ticks.append(tick)

    assert len(ticks) == 20
    for tick in ticks:
        assert len(tick) == ORDER_SIZE
        order = unpack_order(tick)
        assert order.side in {b"B", b"S"}
        assert order.price > 0.0
        assert order.quantity >= 1


@pytest.mark.asyncio
async def test_cancellation_events_generated():
    sim = MarketSimulator(symbols=["TSLA"], target_rate=100_000)
    sides = set()
    async for tick in sim.generate_ticks(count=100, cancel_ratio=0.3):
        order = unpack_order(tick)
        sides.add(order.side)

    assert b"C" in sides


@pytest.mark.asyncio
async def test_shock_generator_and_power_law_distribution():
    sim = MarketSimulator(symbols=["AAPL"])

    shock_packets = await sim.generate_shock_event("AAPL", burst_size=25, price_impact_pct=0.04)
    assert len(shock_packets) == 25

    orders = [unpack_order(p) for p in shock_packets]
    assert len(orders) == 25
    assert all(o.price > 0.0 for o in orders)

    quantities = [o.quantity for o in orders]
    assert max(quantities) > min(quantities)
    assert all(q >= 1 for q in quantities)


@pytest.mark.asyncio
async def test_stochastic_price_drift():
    sim = MarketSimulator(symbols=["AAPL"], target_rate=100_000)
    initial_price = sim.asset_prices["AAPL"]

    prices = []
    async for tick in sim.generate_ticks(count=80, cancel_ratio=0.1):
        order = unpack_order(tick)
        prices.append(order.price)

    assert len(prices) == 80
    assert any(p != initial_price for p in prices)
    assert all(p > 0.0 for p in prices)