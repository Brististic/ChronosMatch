import time
from typing import Dict, List, Tuple, Optional


class MultiAssetOrderBookUI:
    """Maintains order book depth, spread, and latency metrics across multiple trading symbols."""

    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.books: Dict[str, Dict[str, any]] = {}
        for sym in symbols:
            self.books[sym] = {
                "mid": 100.0,
                "bids": [(99.95 - i * 0.05, 50 + i * 10) for i in range(5)],
                "asks": [(100.05 + i * 0.05, 50 + i * 10) for i in range(5)],
                "total_orders": 0,
                "latencies": [],
            }

    def update_symbol_order(self, symbol: str, side: bytes, price: float, quantity: int, latency_us: float = 0.0):
        """Updates internal order depth and latency metrics for a designated symbol."""
        if symbol not in self.books:
            return

        book = self.books[symbol]
        book["total_orders"] += 1
        book["mid"] = price
        if latency_us > 0:
            book["latencies"].append(latency_us)

        # Update top level ladder dynamically
        if side == b"B":
            book["bids"][0] = (round(price, 2), quantity)
            # Maintain ask spread sanity
            if book["asks"][0][0] <= price:
                book["asks"][0] = (round(price + 0.05, 2), 50)
        elif side == b"S":
            book["asks"][0] = (round(price, 2), quantity)
            # Maintain bid spread sanity
            if book["bids"][0][0] >= price:
                book["bids"][0] = (round(price - 0.05, 2), 50)

    def get_symbol_metrics(self, symbol: str) -> Dict[str, any]:
        """Calculates current spread and latency profile for a symbol."""
        if symbol not in self.books:
            return {}

        book = self.books[symbol]
        best_bid = book["bids"][0][0]
        best_ask = book["asks"][0][0]
        spread = round(max(0.0, best_ask - best_bid), 2)

        lats = book["latencies"]
        p50 = round(sorted(lats)[len(lats) // 2], 2) if lats else 0.0

        return {
            "symbol": symbol,
            "mid": book["mid"],
            "spread": spread,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "total_orders": book["total_orders"],
            "p50_latency_us": p50,
        }

    def render_text_snapshot(self) -> str:
        """Generates a headless multi-book text snapshot suitable for terminals and CI testing."""
        lines = [
            "=" * 70,
            f"{'CHRONOSMATCH MULTI-ASSET DASHBOARD':^70}",
            "=" * 70,
        ]
        for sym in self.symbols:
            m = self.get_symbol_metrics(sym)
            lines.append(
                f"[{sym}] Mid: ${m['mid']:.2f} | Spread: ${m['spread']:.2f} | "
                f"Top Bid: ${m['best_bid']:.2f} | Top Ask: ${m['best_ask']:.2f} | "
                f"Orders: {m['total_orders']} | P50: {m['p50_latency_us']} us"
            )
        lines.append("=" * 70)
        return "\n".join(lines)


# Backwards-compatibility alias for single-book mock tests
class MockOrderBook:
    def __init__(self, mid: float = 180.0):
        self.mid = mid
        self.bids = [(mid - 0.05, 100)]
        self.asks = [(mid + 0.05, 100)]

    def update(self):
        self.bids[0] = (self.mid - 0.05, 100)
        self.asks[0] = (self.mid + 0.05, 100)
