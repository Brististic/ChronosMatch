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
"""
import time
from libc.stdint cimport uint8_t, uint64_t
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
    BOOK_ERR_NOMEM, BOOK_ERR_NOT_FOUND,
    book_init, book_destroy, book_insert, book_cancel, book_check,
    side_best, side_find_level, idmap_find,
)

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
        level_init(&self._lvl, price)

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
    Python-visible wrapper around the C limit order book (insert / cancel / queries).
    Test scaffolding: the engine itself calls the nogil C functions directly.
    """
    cdef Book _book

    def __cinit__(self):
        if book_init(&self._book) != BOOK_OK:
            raise MemoryError()

    def __dealloc__(self):
        book_destroy(&self._book)

    def __len__(self):
        return self._book.ids.used

    def __contains__(self, unsigned long long order_id):
        return idmap_find(&self._book.ids, order_id) != NULL

    cdef int _insert(self, Order* order) except -1:
        cdef int rc = book_insert(&self._book, order)
        if rc == BOOK_OK:
            return 0
        if rc == BOOK_ERR_DUPLICATE:
            raise ValueError("duplicate order id")
        if rc == BOOK_ERR_INVALID:
            raise ValueError("invalid order (side must be b'B'/b'S', quantity > 0, 0 < price < 1e15)")
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
        self._insert(&order)

    def insert_wire(self, const unsigned char[::1] data):
        """Insert one 29-byte wire message (struct format "=QcdIQ") straight from bytes."""
        cdef WireOrder wire
        cdef Order order
        if data.shape[0] < <Py_ssize_t> sizeof(WireOrder):
            raise ValueError("buffer shorter than one wire order (29 bytes)")
        memcpy(&wire, &data[0], sizeof(WireOrder))
        order_from_wire(&order, &wire)
        self._insert(&order)

    def cancel(self, unsigned long long order_id):
        """Cancel a resting order. Returns True if it was on the book, else False."""
        return book_cancel(&self._book, order_id) == BOOK_OK

    def best_bid(self):
        """(price, total_quantity) of the best bid level, or None."""
        cdef PriceLevel* lvl = side_best(&self._book.bids)
        if lvl == NULL:
            return None
        return (lvl.price, lvl.total_quantity)

    def best_ask(self):
        """(price, total_quantity) of the best ask level, or None."""
        cdef PriceLevel* lvl = side_best(&self._book.asks)
        if lvl == NULL:
            return None
        return (lvl.price, lvl.total_quantity)

    def spread(self):
        """best ask - best bid, or None when one side is empty."""
        cdef PriceLevel* bid = side_best(&self._book.bids)
        cdef PriceLevel* ask = side_best(&self._book.asks)
        if bid == NULL or ask == NULL:
            return None
        return ask.price - bid.price

    def level_count(self, bytes side):
        return _side_ptr(&self._book, side).count

    def depth(self, bytes side, unsigned int levels=5):
        """Top `levels` price levels of a side, best first: [(price, total_qty, order_count)]."""
        cdef BookSide* s = _side_ptr(&self._book, side)
        cdef unsigned int i
        out = []
        for i in range(s.count):
            if i >= levels:
                break
            out.append((s.levels[i].price, s.levels[i].total_quantity, s.levels[i].order_count))
        return out

    def level_orders(self, bytes side, double price):
        """[(order_id, quantity), ...] at one price in time priority order ([] if no level)."""
        cdef PriceLevel* lvl = side_find_level(_side_ptr(&self._book, side), price)
        cdef OrderNode* node
        out = []
        if lvl == NULL:
            return out
        node = lvl.head
        while node != NULL:
            out.append((node.order.order_id, node.order.quantity))
            node = node.next
        return out

    def check_integrity(self):
        """Verify sorting, links, totals and the id table against each other."""
        return bool(book_check(&self._book))


def bench_insert_cancel(int n=100000):
    """
    Time n inserts then n cancels entirely inside nogil C code.
    Returns (nanoseconds per insert, nanoseconds per cancel).
    """
    cdef Book b
    cdef Order o
    cdef int i
    cdef uint64_t seed = 88172645463325252ULL
    cdef int rc = 0
    if book_init(&b) != BOOK_OK:
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
                o.price = 100.0 - (seed % 200) * 0.01      # bids 98.01 .. 100.00
            else:
                o.side = SIDE_SELL
                o.price = 101.0 + (seed % 200) * 0.01      # asks 101.00 .. 102.99
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
