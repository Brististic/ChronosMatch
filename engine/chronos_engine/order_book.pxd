# cython: language_level=3
"""
Limit order book: order INSERTION and active order CANCELLATION (Day 4).

Layout
  Book
   |- bids : BookSide  -> array of PriceLevel*, sorted best price first (highest)
   |- asks : BookSide  -> array of PriceLevel*, sorted best price first (lowest)
   '- ids  : IdMap     -> order_id -> (OrderNode*, PriceLevel*) hash table

  * insert = hash lookup (duplicate check) + binary search for the level (O(log L))
             + O(1) append to that level's FIFO queue.
  * cancel = hash lookup gives node AND level directly, then an O(1) unlink.
             A level is removed from the sorted array only when it becomes empty.

Day 4 only RESTS orders (no matching yet): a crossed book is possible until the
matching routine arrives. Everything is plain C (noexcept nogil), no Python objects.
"""
from libc.stdint cimport uint8_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, calloc, realloc, free
from libc.string cimport memmove

from chronos_engine.order_types cimport (
    Order, OrderNode, SIDE_BUY, SIDE_SELL, node_alloc, node_free, side_is_valid,
)
from chronos_engine.price_level cimport (
    PriceLevel, level_init, level_is_empty, level_push_back, level_remove,
    price_better,
)


cdef enum BookStatus:
    BOOK_OK = 0
    BOOK_ERR_DUPLICATE = 1
    BOOK_ERR_INVALID = 2
    BOOK_ERR_NOMEM = 3
    BOOK_ERR_NOT_FOUND = 4


# ------------------------------------------------------------------ IdMap ----
# Open-addressing hash table (linear probing, power-of-two capacity, load <= 0.5).
cdef enum SlotState:
    SLOT_EMPTY = 0
    SLOT_USED = 1
    SLOT_DELETED = 2        # tombstone: keeps probe chains unbroken after a cancel


cdef struct IdEntry:
    uint64_t key
    OrderNode* node
    PriceLevel* level
    uint8_t state


cdef struct IdMap:
    IdEntry* slots
    uint64_t capacity
    uint64_t used
    uint64_t tombstones


cdef inline uint64_t idmap_hash(uint64_t key) noexcept nogil:
    # splitmix64 finaliser: spreads sequential order ids evenly over the table
    key ^= key >> 30
    key *= <uint64_t> 0xBF58476D1CE4E5B9
    key ^= key >> 27
    key *= <uint64_t> 0x94D049BB133111EB
    key ^= key >> 31
    return key


cdef inline int idmap_init(IdMap* m, uint64_t capacity) noexcept nogil:
    m.slots = <IdEntry*> calloc(capacity, sizeof(IdEntry))
    m.used = 0
    m.tombstones = 0
    if m.slots == NULL:
        m.capacity = 0
        return BOOK_ERR_NOMEM
    m.capacity = capacity
    return BOOK_OK


cdef inline void idmap_free(IdMap* m) noexcept nogil:
    free(m.slots)
    m.slots = NULL
    m.capacity = 0
    m.used = 0
    m.tombstones = 0


cdef inline IdEntry* idmap_find(IdMap* m, uint64_t key) noexcept nogil:
    cdef uint64_t mask = m.capacity - 1
    cdef uint64_t i = idmap_hash(key) & mask
    cdef IdEntry* e
    while True:                         # terminates: load factor keeps empty slots
        e = &m.slots[i]
        if e.state == SLOT_EMPTY:
            return NULL
        if e.state == SLOT_USED and e.key == key:
            return e
        i = (i + 1) & mask


cdef inline int idmap_rehash(IdMap* m, uint64_t new_capacity) noexcept nogil:
    cdef IdEntry* old = m.slots
    cdef uint64_t old_capacity = m.capacity
    cdef IdEntry* fresh = <IdEntry*> calloc(new_capacity, sizeof(IdEntry))
    cdef uint64_t mask = new_capacity - 1
    cdef uint64_t i, j
    if fresh == NULL:
        return BOOK_ERR_NOMEM
    for i in range(old_capacity):
        if old[i].state == SLOT_USED:
            j = idmap_hash(old[i].key) & mask
            while fresh[j].state != SLOT_EMPTY:
                j = (j + 1) & mask
            fresh[j] = old[i]
    free(old)
    m.slots = fresh
    m.capacity = new_capacity
    m.tombstones = 0                    # a rehash drops every tombstone
    return BOOK_OK


cdef inline int idmap_insert(IdMap* m, uint64_t key, OrderNode* node,
                             PriceLevel* level) noexcept nogil:
    cdef uint64_t mask
    cdef uint64_t i
    cdef IdEntry* e
    cdef IdEntry* reuse = NULL
    cdef int rc
    if (m.used + m.tombstones + 1) * 2 > m.capacity:
        if (m.used + 1) * 4 > m.capacity:
            rc = idmap_rehash(m, m.capacity * 2)      # really full: grow
        else:
            rc = idmap_rehash(m, m.capacity)          # mostly tombstones: clean up
        if rc != BOOK_OK:
            return rc
    mask = m.capacity - 1
    i = idmap_hash(key) & mask
    while True:
        e = &m.slots[i]
        if e.state == SLOT_EMPTY:
            break
        if e.state == SLOT_DELETED:
            if reuse == NULL:
                reuse = e
        elif e.key == key:
            return BOOK_ERR_DUPLICATE
        i = (i + 1) & mask
    if reuse != NULL:
        e = reuse
        m.tombstones -= 1
    e.key = key
    e.node = node
    e.level = level
    e.state = SLOT_USED
    m.used += 1
    return BOOK_OK


cdef inline bint idmap_erase(IdMap* m, uint64_t key) noexcept nogil:
    cdef IdEntry* e = idmap_find(m, key)
    if e == NULL:
        return False
    e.state = SLOT_DELETED
    m.used -= 1
    m.tombstones += 1
    return True


# --------------------------------------------------------------- BookSide ----
cdef struct BookSide:
    PriceLevel** levels         # sorted best price first
    uint32_t count
    uint32_t capacity
    uint8_t side                # SIDE_BUY or SIDE_SELL


cdef struct Book:
    BookSide bids
    BookSide asks
    IdMap ids


cdef inline uint32_t side_lower_bound(const BookSide* s, double price) noexcept nogil:
    # First index whose level is NOT strictly better than `price`
    # (so it is either the level with this exact price, or the insert position).
    cdef uint32_t lo = 0
    cdef uint32_t hi = s.count
    cdef uint32_t mid
    while lo < hi:
        mid = lo + ((hi - lo) >> 1)
        if price_better(s.side, s.levels[mid].price, price):
            lo = mid + 1
        else:
            hi = mid
    return lo


cdef inline PriceLevel* side_find_level(const BookSide* s, double price) noexcept nogil:
    cdef uint32_t idx = side_lower_bound(s, price)
    if idx < s.count and s.levels[idx].price == price:
        return s.levels[idx]
    return NULL


cdef inline PriceLevel* side_get_or_create(BookSide* s, double price) noexcept nogil:
    cdef uint32_t idx = side_lower_bound(s, price)
    cdef uint32_t new_capacity
    cdef PriceLevel** grown
    cdef PriceLevel* lvl
    if idx < s.count and s.levels[idx].price == price:
        return s.levels[idx]
    if s.count == s.capacity:
        new_capacity = 16 if s.capacity == 0 else s.capacity * 2
        grown = <PriceLevel**> realloc(s.levels, new_capacity * sizeof(PriceLevel*))
        if grown == NULL:
            return NULL
        s.levels = grown
        s.capacity = new_capacity
    lvl = <PriceLevel*> malloc(sizeof(PriceLevel))
    if lvl == NULL:
        return NULL
    level_init(lvl, price)
    memmove(&s.levels[idx + 1], &s.levels[idx], (s.count - idx) * sizeof(PriceLevel*))
    s.levels[idx] = lvl
    s.count += 1
    return lvl


cdef inline void side_remove_level(BookSide* s, PriceLevel* lvl) noexcept nogil:
    cdef uint32_t idx = side_lower_bound(s, lvl.price)
    if idx < s.count and s.levels[idx] == lvl:
        memmove(&s.levels[idx], &s.levels[idx + 1], (s.count - idx - 1) * sizeof(PriceLevel*))
        s.count -= 1
        free(lvl)


cdef inline void side_init(BookSide* s, uint8_t side) noexcept nogil:
    s.levels = NULL
    s.count = 0
    s.capacity = 0
    s.side = side


cdef inline void side_destroy(BookSide* s) noexcept nogil:
    cdef uint32_t i
    cdef OrderNode* node
    cdef OrderNode* nxt
    for i in range(s.count):
        node = s.levels[i].head
        while node != NULL:
            nxt = node.next
            node_free(node)
            node = nxt
        free(s.levels[i])
    free(s.levels)
    s.levels = NULL
    s.count = 0
    s.capacity = 0


cdef inline PriceLevel* side_best(const BookSide* s) noexcept nogil:
    if s.count == 0:
        return NULL
    return s.levels[0]


# ------------------------------------------------------------------- Book ----
cdef inline int book_init(Book* b) noexcept nogil:
    side_init(&b.bids, SIDE_BUY)
    side_init(&b.asks, SIDE_SELL)
    return idmap_init(&b.ids, 1024)


cdef inline void book_destroy(Book* b) noexcept nogil:
    side_destroy(&b.bids)
    side_destroy(&b.asks)
    idmap_free(&b.ids)


cdef inline int book_insert(Book* b, const Order* order) noexcept nogil:
    # Rest a limit order on the book. Returns a BookStatus code.
    cdef BookSide* s
    cdef PriceLevel* lvl
    cdef OrderNode* node
    cdef int rc
    if (not side_is_valid(order.side) or order.quantity == 0
            or not (order.price > 0.0 and order.price < 1e15)):
        return BOOK_ERR_INVALID                       # also rejects NaN / inf
    if idmap_find(&b.ids, order.order_id) != NULL:
        return BOOK_ERR_DUPLICATE
    s = &b.bids if order.side == SIDE_BUY else &b.asks
    node = node_alloc()
    if node == NULL:
        return BOOK_ERR_NOMEM
    node.order = order[0]
    lvl = side_get_or_create(s, order.price)
    if lvl == NULL:
        node_free(node)
        return BOOK_ERR_NOMEM
    level_push_back(lvl, node)
    rc = idmap_insert(&b.ids, order.order_id, node, lvl)
    if rc != BOOK_OK:                                 # roll everything back
        level_remove(lvl, node)
        if level_is_empty(lvl):
            side_remove_level(s, lvl)
        node_free(node)
        return rc
    return BOOK_OK


cdef inline int book_cancel(Book* b, uint64_t order_id) noexcept nogil:
    # Remove a resting order by id. O(1) unlink thanks to the id table.
    cdef IdEntry* e = idmap_find(&b.ids, order_id)
    cdef OrderNode* node
    cdef PriceLevel* lvl
    cdef BookSide* s
    if e == NULL:
        return BOOK_ERR_NOT_FOUND
    node = e.node
    lvl = e.level
    s = &b.bids if node.order.side == SIDE_BUY else &b.asks
    idmap_erase(&b.ids, order_id)
    level_remove(lvl, node)
    if level_is_empty(lvl):
        side_remove_level(s, lvl)                     # no empty levels on the book
    node_free(node)
    return BOOK_OK


# -------------------------------------------------------------- integrity ----
cdef inline bint side_check(BookSide* s, IdMap* ids, uint64_t* total_orders) noexcept nogil:
    cdef uint32_t i
    cdef PriceLevel* lvl
    cdef OrderNode* node
    cdef OrderNode* last
    cdef IdEntry* e
    cdef uint64_t qty
    cdef uint32_t count
    for i in range(s.count):
        lvl = s.levels[i]
        if lvl == NULL or lvl.head == NULL:
            return False                              # empty level left behind
        if i > 0 and not price_better(s.side, s.levels[i - 1].price, lvl.price):
            return False                              # not strictly best-first
        node = lvl.head
        last = NULL
        qty = 0
        count = 0
        if node.prev != NULL:
            return False
        while node != NULL:
            if node.order.side != s.side or node.order.price != lvl.price:
                return False
            if node.prev != last:
                return False
            e = idmap_find(ids, node.order.order_id)
            if e == NULL or e.node != node or e.level != lvl:
                return False
            qty += node.order.quantity
            count += 1
            last = node
            node = node.next
        if last != lvl.tail or qty != lvl.total_quantity or count != lvl.order_count:
            return False
        total_orders[0] += count
    return True


cdef inline bint book_check(Book* b) noexcept nogil:
    cdef uint64_t total = 0
    if not side_check(&b.bids, &b.ids, &total):
        return False
    if not side_check(&b.asks, &b.ids, &total):
        return False
    return total == b.ids.used
