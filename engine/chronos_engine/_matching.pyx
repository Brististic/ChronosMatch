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
Day 12: order nodes and price levels come from pre-allocated memory pools (pool.pxd) instead of
        malloc/free; OrderBook(order_capacity=..., level_capacity=..., grow=...) sizes them.
Day 9: RingConsumer reads orders straight out of the memory-mapped ring buffer (Member 1's
       layout) into the matching loop - no Python bytes objects, no per-order Python call.
Day 8: price levels are found through a contiguous table indexed by integer price ticks
       (O(1) lookup instead of binary search); prices are exact integer ticks (price_scale).
Day 7: partial execution with remainder persistence; every fill carries a trade id and the
       quantity left on both orders (submit_traced), so a ledger can be rebuilt from fills alone.
Day 6: the matching runs in `with nogil:` blocks (GIL released); submit_batch() feeds many
       orders per nogil section; a per-book mutex keeps shared use from threads safe.
"""
import mmap
import os
import sys
import time
from libc.stdint cimport int64_t, uint8_t, uint32_t, uint64_t
from libc.string cimport memcpy, memset
from libc.stdlib cimport malloc, free, qsort
from cpython.buffer cimport PyObject_GetBuffer, PyBuffer_Release, PyBUF_WRITABLE

from chronos_engine.order_types cimport (
    Order, OrderNode, WireOrder, SIDE_BUY, SIDE_SELL,
    side_is_valid, order_from_wire, node_alloc, node_free, node_link,
)
from chronos_engine.price_level cimport (
    PriceLevel, level_init, level_is_empty, level_peek_front, level_push_back,
    level_remove, level_pop_front, price_better,
)
from chronos_engine.pool cimport Pool
from chronos_engine.order_book cimport (
    Book, BookSide, BOOK_OK, BOOK_ERR_DUPLICATE, BOOK_ERR_INVALID,
    BOOK_ERR_NOMEM, BOOK_ERR_NOT_FOUND, BOOK_ERR_RANGE, BOOK_ERR_FULL,
    book_init, book_destroy, book_insert, book_cancel, book_check,
    side_best, side_next, side_find_tick, price_to_tick, tick_crosses, idmap_find, IdEntry, order_is_valid,
)
from chronos_engine.matching cimport (
    Fill, FillBuffer, SubmitSummary, fillbuf_init, fillbuf_free, fillbuf_clear,
    book_submit, book_submit_ex, fillbuf_prealloc,
)

cdef extern from *:
    """
    #include <chrono>
    static inline unsigned long long cm_now_ns() {
        return (unsigned long long) std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    """
    unsigned long long cm_now_ns() noexcept nogil


cdef int _cmp_u64(const void* a, const void* b) noexcept nogil:
    cdef unsigned long long x = (<const unsigned long long*> a)[0]
    cdef unsigned long long y = (<const unsigned long long*> b)[0]
    return -1 if x < y else (1 if x > y else 0)


cdef extern from "<mutex>" namespace "std" nogil:
    cdef cppclass mutex:
        mutex() noexcept
        void lock() noexcept
        void unlock() noexcept


cdef extern from "Python.h":
    int PyGILState_Check() nogil     # 1 if this thread holds the GIL, else 0 (safe to call without it)


cdef extern from *:
    """
    #include <atomic>
    #include <cstdint>
    static inline uint32_t cm_load_acquire(const unsigned char* p) {
        return reinterpret_cast<const std::atomic<uint32_t>*>(p)->load(std::memory_order_acquire);
    }
    static inline void cm_store_release(unsigned char* p, uint32_t v) {
        reinterpret_cast<std::atomic<uint32_t>*>(p)->store(v, std::memory_order_release);
    }
    static inline int cm_cas(unsigned char* p, uint32_t expected, uint32_t desired) {
        return reinterpret_cast<std::atomic<uint32_t>*>(p)->compare_exchange_strong(
            expected, desired, std::memory_order_acq_rel, std::memory_order_acquire) ? 1 : 0;
    }
    """
    uint32_t cm_load_acquire(const unsigned char* p) noexcept nogil
    void cm_store_release(unsigned char* p, uint32_t v) noexcept nogil
    int cm_cas(unsigned char* p, uint32_t expected, uint32_t desired) noexcept nogil


cdef extern from *:
    """
    #ifdef _WIN32
    #include <process.h>
    static inline unsigned int cm_getpid() { return (unsigned int) _getpid(); }
    #else
    #include <unistd.h>
    static inline unsigned int cm_getpid() { return (unsigned int) getpid(); }
    #endif
    """
    uint32_t cm_getpid() noexcept nogil


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

    def __cinit__(self, long long price_scale=100, unsigned long long order_capacity=1024,
                  unsigned long long level_capacity=128, bint grow=True):
        if price_scale < 1 or price_scale > 1000000:
            raise ValueError("price_scale must be between 1 and 1,000,000 (ticks per 1.00)")
        if order_capacity < 1 or order_capacity > 200000000:
            raise ValueError("order_capacity must be between 1 and 200,000,000")
        if level_capacity < 1 or level_capacity > 50000000:
            raise ValueError("level_capacity must be between 1 and 50,000,000")
        self._gil_probe = -1
        if book_init(&self._book, price_scale, <uint32_t> order_capacity, <uint32_t> level_capacity, grow) != BOOK_OK:
            raise MemoryError("could not reserve the order and price-level pools")

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

    def pool_stats(self):
        """
        State of the two memory pools:
          {"orders": {...}, "levels": {...}, "grow": bool} where each pool reports
          capacity (elements reserved), in_use, peak (highest in_use so far), slabs (blocks obtained
          from the system) and system_allocations (malloc/realloc calls the pool has made in total).
        Once the pools are big enough, system_allocations stops changing: that is the proof that
        insert, cancel and matching no longer call the system allocator.
        """
        self._acquire()
        try:
            return {"orders": {"capacity": self._book.nodes.capacity, "in_use": self._book.nodes.in_use,
                               "peak": self._book.nodes.peak, "slabs": self._book.nodes.slab_count,
                               "system_allocations": self._book.nodes.system_allocs,
                               "element_bytes": self._book.nodes.elem_size},
                    "levels": {"capacity": self._book.levels.capacity, "in_use": self._book.levels.in_use,
                               "peak": self._book.levels.peak, "slabs": self._book.levels.slab_count,
                               "system_allocations": self._book.levels.system_allocs,
                               "element_bytes": self._book.levels.elem_size},
                    "grow": bool(self._book.nodes.grow)}
        finally:
            self._release()

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
        if rc == BOOK_ERR_FULL:
            raise MemoryError("the order or price-level pool is full (grow=False)")
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
        if rc == BOOK_ERR_FULL:
            raise MemoryError("the order or price-level pool is full (grow=False)")
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

    def submit_to_outbound_ring(self, unsigned long long order_id, bytes side,
                                double price, unsigned int quantity,
                                unsigned long long timestamp_ns,
                                unsigned char[::1] ring):
        """Match one order and publish complete 53-byte audit fills to a writable
        Member-1-style mmap ring (24-byte <IIIIQ header, slot_size=53).

        Wire fill format: <QQQdIIIQB = trade_id, taker_id, maker_id, price,
        quantity, taker_remaining, maker_remaining, timestamp_ns, taker_side.
        The ring must be single-producer/single-consumer and the consumer must
        only advance tail after copying a complete slot. If the ring lacks room
        for every potential fill, BufferError is raised BEFORE the book changes.
        This method creates no Python fill tuples/lists; fills are staged in a
        preallocated C array and copied into mmap slots before head is published.
        """
        cdef Order order
        cdef FillBuffer fills
        cdef SubmitSummary summary
        cdef BookSide* opposite
        cdef PriceLevel* lvl
        cdef OrderNode* node
        cdef Fill* f
        cdef unsigned char* raw = &ring[0]
        cdef unsigned char* raw_slot
        cdef uint32_t head, tail, cap, slot_size, drops
        cdef uint32_t occupied, free_slots, needed = 0
        cdef uint32_t pre_remaining, pre_traded
        cdef uint32_t new_head
        cdef uint32_t i
        cdef Py_ssize_t expected_size
        cdef int64_t order_tick
        cdef int rc = BOOK_OK
        cdef int gil_state = -1
        if len(side) != 1:
            raise ValueError("side must be b'B' or b'S'")
        if ring.shape[0] < 24:
            raise ValueError("outbound ring buffer is shorter than its 24-byte header")
        order.order_id = order_id
        order.timestamp_ns = timestamp_ns
        order.price = price
        order.quantity = quantity
        order.side = side[0]

        # Member 1 header: little-endian <IIIIQ at byte offsets 0,4,8,12,16.
        memcpy(&head, raw + 0, sizeof(uint32_t))
        memcpy(&tail, raw + 4, sizeof(uint32_t))
        memcpy(&cap, raw + 8, sizeof(uint32_t))
        memcpy(&slot_size, raw + 12, sizeof(uint32_t))
        memcpy(&drops, raw + 16, sizeof(uint32_t))
        if cap < 2 or slot_size != 53:
            raise ValueError("outbound ring must have capacity >= 2 and slot_size=53")
        expected_size = 24 + <Py_ssize_t>cap * slot_size
        if ring.shape[0] < expected_size:
            raise ValueError("outbound ring mapping is smaller than header capacity requires")
        if head >= cap or tail >= cap:
            raise ValueError("outbound ring header has invalid head/tail")

        fillbuf_init(&fills)
        self._acquire()
        try:
            # Count all makers the order would cross while book is locked. This
            # makes ring-full rejection atomic with respect to book mutation.
            if order_is_valid(&order) and price_to_tick(&self._book, price, &order_tick) and \
                    idmap_find(&self._book.ids, order_id) == NULL:
                opposite = &self._book.asks if order.side == SIDE_BUY else &self._book.bids
                pre_remaining = order.quantity
                lvl = side_best(opposite)
                while pre_remaining > 0 and lvl != NULL and tick_crosses(order.side, order_tick, lvl.tick):
                    node = lvl.head
                    while pre_remaining > 0 and node != NULL:
                        pre_traded = pre_remaining if pre_remaining < node.order.quantity else node.order.quantity
                        pre_remaining -= pre_traded
                        needed += 1
                        node = node.next
                    lvl = side_next(opposite, lvl)

            if head >= tail:
                occupied = head - tail
            else:
                occupied = cap - (tail - head)
            free_slots = (cap - 1) - occupied
            if needed > free_slots:
                raise BufferError("outbound fill ring has insufficient free slots; order not submitted")
            if needed > 0 and fillbuf_prealloc(&fills, needed) != BOOK_OK:
                raise MemoryError("could not preallocate C fill staging buffer")

            with nogil:
                gil_state = PyGILState_Check()
                rc = book_submit_ex(&self._book, &order, &fills, &summary)
            self._gil_probe = gil_state

            # Serialize directly into mapped ring slots. No Python bytes/tuple
            # is constructed for an individual fill.
            new_head = head
            for i in range(fills.count):
                f = &fills.items[i]
                raw_slot = raw + 24 + <Py_ssize_t>new_head * slot_size
                memcpy(raw_slot + 0, &f.trade_id, 8)
                memcpy(raw_slot + 8, &f.taker_id, 8)
                memcpy(raw_slot + 16, &f.maker_id, 8)
                memcpy(raw_slot + 24, &f.price, 8)
                memcpy(raw_slot + 32, &f.quantity, 4)
                memcpy(raw_slot + 36, &f.taker_remaining, 4)
                memcpy(raw_slot + 40, &f.maker_remaining, 4)
                memcpy(raw_slot + 44, &f.timestamp_ns, 8)
                raw_slot[52] = f.taker_side
                new_head = (new_head + 1) % cap
            # Publish head only after every slot payload has been copied.
            memcpy(raw + 0, &new_head, sizeof(uint32_t))
        finally:
            self._release()
            fillbuf_free(&fills)
        self._raise_for(rc)
        return {"fill_count": summary.fill_count,
                "filled_quantity": summary.filled_quantity,
                "resting_quantity": summary.resting_quantity,
                "head": new_head}

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
                "resting_quantity": summary.resting_quantity,
                "levels_swept": summary.levels_swept,
                "sweep_detected": summary.levels_swept >= 2, "fills": out}

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
    if book_init(&b, 100, <uint32_t> (n if n > 1024 else 1024), <uint32_t> (2 * levels + 16), True) != BOOK_OK:
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
    if book_init(&b, 100, <uint32_t> (n + 1024), 256, True) != BOOK_OK:
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


# ----------------------------------------------------------------- Day 9 ----
# Ring buffer file layout written by Member 1's MmapRingBuffer (src/ring_buffer.py):
#   header  struct "<IIIIQ" = 24 bytes, no padding:
#       head u32 @0 (producer), tail u32 @4 (consumer), capacity u32 @8, slot_size u32 @12, drops u64 @16
#   slots   capacity x 29 bytes from offset 24; slot i starts at 24 + i * 29; one slot is always
#           kept free, so the ring is empty when head == tail
# Lock file written by MmapProcessLock (src/ipc_sync.py): "<II" = flag u32 @0, owner pid u32 @4.
cdef enum:
    RING_HEADER_SIZE = 24
    RING_OFF_HEAD = 0
    RING_OFF_TAIL = 4
    RING_OFF_CAPACITY = 8
    RING_OFF_SLOT_SIZE = 12
    RING_OFF_DROPS = 16
    RING_LOCK_SIZE = 8
    WIRE_SIDE_CANCEL = 67        # ord('C'): cancel message, order_id = the order to cancel


def _pid_alive(int pid):
    """True if a process with this id exists (used to detect a stale lock left by a dead process)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if handle == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True                  # exists, but belongs to another user
    except OSError:
        return False


cdef int _ring_take(const unsigned char* ring, uint32_t capacity, unsigned char* staging,
                    uint32_t limit, uint32_t* taken, unsigned long long* drops,
                    uint32_t* left) noexcept nogil:
    # Copy up to `limit` slots out of the ring into `staging` and advance the tail.
    # Only the TAIL field is ever written: head, capacity, slot size and drops belong to the producer.
    # Returns 0 on success, 1 if the header is corrupt.
    cdef uint32_t head = cm_load_acquire(ring + RING_OFF_HEAD)      # acquire: slot bytes written before head are visible
    cdef uint32_t tail = cm_load_acquire(ring + RING_OFF_TAIL)
    cdef uint32_t available
    cdef uint32_t n
    cdef uint32_t first
    cdef uint32_t slot = sizeof(WireOrder)
    taken[0] = 0
    left[0] = 0
    memcpy(drops, ring + RING_OFF_DROPS, sizeof(unsigned long long))
    if head >= capacity or tail >= capacity:
        return 1
    available = head - tail if head >= tail else capacity - (tail - head)
    n = available if available < limit else limit
    if n == 0:
        return 0
    first = n if n < capacity - tail else capacity - tail            # slots before the wrap-around
    memcpy(staging, ring + RING_HEADER_SIZE + <size_t> tail * slot, <size_t> first * slot)
    if n > first:
        memcpy(staging + <size_t> first * slot, ring + RING_HEADER_SIZE, <size_t> (n - first) * slot)
    cm_store_release(<unsigned char*> ring + RING_OFF_TAIL, (tail + n) % capacity)   # release: consumed before slot is freed
    taken[0] = n
    left[0] = available - n
    return 0


cdef struct DrainStats:
    uint32_t messages           # slots taken out of the ring by this drain
    uint32_t left               # slots still waiting afterwards
    unsigned long long submitted
    unsigned long long cancels
    unsigned long long cancels_missed
    unsigned long long rejected
    unsigned long long fill_count
    unsigned long long filled_quantity
    unsigned long long lat_min
    unsigned long long lat_max
    unsigned long long lat_total
    unsigned long long drops    # the producer's overrun counter, as read from the header


cdef class RingConsumer:
    """
    Reads orders directly out of a memory-mapped ring buffer file and matches them in an
    OrderBook, with no Python object created per order.

      consumer = RingConsumer(ring_path, lock_path)
      summary = consumer.drain_into(book)          # returns a dict of counters
      n = consumer.drain(book)                     # lean: returns only the message count

    One drain, all in ONE section without the GIL (Day 11): take the ring's lock (the same lock file
    Member 1's code uses; one atomic compare-and-swap when it is free), copy the waiting 29-byte slots
    into a preallocated C buffer, advance the tail and release the lock, then decode every slot and
    submit it (side b'B' / b'S') or cancel it (side b'C') under the book's mutex.
    Producers are only blocked for the short copy.

    track_latency=True (default) reads time.perf_counter_ns() once per drain so the summary can report
    how long orders waited; track_latency=False skips that one Python call for the lowest overhead.
    """
    cdef object _ring_mm
    cdef object _lock_mm
    cdef object _ring_fd
    cdef object _lock_fd
    cdef Py_buffer _ring_buf
    cdef Py_buffer _lock_buf
    cdef bint _ring_held
    cdef bint _lock_held
    cdef unsigned char* _ring
    cdef unsigned char* _lockp
    cdef unsigned char* _staging
    cdef uint32_t _capacity
    cdef uint32_t _max_batch
    cdef double _spin_timeout
    cdef bint _track_latency
    cdef int _gil_probe
    cdef unsigned long long _consumed_total
    cdef FillBuffer _fills            # reused by every drain (cleared, never reallocated unless it must grow)
    cdef DrainStats _tot              # running totals for drain()/totals()
    cdef unsigned long long _tot_drains

    def __cinit__(self, ring_path, lock_path=None, unsigned int max_batch=4096,
                  double spin_timeout_sec=1.0, bint track_latency=True):
        cdef uint32_t slot_size
        self._ring_held = False
        self._lock_held = False
        self._ring = NULL
        self._lockp = NULL
        self._staging = NULL
        self._gil_probe = -1
        self._consumed_total = 0
        self._tot_drains = 0
        self._track_latency = track_latency
        memset(&self._tot, 0, sizeof(DrainStats))
        fillbuf_init(&self._fills)
        if sys.byteorder != "little":
            raise RuntimeError("RingConsumer needs a little-endian machine (the ring format is little-endian)")
        if max_batch < 1:
            raise ValueError("max_batch must be at least 1")
        if not os.path.exists(ring_path):
            raise FileNotFoundError("ring buffer file '%s' not found" % ring_path)
        self._max_batch = max_batch
        self._spin_timeout = spin_timeout_sec
        self._ring_fd = os.open(ring_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self._ring_mm = mmap.mmap(self._ring_fd, 0, access=mmap.ACCESS_WRITE)
        if PyObject_GetBuffer(self._ring_mm, &self._ring_buf, PyBUF_WRITABLE) != 0:
            raise ValueError("cannot export the ring mapping as a buffer")
        self._ring_held = True
        self._ring = <unsigned char*> self._ring_buf.buf
        if self._ring_buf.len < RING_HEADER_SIZE:
            raise ValueError("ring buffer file is too small to hold a header")
        self._capacity = cm_load_acquire(self._ring + RING_OFF_CAPACITY)
        slot_size = cm_load_acquire(self._ring + RING_OFF_SLOT_SIZE)
        if slot_size != sizeof(WireOrder):
            raise ValueError("ring slot size is %d bytes, the engine expects %d" % (slot_size, sizeof(WireOrder)))
        if self._capacity < 2 or self._ring_buf.len < RING_HEADER_SIZE + <Py_ssize_t> self._capacity * slot_size:
            raise ValueError("ring header capacity does not fit the file size")
        if lock_path is not None:
            if not os.path.exists(lock_path):
                raise FileNotFoundError("lock file '%s' not found" % lock_path)
            self._lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            self._lock_mm = mmap.mmap(self._lock_fd, RING_LOCK_SIZE, access=mmap.ACCESS_WRITE)
            if PyObject_GetBuffer(self._lock_mm, &self._lock_buf, PyBUF_WRITABLE) != 0:
                raise ValueError("cannot export the lock mapping as a buffer")
            self._lock_held = True
            self._lockp = <unsigned char*> self._lock_buf.buf
        self._staging = <unsigned char*> malloc(<size_t> max_batch * sizeof(WireOrder))
        if self._staging == NULL:
            raise MemoryError()
        if fillbuf_prealloc(&self._fills, max_batch) != BOOK_OK:        # no realloc during ordinary batches
            raise MemoryError()

    def __dealloc__(self):
        free(self._staging)
        self._staging = NULL
        fillbuf_free(&self._fills)
        if self._ring_held:
            PyBuffer_Release(&self._ring_buf)
            self._ring_held = False
        if self._lock_held:
            PyBuffer_Release(&self._lock_buf)
            self._lock_held = False

    def close(self):
        """Release the mappings and files (safe to call twice)."""
        self._ring = NULL
        self._lockp = NULL
        if self._ring_held:
            PyBuffer_Release(&self._ring_buf)
            self._ring_held = False
        if self._lock_held:
            PyBuffer_Release(&self._lock_buf)
            self._lock_held = False
        if self._ring_mm is not None:
            self._ring_mm.close()
        if self._lock_mm is not None:
            self._lock_mm.close()
        if self._ring_fd is not None:
            os.close(self._ring_fd)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
        self._ring_mm = None
        self._lock_mm = None
        self._ring_fd = None
        self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    def capacity(self):
        return self._capacity

    @property
    def slot_size(self):
        return sizeof(WireOrder)

    @property
    def max_batch(self):
        return self._max_batch

    @property
    def consumed_total(self):
        """Messages this consumer has taken out of the ring so far."""
        return self._consumed_total

    @property
    def gil_held_in_last_drain(self):
        """1 if the GIL was held while the last drain matched, 0 if released, -1 if none yet."""
        return self._gil_probe

    cdef int _check_open(self) except -1:
        if self._ring == NULL:
            raise ValueError("RingConsumer is closed")
        return 0

    def pending(self):
        """Number of messages waiting in the ring right now (not locked: a snapshot)."""
        cdef uint32_t head
        cdef uint32_t tail
        self._check_open()
        head = cm_load_acquire(self._ring + RING_OFF_HEAD)
        tail = cm_load_acquire(self._ring + RING_OFF_TAIL)
        if head >= self._capacity or tail >= self._capacity:
            raise ValueError("ring header is corrupt (head/tail outside the capacity)")
        return head - tail if head >= tail else self._capacity - (tail - head)

    def producer_drops(self):
        """Overrun counter kept by the producer: messages it had to drop because the ring was full."""
        cdef unsigned long long drops
        self._check_open()
        memcpy(&drops, self._ring + RING_OFF_DROPS, sizeof(unsigned long long))
        return drops

    cdef bint _try_lock(self) noexcept nogil:
        # Fast path, pure C: atomic compare-and-swap of the flag 0 -> 1, then record our pid.
        if cm_cas(self._lockp, 0, 1):
            cm_store_release(self._lockp + 4, cm_getpid())
            return True
        return False

    cdef int _acquire_lock_slow(self) except -1:
        # Slow path (the lock is busy): same protocol as MmapProcessLock, with a time-out and
        # take-over of a lock left behind by a process that no longer exists.
        cdef uint32_t owner
        deadline = time.perf_counter() + self._spin_timeout
        while True:
            if self._try_lock():
                return 0
            owner = cm_load_acquire(self._lockp + 4)
            if owner != 0 and not _pid_alive(<int> owner):
                cm_store_release(self._lockp + 4, cm_getpid())     # stale lock of a dead process: take over
                return 0
            if time.perf_counter() >= deadline:
                raise TimeoutError("could not acquire the ring lock within %.3f s" % self._spin_timeout)
            time.sleep(0.0001)

    cdef int _drain_core(self, OrderBook book, uint32_t limit, bint collect, DrainStats* st) except -1:
        # One complete drain. No Python object is created, no Python function is called (except the
        # optional once-per-drain clock read and the slow lock path). The ring lock is released as soon
        # as the slots are copied; matching then runs under the book's mutex.
        cdef int rc = 0
        cdef int gil_state = -1
        cdef int out_of_memory = 0
        cdef uint32_t i
        cdef uint32_t j
        cdef unsigned long long now_ns = 0
        cdef unsigned long long lat
        cdef WireOrder wire
        cdef Order order
        cdef const unsigned char* staging = self._staging
        cdef unsigned char* ring = self._ring
        cdef uint32_t capacity = self._capacity
        cdef unsigned char* lockp = self._lockp
        cdef uint32_t head
        cdef uint32_t tail
        cdef FillBuffer* fills = &self._fills
        cdef bint track = self._track_latency
        memset(st, 0, sizeof(DrainStats))
        self._check_open()
        # Empty ring: look at head and tail WITHOUT the lock and return at once. A polling loop on a quiet
        # market then never touches the lock, so it cannot get in the producer's way. (A corrupt header is
        # not treated as empty; it goes on to the normal path, which reports it.)
        fillbuf_clear(fills)                                # fills of the PREVIOUS drain must never be reported again
        head = cm_load_acquire(ring + RING_OFF_HEAD)
        tail = cm_load_acquire(ring + RING_OFF_TAIL)
        if head == tail and head < capacity:
            memcpy(&st.drops, ring + RING_OFF_DROPS, sizeof(unsigned long long))
            return 0
        if track:
            now_ns = time.perf_counter_ns()
        if lockp != NULL and not self._try_lock():
            self._acquire_lock_slow()
        with nogil:
            gil_state = PyGILState_Check()                  # once per drain: proves this section runs without the GIL
            rc = _ring_take(ring, capacity, self._staging, limit, &st.messages, &st.drops, &st.left)
            if lockp != NULL:                               # release the ring lock before matching
                cm_store_release(lockp + 4, 0)
                cm_store_release(lockp, 0)
            if rc == 0 and st.messages > 0:
                book._lock.lock()
                for i in range(st.messages):
                    memcpy(&wire, staging + <size_t> i * sizeof(WireOrder), sizeof(WireOrder))
                    if track:
                        lat = now_ns - wire.timestamp_ns if now_ns >= wire.timestamp_ns else 0
                        if i == 0 or lat < st.lat_min:
                            st.lat_min = lat
                        if lat > st.lat_max:
                            st.lat_max = lat
                        st.lat_total += lat
                    if wire.side == WIRE_SIDE_CANCEL:
                        if book_cancel(&book._book, wire.order_id) == BOOK_OK:
                            st.cancels += 1
                        else:
                            st.cancels_missed += 1
                    else:
                        order_from_wire(&order, &wire)
                        rc = book_submit(&book._book, &order, fills)
                        if rc == BOOK_ERR_NOMEM:
                            out_of_memory = 1
                            break
                        if rc == BOOK_OK:
                            st.submitted += 1
                        else:
                            st.rejected += 1
                        if not collect:
                            for j in range(fills.count):
                                st.filled_quantity += fills.items[j].quantity
                            st.fill_count += fills.count
                            fillbuf_clear(fills)
                if collect:
                    for j in range(fills.count):
                        st.filled_quantity += fills.items[j].quantity
                    st.fill_count = fills.count
                book._lock.unlock()
        self._gil_probe = gil_state
        if rc == 1 and st.messages == 0 and st.left == 0 and not out_of_memory:
            # _ring_take reports a corrupt header with rc == 1 (nothing was taken)
            raise ValueError("ring header is corrupt (head/tail outside the capacity)")
        if out_of_memory:
            raise MemoryError()
        self._consumed_total += st.messages
        return 0

    def drain_into(self, OrderBook book, unsigned int max_orders=0, bint collect=False):
        """
        Take up to max_orders messages (0 = the consumer's max_batch) out of the ring and feed them
        to `book`. Returns a dict:
          messages, submitted, cancels, cancels_missed, rejected, fill_count, filled_quantity,
          fills (list of (taker_id, maker_id, price, quantity) when collect=True, else None),
          ring_left, ring_drops, latency_min_ns / latency_max_ns / latency_total_ns
          (time from the order's own timestamp_ns to the start of this drain; same clock as
          time.perf_counter_ns(); all 0 when the consumer was made with track_latency=False).
        A cancel (side b'C') for an order that is not on the book counts as cancels_missed (normal when
        the order has already been filled). Invalid or duplicate orders are counted as rejected and skipped.
        Raises TimeoutError if the ring lock cannot be taken, ValueError if the ring header is corrupt.
        """
        cdef uint32_t limit = self._max_batch if (max_orders == 0 or max_orders > self._max_batch) else max_orders
        cdef DrainStats st
        cdef uint32_t j
        self._drain_core(book, limit, collect, &st)
        out = None
        if collect:
            out = []
            for j in range(self._fills.count):
                out.append((self._fills.items[j].taker_id, self._fills.items[j].maker_id,
                            self._fills.items[j].price, self._fills.items[j].quantity))
        return {"messages": st.messages, "submitted": st.submitted, "cancels": st.cancels,
                "cancels_missed": st.cancels_missed, "rejected": st.rejected,
                "fill_count": st.fill_count, "filled_quantity": st.filled_quantity, "fills": out,
                "ring_left": st.left, "ring_drops": st.drops,
                "latency_min_ns": st.lat_min, "latency_max_ns": st.lat_max, "latency_total_ns": st.lat_total}

    def drain(self, OrderBook book, unsigned int max_orders=0):
        """
        Lean version of drain_into(): takes up to max_orders messages out of the ring, matches them,
        adds the counters to the running totals (see totals()) and returns only the number of messages
        taken. No dict and no list is built, so a polling loop pays almost nothing per call.
        """
        cdef uint32_t limit = self._max_batch if (max_orders == 0 or max_orders > self._max_batch) else max_orders
        cdef DrainStats st
        self._drain_core(book, limit, False, &st)
        if st.messages > 0:
            self._tot_drains += 1
            self._tot.messages += st.messages
            self._tot.submitted += st.submitted
            self._tot.cancels += st.cancels
            self._tot.cancels_missed += st.cancels_missed
            self._tot.rejected += st.rejected
            self._tot.fill_count += st.fill_count
            self._tot.filled_quantity += st.filled_quantity
            if self._tot.lat_total == 0 and self._tot.lat_max == 0 or st.lat_min < self._tot.lat_min:
                self._tot.lat_min = st.lat_min
            if st.lat_max > self._tot.lat_max:
                self._tot.lat_max = st.lat_max
            self._tot.lat_total += st.lat_total
        self._tot.left = st.left
        self._tot.drops = st.drops
        return st.messages

    def totals(self):
        """Running totals of everything taken by drain() (drain_into() is not added to them)."""
        return {"drains": self._tot_drains, "messages": self._tot.messages, "submitted": self._tot.submitted,
                "cancels": self._tot.cancels, "cancels_missed": self._tot.cancels_missed,
                "rejected": self._tot.rejected, "fill_count": self._tot.fill_count,
                "filled_quantity": self._tot.filled_quantity, "ring_left": self._tot.left,
                "ring_drops": self._tot.drops, "latency_min_ns": self._tot.lat_min,
                "latency_max_ns": self._tot.lat_max, "latency_total_ns": self._tot.lat_total}

    def drain_all(self, OrderBook book):
        """Keep draining until the ring is empty. Returns the summed counters (no fill list)."""
        cdef dict total = {"messages": 0, "submitted": 0, "cancels": 0, "cancels_missed": 0,
                           "rejected": 0, "fill_count": 0, "filled_quantity": 0, "drains": 0}
        while True:
            r = self.drain_into(book)
            if r["messages"] == 0:
                break
            total["drains"] += 1
            for key in ("messages", "submitted", "cancels", "cancels_missed", "rejected",
                        "fill_count", "filled_quantity"):
                total[key] += r[key]
            total["ring_drops"] = r["ring_drops"]
        return total


def gil_check_demo():
    """(held_outside, held_inside): PyGILState_Check() before and inside a `with nogil:` block -> (1, 0)."""
    cdef int outside = PyGILState_Check()
    cdef int inside
    with nogil:
        inside = PyGILState_Check()
    return (outside, inside)


def bench_latency(int n=200000, int levels=200):
    """
    Time EVERY insert and EVERY cancel separately inside nogil C code and report the distribution in
    nanoseconds: {"insert": {...}, "cancel": {...}} with p50, p90, p99, p99_9 and max.
    The tail (p99.9 and max) is what a memory pool improves: malloc is fast on average but sometimes slow.
    Each timing includes about 25-50 ns of clock-reading overhead (the same for every build).
    """
    cdef Book b
    cdef Order o
    cdef int i
    cdef int rc = 0
    cdef uint64_t seed = 88172645463325252ULL
    cdef unsigned long long t0
    cdef unsigned long long* ins = <unsigned long long*> malloc(<size_t> n * sizeof(unsigned long long))
    cdef unsigned long long* can = <unsigned long long*> malloc(<size_t> n * sizeof(unsigned long long))
    if ins == NULL or can == NULL:
        free(ins)
        free(can)
        raise MemoryError()
    if book_init(&b, 100, <uint32_t> (n if n > 1024 else 1024), <uint32_t> (2 * levels + 16), True) != BOOK_OK:
        free(ins)
        free(can)
        raise MemoryError()
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
                o.price = 50000.0 - (seed % levels) * 0.01
            else:
                o.side = SIDE_SELL
                o.price = 50001.0 + (seed % levels) * 0.01
            t0 = cm_now_ns()
            rc |= book_insert(&b, &o)
            ins[i] = cm_now_ns() - t0
        for i in range(n):
            t0 = cm_now_ns()
            rc |= book_cancel(&b, i + 1)
            can[i] = cm_now_ns() - t0
        qsort(ins, n, sizeof(unsigned long long), _cmp_u64)
        qsort(can, n, sizeof(unsigned long long), _cmp_u64)
    book_destroy(&b)
    result = {}
    for name, arr in (("insert", 0), ("cancel", 1)):
        a = ins if arr == 0 else can
        result[name] = {"p50": a[n // 2], "p90": a[n * 9 // 10], "p99": a[n * 99 // 100],
                        "p99_9": a[n * 999 // 1000], "max": a[n - 1]}
    free(ins)
    free(can)
    if rc != 0:
        raise RuntimeError("benchmark operation failed")
    return result
