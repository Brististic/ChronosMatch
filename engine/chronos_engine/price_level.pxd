# cython: language_level=3
"""
Price-time priority queue for ONE price level (Day 3).

Price priority : a better price is matched first (higher bid / lower ask).
                 -> `price_better()` decides; the book (Day 4) picks the best level.
Time priority  : at the same price, the order that arrived first is matched first.
                 -> a FIFO doubly-linked list: append at the tail, match from the head.

All operations are plain C (noexcept nogil): no Python objects, no GC.
Every operation is O(1).
"""
from libc.stdint cimport uint8_t, uint32_t, uint64_t

from chronos_engine.order_types cimport OrderNode, SIDE_BUY


cdef struct PriceLevel:
    double price
    uint64_t total_quantity     # sum of remaining quantity of all resting orders
    uint32_t order_count
    OrderNode* head             # OLDEST order: first in line to be matched
    OrderNode* tail             # NEWEST order: last in line


cdef inline void level_init(PriceLevel* lvl, double price) noexcept nogil:
    lvl.price = price
    lvl.total_quantity = 0
    lvl.order_count = 0
    lvl.head = NULL
    lvl.tail = NULL


cdef inline bint level_is_empty(const PriceLevel* lvl) noexcept nogil:
    return lvl.head == NULL


cdef inline OrderNode* level_peek_front(const PriceLevel* lvl) noexcept nogil:
    return lvl.head


cdef inline void level_push_back(PriceLevel* lvl, OrderNode* node) noexcept nogil:
    # New order joins the END of the queue (lowest time priority).
    node.next = NULL
    node.prev = lvl.tail
    if lvl.tail != NULL:
        lvl.tail.next = node
    else:
        lvl.head = node            # queue was empty
    lvl.tail = node
    lvl.order_count += 1
    lvl.total_quantity += node.order.quantity


cdef inline void level_remove(PriceLevel* lvl, OrderNode* node) noexcept nogil:
    # Unlink ANY node in O(1) (used for cancels). Caller guarantees that
    # `node` is currently in THIS level; the node is not freed here.
    if node.prev != NULL:
        node.prev.next = node.next
    else:
        lvl.head = node.next       # node was the head
    if node.next != NULL:
        node.next.prev = node.prev
    else:
        lvl.tail = node.prev       # node was the tail
    node.prev = NULL
    node.next = NULL
    lvl.order_count -= 1
    lvl.total_quantity -= node.order.quantity


cdef inline OrderNode* level_pop_front(PriceLevel* lvl) noexcept nogil:
    # Take the oldest order out of the queue (highest time priority).
    cdef OrderNode* node = lvl.head
    if node != NULL:
        level_remove(lvl, node)
    return node


cdef inline bint price_better(uint8_t side, double a, double b) noexcept nogil:
    # True if price `a` has priority over `b` for orders on `side`.
    if side == SIDE_BUY:
        return a > b               # buyers: higher price first
    return a < b                   # sellers: lower price first
