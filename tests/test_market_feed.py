import pytest
from src.market_feed import MarketSimulator
from src.schema import unpack_order


@pytest.mark.asyncio
async def test_market_feed_generation():
    sim = MarketSimulator(target_rate=100_000)
    ticks = []
    async for tick in sim.generate_ticks(count=10):
        ticks.append(tick)

    assert len(ticks) == 10
    first_order = unpack_order(ticks[0])
    assert first_order.order_id == 1
    assert first_order.side in (b"B", b"S")
    assert first_order.quantity > 0


if __name__ == "__main__":
    import asyncio

    async def run_manual():
        sim = MarketSimulator()
        ticks = [t async for t in sim.generate_ticks(5)]
        print(f"Generated {len(ticks)} binary ticks successfully.")
        print(f"Sample tick unpacked: {unpack_order(ticks[0])}")

    asyncio.run(run_manual())