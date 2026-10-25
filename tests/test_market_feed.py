import pytest
from src.market_feed import MarketSimulator
from src.schema import unpack_order


def test_generate_ticks_count_and_size():
    sim = MarketSimulator(symbols=["AAPL"])
    orders = sim.generate_ticks(count=20, symbol="AAPL")
    assert len(orders) == 20
    assert all(len(o) == 29 for o in orders)


def test_cancellation_events_generated():
    sim = MarketSimulator(symbols=["AAPL"])
    cancels = sim.generate_cancellations([1, 2, 3], count=2)
    assert len(cancels) == 2
    for c in cancels:
        order = unpack_order(c)
        assert order.quantity == 0


def test_shock_generator_and_power_law_distribution():
    sim = MarketSimulator(symbols=["AAPL"])
    shocks = sim.generate_shock_sequence("AAPL", drop_percent=0.10)
    assert len(shocks) == 20
    assert sim.asset_prices["AAPL"] < 180.0


def test_stochastic_price_drift():
    sim = MarketSimulator(symbols=["MSFT"])
    sim.generate_ticks(count=100, symbol="MSFT")
    assert sim.asset_prices["MSFT"] > 0


def test_l2_depth_ladder_generation():
    sim = MarketSimulator(symbols=["TSLA"])
    ladder = sim.generate_l2_depth_ladder("TSLA", depth_levels=5)
    assert len(ladder) == 10


def test_correlated_multi_asset_batch_generation():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"])
    batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=10)
    assert "AAPL" in batch and "MSFT" in batch
    assert len(batch["AAPL"]) == 10
    assert len(batch["MSFT"]) == 10


def test_continuous_multi_asset_streaming():
    sim = MarketSimulator(symbols=["AAPL", "TSLA"])
    batch = sim.generate_correlated_multi_asset_batch(batch_size_per_symbol=5)
    assert len(batch) == 2


def test_market_feed_regime_switching():
    sim = MarketSimulator(symbols=["AAPL"])
    sim.set_market_regime("TURBULENT")
    assert sim.market_regime == "TURBULENT"
    orders = sim.generate_ticks(count=10, symbol="AAPL")
    assert len(orders) == 10


def test_jittered_packet_generation():
    sim = MarketSimulator(symbols=["AAPL"])
    orders = sim.generate_jittered_burst("AAPL", count=10, max_jitter_us=50)
    assert len(orders) == 10


def test_iceberg_order_slicing():
    sim = MarketSimulator(symbols=["AAPL"])
    slices = sim.generate_iceberg_order_slice("AAPL", b"B", total_quantity=1000, clip_count=5)
    assert len(slices) == 5
    total_qty = sum(unpack_order(s).quantity for s in slices)
    assert total_qty == 1000


def test_level3_order_lifecycle_stream():
    sim = MarketSimulator(symbols=["AAPL"])
    stream = sim.generate_l3_lifecycle_stream("AAPL", events_count=10)
    assert len(stream) == 10
    types = [t for t, _ in stream]
    assert types[0] == "ADD"
    assert types[1] == "MODIFY"
    assert types[2] == "CANCEL"


def test_cross_asset_arbitrage_generation():
    sim = MarketSimulator(symbols=["AAPL", "MSFT"])
    arb = sim.generate_arbitrage_pair_burst("AAPL", "MSFT")
    assert "AAPL" in arb and "MSFT" in arb
    assert len(arb["AAPL"]) == 1
    assert len(arb["MSFT"]) == 1


def test_order_book_imbalance_pressure():
    sim = MarketSimulator(symbols=["AAPL"])
    orders = sim.generate_book_imbalance_pressure("AAPL", target_imbalance=0.85, batch_size=50)
    assert len(orders) == 50
    unpacked = [unpack_order(p) for p in orders]
    bids = [o for o in unpacked if o.side == b"B"]
    asks = [o for o in unpacked if o.side == b"S"]
    assert len(bids) > len(asks)
    assert sum(o.quantity for o in bids) > sum(o.quantity for o in asks)


def test_toxic_flow_simulation():
    sim = MarketSimulator(symbols=["AAPL"])
    initial_price = sim.asset_prices["AAPL"]

    toxic_orders, new_price = sim.generate_toxic_flow("AAPL", sweep_depth=10, price_jump=2.0)

    assert len(toxic_orders) == 10
    assert new_price == round(initial_price + 2.0, 2)
    assert sim.asset_prices["AAPL"] == new_price

    unpacked = [unpack_order(p) for p in toxic_orders]
    assert all(o.side == b"B" for o in unpacked)
    assert unpacked[-1].price >= unpacked[0].price


def test_spoofing_attack_generation():
    sim = MarketSimulator(symbols=["AAPL"])
    fake_bids, real_sell, cancels = sim.generate_spoofing_attack("AAPL", fake_layers=4, fake_qty_per_layer=500)

    assert len(fake_bids) == 4
    assert len(cancels) == 4

    unpacked_bids = [unpack_order(b) for b in fake_bids]
    assert all(b.side == b"B" for b in unpacked_bids)
    assert all(b.quantity == 500 for b in unpacked_bids)

    unpacked_sell = unpack_order(real_sell)
    assert unpacked_sell.side == b"S"
    assert unpacked_sell.quantity == 150

    unpacked_cancels = [unpack_order(c) for c in cancels]
    assert all(c.quantity == 0 for c in unpacked_cancels)
    fake_ids = [b.order_id for b in unpacked_bids]
    cancel_ids = [c.order_id for c in unpacked_cancels]
    assert fake_ids == cancel_ids
