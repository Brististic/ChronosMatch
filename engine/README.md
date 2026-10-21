# chronos_engine - Cython matching engine (Member 2)

## Build
```bash
cd engine
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python setup.py build_ext --inplace
python -m unittest discover -s tests -v
```
Needs a C++17 compiler: `g++`/`clang++` (Linux/macOS) or MSVC Build Tools (Windows).

## Switches
| Variable | Effect |
|---|---|
| `CHRONOS_ANNOTATE=1` | Writes `.html` annotation reports (find Python C-API calls) |
| `CHRONOS_NATIVE=1` | Adds `-march=native` (fastest locally, not portable) |
| `CHRONOS_DEBUG=1` | `-O0 -g`, forces a clean rebuild |

## Reading the ring buffer directly (Day 9)
```python
from chronos_engine import OrderBook, RingConsumer

book = OrderBook()
consumer = RingConsumer(ring_path, lock_path)      # the files MmapRingBuffer / MmapProcessLock use
summary = consumer.drain_into(book)                # one batch: copy out of the ring, then match
# summary: messages, submitted, cancels, cancels_missed, rejected, fill_count, filled_quantity,
#          ring_left, ring_drops, latency_min_ns / latency_max_ns / latency_total_ns
```
- Ring layout (Member 1): 24-byte header `<IIIIQ` (head, tail, capacity, slot size, drops), then 29-byte slots.
- Side `b"B"` / `b"S"` = limit order, `b"C"` = cancel (the order id is the order to cancel).
- Only the tail field is written, and the ring lock is held only while the slots are copied out.
- Needs a little-endian machine, which covers Windows, Linux and macOS on x86 and ARM.

## Validation under load (Day 10)
```
python validation/run_match_validation.py              # about 2.5 million messages, writes reports/match_accuracy_report.md
python validation/run_match_validation.py --scale 0.1  # quick check
```
Five stress scenarios are run through the engine on four entry paths (sequential, ring buffer, 4 threads, repeated)
and compared, fill by fill and order by order, with an independent pure-Python reference engine.
The command exits with status 0 only if every check passes.

## Hot-path audit and the lean drain (Day 11)
```
python validation/check_python_api_free.py       # proves the matching hot path has no Python C-API calls
```
It translates the engine with Cython, checks every generated hot-path C function and every `with nogil:` block,
reads the `cython -a` annotation scores, and writes `reports/python_api_audit.md`. The tests run it too.

`RingConsumer.drain(book)` is the lean way to read the ring: it returns only the number of messages and adds the
counters to `consumer.totals()`. `RingConsumer(..., track_latency=False)` also skips the latency clock read.
A drain on an empty ring returns at once without taking the lock.

## Memory pools (Day 12)
Order nodes and price levels come from pre-allocated pools, not from malloc/free:
```python
book = OrderBook(order_capacity=1_000_000, level_capacity=10_000)   # reserve room for the expected peak
book.pool_stats()      # capacity, in_use, peak, slabs, system_allocations for both pools
```
- While the book stays inside the reserved size, insert, cancel and matching make no system allocator call
  (`system_allocations` stops changing). The id table is sized for the same number of orders and made resident at creation.
- Defaults are 1,024 orders and 128 levels; a pool that runs out adds a slab (a rare, one-off cost).
  `grow=False` makes the pools strictly fixed: new orders then raise `MemoryError` (counted as rejected on the ring path).
- `CHRONOS_DEBUG=1 python setup.py build_ext --inplace` builds a debug version that fills freed pool elements with 0xDD.
- `bench_latency()` reports the per-operation latency distribution (p50 / p99 / p99.9 / max).
