import asyncio
import random
import time
from typing import AsyncGenerator
from src.schema import pack_order


class MarketSimulator:
    """Async tick generator supporting constant-rate pacing and burst injection."""

    def __init__(self, target_rate: int = 100_000):
        self.target_rate = target_rate
        self.order_counter = 0

    async def generate_ticks(
        self,
        count: int = 100,
        rate_limit: int | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Yields packed binary order ticks with optional pacing throttle."""
        effective_rate = rate_limit if rate_limit is not None else self.target_rate
        delay = 1.0 / effective_rate if effective_rate > 0 else 0.0

        for _ in range(count):
            self.order_counter += 1
            side = b"B" if random.random() > 0.5 else b"S"
            price = round(150.00 + random.uniform(-2.0, 2.0), 2)
            quantity = random.randint(10, 500)
            timestamp_ns = time.perf_counter_ns()

            yield pack_order(self.order_counter, side, price, quantity, timestamp_ns)

            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def generate_burst(
        self,
        burst_size: int = 500,
    ) -> AsyncGenerator[bytes, None]:
        """Injects a burst of orders without delay to stress-test ring buffer ingestion."""
        async for tick in self.generate_ticks(count=burst_size, rate_limit=0):
            yield tick