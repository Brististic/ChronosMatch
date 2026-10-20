# ChronosMatch - Python C-API Audit of the Matching Hot Path

**Component:** Cython matching engine (`chronos_engine` 0.10.0)  
**Author:** Member 2 (Core Quant Engine Engineer)  
**Generated:** 2026-10-20 11:26:51 UTC by `validation/check_python_api_free.py`  
**Tools:** Cython 3.3.0, Python 3.12.3 on Linux-6.18.44-fc-v139-x86_64-with-glibc2.39

## Verdict: PASS - the matching hot path contains no Python C-API calls

- 45 of 45 hot-path C functions are pure C (no `Py*`, no `__Pyx_*` helper, no Python global).
- 10 `with nogil:` blocks were checked; the only Python call in any of them is one `PyGILState_Check()` probe per call (used to prove the GIL is released), outside every per-order loop.
- The Cython annotation (`cython -a`) shows score 0 (no yellow) on every source line inside those blocks.

## How the audit works

1. `_matching.pyx` and the four `.pxd` helper files are translated to C++ by Cython with annotation enabled.
2. Every C function generated from the helpers (the order book, price-level queues, id table, matcher, and the ring-copy routine) is searched for Python runtime calls: any `Py...`, `__Pyx_...` or Python global name counts as a violation.
3. Every `with nogil:` block in the Python-facing methods is searched the same way, allowing only Cython's own GIL bookkeeping and the probe.
4. The annotation scores are read back: a score above 0 inside a nogil block means Cython had to talk to Python on that line.
5. The audit runs again inside the test-suite, so a future change that adds a Python call to the hot path fails a test. The tests also break the code on purpose (a `with gil:` inside a nogil function) to prove the audit can fail.

## Hot-path C functions

| Module | Function | Generated size | Python C-API calls |
|---|---|---:|---|
| _matching | `RingConsumer__try_lock` | 338 chars | none (pure C) |
| _matching | `_ring_take` | 2,667 chars | none (pure C) |
| matching | `book_submit` | 198 chars | none (pure C) |
| matching | `book_submit_ex` | 5,852 chars | none (pure C) |
| matching | `fillbuf_clear` | 38 chars | none (pure C) |
| matching | `fillbuf_free` | 130 chars | none (pure C) |
| matching | `fillbuf_init` | 100 chars | none (pure C) |
| matching | `fillbuf_reserve` | 1,106 chars | none (pure C) |
| order_book | `book_cancel` | 1,771 chars | none (pure C) |
| order_book | `book_check` | 689 chars | none (pure C) |
| order_book | `book_destroy` | 241 chars | none (pure C) |
| order_book | `book_init` | 513 chars | none (pure C) |
| order_book | `book_insert` | 3,562 chars | none (pure C) |
| order_book | `idmap_erase` | 590 chars | none (pure C) |
| order_book | `idmap_find` | 1,177 chars | none (pure C) |
| order_book | `idmap_free` | 162 chars | none (pure C) |
| order_book | `idmap_hash` | 427 chars | none (pure C) |
| order_book | `idmap_init` | 663 chars | none (pure C) |
| order_book | `idmap_insert` | 2,509 chars | none (pure C) |
| order_book | `idmap_rehash` | 1,973 chars | none (pure C) |
| order_book | `order_is_valid` | 728 chars | none (pure C) |
| order_book | `price_to_tick` | 819 chars | none (pure C) |
| order_book | `side_best` | 378 chars | none (pure C) |
| order_book | `side_check` | 6,501 chars | none (pure C) |
| order_book | `side_create_level` | 1,243 chars | none (pure C) |
| order_book | `side_destroy` | 1,176 chars | none (pure C) |
| order_book | `side_ensure` | 3,404 chars | none (pure C) |
| order_book | `side_find_tick` | 637 chars | none (pure C) |
| order_book | `side_init` | 204 chars | none (pure C) |
| order_book | `side_next` | 1,216 chars | none (pure C) |
| order_book | `side_remove_level` | 1,318 chars | none (pure C) |
| order_book | `tick_better` | 337 chars | none (pure C) |
| order_book | `tick_crosses` | 381 chars | none (pure C) |
| order_types | `node_alloc` | 598 chars | none (pure C) |
| order_types | `node_free` | 36 chars | none (pure C) |
| order_types | `node_link` | 98 chars | none (pure C) |
| order_types | `order_from_wire` | 484 chars | none (pure C) |
| order_types | `side_is_valid` | 347 chars | none (pure C) |
| price_level | `level_init` | 231 chars | none (pure C) |
| price_level | `level_is_empty` | 129 chars | none (pure C) |
| price_level | `level_peek_front` | 172 chars | none (pure C) |
| price_level | `level_pop_front` | 557 chars | none (pure C) |
| price_level | `level_push_back` | 616 chars | none (pure C) |
| price_level | `level_remove` | 888 chars | none (pure C) |
| price_level | `price_better` | 337 chars | none (pure C) |

## `with nogil:` blocks in the Python-facing methods

| Method | .pyx lines | Other Python calls | GIL probes | Yellow annotation lines |
|---|---|---|---:|---:|
| `selftest_nogil` | 119-120 | none | 0 | 0 |
| `selftest_linked_nodes` | 200-225 | none | 0 | 0 |
| `OrderBook__acquire` | 385-386 | none | 0 | 0 |
| `OrderBook__run_submit` | 454-456 | none | 1 | 0 |
| `OrderBook_submit_batch` | 598-618 | none | 1 | 0 |
| `bench_insert_cancel` | 761-775 | none | 0 | 0 |
| `bench_insert_cancel` | 777-779 | none | 0 | 0 |
| `bench_match` | 810-816 | none | 0 | 0 |
| `RingConsumer__drain_core` | 1143-1184 | none | 1 | 0 |
| `gil_check_demo` | 1279-1280 | none | 1 | 0 |

Across the whole `.pyx` file the annotation marks 966 of 1281 lines as pure C (score 0); the remaining lines are the Python-facing wrappers (argument parsing, building result lists and dicts), which are outside the matching loop.

## Per-call cost of reading from the ring (measured on this machine)

One message waiting per call, the way a polling loop sees a quiet market. Time includes matching that one order.

| Call | Day 11 code (this run) | Day 9 code (author's sandbox, before the change) |
|---|---:|---:|
| drain_into() - returns a dict | 837 ns | 1250 ns |
| drain() - lean, latency tracked | 471 ns | n/a (new in Day 11) |
| drain() - lean, no latency clock | 351 ns | n/a (new in Day 11) |
| drain_into() on an EMPTY ring | 306 ns | 1020 ns |

The Day 9 column is a historical measurement taken on the author's development sandbox with the same loop, so compare it with this run's column only when both ran on similar hardware.

The lean `drain()` skips building the result dict and (optionally) the latency clock read, and the whole drain, from taking the ring lock to matching, runs in one section without the GIL. These are Python-timed micro-benchmarks, not the engine's matching latency (Day 15).

## Violations

None.

## How to reproduce

```
cd engine
python setup.py build_ext --inplace
python validation/check_python_api_free.py
```
