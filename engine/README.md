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
