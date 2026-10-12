import asyncio
import time
from src.market_feed import MarketSimulator
from src.schema import unpack_order


def test_market_feed_generation():
    async def _test():
        sim = MarketSimulator(target_rate=100_000, symbols=["AAPL", "TSLA"])
        ticks = []
        async for tick in sim.generate_ticks(count=20, cancel_ratio=0.2):
            ticks.append(tick)

        assert len(ticks) == 20
        first_order = unpack_order(ticks[0])
        assert first_order.order_id == 1
        assert first_order.side in (b"B", b"S", b"C")
        assert first_order.quantity > 0

    asyncio.run(_test())


def test_market_feed_stochastic_drift():
    async def _test():
        sim = MarketSimulator(symbols=["AAPL"])
        start_price = sim.asset_prices["AAPL"]

        async for _ in sim.generate_ticks(count=50, cancel_ratio=0.0):
            pass

        end_price = sim.asset_prices["AAPL"]
        assert end_price > 0.0
        assert isinstance(end_price, float)

    asyncio.run(_test())


def test_market_feed_burst_generation():
    async def _test():
        sim = MarketSimulator()
        burst = []
        start_time = time.perf_counter()

        async for tick in sim.generate_burst(burst_size=50):
            burst.append(tick)

        elapsed = time.perf_counter() - start_time
        assert len(burst) == 50
        assert elapsed < 0.05

    asyncio.run(_test())