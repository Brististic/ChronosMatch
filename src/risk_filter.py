import time
from typing import Tuple, Optional
from src.schema import Order, unpack_order


class PreTradeRiskFilter:
    def __init__(
        self,
        max_notional: float = 100_000.0,
        max_price_band_pct: float = 0.10,
        max_orders_per_window: int = 100,
        window_ms: float = 1.0,
    ):
        self.max_notional = max_notional
        self.max_price_band_pct = max_price_band_pct
        self.max_orders_per_window = max_orders_per_window
        self.window_ns = int(window_ms * 1_000_000)

        self._window_start_ns = time.perf_counter_ns()
        self._order_count_in_window = 0

    def validate_order(self, raw_bytes: bytes, reference_price: float) -> Tuple[bool, Optional[str]]:
        order: Order = unpack_order(raw_bytes)

        # 1. Cancellations bypass notional / band checks
        if order.quantity == 0:
            return True, None

        # 2. Max Notional Check
        notional = order.price * order.quantity
        if notional > self.max_notional:
            return False, f"EXCEEDS_MAX_NOTIONAL: {notional:.2f} > {self.max_notional:.2f}"

        # 3. Fat-Finger Price Banding Check
        if reference_price > 0.0:
            pct_deviation = abs(order.price - reference_price) / reference_price
            if pct_deviation > self.max_price_band_pct:
                return False, f"PRICE_BAND_VIOLATION: {pct_deviation:.4f} > {self.max_price_band_pct:.4f}"

        # 4. Rate-Limit Throttle
        now_ns = time.perf_counter_ns()
        if now_ns - self._window_start_ns > self.window_ns:
            self._window_start_ns = now_ns
            self._order_count_in_window = 1
        else:
            self._order_count_in_window += 1
            if self._order_count_in_window > self.max_orders_per_window:
                return False, f"RATE_LIMIT_EXCEEDED: count {self._order_count_in_window} > {self.max_orders_per_window}"

        return True, None
