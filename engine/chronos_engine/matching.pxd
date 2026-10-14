# cython: language_level=3
"""
Matching of crossing limit orders (Day 5).

book_submit(book, order, fills) is the entry point for an arriving limit order:

  1. It is validated; an id that is already RESTING on the book is rejected.
  2. While it crosses the best price on the opposite side, it trades with the
     resting orders of that level in arrival order (time priority), best price
     first (price priority).
  3. Every trade executes at the RESTING order's price (the taker may get a
     better price than its limit) and is appended to `fills`.
  4. A resting order that is fully used leaves the book; one that is only
     partly used keeps its place with the reduced quantity.
  5. Whatever quantity of the arriving order is left rests on the book at its limit.

Deterministic: same input sequence -> same fills and same book. No Python objects.
"""
from libc.stdint cimport uint8_t, uint32_t, uint64_t
from libc.stdlib cimport realloc, free

from chronos_engine.order_types cimport Order, OrderNode, SIDE_BUY
from chronos_engine.price_level cimport PriceLevel, price_better
from chronos_engine.order_book cimport (
    Book, BookSide, BOOK_OK, BOOK_ERR_INVALID, BOOK_ERR_DUPLICATE, BOOK_ERR_NOMEM,
    idmap_find, side_best, book_insert, book_cancel, order_is_valid,
)


cdef struct Fill:
    uint64_t taker_id           # the arriving (aggressive) order
    uint64_t maker_id           # the resting order it traded with
    double price                # execution price = the maker's price
    uint32_t quantity
    uint8_t taker_side


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


cdef inline int fillbuf_push(FillBuffer* f, uint64_t taker_id, uint64_t maker_id,
                             double price, uint32_t quantity, uint8_t taker_side) noexcept nogil:
    cdef uint32_t new_capacity
    cdef Fill* grown
    cdef Fill* slot
    if f.count == f.capacity:
        new_capacity = 64 if f.capacity == 0 else f.capacity * 2
        grown = <Fill*> realloc(f.items, new_capacity * sizeof(Fill))
        if grown == NULL:
            return BOOK_ERR_NOMEM
        f.items = grown
        f.capacity = new_capacity
    slot = &f.items[f.count]
    slot.taker_id = taker_id
    slot.maker_id = maker_id
    slot.price = price
    slot.quantity = quantity
    slot.taker_side = taker_side
    f.count += 1
    return BOOK_OK


cdef inline bint order_crosses(const Order* order, double level_price) noexcept nogil:
    # A buy crosses an ask priced at or below its limit; a sell crosses a bid at or above.
    return order.price == level_price or price_better(order.side, order.price, level_price)


cdef inline int book_submit(Book* b, const Order* order, FillBuffer* fills) noexcept nogil:
    cdef BookSide* opposite
    cdef PriceLevel* lvl
    cdef OrderNode* maker
    cdef uint32_t remaining
    cdef uint32_t traded
    cdef Order rest
    cdef int rc
    if not order_is_valid(order):
        return BOOK_ERR_INVALID
    if idmap_find(&b.ids, order.order_id) != NULL:
        return BOOK_ERR_DUPLICATE
    opposite = &b.asks if order.side == SIDE_BUY else &b.bids
    remaining = order.quantity
    while remaining > 0:
        lvl = side_best(opposite)                      # price priority: best level first
        if lvl == NULL or not order_crosses(order, lvl.price):
            break
        maker = lvl.head                               # time priority: oldest order first
        traded = remaining if remaining < maker.order.quantity else maker.order.quantity
        rc = fillbuf_push(fills, order.order_id, maker.order.order_id,
                          lvl.price, traded, order.side)
        if rc != BOOK_OK:
            return rc                                  # fills so far stay applied
        remaining -= traded
        if traded == maker.order.quantity:
            book_cancel(b, maker.order.order_id)       # fully used: leaves the book
        else:
            maker.order.quantity -= traded             # partly used: keeps its place
            lvl.total_quantity -= traded
    if remaining > 0:
        rest = order[0]
        rest.quantity = remaining
        return book_insert(b, &rest)                   # unfilled remainder rests
    return BOOK_OK
