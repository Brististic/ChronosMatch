# ChronosMatch - Match Accuracy and Determinism Validation Report

**Component:** Cython matching engine (`chronos_engine` 0.10.0)  
**Author:** Member 2 (Core Quant Engine Engineer)  
**Generated:** 2026-10-20 11:26:37 UTC by `validation/run_match_validation.py` (scale 1.00)

## Verdict: PASS - all 75 checks passed

2,500,000 messages (1,797,383 trades) were processed across 5 stress scenarios. The engine's fills and final books were identical, order by order, to an independent reference engine in every scenario and through every entry path.

## Test environment

| | |
|---|---|
| Python | 3.12.3 (CPython) |
| Operating system | Linux-6.18.44-fc-v139-x86_64-with-glibc2.39 |
| CPU | Intel(R) Xeon(R) Processor @ 2.10GHz (1 logical cores) |
| Engine version | 0.10.0 |
| Run time | 35 s |

## Method

1. Each scenario is a seeded, deterministic stream of limit orders and cancels (own random generator, so the stream is the same on every Python version and operating system).
2. An **independent reference engine** (pure Python, integer ticks, ordered queues; it shares no code with the Cython engine) processes the same stream and is the oracle.
3. The stream is run through the compiled engine on four entry paths: sequential `submit`/`cancel` (twice), the memory-mapped ring buffer read by `RingConsumer` (Member 1's file format), and four threads on four separate books.
4. **Fingerprints:** every fill (taker id, maker id, price in ticks, quantity) in execution order, and the final book (every price level and every resting order in queue order), are each hashed with SHA-256. Two runs are identical only if every single value and its order match.
5. **Invariants** are checked every 50,000 messages and at the end: the engine's own integrity check (sorting, links, totals, id table), the book is never crossed, trade ids are contiguous, and quantity is conserved (accepted quantity = 2 x traded + still resting + cancelled).

## Summary

| Scenario | Messages | New orders | Cancels | Trades | Traded volume | Resting at end | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| S1 balanced two-sided flow | 1,000,000 | 969,762 | 30,238 | 758,892 | 19,357,235 | 9,914,151 | PASS |
| S2 earnings burst with whale sweeps | 500,000 | 500,000 | 0 | 426,142 | 372,413,205 | 12,857,423 | PASS |
| S3 flash crash with panic cancels | 300,000 | 155,834 | 144,166 | 81,563 | 5,047,702 | 1,192,481 | PASS |
| S4 single hot price level | 400,000 | 400,000 | 0 | 366,492 | 5,693,506 | 834,489 | PASS |
| S5 wide sparse book (+-50,000 ticks) | 300,000 | 300,000 | 0 | 164,294 | 10,254,474 | 6,849,327 | PASS |

## S1 balanced two-sided flow

- Messages: 1,000,000 (969,762 new orders, 30,238 cancels: 6,091 cancelled an order, 24,147 targeted an order already filled or unknown)
- Trades: 758,892, traded volume 19,357,235, cancelled quantity 304,236, resting quantity at the end 9,914,151
- Final book: 11 bid levels, 18 ask levels
- Fill fingerprint (SHA-256): `56e70df9fc90a2d3bd88adf72c9c408d174d40b506d8d3e65c1db5b0a303b4ce`
- Final-book fingerprint (SHA-256): `8d334497a400cd459a7b5e78b12b11e91c54ec44ff2b4ce5ba0056bc93b45206`

| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |
|---|---:|---|---|---|---:|---:|
| oracle (pure Python) | 758,892 | `56e70df9fc90...` | `8d334497a400...` | reference | 1.7 s | 0.59 M/s |
| engine, sequential submit/cancel (run 1) | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 1.4 s | 0.74 M/s |
| engine, sequential submit/cancel (run 2) | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 1.4 s | 0.73 M/s |
| engine, mmap ring buffer + RingConsumer | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 0.8 s | 1.26 M/s |
| engine, 4 threads on separate books #1 | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 6.8 s | 0.59 M/s |
| engine, 4 threads on separate books #2 | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 6.8 s | 0.59 M/s |
| engine, 4 threads on separate books #3 | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 6.8 s | 0.59 M/s |
| engine, 4 threads on separate books #4 | 758,892 | `56e70df9fc90...` | `8d334497a400...` | yes | 6.8 s | 0.59 M/s |

| Check | Result |
|---|---|
| engine, sequential submit/cancel (run 1): fills, final book, counts equal the oracle | PASS |
| engine, sequential submit/cancel (run 2): fills, final book, counts equal the oracle | PASS |
| engine, mmap ring buffer + RingConsumer: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #1: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #2: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #3: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #4: fills, final book, counts equal the oracle | PASS |
| all 7 runs and the oracle produce ONE fill fingerprint | PASS |
| all 7 runs and the oracle produce ONE final-book fingerprint | PASS |
| book integrity check passed at every checkpoint | PASS |
| book never left crossed at any checkpoint | PASS |
| trade ids contiguous (trades executed == fills) | PASS |
| quantity conserved: accepted = 2 x traded + resting + cancelled | PASS |
| every generated order was valid (0 rejected) | PASS |
| no engine run raised an error | PASS |

## S2 earnings burst with whale sweeps

- Messages: 500,000 (500,000 new orders, 0 cancels: 0 cancelled an order, 0 targeted an order already filled or unknown)
- Trades: 426,142, traded volume 372,413,205, cancelled quantity 0, resting quantity at the end 12,857,423
- Final book: 37 bid levels, 74 ask levels
- Fill fingerprint (SHA-256): `d136b1159957abb7bd41a3351a9527a885b56730d9540d54cb74fbd5ca824999`
- Final-book fingerprint (SHA-256): `42599b658c78499d51b48e42e7b3f6c18a46ceaf29bab3c80c2da5a671d6fe7c`

| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |
|---|---:|---|---|---|---:|---:|
| oracle (pure Python) | 426,142 | `d136b1159957...` | `42599b658c78...` | reference | 0.9 s | 0.56 M/s |
| engine, sequential submit/cancel (run 1) | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 0.9 s | 0.57 M/s |
| engine, sequential submit/cancel (run 2) | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 0.8 s | 0.67 M/s |
| engine, mmap ring buffer + RingConsumer | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 0.4 s | 1.43 M/s |
| engine, 4 threads on separate books #1 | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 3.2 s | 0.62 M/s |
| engine, 4 threads on separate books #2 | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 3.2 s | 0.62 M/s |
| engine, 4 threads on separate books #3 | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 3.2 s | 0.62 M/s |
| engine, 4 threads on separate books #4 | 426,142 | `d136b1159957...` | `42599b658c78...` | yes | 3.2 s | 0.62 M/s |

| Check | Result |
|---|---|
| engine, sequential submit/cancel (run 1): fills, final book, counts equal the oracle | PASS |
| engine, sequential submit/cancel (run 2): fills, final book, counts equal the oracle | PASS |
| engine, mmap ring buffer + RingConsumer: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #1: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #2: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #3: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #4: fills, final book, counts equal the oracle | PASS |
| all 7 runs and the oracle produce ONE fill fingerprint | PASS |
| all 7 runs and the oracle produce ONE final-book fingerprint | PASS |
| book integrity check passed at every checkpoint | PASS |
| book never left crossed at any checkpoint | PASS |
| trade ids contiguous (trades executed == fills) | PASS |
| quantity conserved: accepted = 2 x traded + resting + cancelled | PASS |
| every generated order was valid (0 rejected) | PASS |
| no engine run raised an error | PASS |

## S3 flash crash with panic cancels

- Messages: 300,000 (155,834 new orders, 144,166 cancels: 65,524 cancelled an order, 78,642 targeted an order already filled or unknown)
- Trades: 81,563, traded volume 5,047,702, cancelled quantity 9,307,834, resting quantity at the end 1,192,481
- Final book: 12 bid levels, 668 ask levels
- Fill fingerprint (SHA-256): `35599f72871abda5a542ea5a67167de7e70769fe5bc9cce67bbcbf069420c99e`
- Final-book fingerprint (SHA-256): `aa7641d15b71ef4b71d79f4266f4d1cc3a02181ae32eadbd7bfc3690a15d52de`

| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |
|---|---:|---|---|---|---:|---:|
| oracle (pure Python) | 81,563 | `35599f72871a...` | `aa7641d15b71...` | reference | 0.3 s | 0.99 M/s |
| engine, sequential submit/cancel (run 1) | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.2 s | 1.40 M/s |
| engine, sequential submit/cancel (run 2) | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.2 s | 1.60 M/s |
| engine, mmap ring buffer + RingConsumer | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.1 s | 5.01 M/s |
| engine, 4 threads on separate books #1 | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.8 s | 1.53 M/s |
| engine, 4 threads on separate books #2 | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.8 s | 1.53 M/s |
| engine, 4 threads on separate books #3 | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.8 s | 1.53 M/s |
| engine, 4 threads on separate books #4 | 81,563 | `35599f72871a...` | `aa7641d15b71...` | yes | 0.8 s | 1.53 M/s |

| Check | Result |
|---|---|
| engine, sequential submit/cancel (run 1): fills, final book, counts equal the oracle | PASS |
| engine, sequential submit/cancel (run 2): fills, final book, counts equal the oracle | PASS |
| engine, mmap ring buffer + RingConsumer: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #1: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #2: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #3: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #4: fills, final book, counts equal the oracle | PASS |
| all 7 runs and the oracle produce ONE fill fingerprint | PASS |
| all 7 runs and the oracle produce ONE final-book fingerprint | PASS |
| book integrity check passed at every checkpoint | PASS |
| book never left crossed at any checkpoint | PASS |
| trade ids contiguous (trades executed == fills) | PASS |
| quantity conserved: accepted = 2 x traded + resting + cancelled | PASS |
| every generated order was valid (0 rejected) | PASS |
| no engine run raised an error | PASS |

## S4 single hot price level

- Messages: 400,000 (400,000 new orders, 0 cancels: 0 cancelled an order, 0 targeted an order already filled or unknown)
- Trades: 366,492, traded volume 5,693,506, cancelled quantity 0, resting quantity at the end 834,489
- Final book: 0 bid levels, 1 ask levels
- Fill fingerprint (SHA-256): `f617d3d54b38a07257140a40dd529fbda15747805c69a998ab5702d19afccfaf`
- Final-book fingerprint (SHA-256): `53cb27add5f567cfaaf6d9a339f5ca25caa1ff874120095f1f46e216b34fa752`

| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |
|---|---:|---|---|---|---:|---:|
| oracle (pure Python) | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | reference | 0.5 s | 0.83 M/s |
| engine, sequential submit/cancel (run 1) | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 0.6 s | 0.62 M/s |
| engine, sequential submit/cancel (run 2) | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 0.7 s | 0.58 M/s |
| engine, mmap ring buffer + RingConsumer | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 0.3 s | 1.41 M/s |
| engine, 4 threads on separate books #1 | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 2.5 s | 0.63 M/s |
| engine, 4 threads on separate books #2 | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 2.5 s | 0.63 M/s |
| engine, 4 threads on separate books #3 | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 2.5 s | 0.63 M/s |
| engine, 4 threads on separate books #4 | 366,492 | `f617d3d54b38...` | `53cb27add5f5...` | yes | 2.5 s | 0.63 M/s |

| Check | Result |
|---|---|
| engine, sequential submit/cancel (run 1): fills, final book, counts equal the oracle | PASS |
| engine, sequential submit/cancel (run 2): fills, final book, counts equal the oracle | PASS |
| engine, mmap ring buffer + RingConsumer: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #1: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #2: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #3: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #4: fills, final book, counts equal the oracle | PASS |
| all 7 runs and the oracle produce ONE fill fingerprint | PASS |
| all 7 runs and the oracle produce ONE final-book fingerprint | PASS |
| book integrity check passed at every checkpoint | PASS |
| book never left crossed at any checkpoint | PASS |
| trade ids contiguous (trades executed == fills) | PASS |
| quantity conserved: accepted = 2 x traded + resting + cancelled | PASS |
| every generated order was valid (0 rejected) | PASS |
| no engine run raised an error | PASS |

## S5 wide sparse book (+-50,000 ticks)

- Messages: 300,000 (300,000 new orders, 0 cancels: 0 cancelled an order, 0 targeted an order already filled or unknown)
- Trades: 164,294, traded volume 10,254,474, cancelled quantity 0, resting quantity at the end 6,849,327
- Final book: 22,072 bid levels, 22,691 ask levels
- Fill fingerprint (SHA-256): `3e4bd63f62c1881663394883c2df0489404cd221029af84033dd6a73488dda2a`
- Final-book fingerprint (SHA-256): `6357450ab2a989e51b7d5f43f802572f77d354149c3f76d199aa6882a2dc10f9`

| Entry path | Fills | Fill fingerprint | Book fingerprint | Matches oracle | Time | Rate |
|---|---:|---|---|---|---:|---:|
| oracle (pure Python) | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | reference | 1.3 s | 0.23 M/s |
| engine, sequential submit/cancel (run 1) | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 0.5 s | 0.63 M/s |
| engine, sequential submit/cancel (run 2) | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 0.4 s | 0.77 M/s |
| engine, mmap ring buffer + RingConsumer | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 0.2 s | 1.38 M/s |
| engine, 4 threads on separate books #1 | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 2.2 s | 0.54 M/s |
| engine, 4 threads on separate books #2 | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 2.2 s | 0.54 M/s |
| engine, 4 threads on separate books #3 | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 2.2 s | 0.54 M/s |
| engine, 4 threads on separate books #4 | 164,294 | `3e4bd63f62c1...` | `6357450ab2a9...` | yes | 2.2 s | 0.54 M/s |

| Check | Result |
|---|---|
| engine, sequential submit/cancel (run 1): fills, final book, counts equal the oracle | PASS |
| engine, sequential submit/cancel (run 2): fills, final book, counts equal the oracle | PASS |
| engine, mmap ring buffer + RingConsumer: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #1: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #2: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #3: fills, final book, counts equal the oracle | PASS |
| engine, 4 threads on separate books #4: fills, final book, counts equal the oracle | PASS |
| all 7 runs and the oracle produce ONE fill fingerprint | PASS |
| all 7 runs and the oracle produce ONE final-book fingerprint | PASS |
| book integrity check passed at every checkpoint | PASS |
| book never left crossed at any checkpoint | PASS |
| trade ids contiguous (trades executed == fills) | PASS |
| quantity conserved: accepted = 2 x traded + resting + cancelled | PASS |
| every generated order was valid (0 rejected) | PASS |
| no engine run raised an error | PASS |

## What the numbers mean

- Rates include the cost of the Python test harness (one Python call per message for the sequential and thread paths, hashing of every fill, packing messages into the ring), so they are **not** the engine's matching latency. The latency benchmark is a separate deliverable (Day 15).
- The thread path processes the stream four times at once, so its rate column counts all four runs.

## Scope and limits

- Prices are on the engine's default tick grid (0.01); off-grid prices are rejected by design and tested separately.
- One symbol per book; the multi-symbol hub uses one book (and one ring) per symbol.
- Only limit orders and cancels exist in the wire format; Market and IOC orders are not part of this validation.
- The oracle is an independent implementation, but both were written by the same author; the rules they check (best price first, oldest first, trade at the resting price, partial fills keep their place, remainder rests) are the standard price-time priority rules.

## How to reproduce

```
cd engine
python setup.py build_ext --inplace
python validation/run_match_validation.py            # full size
python validation/run_match_validation.py --scale 0.1  # quick check
```
