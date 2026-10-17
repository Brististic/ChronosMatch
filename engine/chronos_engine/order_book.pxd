# cython: language_level=3
"""
Limit order book: order INSERTION and active order CANCELLATION (Day 4).

Layout
  Book
   |- bids : BookSide  -> contiguous table of PriceLevel*, indexed by price in TICKS
   |- asks : BookSide  -> same
   '- ids  : IdMap     -> order_id -> (OrderNode*, PriceLevel*) hash table

  Day 8: a price is converted to an INTEGER number of ticks (price * price_scale,
  default scale 100 = two decimals). table[tick - base_tick] IS the level at that
  price, so finding a level is one array read, O(1) (it was a binary search, O(log L),
  plus an O(L) memmove for every new level). `best_tick` tracks the best price, so the
  best level is also one read; it moves only when the best level empties.

  * insert = hash lookup (duplicate check) + O(1) level lookup + O(1) FIFO append.
  * cancel = hash lookup gives node AND level directly, then an O(1) unlink.
             A level is freed only when it becomes empty.

Day 4 only RESTS orders (no matching yet): a crossed book is possible until the
matching routine arrives. Everything is plain C (noexcept nogil), no Python objects.
"""
from libc.stdint cimport int64_t, uint8_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, calloc, realloc, free
from libc.string cimport memmove
from libc.math cimport llround, fabs

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
    BOOK_ERR_RANGE = 5          # price too far from the prices already on this side (Day 8)


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
cdef enum:
    INITIAL_WINDOW_TICKS = 4096      # first table size: 4096 pointers = 32 KB
    MAX_SPAN_TICKS = 4194304         # table never covers more than 4M ticks ($41,943 at scale 100)


cdef struct BookSide:
    PriceLevel** table          # table[i] = level at tick (base_tick + i), or NULL
    int64_t base_tick           # tick that table[0] stands for
    int64_t size                # number of entries in the table
    uint32_t level_count        # how many entries are non-NULL
    int64_t best_tick           # tick of the best level (valid when level_count > 0)
    uint8_t side                # SIDE_BUY or SIDE_SELL


cdef struct Book:
    BookSide bids
    BookSide asks
    IdMap ids
    uint64_t next_trade_id      # id given to the next executed trade (starts at 1)
    int64_t price_scale         # ticks per 1.00 of price (100 -> tick = 0.01)


cdef inline bint price_to_tick(const Book* b, double price, int64_t* tick) noexcept nogil:
    # Exact conversion of a price to integer ticks. Rejects prices that are not on the
    # tick grid (like a real exchange) and prices too large to convert exactly.
    cdef double x = price * b.price_scale
    cdef int64_t t
    if not (x >= 1.0 and x < 9.0e15):
        return False
    t = llround(x)
    if fabs(x - <double> t) > 1e-6:
        return False
    tick[0] = t
    return True


cdef inline bint tick_better(uint8_t side, int64_t a, int64_t b) noexcept nogil:
    if side == SIDE_BUY:
        return a > b               # buyers: higher price first
    return a < b                   # sellers: lower price first


cdef inline bint tick_crosses(uint8_t taker_side, int64_t taker_tick, int64_t level_tick) noexcept nogil:
    # A buy crosses an ask at or below its limit; a sell crosses a bid at or above.
    if taker_side == SIDE_BUY:
        return taker_tick >= level_tick
    return taker_tick <= level_tick


cdef inline void side_init(BookSide* s, uint8_t side) noexcept nogil:
    s.table = NULL
    s.base_tick = 0
    s.size = 0
    s.level_count = 0
    s.best_tick = 0
    s.side = side


cdef inline PriceLevel* side_find_tick(const BookSide* s, int64_t tick) noexcept nogil:
    # THE O(1) PRICE LOOKUP: one subtraction, one bounds test, one array read.
    cdef int64_t idx = tick - s.base_tick
    if idx < 0 or idx >= s.size:
        return NULL
    return s.table[idx]


cdef inline int side_ensure(BookSide* s, int64_t tick) noexcept nogil:
    # Make sure the table covers `tick`; grows (and re-bases) the table when needed.
    cdef int64_t new_lo
    cdef int64_t new_hi
    cdef int64_t margin
    cdef int64_t i
    cdef PriceLevel** fresh
    if s.size == 0:
        new_lo = tick - INITIAL_WINDOW_TICKS // 2
        new_hi = new_lo + INITIAL_WINDOW_TICKS
    else:
        new_lo = s.base_tick
        new_hi = s.base_tick + s.size
        if tick >= new_lo and tick < new_hi:
            return BOOK_OK
        margin = s.size // 4
        if tick < new_lo:
            new_lo = tick - margin
        else:
            new_hi = tick + margin + 1
        if new_hi - new_lo > MAX_SPAN_TICKS:           # margin too generous: try the bare minimum
            new_lo = s.base_tick if tick >= s.base_tick else tick
            new_hi = s.base_tick + s.size if tick < s.base_tick + s.size else tick + 1
            if new_hi - new_lo > MAX_SPAN_TICKS:
                return BOOK_ERR_RANGE
    fresh = <PriceLevel**> calloc(new_hi - new_lo, sizeof(PriceLevel*))
    if fresh == NULL:
        return BOOK_ERR_NOMEM
    for i in range(s.size):
        fresh[(s.base_tick - new_lo) + i] = s.table[i]
    free(s.table)
    s.table = fresh
    s.base_tick = new_lo
    s.size = new_hi - new_lo
    return BOOK_OK


cdef inline PriceLevel* side_create_level(BookSide* s, int64_t tick, int64_t scale) noexcept nogil:
    # Caller has called side_ensure(tick) and found no level there.
    cdef PriceLevel* lvl = <PriceLevel*> malloc(sizeof(PriceLevel))
    if lvl == NULL:
        return NULL
    level_init(lvl, <double> tick / <double> scale, tick)   # exact integer / exact integer
    s.table[tick - s.base_tick] = lvl
    if s.level_count == 0 or tick_better(s.side, tick, s.best_tick):
        s.best_tick = tick
    s.level_count += 1
    return lvl


cdef inline PriceLevel* side_best(const BookSide* s) noexcept nogil:
    if s.level_count == 0:
        return NULL
    return s.table[s.best_tick - s.base_tick]


cdef inline PriceLevel* side_next(const BookSide* s, const PriceLevel* lvl) noexcept nogil:
    # The next-best level after `lvl` (scans the contiguous table), or NULL.
    cdef int64_t i = lvl.tick - s.base_tick
    if s.side == SIDE_BUY:
        i -= 1
        while i >= 0:
            if s.table[i] != NULL:
                return s.table[i]
            i -= 1
    else:
        i += 1
        while i < s.size:
            if s.table[i] != NULL:
                return s.table[i]
            i += 1
    return NULL


cdef inline void side_remove_level(BookSide* s, PriceLevel* lvl) noexcept nogil:
    cdef int64_t idx = lvl.tick - s.base_tick
    cdef PriceLevel* nxt
    if idx < 0 or idx >= s.size or s.table[idx] != lvl:
        return
    s.table[idx] = NULL
    s.level_count -= 1
    if s.level_count > 0 and lvl.tick == s.best_tick:
        nxt = side_next(s, lvl)                         # best level emptied: find the new best
        s.best_tick = nxt.tick
    free(lvl)


cdef inline void side_destroy(BookSide* s) noexcept nogil:
    cdef int64_t i
    cdef OrderNode* node
    cdef OrderNode* nxt
    for i in range(s.size):
        if s.table[i] != NULL:
            node = s.table[i].head
            while node != NULL:
                nxt = node.next
                node_free(node)
                node = nxt
            free(s.table[i])
    free(s.table)
    s.table = NULL
    s.size = 0
    s.level_count = 0


# ------------------------------------------------------------------- Book ----
cdef inline int book_init(Book* b, int64_t price_scale) noexcept nogil:
    side_init(&b.bids, SIDE_BUY)
    side_init(&b.asks, SIDE_SELL)
    b.next_trade_id = 1
    b.price_scale = price_scale
    return idmap_init(&b.ids, 1024)


cdef inline void book_destroy(Book* b) noexcept nogil:
    side_destroy(&b.bids)
    side_destroy(&b.asks)
    idmap_free(&b.ids)


cdef inline bint order_is_valid(const Order* order) noexcept nogil:
    # side must be B/S, quantity > 0, 0 < price < 1e15 (this also rejects NaN and inf,
    # because every comparison with NaN is false)
    return (side_is_valid(order.side) and order.quantity > 0
            and order.price > 0.0 and order.price < 1e15)


cdef inline int book_insert(Book* b, const Order* order) noexcept nogil:
    # Rest a limit order on the book. Returns a BookStatus code.
    cdef BookSide* s
    cdef PriceLevel* lvl
    cdef OrderNode* node
    cdef int64_t tick
    cdef bint created = False
    cdef int rc
    if not order_is_valid(order) or not price_to_tick(b, order.price, &tick):
        return BOOK_ERR_INVALID                       # incl. off-tick, NaN, inf
    if idmap_find(&b.ids, order.order_id) != NULL:
        return BOOK_ERR_DUPLICATE
    s = &b.bids if order.side == SIDE_BUY else &b.asks
    rc = side_ensure(s, tick)
    if rc != BOOK_OK:
        return rc
    node = node_alloc()
    if node == NULL:
        return BOOK_ERR_NOMEM
    lvl = side_find_tick(s, tick)
    if lvl == NULL:
        lvl = side_create_level(s, tick, b.price_scale)
        if lvl == NULL:
            node_free(node)
            return BOOK_ERR_NOMEM
        created = True
    node.order = order[0]
    node.order.price = lvl.price                      # snap to the exact tick price
    level_push_back(lvl, node)
    rc = idmap_insert(&b.ids, order.order_id, node, lvl)
    if rc != BOOK_OK:                                 # roll everything back
        level_remove(lvl, node)
        if created:
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
cdef inline bint side_check(BookSide* s, IdMap* ids, uint64_t* total_orders, int64_t scale) noexcept nogil:
    cdef int64_t i
    cdef uint32_t seen = 0
    cdef PriceLevel* lvl
    cdef PriceLevel* prev_level = NULL
    cdef OrderNode* node
    cdef OrderNode* last
    cdef IdEntry* e
    cdef uint64_t qty
    cdef uint32_t count
    if s.level_count == 0:
        for i in range(s.size):
            if s.table[i] != NULL:
                return False
        return True
    lvl = side_best(s)
    if lvl == NULL or lvl.tick != s.best_tick:
        return False
    for i in range(s.size):                           # table entries <-> level_count
        if s.table[i] != NULL:
            seen += 1
            if s.table[i].tick != s.base_tick + i:
                return False
    if seen != s.level_count:
        return False
    seen = 0
    while lvl != NULL:                                # walk best -> worst
        seen += 1
        if prev_level != NULL and not tick_better(s.side, prev_level.tick, lvl.tick):
            return False                              # not strictly best-first
        if lvl.head == NULL or lvl.price != <double> lvl.tick / <double> scale:
            return False                              # empty level, or price != tick
        node = lvl.head
        last = NULL
        qty = 0
        count = 0
        if node.prev != NULL:
            return False
        while node != NULL:
            if node.order.side != s.side or node.order.price != lvl.price:
                return False
            if node.order.quantity == 0:
                return False                          # a used-up order must have left the book
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
        prev_level = lvl
        lvl = side_next(s, lvl)
    return seen == s.level_count


cdef inline bint book_check(Book* b) noexcept nogil:
    cdef uint64_t total = 0
    if not side_check(&b.bids, &b.ids, &total, b.price_scale):
        return False
    if not side_check(&b.asks, &b.ids, &total, b.price_scale):
        return False
    return total == b.ids.used
