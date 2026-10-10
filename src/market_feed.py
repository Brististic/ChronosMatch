import asyncio
import random
import time
from typing import AsyncGenerator
from src.schema import pack_order


class MarketSimulator:
    def __init__(self, target_rate: int = 100_000):
        self.target_rate = target_rate
        self.order_counter = 0

    async def generate_ticks(self, count: int = 100) -> AsyncGenerator[bytes, None]:
        """Yields packed binary order ticks."""
        for _ in range(count):
            self.order_counter += 1
            side = b"B" if random.random() > 0.5 else b"S"
            price = round(150.00 + random.uniform(-2.0, 2.0), 2)
            quantity = random.randint(10, 500)
            timestamp_ns = time.perf_counter_ns()

            yield pack_order(self.order_counter, side, price, quantity, timestamp_ns)
            await asyncio.sleep(0)  