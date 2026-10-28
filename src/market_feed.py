import time
from typing import Dict, List, Tuple
from src.schema import pack_order

class MarketSimulator:
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or ["AAPL", "MSFT", "TSLA"]
        self.asset_prices = {s: 100.0 for s in self.symbols}
        self.next_order_id = 1

    def generate_correlated_multi_asset_batch(self, batch_size_per_symbol: int = 10) -> Dict[str, List[bytes]]:
        batch: Dict[str, List[bytes]] = {}
        for sym in self.symbols:
            pkts = []
            mid = self.asset_prices[sym]
            for i in range(batch_size_per_symbol):
                side = b"B" if i % 2 == 0 else b"S"
                px = round(mid - 0.05 if side == b"B" else mid + 0.05, 2)
                pkt = pack_order(
                    order_id=self.next_order_id,
                    side=side,
                    price=px,
                    quantity=100,
                    timestamp_ns=time.time_ns()
                )
                self.next_order_id += 1
                pkts.append(pkt)
            batch[sym] = pkts
        return batch

    def generate_flash_crash_cascade(
        self,
        symbol: str,
        crash_depth_pct: float = 0.08,
        cascade_steps: int = 6,
        panic_volume: int = 1000
    ) -> Tuple[List[bytes], List[bytes], float]:
        """Day 19 Member 3: Cascading market sell sweeps creating an intraday flash crash."""
        orders = []
        current_price = self.asset_prices.get(symbol, 100.0)
        step_drop = (current_price * crash_depth_pct) / cascade_steps

        for step in range(1, cascade_steps + 1):
            sweep_price = round(current_price - (step * step_drop), 2)
            orders.append(
                pack_order(
                    order_id=self.next_order_id,
                    side=b"S",
                    price=sweep_price,
                    quantity=panic_volume * step,
                    timestamp_ns=time.time_ns()
                )
            )
            self.next_order_id += 1

        new_mid = round(current_price * (1.0 - crash_depth_pct), 2)
        self.asset_prices[symbol] = new_mid

        rebound_bids = []
        for i in range(1, 4):
            rebound_bids.append(
                pack_order(
                    order_id=self.next_order_id,
                    side=b"B",
                    price=round(new_mid * (1.0 + 0.005 * i), 2),
                    quantity=panic_volume // 2,
                    timestamp_ns=time.time_ns()
                )
            )
            self.next_order_id += 1

        return orders, rebound_bids, new_mid

    def generate_quote_flood(self, symbol: str, num_quotes: int = 100) -> List[bytes]:
        """Day 19 Member 3: High-frequency burst quote stream for stress tests."""
        mid = self.asset_prices.get(symbol, 100.0)
        packets = []
        for i in range(num_quotes):
            side = b"B" if i % 2 == 0 else b"S"
            offset = 0.01 * (i % 5 + 1)
            px = round(mid - offset if side == b"B" else mid + offset, 2)
            packets.append(
                pack_order(
                    order_id=self.next_order_id,
                    side=side,
                    price=px,
                    quantity=100,
                    timestamp_ns=time.time_ns()
                )
            )
            self.next_order_id += 1
        return packets
