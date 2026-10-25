import time
import math
import random
from typing import List, Dict, Tuple
from src.schema import pack_order


class MarketSimulator:
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols if symbols else ["AAPL", "MSFT", "TSLA"]
        self.asset_prices: Dict[str, float] = {
            "AAPL": 180.0,
            "MSFT": 410.0,
            "TSLA": 240.0,
        }
        for s in self.symbols:
            if s not in self.asset_prices:
                self.asset_prices[s] = 100.0

        self.order_counter = 0
        self.market_regime = "CALM"

    def set_market_regime(self, regime: str):
        self.market_regime = regime

    def generate_ticks(self, count: int = 50, symbol: str = None) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        target_syms = [symbol] if symbol else self.symbols

        for _ in range(count):
            self.order_counter += 1
            sym = random.choice(target_syms)
            base_px = self.asset_prices[sym]

            drift = random.gauss(0, 0.05 if self.market_regime == "CALM" else 0.25)
            self.asset_prices[sym] = max(1.0, round(base_px + drift, 2))
            px = self.asset_prices[sym]

            side = b"B" if random.random() < 0.5 else b"S"
            qty = int((random.paretovariate(1.5) * 10) + 1)
            orders.append(pack_order(self.order_counter, side, px, qty, now_ns))

        return orders

    def generate_cancellations(self, active_order_ids: List[int], count: int = 5) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        to_cancel = random.sample(active_order_ids, min(len(active_order_ids), count))
        for oid in to_cancel:
            orders.append(pack_order(oid, b"B", 0.0, 0, now_ns))
        return orders

    def generate_stress_burst(self, symbol: str, count: int = 100) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        base = self.asset_prices[symbol]
        for _ in range(count):
            self.order_counter += 1
            side = b"B" if random.random() < 0.5 else b"S"
            px = round(base + random.uniform(-0.50, 0.50), 2)
            qty = random.randint(10, 500)
            orders.append(pack_order(self.order_counter, side, px, qty, now_ns))
        return orders

    def generate_shock_sequence(self, symbol: str, drop_percent: float = 0.08) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        base = self.asset_prices[symbol]
        crashed_px = base * (1.0 - drop_percent)

        for step in range(20):
            self.order_counter += 1
            curr_px = round(base - (step * ((base - crashed_px) / 20.0)), 2)
            orders.append(pack_order(self.order_counter, b"S", curr_px, 100, now_ns))

        self.asset_prices[symbol] = round(crashed_px, 2)
        return orders

    def generate_l2_depth_ladder(self, symbol: str, depth_levels: int = 5) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        mid = self.asset_prices[symbol]
        for level in range(1, depth_levels + 1):
            bid_px = round(mid - (level * 0.05), 2)
            ask_px = round(mid + (level * 0.05), 2)
            self.order_counter += 1
            orders.append(pack_order(self.order_counter, b"B", bid_px, level * 50, now_ns))
            self.order_counter += 1
            orders.append(pack_order(self.order_counter, b"S", ask_px, level * 50, now_ns))
        return orders

    def generate_correlated_multi_asset_batch(
        self, batch_size_per_symbol: int = 50, macro_market_drift: float = 0.0005
    ) -> Dict[str, List[bytes]]:
        result = {}
        now_ns = time.perf_counter_ns()
        for sym in self.symbols:
            self.asset_prices[sym] = round(self.asset_prices[sym] * (1.0 + macro_market_drift), 2)
            base = self.asset_prices[sym]
            pkts = []
            for _ in range(batch_size_per_symbol):
                self.order_counter += 1
                side = b"B" if random.random() < 0.52 else b"S"
                px = round(base + random.uniform(-0.10, 0.10), 2)
                qty = random.randint(10, 100)
                pkts.append(pack_order(self.order_counter, side, px, qty, now_ns))
            result[sym] = pkts
        return result

    def generate_jittered_burst(self, symbol: str, count: int = 20, max_jitter_us: int = 100) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        base = self.asset_prices[symbol]
        for _ in range(count):
            self.order_counter += 1
            side = b"B" if random.random() < 0.5 else b"S"
            px = round(base + random.uniform(-0.05, 0.05), 2)
            qty = random.randint(5, 50)
            jitter_ns = int(random.uniform(0, max_jitter_us) * 1000)
            orders.append(pack_order(self.order_counter, side, px, qty, now_ns + jitter_ns))
        return orders

    def generate_iceberg_order_slice(
        self, symbol: str, side: bytes, total_quantity: int = 1000, clip_count: int = 10
    ) -> List[bytes]:
        orders = []
        now_ns = time.perf_counter_ns()
        px = self.asset_prices[symbol]
        clip_size = total_quantity // clip_count
        remainder = total_quantity % clip_count

        for i in range(clip_count):
            self.order_counter += 1
            qty = clip_size + (remainder if i == clip_count - 1 else 0)
            orders.append(pack_order(self.order_counter, side, px, qty, now_ns))
        return orders

    def generate_l3_lifecycle_stream(self, symbol: str, events_count: int = 30) -> List[Tuple[str, bytes]]:
        events = []
        now_ns = time.perf_counter_ns()
        base = self.asset_prices[symbol]
        self.order_counter += 1
        oid = self.order_counter

        events.append(("ADD", pack_order(oid, b"B", base, 100, now_ns)))
        events.append(("MODIFY", pack_order(oid, b"B", round(base + 0.05, 2), 150, now_ns + 1000)))
        events.append(("CANCEL", pack_order(oid, b"B", 0.0, 0, now_ns + 2000)))

        for _ in range(events_count - 3):
            self.order_counter += 1
            side = b"B" if random.random() < 0.5 else b"S"
            px = round(base + random.uniform(-0.1, 0.1), 2)
            events.append(("ADD", pack_order(self.order_counter, side, px, random.randint(10, 50), now_ns)))

        return events

    def generate_arbitrage_pair_burst(
        self, sym_a: str = "AAPL", sym_b: str = "MSFT", spread_threshold: float = 0.20
    ) -> Dict[str, List[bytes]]:
        now_ns = time.perf_counter_ns()
        self.order_counter += 1
        buy_leg = pack_order(self.order_counter, b"B", self.asset_prices[sym_a], 50, now_ns)
        self.order_counter += 1
        sell_leg = pack_order(self.order_counter, b"S", self.asset_prices[sym_b], 50, now_ns)
        return {sym_a: [buy_leg], sym_b: [sell_leg]}

    def generate_book_imbalance_pressure(
        self,
        symbol: str,
        target_imbalance: float = 0.80,
        batch_size: int = 30,
    ) -> List[bytes]:
        if symbol not in self.asset_prices:
            self.asset_prices[symbol] = 150.0

        base_px = self.asset_prices[symbol]
        now_ns = time.perf_counter_ns()
        orders: List[bytes] = []

        bid_probability = max(0.05, min(0.95, (target_imbalance + 1.0) / 2.0))

        for _ in range(batch_size):
            self.order_counter += 1
            if random.random() < bid_probability:
                side = b"B"
                px = round(base_px - random.uniform(0.01, 0.15), 2)
                qty = random.randint(50, 250)
            else:
                side = b"S"
                px = round(base_px + random.uniform(0.01, 0.15), 2)
                qty = random.randint(5, 40)

            orders.append(pack_order(self.order_counter, side, px, qty, now_ns))

        return orders

    def generate_toxic_flow(
        self,
        symbol: str,
        sweep_depth: int = 15,
        price_jump: float = 1.50,
    ) -> Tuple[List[bytes], float]:
        if symbol not in self.asset_prices:
            self.asset_prices[symbol] = 150.0

        base_px = self.asset_prices[symbol]
        now_ns = time.perf_counter_ns()
        toxic_orders: List[bytes] = []

        for step in range(sweep_depth):
            self.order_counter += 1
            sweep_px = round(base_px + (step * (price_jump / sweep_depth)), 2)
            qty = random.randint(100, 300)
            toxic_orders.append(pack_order(self.order_counter, b"B", sweep_px, qty, now_ns))

        new_price = round(base_px + price_jump, 2)
        self.asset_prices[symbol] = new_price

        return toxic_orders, new_price
