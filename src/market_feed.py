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


    def generate_spoofing_attack(
        self,
        symbol: str,
        fake_layers: int = 5,
        fake_qty_per_layer: int = 1000,
    ) -> Tuple[List[bytes], bytes, List[bytes]]:
        if symbol not in self.asset_prices:
            self.asset_prices[symbol] = 150.0

        base_px = self.asset_prices[symbol]
        now_ns = time.perf_counter_ns()

        fake_bids: List[bytes] = []
        fake_order_ids: List[int] = []

        for i in range(1, fake_layers + 1):
            self.order_counter += 1
            fake_px = round(base_px - (i * 0.02), 2)
            fake_bids.append(pack_order(self.order_counter, b"B", fake_px, fake_qty_per_layer, now_ns))
            fake_order_ids.append(self.order_counter)

        self.order_counter += 1
        real_sell = pack_order(self.order_counter, b"S", base_px, 150, now_ns + 500)

        cancellations: List[bytes] = []
        for oid in fake_order_ids:
            cancellations.append(pack_order(oid, b"B", 0.0, 0, now_ns + 1500))

        return fake_bids, real_sell, cancellations


    def generate_quote_stuffing_burst(
        self,
        symbol: str,
        burst_size: int = 100,
        price_offset: float = 5.0,
    ) -> List[bytes]:
        if symbol not in self.asset_prices:
            self.asset_prices[symbol] = 150.0

        base_px = self.asset_prices[symbol]
        now_ns = time.perf_counter_ns()
        stuffed_packets: List[bytes] = []

        for i in range(burst_size):
            self.order_counter += 1
            oid = self.order_counter
            deep_bid_px = max(0.01, round(base_px - price_offset - (i * 0.01), 2))
            
            # Rapid microsecond-spaced ADD followed by immediate CANCEL
            stuffed_packets.append(pack_order(oid, b"B", deep_bid_px, 1, now_ns + (i * 100)))
            stuffed_packets.append(pack_order(oid, b"B", 0.0, 0, now_ns + (i * 100) + 50))

        return stuffed_packets

    def _get_next_id(self) -> int:
        for attr in ("current_order_id", "_order_id", "next_order_id", "order_id"):
            if hasattr(self, attr):
                val = getattr(self, attr)
                setattr(self, attr, val + 1)
                return val
        if not hasattr(self, "_fallback_id"):
            self._fallback_id = 1000
        val = self._fallback_id
        self._fallback_id += 1
        return val

    def generate_flash_crash_cascade(
        self,
        symbol: str,
        crash_depth_pct: float = 0.08,
        cascade_steps: int = 6,
        panic_volume: int = 1000
    ):
        """Day 19 Member 3: Cascading market sell sweeps creating an intraday flash crash."""
        orders = []
        current_price = self.asset_prices.get(symbol, 100.0)
        step_drop = (current_price * crash_depth_pct) / cascade_steps

        for step in range(1, cascade_steps + 1):
            sweep_price = round(current_price - (step * step_drop), 2)
            orders.append(
                pack_order(
                    order_id=self._get_next_id(),
                    side=b"S",
                    price=sweep_price,
                    quantity=panic_volume * step,
                    timestamp_ns=time.time_ns()
                )
            )

        new_mid = round(current_price * (1.0 - crash_depth_pct), 2)
        self.asset_prices[symbol] = new_mid

        rebound_bids = []
        for i in range(1, 4):
            rebound_bids.append(
                pack_order(
                    order_id=self._get_next_id(),
                    side=b"B",
                    price=round(new_mid * (1.0 + 0.005 * i), 2),
                    quantity=panic_volume // 2,
                    timestamp_ns=time.time_ns()
                )
            )

        return orders, rebound_bids, new_mid

    def generate_quote_flood(self, symbol: str, num_quotes: int = 100):
        """Day 19 Member 3: High-frequency burst quote stream for stress tests."""
        mid = self.asset_prices.get(symbol, 100.0)
        packets = []
        for i in range(num_quotes):
            side = b"B" if i % 2 == 0 else b"S"
            offset = 0.01 * (i % 5 + 1)
            px = round(mid - offset if side == b"B" else mid + offset, 2)
            packets.append(
                pack_order(
                    order_id=self._get_next_id(),
                    side=side,
                    price=px,
                    quantity=100,
                    timestamp_ns=time.time_ns()
                )
            )
        return packets

    def generate_systemic_crisis_burst(
        self,
        volatility_multiplier: float = 2.5,
        orders_per_symbol: int = 50
    ) -> dict[str, list[bytes]]:
        """Day 20 Member 3: Multi-asset cross-market synchronized liquidity collapse simulation."""
        crisis_batches: dict[str, list[bytes]] = {}
        for sym in self.symbols:
            base_px = self.asset_prices.get(sym, 100.0)
            pkts = []
            for i in range(orders_per_symbol):
                # Spread blowout and directional panic bias
                is_panic_sell = (i % 3 != 0)
                spread_widen = (i % 10 + 1) * 0.25 * volatility_multiplier
                px = round(base_px - spread_widen if is_panic_sell else base_px + spread_widen, 2)
                qty = (i % 5 + 1) * 200

                pkts.append(
                    pack_order(
                        order_id=self._get_next_id(),
                        side=b"S" if is_panic_sell else b"B",
                        price=max(1.0, px),
                        quantity=qty,
                        timestamp_ns=time.time_ns()
                    )
                )
            self.asset_prices[sym] = round(base_px * (0.95 if sym == "TSLA" else 0.97), 2)
            crisis_batches[sym] = pkts
        return crisis_batches

    def run_producer_stream(
        self,
        hub_base_dir: str,
        cycles: int = 10,
        batch_size: int = 10,
        delay_s: float = 0.02
    ) -> int:
        """Day 21 Member 3: Standalone streaming loop designed to run in an isolated OS process."""
        from src.ipc_hub import MultiRingIPCHub
        hub = MultiRingIPCHub(symbols=self.symbols, base_dir=hub_base_dir, create=False)
        total_produced = 0
        try:
            for _ in range(cycles):
                batch = self.generate_correlated_multi_asset_batch(batch_size_per_symbol=batch_size)
                for sym, pkts in batch.items():
                    if pkts:
                        hub.write_symbol_batch(sym, pkts)
                        total_produced += len(pkts)
                time.sleep(delay_s)
        finally:
            hub.close()
        return total_produced
