import asyncio
import math
import random
import time
from typing import AsyncGenerator, Dict, List, Optional
from src.schema import pack_order


class MarketSimulator:
    """Multi-asset market feed with stochastic drift, power-law sizes, and volatility shocks."""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        base_prices: Optional[Dict[str, float]] = None,
        volatilities: Optional[Dict[str, float]] = None,
        target_rate: int = 50_000,
    ):
        self.symbols = symbols or ["AAPL", "MSFT", "TSLA"]
        self.asset_prices = base_prices or {
            "AAPL": 180.00,
            "MSFT": 410.00,
            "TSLA": 240.00,
        }
        self.volatilities = volatilities or {
            s: 0.0015 for s in self.symbols
        }
        self.target_rate = target_rate
        self.order_counter = 0
        self.active_orders: Dict[str, List[int]] = {s: [] for s in self.symbols}

    def _sample_order_quantity(self, min_qty: int = 1, alpha: float = 1.5) -> int:
        """Pareto / power-law distribution for realistic order sizes."""
        raw_qty = int(random.paretovariate(alpha) * 10)
        return max(min_qty, min(raw_qty, 50_000))

    def _step_price(self, symbol: str) -> float:
        """Geometric Brownian Motion incremental price tick."""
        curr = self.asset_prices[symbol]
        vol = self.volatilities[symbol]
        drift = 0.00001
        rand_shock = random.gauss(0, 1)
        new_price = curr * math.exp(drift + vol * rand_shock)
        self.asset_prices[symbol] = round(max(0.01, new_price), 2)
        return self.asset_prices[symbol]

    async def generate_ticks(
        self,
        count: int = 100,
        rate_limit: Optional[int] = None,
        cancel_ratio: float = 0.15,
    ) -> AsyncGenerator[bytes, None]:
        rate = rate_limit if rate_limit is not None else self.target_rate
        interval = (1.0 / rate) if rate > 0 else 0.0

        for _ in range(count):
            sym = random.choice(self.symbols)
            price = self._step_price(sym)
            qty = self._sample_order_quantity()
            ts = time.perf_counter_ns()

            if self.active_orders[sym] and (random.random() < cancel_ratio):
                target_id = self.active_orders[sym].pop(0)
                pkt = pack_order(target_id, b"C", price, qty, ts)
            else:
                self.order_counter += 1
                oid = self.order_counter
                side = random.choice([b"B", b"S"])
                self.active_orders[sym].append(oid)
                if len(self.active_orders[sym]) > 500:
                    self.active_orders[sym].pop(0)
                pkt = pack_order(oid, side, price, qty, ts)

            yield pkt

            if interval > 0:
                await asyncio.sleep(interval)

    async def generate_shock_event(
        self,
        shock_symbol: str,
        burst_size: int = 50,
        price_impact_pct: float = 0.05,
    ) -> List[bytes]:
        """Simulates a sudden liquidity sweep / market shock event."""
        packets = []
        base = self.asset_prices[shock_symbol]
        # Directional aggressive sweep
        side = b"S" if random.random() < 0.5 else b"B"
        step = (base * price_impact_pct) / burst_size

        for i in range(burst_size):
            self.order_counter += 1
            oid = self.order_counter
            price = base + (i * step) if side == b"B" else base - (i * step)
            price = round(max(0.01, price), 2)
            qty = self._sample_order_quantity() * 5  # Institutional scale
            ts = time.perf_counter_ns()
            packets.append(pack_order(oid, side, price, qty, ts))

        self.asset_prices[shock_symbol] = price
        return packets