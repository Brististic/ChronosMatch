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


def test_l2_depth_ladder_generation():
    sim = MarketSimulator(symbols=["AAPL"])
    bids, asks = sim.generate_l2_depth_ladder("AAPL", levels=5, tick_size=0.01)

    assert len(bids) == 5
    assert len(asks) == 5

    unpacked_bids = [unpack_order(b) for b in bids]
    unpacked_asks = [unpack_order(a) for a in asks]

    assert unpacked_asks[0].price > unpacked_bids[0].price

    for i in range(len(unpacked_bids) - 1):
        assert unpacked_bids[i].price > unpacked_bids[i + 1].price
        assert unpacked_asks[i].price < unpacked_asks[i + 1].price


def test_correlated_multi_asset_batch_generation():
    sim = MarketSimulator(symbols=["AAPL", "MSFT", "TSLA"])
    batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=25, macro_market_drift=0.01)

    assert set(batch.keys()) == {"AAPL", "MSFT", "TSLA"}
    assert all(len(orders) == 25 for orders in batch.values())


def test_continuous_multi_asset_streaming():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"])
    batches = list(sim.stream_continuous_multi_asset(duration_sec=0.2, burst_interval_ms=20.0, batch_size_per_symbol=10))
    assert len(batches) >= 2
    for b in batches:
        assert "AAPL" in b and "MSFT" in b


def test_market_feed_regime_switching():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"])

    calm_batch = sim.generate_regime_switching_batch(regime="CALM", batch_size_per_symbol=15)
    turbulent_batch = sim.generate_regime_switching_batch(regime="TURBULENT", batch_size_per_symbol=15)

    assert len(calm_batch["AAPL"]) == 15
    assert len(turbulent_batch["AAPL"]) == 30  # Turbulent doubles burst count


def test_jittered_packet_generation():
    sim = MarketSimulator(symbols=["AAPL"])
    jittered = sim.generate_jittered_burst("AAPL", count=25, max_jitter_us=100.0, reorder_probability=0.3)

    assert len(jittered) == 25
    timestamps = [unpack_order(p).timestamp_ns for p in jittered]
    # Check that timestamps have microsecond dispersion
    assert len(set(timestamps)) > 1


def test_iceberg_order_slicing():
    sim = MarketSimulator(symbols=["AAPL"])
    slices = sim.generate_iceberg_order_slice("AAPL", side=b"B", total_quantity=250, display_size=50)

    assert len(slices) == 5
    total_qty = sum(unpack_order(p).quantity for p in slices)
    assert total_qty == 250
    assert all(unpack_order(p).side == b"B" for p in slices)


def test_level3_order_lifecycle_stream():
    sim = MarketSimulator(symbols=["TSLA"])
    events = sim.generate_l3_lifecycle_stream("TSLA", events_count=30)

    assert len(events) == 30
    actions = {action for action, _ in events}
    assert "ADD" in actions  # Must at least generate ADD events
    for action, raw_pkt in events:
        assert action in ("ADD", "MODIFY", "CANCEL", "EXECUTE")
        assert len(raw_pkt) == 29


def test_cross_asset_arbitrage_generation():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"])
    sim.asset_prices["AAPL"] = 185.0
    sim.asset_prices["MSFT"] = 180.0  # Spread = +$5.00 (> $0.40 threshold)

    arb_orders = sim.generate_arbitrage_pair_burst("AAPL", "MSFT", spread_threshold=0.40)
    assert len(arb_orders["AAPL"]) == 1
    assert len(arb_orders["MSFT"]) == 1

    order_a = unpack_order(arb_orders["AAPL"][0])
    order_b = unpack_order(arb_orders["MSFT"][0])

    # AAPL was overvalued -> should generate Sell
    assert order_a.side == b"S"
    # MSFT was undervalued -> should generate Buy
    assert order_b.side == b"B"
