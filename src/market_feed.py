import asyncio
import math
import random
import time
from typing import AsyncGenerator
from src.schema import pack_order

DEFAULT_ASSETS = {
    "AAPL": 150.00,
    "MSFT": 310.00,
    "TSLA": 220.00,
}


class MarketSimulator:
    """Async tick generator supporting constant-rate pacing, burst injection,

    multi-symbol stochastic walks, and order cancellation events.
    """

    def __init__(self, target_rate: int = 100_000, symbols: list[str] | None = None):
        self.target_rate = target_rate
        self.symbols = symbols or list(DEFAULT_ASSETS.keys())
        self.asset_prices = {s: DEFAULT_ASSETS.get(s, 100.00) for s in self.symbols}
        self.order_counter = 0

    def _stochastic_price_step(self, symbol: str, volatility: float = 0.002) -> float:
        """Evolves asset spot price via a basic Geometric Brownian Motion tick drift."""
        current = self.asset_prices[symbol]
        shock = random.gauss(0, 1) * volatility
        new_price = round(current * math.exp(shock), 2)
        self.asset_prices[symbol] = max(0.01, new_price)
        return self.asset_prices[symbol]

    async def generate_ticks(
        self,
        count: int = 100,
        rate_limit: int | None = None,
        cancel_ratio: float = 0.1,
    ) -> AsyncGenerator[bytes, None]:
        """Yields packed binary orders with random-walk pricing and cancel flags.

        cancel_ratio specifies the proportion of 'C' (Cancel) side actions.
        """
        effective_rate = rate_limit if rate_limit is not None else self.target_rate
        delay = 1.0 / effective_rate if effective_rate > 0 else 0.0

        for _ in range(count):
            self.order_counter += 1
            symbol = random.choice(self.symbols)
            spot = self._stochastic_price_step(symbol)

            
            if random.random() < cancel_ratio and self.order_counter > 1:
                side = b"C"
                price = spot
                quantity = random.randint(10, 100)
            else:
                side = b"B" if random.random() > 0.5 else b"S"
                spread_offset = random.uniform(0.01, 0.50)
                price = round(spot + (spread_offset if side == b"S" else -spread_offset), 2)
                quantity = random.randint(10, 500)

            timestamp_ns = time.perf_counter_ns()
            yield pack_order(self.order_counter, side, max(0.01, price), quantity, timestamp_ns)

            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def generate_burst(
        self,
        burst_size: int = 500,
    ) -> AsyncGenerator[bytes, None]:
        """Injects a burst of orders without delay to stress-test ring buffer ingestion."""
        async for tick in self.generate_ticks(count=burst_size, rate_limit=0, cancel_ratio=0.0):
            yield tick