# cython: language_level=3
"""
ChronosMatch matching engine extension.

Day 1: build toolchain proof (C++17, nogil).
Day 2: C structs Order / OrderNode (see order_types.pxd) and the Python-visible
       helpers that let the test-suite inspect them.
Day 3: price-time priority queue per price level (see price_level.pxd) and
       the LevelQueue wrapper used to test it from Python.
Day 4: limit order book with insert and cancel (see order_book.pxd) and the
       OrderBook wrapper used to test it from Python.
Day 5: matching of crossing limit orders (see matching.pxd): OrderBook.submit().
Day 8: price levels are found through a contiguous table indexed by integer price ticks
       (O(1) lookup instead of binary search); prices are exact integer ticks (price_scale).
Day 7: partial execution with remainder persistence; every fill carries a trade id and the
       quantity left on both orders (submit_traced), so a ledger can be rebuilt from fills alone.
Day 6: the matching runs in `with nogil:` blocks (GIL released); submit_batch() feeds many
       orders per nogil section; a per-book mutex keeps shared use from threads safe.
"""
import time
from libc.stdint cimport int64_t, uint8_t, uint64_t
from libc.string cimport memcpy

from chronos_engine.order_types cimport (
    Order, OrderNode, WireOrder, SIDE_BUY, SIDE_SELL,
    side_is_valid, order_from_wire, node_alloc, node_free, node_link,
)
from chronos_engine.price_level cimport (
    PriceLevel, level_init, level_is_empty, level_peek_front, level_push_back,
    level_remove, level_pop_front, price_better,
)
from chronos_engine.order_book cimport (
    Book, BookSide, BOOK_OK, BOOK_ERR_DUPLICATE, BOOK_ERR_INVALID,
    BOOK_ERR_NOMEM, BOOK_ERR_NOT_FOUND, BOOK_ERR_RANGE,
    book_init, book_destroy, book_insert, book_cancel, book_check,
    side_best, side_next, side_find_tick, price_to_tick, idmap_find, IdEntry,
)
from chronos_engine.matching cimport (
    Fill, FillBuffer, SubmitSummary, fillbuf_init, fillbuf_free, fillbuf_clear,
    book_submit, book_submit_ex,
)

cdef extern from "<mutex>" namespace "std" nogil:
    cdef cppclass mutex:
        mutex() noexcept
        void lock() noexcept
        void unlock() noexcept


cdef extern from "Python.h":
    int PyGILState_Check() nogil     # 1 if this thread holds the GIL, else 0 (safe to call without it)


cdef extern from *:
    """
    #include <cstdint>
    static inline long chronos_cpp_standard() { return (long)__cplusplus; }
    """
    long chronos_cpp_standard() noexcept nogil


cdef int ENGINE_ABI = 1


cdef int _add_nogil(int a, int b) noexcept nogil:
    # Pure C: touches no Python object, so it can run without the GIL.
    return a + b


# ----------------------------------------------------------------- Day 1 ----
def cpp_standard():
    """Value of __cplusplus seen by the compiler (201703 means C++17)."""
    return chronos_cpp_standard()


def selftest_nogil(int a, int b):
    """Run a C function inside a `with nogil` block and return the result."""
    cdef int result
    with nogil:
        result = _add_nogil(a, b)
    return result


def build_info():
    """Small dict used by the test-suite and the demo to verify the build."""
    return {
        "abi": ENGINE_ABI,
        "cpp_standard": chronos_cpp_standard(),
        "nogil_selftest": selftest_nogil(2, 3) == 5,
    }


# ----------------------------------------------------------------- Day 2 ----
def layout_info():
    """Sizes and field offsets (bytes) of the C structs, for layout tests."""
    cdef WireOrder w
    cdef Order o
    cdef OrderNode n
    cdef char* wb = <char*> &w
    cdef char* ob = <char*> &o
    cdef char* nb = <char*> &n
    return {
        "sizeof_wire_order": sizeof(WireOrder),
        "sizeof_order": sizeof(Order),
        "sizeof_order_node": sizeof(OrderNode),
        "wire_offsets": {
            "order_id": <char*> &w.order_id - wb,
            "side": <char*> &w.side - wb,
            "price": <char*> &w.price - wb,
            "quantity": <char*> &w.quantity - wb,
            "timestamp_ns": <char*> &w.timestamp_ns - wb,
        },
        "order_offsets": {
            "order_id": <char*> &o.order_id - ob,
            "timestamp_ns": <char*> &o.timestamp_ns - ob,
            "price": <char*> &o.price - ob,
            "quantity": <char*> &o.quantity - ob,
            "side": <char*> &o.side - ob,
        },
        "node_offsets": {
            "order": <char*> &n.order - nb,
            "prev": <char*> &n.prev - nb,
            "next": <char*> &n.next - nb,
        },
    }


def decode_wire(const unsigned char[::1] data):
    """
    Decode one 29-byte message written with struct format "=QcdIQ" into the
    engine's C Order and return it as a tuple
    (order_id, side, price, quantity, timestamp_ns).
    Raises ValueError for a short buffer or an invalid side byte.
    """
    cdef WireOrder wire
    cdef Order order
    if data.shape[0] < <Py_ssize_t> sizeof(WireOrder):
        raise ValueError("buffer shorter than one wire order (29 bytes)")
    memcpy(&wire, &data[0], sizeof(WireOrder))   # memcpy: wire data may be unaligned
    if not side_is_valid(wire.side):
        raise ValueError("invalid side byte, expected b'B' or b'S'")
    order_from_wire(&order, &wire)
    return (order.order_id, bytes([order.side]), order.price,
            order.quantity, order.timestamp_ns)


def selftest_linked_nodes(int count=3):
    """
    Build `count` OrderNodes in a doubly-linked chain with C pointers only,
    walk it forwards and backwards, free everything and return
    (forward_sum, backward_sum) of the quantities (both must be equal).
    """
    cdef OrderNode* head = NULL
    cdef OrderNode* tail = NULL
    cdef OrderNode* node
    cdef OrderNode* nxt
    cdef unsigned long long fwd = 0
    cdef unsigned long long bwd = 0
    cdef int i
    with nogil:
        for i in range(count):
            node = node_alloc()
            if node == NULL:
                break
            node.order.order_id = i + 1
            node.order.quantity = (i + 1) * 100
            node.order.side = SIDE_BUY
            if head == NULL:
                head = node
            else:
                node_link(tail, node)
            tail = node
        node = head
        while node != NULL:           # forward walk
            fwd += node.order.quantity
            node = node.next
        node = tail
        while node != NULL:           # backward walk
            bwd += node.order.quantity
            node = node.prev
        node = head
        while node != NULL:           # free the chain
            nxt = node.next
            node_free(node)
            node = nxt
    return (fwd, bwd)


# ----------------------------------------------------------------- Day 3 ----
def is_better_price(bytes side, double a, double b):
    """Price priority rule: True if price `a` beats `b` for side b'B' or b'S'."""
    if len(side) != 1 or not side_is_valid(side[0]):
        raise ValueError("side must be b'B' or b'S'")
    return bool(price_better(side[0], a, b))


cdef class LevelQueue:
    """
    Python-visible wrapper around ONE C PriceLevel (FIFO queue of OrderNodes).
    It exists so the C queue can be tested from Python; the engine itself will
    call the C functions directly. Remaining nodes are freed on deallocation.
    """
    cdef PriceLevel _lvl

    def __cinit__(self, double price):
        level_init(&self._lvl, price, 0)

    def __dealloc__(self):
        cdef OrderNode* node = self._lvl.head
        cdef OrderNode* nxt
        while node != NULL:
            nxt = node.next
            node_free(node)
            node = nxt

    @property
    def price(self):
        return self._lvl.price

    @property
    def total_quantity(self):
        return self._lvl.total_quantity

    @property
    def order_count(self):
        return self._lvl.order_count

    def __len__(self):
        return self._lvl.order_count

    def push(self, unsigned long long order_id, unsigned int quantity,
             unsigned long long timestamp_ns=0, bytes side=b"B"):
        """Append an order at the tail (lowest time priority)."""
        cdef OrderNode* node
        if quantity == 0:
            raise ValueError("quantity must be > 0")
        if len(side) != 1 or not side_is_valid(side[0]):
            raise ValueError("side must be b'B' or b'S'")
        node = node_alloc()
        if node == NULL:
            raise MemoryError()
        node.order.order_id = order_id
        node.order.timestamp_ns = timestamp_ns
        node.order.price = self._lvl.price
        node.order.quantity = quantity
        node.order.side = side[0]
        level_push_back(&self._lvl, node)

    def peek(self):
        """(order_id, quantity) of the oldest order, or None if empty."""
        cdef OrderNode* node = level_peek_front(&self._lvl)
        if node == NULL:
            return None
        return (node.order.order_id, node.order.quantity)

    def pop(self):
        """Remove and return (order_id, quantity) of the oldest order, or None."""
        cdef OrderNode* node = level_pop_front(&self._lvl)
        cdef unsigned long long oid
        cdef unsigned int qty
        if node == NULL:
            return None
        oid = node.order.order_id
        qty = node.order.quantity
        node_free(node)
        return (oid, qty)

    def remove(self, unsigned long long order_id):
        """Cancel an order by id (linear search: test helper only; Day 4 adds O(1) lookup)."""
        cdef OrderNode* node = self._lvl.head
        while node != NULL:
            if node.order.order_id == order_id:
                level_remove(&self._lvl, node)
                node_free(node)
                return True
            node = node.next
        return False

    def orders(self):
        """All resting orders as [(order_id, quantity), ...] in time priority order."""
        out = []
        cdef OrderNode* node = self._lvl.head
        while node != NULL:
            out.append((node.order.order_id, node.order.quantity))
            node = node.next
        return out

    def check_integrity(self):
        """Walk forwards and backwards; verify links, count and total quantity."""
        cdef OrderNode* node = self._lvl.head
        cdef OrderNode* last = NULL
        cdef unsigned long long total = 0
        cdef unsigned long count = 0
        cdef unsigned long back = 0
        if node != NULL and node.prev != NULL:
            return False
        while node != NULL:
            if node.next != NULL and node.next.prev != node:
                return False
            total += node.order.quantity
            count += 1
            last = node
            node = node.next
        if last != self._lvl.tail:
            return False
        node = self._lvl.tail
        while node != NULL:
            back += 1
            node = node.prev
        return count == self._lvl.order_count and back == count and total == self._lvl.total_quantity


# ----------------------------------------------------------------- Day 4 ----
cdef inline BookSide* _side_ptr(Book* b, bytes side) except NULL:
    if len(side) != 1 or not side_is_valid(side[0]):
        raise ValueError("side must be b'B' or b'S'")
    return &b.bids if side[0] == SIDE_BUY else &b.asks


cdef class OrderBook:
    """
    Python-visible wrapper around the C limit order book.

    Day 6: matching runs inside `with nogil:` blocks, i.e. WITHOUT the Python GIL,
    so other Python threads keep running while the engine matches. Because that
    would let two threads touch one book at the same moment, every method takes
    the book's own C++ mutex first (waiting for it with the GIL released); the
    matching code itself never calls back into Python while holding that mutex.
    """
    cdef Book _book
    cdef mutex _lock
    cdef int _gil_probe          # PyGILState_Check() seen inside the last nogil match (-1 = none yet)

    def __cinit__(self, long long price_scale=100):
        if price_scale < 1 or price_scale > 1000000:
            raise ValueError("price_scale must be between 1 and 1,000,000 (ticks per 1.00)")
        self._gil_probe = -1
        if book_init(&self._book, price_scale) != BOOK_OK:
            raise MemoryError()

    def __dealloc__(self):
        book_destroy(&self._book)

    cdef void _acquire(self) noexcept:
        with nogil:                  # wait for the book mutex WITHOUT holding the GIL
            self._lock.lock()

    cdef void _release(self) noexcept:
        self._lock.unlock()

    @property
    def price_scale(self):
        """Ticks per 1.00 of price (100 means the smallest price step is 0.01)."""
        return self._book.price_scale

    @property
    def tick_size(self):
        return 1.0 / self._book.price_scale

    @property
    def gil_held_in_last_match(self):
        """1 if the GIL was held while the last submit/submit_batch matched, 0 if released, -1 if none yet."""
        return self._gil_probe

    def __len__(self):
        self._acquire()
        try:
            return self._book.ids.used
        finally:
            self._release()

    def __contains__(self, unsigned long long order_id):
        self._acquire()
        try:
            return idmap_find(&self._book.ids, order_id) != NULL
        finally:
            self._release()

    cdef int _insert(self, Order* order) except -1:
        cdef int rc = book_insert(&self._book, order)
        if rc == BOOK_OK:
            return 0
        if rc == BOOK_ERR_DUPLICATE:
            raise ValueError("duplicate order id")
        if rc == BOOK_ERR_INVALID:
            raise ValueError("invalid order (side must be b'B'/b'S', quantity > 0, price > 0 and a whole number of ticks)")
        if rc == BOOK_ERR_RANGE:
            raise ValueError("price is too far from the other prices on this side of the book")
        raise MemoryError()

    def insert(self, unsigned long long order_id, bytes side, double price,
               unsigned int quantity, unsigned long long timestamp_ns=0):
        """Rest a limit order on the book. Raises ValueError on bad input or duplicate id."""
        cdef Order order
        if len(side) != 1:
            raise ValueError("side must be b'B' or b'S'")
        order.order_id = order_id
        order.timestamp_ns = timestamp_ns
        order.price = price
        order.quantity = quantity
        order.side = side[0]
        self._acquire()
        try:
            self._insert(&order)
        finally:
            self._release()

    cdef int _run_submit(self, Order* order, FillBuffer* fills, SubmitSummary* summary) noexcept:
        # Match one order with the GIL released, under the book mutex. Returns a BookStatus.
        cdef int rc
        cdef int gil_state = -1
        self._acquire()
        try:
            with nogil:                                  # <-- core matching without the GIL
                gil_state = PyGILState_Check()
                rc = book_submit_ex(&self._book, order, fills, summary)
            self._gil_probe = gil_state
        finally:
            self._release()
        return rc

    cdef int _raise_for(self, int rc) except -1:
        if rc == BOOK_OK:
            return 0
        if rc == BOOK_ERR_DUPLICATE:
            raise ValueError("duplicate order id")
        if rc == BOOK_ERR_INVALID:
            raise ValueError("invalid order (side must be b'B'/b'S', quantity > 0, price > 0 and a whole number of ticks)")
        if rc == BOOK_ERR_RANGE:
            raise ValueError("price is too far from the other prices on this side of the book")
        raise MemoryError()

    def submit(self, unsigned long long order_id, bytes side, double price,
               unsigned int quantity, unsigned long long timestamp_ns=0):
        """
        Submit a limit order: trade against crossing orders on the other side
        (best price first, oldest first, at the resting order's price), then rest
        any remainder. The matching runs with the GIL released.
        Returns the fills as [(taker_id, maker_id, price, quantity)].
        Raises ValueError for invalid input or an id already resting on the book.
        """
        cdef Order order
        cdef FillBuffer fills
        cdef unsigned int i
        cdef int rc
        if len(side) != 1:
            raise ValueError("side must be b'B' or b'S'")
        order.order_id = order_id
        order.timestamp_ns = timestamp_ns
        order.price = price
        order.quantity = quantity
        order.side = side[0]
        fillbuf_init(&fills)
        try:
            rc = self._run_submit(&order, &fills, NULL)
            out = []
            for i in range(fills.count):
                out.append((fills.items[i].taker_id, fills.items[i].maker_id,
                            fills.items[i].price, fills.items[i].quantity))
        finally:
            fillbuf_free(&fills)
        self._raise_for(rc)
        return out

    def submit_traced(self, unsigned long long order_id, bytes side, double price,
                      unsigned int quantity, unsigned long long timestamp_ns=0):
        """
        Like submit(), but returns a full audit record:
          {"status": "FILLED" | "PARTIAL" | "RESTING",
           "filled_quantity": int, "resting_quantity": int,
           "fills": [(trade_id, taker_id, maker_id, price, quantity,
                      taker_remaining, maker_remaining, timestamp_ns), ...]}
        FILLED  = the whole order traded; PARTIAL = part traded and the remainder now rests;
        RESTING = nothing traded and the whole order rests.
        """
        cdef Order order
        cdef FillBuffer fills
        cdef SubmitSummary summary
        cdef unsigned int i
        cdef int rc
        if len(side) != 1:
            raise ValueError("side must be b'B' or b'S'")
        order.order_id = order_id
        order.timestamp_ns = timestamp_ns
        order.price = price
        order.quantity = quantity
        order.side = side[0]
        fillbuf_init(&fills)
        try:
            rc = self._run_submit(&order, &fills, &summary)
            out = []
            for i in range(fills.count):
                out.append((fills.items[i].trade_id, fills.items[i].taker_id,
                            fills.items[i].maker_id, fills.items[i].price,
                            fills.items[i].quantity, fills.items[i].taker_remaining,
                            fills.items[i].maker_remaining, fills.items[i].timestamp_ns))
        finally:
            fillbuf_free(&fills)
        self._raise_for(rc)
        if summary.filled_quantity == 0:
            status = "RESTING"
        elif summary.filled_quantity == quantity:
            status = "FILLED"
        else:
            status = "PARTIAL"
        return {"status": status, "filled_quantity": summary.filled_quantity,
                "resting_quantity": summary.resting_quantity, "fills": out}

    def remaining(self, unsigned long long order_id):
        """Quantity still resting for this order id, or None if it is not on the book."""
        cdef IdEntry* e
        self._acquire()
        try:
            e = idmap_find(&self._book.ids, order_id)
            if e == NULL:
                return None
            return e.node.order.quantity
        finally:
            self._release()

    @property
    def trades_executed(self):
        """Number of trades executed on this book so far (the last trade id)."""
        return self._book.next_trade_id - 1

    def submit_batch(self, const unsigned char[::1] data, bint collect=True):
        """
        Submit many orders in ONE nogil section. `data` is a sequence of 29-byte wire
        messages (struct format "=QcdIQ"), e.g. a slice of the shared ring buffer.
        Invalid or duplicate-id orders are counted as rejected and skipped.
        Returns {"orders", "rejected", "fill_count", "filled_quantity", "fills"};
        "fills" is a list of (taker_id, maker_id, price, quantity) when collect=True, else None.
        """
        cdef Py_ssize_t wire_size = sizeof(WireOrder)
        cdef Py_ssize_t n = data.shape[0] // wire_size
        cdef Py_ssize_t i
        cdef Py_ssize_t done = 0
        cdef unsigned int j
        cdef WireOrder wire
        cdef Order order
        cdef FillBuffer fills
        cdef const unsigned char* base
        cdef int rc
        cdef int gil_state = -1
        cdef int out_of_memory = 0
        cdef unsigned long long rejected = 0
        cdef unsigned long long fill_count = 0
        cdef unsigned long long filled_quantity = 0
        if data.shape[0] % wire_size != 0:
            raise ValueError("buffer length must be a multiple of 29 bytes")
        if n == 0:
            return {"orders": 0, "rejected": 0, "fill_count": 0,
                    "filled_quantity": 0, "fills": [] if collect else None}
        base = &data[0]
        fillbuf_init(&fills)
        self._acquire()
        try:
            with nogil:                                  # <-- whole batch without the GIL
                gil_state = PyGILState_Check()
                for i in range(n):
                    memcpy(&wire, base + i * wire_size, wire_size)
                    order_from_wire(&order, &wire)
                    rc = book_submit(&self._book, &order, &fills)
                    if rc == BOOK_ERR_NOMEM:
                        out_of_memory = 1
                        break
                    done += 1
                    if rc != BOOK_OK:
                        rejected += 1
                    if not collect:
                        for j in range(fills.count):
                            filled_quantity += fills.items[j].quantity
                        fill_count += fills.count
                        fillbuf_clear(&fills)
                if collect:
                    for j in range(fills.count):
                        filled_quantity += fills.items[j].quantity
                    fill_count = fills.count
            self._gil_probe = gil_state
            out = None
            if collect:
                out = []
                for j in range(fills.count):
                    out.append((fills.items[j].taker_id, fills.items[j].maker_id,
                                fills.items[j].price, fills.items[j].quantity))
        finally:
            self._release()
            fillbuf_free(&fills)
        if out_of_memory:
            raise MemoryError()
        return {"orders": done, "rejected": rejected, "fill_count": fill_count,
                "filled_quantity": filled_quantity, "fills": out}

    def insert_wire(self, const unsigned char[::1] data):
        """Insert one 29-byte wire message (struct format "=QcdIQ") straight from bytes."""
        cdef WireOrder wire
        cdef Order order
        if data.shape[0] < <Py_ssize_t> sizeof(WireOrder):
            raise ValueError("buffer shorter than one wire order (29 bytes)")
        memcpy(&wire, &data[0], sizeof(WireOrder))
        order_from_wire(&order, &wire)
        self._acquire()
        try:
            self._insert(&order)
        finally:
            self._release()

    def cancel(self, unsigned long long order_id):
        """Cancel a resting order. Returns True if it was on the book, else False."""
        self._acquire()
        try:
            return book_cancel(&self._book, order_id) == BOOK_OK
        finally:
            self._release()

    def best_bid(self):
        """(price, total_quantity) of the best bid level, or None."""
        cdef PriceLevel* lvl
        self._acquire()
        try:
            lvl = side_best(&self._book.bids)
            if lvl == NULL:
                return None
            return (lvl.price, lvl.total_quantity)
        finally:
            self._release()

    def best_ask(self):
        """(price, total_quantity) of the best ask level, or None."""
        cdef PriceLevel* lvl
        self._acquire()
        try:
            lvl = side_best(&self._book.asks)
            if lvl == NULL:
                return None
            return (lvl.price, lvl.total_quantity)
        finally:
            self._release()

    def spread(self):
        """best ask - best bid, or None when one side is empty."""
        cdef PriceLevel* bid
        cdef PriceLevel* ask
        self._acquire()
        try:
            bid = side_best(&self._book.bids)
            ask = side_best(&self._book.asks)
            if bid == NULL or ask == NULL:
                return None
            return ask.price - bid.price
        finally:
            self._release()

    def level_count(self, bytes side):
        cdef BookSide* s = _side_ptr(&self._book, side)
        self._acquire()
        try:
            return s.level_count
        finally:
            self._release()

    def depth(self, bytes side, unsigned int levels=5):
        """Top `levels` price levels of a side, best first: [(price, total_qty, order_count)]."""
        cdef BookSide* s = _side_ptr(&self._book, side)
        cdef PriceLevel* lvl
        cdef unsigned int n = 0
        out = []
        self._acquire()
        try:
            lvl = side_best(s)
            while lvl != NULL and n < levels:
                out.append((lvl.price, lvl.total_quantity, lvl.order_count))
                n += 1
                lvl = side_next(s, lvl)
        finally:
            self._release()
        return out

    def level_orders(self, bytes side, double price):
        """[(order_id, quantity), ...] at one price in time priority order ([] if no level)."""
        cdef BookSide* s = _side_ptr(&self._book, side)
        cdef PriceLevel* lvl
        cdef OrderNode* node
        cdef int64_t tick
        out = []
        self._acquire()
        try:
            if price_to_tick(&self._book, price, &tick):
                lvl = side_find_tick(s, tick)
                if lvl != NULL:
                    node = lvl.head
                    while node != NULL:
                        out.append((node.order.order_id, node.order.quantity))
                        node = node.next
        finally:
            self._release()
        return out

    def check_integrity(self):
        """Verify sorting, links, totals and the id table against each other."""
        self._acquire()
        try:
            return bool(book_check(&self._book))
        finally:
            self._release()

def bench_insert_cancel(int n=100000, int levels=200):
    """
    Time n inserts then n cancels entirely inside nogil C code.
    `levels` = how many distinct price levels per side the orders are spread over.
    Returns (nanoseconds per insert, nanoseconds per cancel).
    """
    cdef Book b
    cdef Order o
    cdef int i
    cdef uint64_t seed = 88172645463325252ULL
    cdef int rc = 0
    if book_init(&b, 100) != BOOK_OK:
        raise MemoryError()
    t0 = time.perf_counter_ns()
    with nogil:
        for i in range(n):
            seed ^= seed << 13
            seed ^= seed >> 7
            seed ^= seed << 17
            o.order_id = i + 1
            o.timestamp_ns = i
            o.quantity = 100
            if i & 1:
                o.side = SIDE_BUY
                o.price = 50000.0 - (seed % levels) * 0.01   # bids just below 50000.00
            else:
                o.side = SIDE_SELL
                o.price = 50001.0 + (seed % levels) * 0.01   # asks from 50001.00 up
            rc |= book_insert(&b, &o)
    t1 = time.perf_counter_ns()
    with nogil:
        for i in range(n):
            rc |= book_cancel(&b, i + 1)
    t2 = time.perf_counter_ns()
    book_destroy(&b)
    if rc != 0:
        raise RuntimeError("benchmark operation failed")
    return ((t1 - t0) / n, (t2 - t1) / n)


def bench_match(int n=100000):
    """
    Rest n sell orders, then time n buy orders that each trade completely with
    one of them, entirely inside nogil C code. Returns nanoseconds per matching submit.
    """
    cdef Book b
    cdef FillBuffer fills
    cdef Order o
    cdef int i
    cdef int rc = 0
    if book_init(&b, 100) != BOOK_OK:
        raise MemoryError()
    fillbuf_init(&fills)
    o.timestamp_ns = 0
    o.quantity = 10
    o.side = SIDE_SELL
    for i in range(n):
        o.order_id = i + 1
        o.price = 100.0 + (i % 100) * 0.01
        rc |= book_insert(&b, &o)
    o.side = SIDE_BUY
    o.price = 200.0
    t0 = time.perf_counter_ns()
    with nogil:
        for i in range(n):
            o.order_id = n + i + 1
            fillbuf_clear(&fills)
            rc |= book_submit(&b, &o, &fills)
            if fills.count != 1:
                rc |= 1
    t1 = time.perf_counter_ns()
    fillbuf_free(&fills)
    book_destroy(&b)
    if rc != 0:
        raise RuntimeError("benchmark operation failed")
    return (t1 - t0) / n


def gil_check_demo():
    """(held_outside, held_inside): PyGILState_Check() before and inside a `with nogil:` block -> (1, 0)."""
    cdef int outside = PyGILState_Check()
    cdef int inside
    with nogil:
        inside = PyGILState_Check()
    return (outside, inside)
