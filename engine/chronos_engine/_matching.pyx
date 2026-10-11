# cython: language_level=3
"""
ChronosMatch matching engine extension.

Day 1: build toolchain proof (C++17, nogil).
Day 2: C structs Order / OrderNode (see order_types.pxd) and the Python-visible
       helpers that let the test-suite inspect them.
Day 3 adds the price-time priority queues, Day 4 insert and cancel.
"""
from libc.stdint cimport uint8_t, uint64_t
from libc.string cimport memcpy

from chronos_engine.order_types cimport (
    Order, OrderNode, WireOrder, SIDE_BUY, SIDE_SELL,
    side_is_valid, order_from_wire, node_alloc, node_free, node_link,
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
