import asyncio
import math
import random
import time
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from src.schema import pack_order


class MarketSimulator:
    """Multi-asset market feed with stochastic drift, power-law sizes, volatility shocks, L2 depth, and stress generators."""

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
        self.volatilities = volatilities or {s: 0.0015 for s in self.symbols}
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

    def generate_l2_depth_ladder(
        self, symbol: str, levels: int = 5, tick_size: float = 0.01
    ) -> Tuple[List[bytes], List[bytes]]:
        """Generates matched L2 bid/ask ladders based on current microstructural spread."""
        mid = self._step_price(symbol)
        vol = self.volatilities[symbol]

        half_spread = max(tick_size, round(mid * vol * random.uniform(0.8, 1.5), 2))
        best_bid = round(mid - half_spread, 2)
        best_ask = round(mid + half_spread, 2)

        bids = []
        asks = []
        now_ns = time.perf_counter_ns()

        for i in range(levels):
            self.order_counter += 1
            bid_price = round(best_bid - (i * tick_size), 2)
            bid_qty = self._sample_order_quantity() * (i + 1)
            bids.append(pack_order(self.order_counter, b"B", bid_price, bid_qty, now_ns))

            self.order_counter += 1
            ask_price = round(best_ask + (i * tick_size), 2)
            ask_qty = self._sample_order_quantity() * (i + 1)
            asks.append(pack_order(self.order_counter, b"S", ask_price, ask_qty, now_ns))

        return bids, asks

    def generate_stress_burst(self, symbol: str, count: int = 1000) -> List[bytes]:
        """Day 7: Vectorized in-memory high-rate order burst for throughput saturation benchmarking."""
        packets = []
        base = self.asset_prices[symbol]
        now_ns = time.perf_counter_ns()

        for _ in range(count):
            self.order_counter += 1
            side = b"B" if (self.order_counter % 2 == 0) else b"S"
            offset = (random.random() - 0.5) * 0.50
            price = round(max(0.01, base + offset), 2)
            qty = self._sample_order_quantity()
            packets.append(pack_order(self.order_counter, side, price, qty, now_ns))

        return packets

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
        packets = []
        base = self.asset_prices[shock_symbol]
        side = b"S" if random.random() < 0.5 else b"B"
        step = (base * price_impact_pct) / burst_size

        for i in range(burst_size):
            self.order_counter += 1
            oid = self.order_counter
            price = base + (i * step) if side == b"B" else base - (i * step)
            price = round(max(0.01, price), 2)
            qty = self._sample_order_quantity() * 5
            ts = time.perf_counter_ns()
            packets.append(pack_order(oid, side, price, qty, ts))

        self.asset_prices[shock_symbol] = price
        return packets

    async def generate_flash_crash_sequence(
        self,
        symbol: str,
        steps: int = 100,
        crash_drop_pct: float = 0.25,
        panic_cancel_rate: float = 0.60,
    ) -> List[bytes]:
        packets = []
        curr_price = self.asset_prices[symbol]
        price_decrement = (curr_price * crash_drop_pct) / steps
        now_ns = time.perf_counter_ns()

        for _ in range(steps):
            self.order_counter += 1
            if random.random() < panic_cancel_rate and self.active_orders[symbol]:
                target_id = self.active_orders[symbol].pop(0)
                pkt = pack_order(target_id, b"C", round(curr_price, 2), 0, now_ns)
            else:
                curr_price = max(0.01, curr_price - price_decrement)
                qty = self._sample_order_quantity(alpha=1.1) * 10
                oid = self.order_counter
                self.active_orders[symbol].append(oid)
                pkt = pack_order(oid, b"S", round(curr_price, 2), qty, now_ns)

            packets.append(pkt)

        self.asset_prices[symbol] = round(curr_price, 2)
        return packets


    def generate_correlated_multi_asset_batch(
        self,
        batch_size_per_symbol: int = 50,
        macro_market_drift: float = 0.0002,
    ) -> Dict[str, List[bytes]]:
        """Day 8 Member 3: Generates parallel order batches with macroeconomic drift correlation across all symbols."""
        results: Dict[str, List[bytes]] = {}

        for sym in self.symbols:
            # Apply shared macro shock + individual asset vol
            self.asset_prices[sym] = round(max(0.01, self.asset_prices[sym] * (1.0 + macro_market_drift)), 2)
            results[sym] = self.generate_stress_burst(sym, count=batch_size_per_symbol)

        return results


    def stream_continuous_multi_asset(
        self,
        duration_sec: float = 2.0,
        burst_interval_ms: float = 50.0,
        batch_size_per_symbol: int = 20,
    ):
        """Day 9 Member 3: Yields continuous real-time correlated order bursts across symbols."""
        start = time.perf_counter()
        while (time.perf_counter() - start) < duration_sec:
            yield self.generate_correlated_multi_asset_batch(
                batch_size_per_symbol=batch_size_per_symbol,
                macro_market_drift=0.0001,
            )
            time.sleep(burst_interval_ms / 1000.0)


    def generate_regime_switching_batch(
        self,
        regime: str = "CALM",
        batch_size_per_symbol: int = 30,
    ) -> Dict[str, List[bytes]]:
        """Day 10 Member 3: Generates order flow adjusted for CALM vs TURBULENT volatility regimes."""
        if regime.upper() == "TURBULENT":
            # Turbulent regime: 4x wider spread spread, aggressive size bursts, and drift
            return self.generate_correlated_multi_asset_batch(
                batch_size_per_symbol=batch_size_per_symbol * 2,
                macro_market_drift=-0.005,
            )
        else:
            # Calm regime: tight spreads and balanced volume
            return self.generate_correlated_multi_asset_batch(
                batch_size_per_symbol=batch_size_per_symbol,
                macro_market_drift=0.0001,
            )
