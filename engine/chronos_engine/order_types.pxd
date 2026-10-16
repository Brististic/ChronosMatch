# cython: language_level=3
"""
C-level data structures for the ChronosMatch matching engine (Day 2).

Everything here is plain C: no Python objects, so it can be used inside
`nogil` code and never creates work for the garbage collector.

Three structs:
  WireOrder  - 29-byte packed layout written by Member 1's ring buffer
               (struct format "=QcdIQ": id, side, price, quantity, timestamp)
  Order      - 32-byte aligned engine-side order (8-byte aligned fields)
  OrderNode  - Order + prev/next pointers = doubly-linked list node (48 bytes)
"""
from libc.stdint cimport uint8_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, free
from libc.string cimport memset


# ASCII codes of b'B' and b'S', the side bytes used on the wire
cdef enum Side:
    SIDE_BUY = 66
    SIDE_SELL = 83


# Byte-for-byte image of one message in the shared ring buffer (no padding).
cdef packed struct WireOrder:
    uint64_t order_id
    uint8_t side
    double price
    uint32_t quantity
    uint64_t timestamp_ns


# Engine-side order. Natural alignment, 4 bytes of padding at the end -> 32 bytes.
cdef struct Order:
    uint64_t order_id
    uint64_t timestamp_ns
    double price
    uint32_t quantity          # remaining quantity (shrinks on partial fills, Day 7)
    uint8_t side               # SIDE_BUY or SIDE_SELL


# Node of a doubly-linked FIFO queue (one queue per price level, Day 3).
# The Order is embedded, not pointed to: one allocation, one cache line.
cdef struct OrderNode:
    Order order
    OrderNode* prev
    OrderNode* next


cdef inline bint side_is_valid(uint8_t side) noexcept nogil:
    return side == SIDE_BUY or side == SIDE_SELL


cdef inline void order_from_wire(Order* dst, const WireOrder* src) noexcept nogil:
    dst.order_id = src.order_id
    dst.timestamp_ns = src.timestamp_ns
    dst.price = src.price
    dst.quantity = src.quantity
    dst.side = src.side


cdef inline OrderNode* node_alloc() noexcept nogil:
    # Zero-filled node. Day 12 replaces malloc with a pre-allocated pool.
    cdef OrderNode* node = <OrderNode*> malloc(sizeof(OrderNode))
    if node != NULL:
        memset(node, 0, sizeof(OrderNode))
    return node


cdef inline void node_free(OrderNode* node) noexcept nogil:
    free(node)


cdef inline void node_link(OrderNode* first, OrderNode* second) noexcept nogil:
    # Make `second` the successor of `first` (both directions).
    first.next = second
    second.prev = first
