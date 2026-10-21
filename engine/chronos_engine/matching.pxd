# cython: language_level=3
"""
Matching of crossing limit orders, with partial execution and a full audit trail.

book_submit_ex(book, order, fills, summary) is the entry point for an arriving limit order:

  1. It is validated; an id that is already RESTING on the book is rejected.
  2. While it crosses the best price on the opposite side, it trades with the
     resting orders of that level in arrival order (time priority), best price
     first (price priority).
  3. Prices are compared as INTEGER TICKS (exact, no floating-point surprises).
     Every trade executes at the RESTING order's price and is appended to `fills`
     as a Fill that records everything needed to audit it later (Day 7): a book-wide
     trade id, both order ids, price, quantity, what is LEFT of the taker and of the
     maker after this trade, and the taker's timestamp.
  4. PARTIAL EXECUTION: a resting order that is only partly used keeps its place in
     the queue with the reduced quantity; one that is fully used leaves the book.
  5. REMAINDER PERSISTENCE: whatever quantity of the arriving order is left rests on
     the book at its limit price (it then acts as a maker for later orders).
  6. `summary` (optional) reports how much traded and how much now rests.

Deterministic: same input sequence -> same fills, trade ids and book. No Python objects.
"""
from libc.stdint cimport int64_t, uint8_t, uint32_t, uint64_t
from libc.stdlib cimport realloc, free

from chronos_engine.order_types cimport Order, OrderNode, SIDE_BUY
from chronos_engine.price_level cimport PriceLevel
from chronos_engine.order_book cimport (
    Book, BookSide, BOOK_OK, BOOK_ERR_INVALID, BOOK_ERR_DUPLICATE, BOOK_ERR_NOMEM,
    idmap_find, side_best, book_insert, book_cancel, order_is_valid,
    price_to_tick, tick_crosses,
)


cdef struct Fill:
    uint64_t trade_id           # 1, 2, 3 ... in execution order, per book
    uint64_t taker_id           # the arriving (aggressive) order
    uint64_t maker_id           # the resting order it traded with
    double price                # execution price = the maker's price
    uint32_t quantity
    uint32_t taker_remaining    # taker quantity still unfilled after THIS trade
    uint32_t maker_remaining    # maker quantity still resting after THIS trade (0 = gone)
    uint64_t timestamp_ns       # the taker's timestamp
    uint8_t taker_side


cdef struct SubmitSummary:
    uint32_t filled_quantity    # total quantity the arriving order traded
    uint32_t resting_quantity   # remainder that now rests on the book (0 if none)
    uint32_t fill_count         # number of trades this order produced


cdef struct FillBuffer:         # growable array of fills (Day 13: outbound ring buffer)
    Fill* items
    uint32_t count
    uint32_t capacity


cdef inline void fillbuf_init(FillBuffer* f) noexcept nogil:
    f.items = NULL
    f.count = 0
    f.capacity = 0


cdef inline void fillbuf_free(FillBuffer* f) noexcept nogil:
    free(f.items)
    f.items = NULL
    f.count = 0
    f.capacity = 0


cdef inline void fillbuf_clear(FillBuffer* f) noexcept nogil:
    f.count = 0


cdef inline int fillbuf_prealloc(FillBuffer* f, uint32_t n) noexcept nogil:
    # Allocate room for n fills up front so that ordinary batches never reallocate (Day 12).
    cdef Fill* grown = <Fill*> realloc(f.items, n * sizeof(Fill))
    if grown == NULL:
        return BOOK_ERR_NOMEM
    f.items = grown
    f.capacity = n
    return BOOK_OK


cdef inline Fill* fillbuf_reserve(FillBuffer* f) noexcept nogil:
    # Returns a pointer to a fresh slot at the end, or NULL when out of memory.
    cdef uint32_t new_capacity
    cdef Fill* grown
    if f.count == f.capacity:
        new_capacity = 64 if f.capacity == 0 else f.capacity * 2
        grown = <Fill*> realloc(f.items, new_capacity * sizeof(Fill))
        if grown == NULL:
            return NULL
        f.items = grown
        f.capacity = new_capacity
    f.count += 1
    return &f.items[f.count - 1]


cdef inline int book_submit_ex(Book* b, const Order* order, FillBuffer* fills,
                               SubmitSummary* summary) noexcept nogil:
    cdef BookSide* opposite
    cdef PriceLevel* lvl
    cdef OrderNode* maker
    cdef Fill* fill
    cdef uint32_t remaining
    cdef uint32_t traded
    cdef uint32_t fills_before = fills.count
    cdef int64_t order_tick
    cdef Order rest
    cdef int rc
    if summary != NULL:
        summary.filled_quantity = 0
        summary.resting_quantity = 0
        summary.fill_count = 0
    if not order_is_valid(order) or not price_to_tick(b, order.price, &order_tick):
        return BOOK_ERR_INVALID                        # incl. a price that is not on the tick grid
    if idmap_find(&b.ids, order.order_id) != NULL:
        return BOOK_ERR_DUPLICATE
    opposite = &b.asks if order.side == SIDE_BUY else &b.bids
    remaining = order.quantity
    while remaining > 0:
        lvl = side_best(opposite)                      # price priority: best level first
        if lvl == NULL or not tick_crosses(order.side, order_tick, lvl.tick):   # exact integer compare
            break
        maker = lvl.head                               # time priority: oldest order first
        traded = remaining if remaining < maker.order.quantity else maker.order.quantity
        fill = fillbuf_reserve(fills)
        if fill == NULL:
            return BOOK_ERR_NOMEM                      # trades so far stay applied
        remaining -= traded
        fill.trade_id = b.next_trade_id
        b.next_trade_id += 1
        fill.taker_id = order.order_id
        fill.maker_id = maker.order.order_id
        fill.price = lvl.price
        fill.quantity = traded
        fill.taker_remaining = remaining
        fill.maker_remaining = maker.order.quantity - traded
        fill.timestamp_ns = order.timestamp_ns
        fill.taker_side = order.side
        if traded == maker.order.quantity:
            book_cancel(b, maker.order.order_id)       # fully used: leaves the book
        else:
            maker.order.quantity -= traded             # PARTIAL: keeps its place, less quantity
            lvl.total_quantity -= traded
    if summary != NULL:
        summary.filled_quantity = order.quantity - remaining
        summary.fill_count = fills.count - fills_before
    if remaining > 0:
        rest = order[0]
        rest.quantity = remaining
        rc = book_insert(b, &rest)                     # REMAINDER rests at its limit price
        if rc == BOOK_OK and summary != NULL:
            summary.resting_quantity = remaining
        return rc
    return BOOK_OK


cdef inline int book_submit(Book* b, const Order* order, FillBuffer* fills) noexcept nogil:
    return book_submit_ex(b, order, fills, NULL)
