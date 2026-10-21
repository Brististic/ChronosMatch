# cython: language_level=3
"""
Fixed-size memory pool (Day 12): a free list over big pre-allocated slabs.

Why: malloc/free per order is slow and, worse, unpredictable (a call can take a few hundred
nanoseconds one time and microseconds the next, when the allocator has to ask the operating system for
memory or search its bins). A pool hands out the next free element with two pointer moves, always in
the same number of steps.

  * Elements are carved out of slabs allocated up front (one malloc per slab, NOT per element).
  * A free element stores the address of the next free element in its first 8 bytes (intrusive list),
    so the pool needs no extra memory.
  * alloc  = pop the list head, zero the element           O(1), no system call in steady state
  * free   = push the element on the list head             O(1)
  * When the pool is empty it either adds one more slab (grow=True, default; rare, a one-off cost)
    or returns NULL (grow=False: a strictly fixed-size pool).

All functions are plain C (noexcept nogil). Not thread-safe by itself: every Book owns its pools and is
protected by the book's mutex.

Debug builds (CHRONOS_DEBUG=1) fill every freed element with 0xDD, so a read of freed memory shows up as an
obviously wrong value instead of silently working.
"""
from libc.stdint cimport uint32_t, uint64_t
from libc.stdlib cimport malloc, realloc, free
from libc.string cimport memset

cdef extern from *:
    """
    #include <cstring>
    #ifdef CHRONOS_POOL_DEBUG
    static inline void cm_poison(void* p, size_t n) { std::memset(p, 0xDD, n); }
    #else
    static inline void cm_poison(void*, size_t) {}
    #endif
    """
    void cm_poison(void* p, size_t n) noexcept nogil


cdef enum:
    POOL_MAX_SLAB_ELEMS = 1048576       # one growth step never adds more than this many elements


cdef struct Pool:
    size_t elem_size
    void* free_head             # first free element (its first 8 bytes point to the next one)
    void** slabs                # base address of every slab
    uint32_t slab_count
    uint32_t slab_slots         # size of the `slabs` array
    uint32_t slab_elems         # smallest growth step
    uint64_t capacity           # elements in all slabs together
    uint64_t in_use
    uint64_t peak               # highest in_use ever seen
    uint64_t system_allocs      # malloc/realloc calls made by this pool (slabs and the slab list)
    bint grow                   # may a new slab be added when the pool is empty?


cdef inline int pool_add_slab(Pool* p, uint64_t elems) noexcept nogil:
    cdef char* slab
    cdef char* e
    cdef void** grown
    cdef uint32_t new_slots
    cdef uint64_t i
    if p.slab_count == p.slab_slots:
        new_slots = p.slab_slots * 2 if p.slab_slots > 0 else 8
        grown = <void**> realloc(p.slabs, <size_t> new_slots * sizeof(void*))
        if grown == NULL:
            return -1
        p.slabs = grown
        p.slab_slots = new_slots
        p.system_allocs += 1
    slab = <char*> malloc(<size_t> elems * p.elem_size)
    if slab == NULL:
        return -1
    p.system_allocs += 1
    p.slabs[p.slab_count] = slab
    p.slab_count += 1
    i = elems                                   # link the new elements so the LOWEST address is handed out first
    while i > 0:
        i -= 1
        e = slab + <size_t> i * p.elem_size
        (<void**> e)[0] = p.free_head
        p.free_head = e
    p.capacity += elems
    return 0


cdef inline int pool_init(Pool* p, size_t elem_size, uint32_t initial_elems, uint32_t slab_elems,
                          bint grow) noexcept nogil:
    # elem_size must be at least the size of a pointer. Returns 0, or -1 if memory ran out.
    p.elem_size = elem_size
    p.free_head = NULL
    p.slabs = NULL
    p.slab_count = 0
    p.slab_slots = 0
    p.slab_elems = slab_elems
    p.capacity = 0
    p.in_use = 0
    p.peak = 0
    p.system_allocs = 0
    p.grow = grow
    if initial_elems > 0:
        return pool_add_slab(p, initial_elems)
    return 0


cdef inline void* pool_alloc(Pool* p) noexcept nogil:
    # Zeroed element, or NULL when the pool is full and may not grow (or memory ran out).
    cdef void* obj = p.free_head
    cdef uint64_t step
    if obj == NULL:
        if not p.grow:
            return NULL
        step = p.capacity if p.capacity > p.slab_elems else p.slab_elems       # grow by doubling ...
        if step > POOL_MAX_SLAB_ELEMS:
            step = POOL_MAX_SLAB_ELEMS                                         # ... up to a cap
        if pool_add_slab(p, step) != 0:
            return NULL
        obj = p.free_head
    p.free_head = (<void**> obj)[0]
    p.in_use += 1
    if p.in_use > p.peak:
        p.peak = p.in_use
    memset(obj, 0, p.elem_size)
    return obj


cdef inline void pool_free(Pool* p, void* obj) noexcept nogil:
    # Caller guarantees obj came from this pool and is not freed twice.
    cm_poison(obj, p.elem_size)
    (<void**> obj)[0] = p.free_head
    p.free_head = obj
    p.in_use -= 1


cdef inline void pool_destroy(Pool* p) noexcept nogil:
    cdef uint32_t i
    for i in range(p.slab_count):
        free(p.slabs[i])
    free(p.slabs)
    p.slabs = NULL
    p.free_head = NULL
    p.slab_count = 0
    p.slab_slots = 0
    p.capacity = 0
    p.in_use = 0


cdef inline bint pool_check(const Pool* p) noexcept nogil:
    # The free list must hold exactly capacity - in_use elements (and must not loop).
    cdef uint64_t n = 0
    cdef void* e = p.free_head
    while e != NULL and n <= p.capacity:
        n += 1
        e = (<void**> e)[0]
    return e == NULL and n == p.capacity - p.in_use
