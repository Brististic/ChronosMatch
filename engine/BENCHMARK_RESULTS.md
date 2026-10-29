\# ChronosMatch Matching Benchmark



\## Workload and method



\- 20,000 resting sell orders at price 100.0, quantity 1 each.

\- Submit 20,000 crossing buy orders at the same price, quantity 1 each.

\- Time each Python-visible `OrderBook.submit()` call with `time.perf\_counter\_ns()`.

\- Confirm that each incoming order produces a fill.



\## Results



| Metric | Run 1 | Run 2 |

|---|---:|---:|

| Submissions | 20,000 | 20,000 |

| Fills | 20,000 | 20,000 |

| p50 latency (us) | 0.6 | 0.7 |

| p99 latency (us) | 1.0 | 1.5 |

| Maximum observed (us) | 16.2 | 20.6 |



Both runs recorded a maximum observed submit latency below 50 us.



\## Scope



This measures the Python-visible `OrderBook.submit()` call for the described workload, including returned-fill handling. It does not measure the complete ring-buffer ingestion-to-fill-consumer pipeline. Results are specific to the machine and test conditions and are not a latency guarantee.



## Order Book Algorithm & Cython Optimization Benchmark

### Order Book Algorithm

ChronosMatch implements a limit order book in the compiled Cython matching engine.

- **Price-time priority:** Incoming orders match against the best available opposing price. At the same price level, the oldest resting order is matched first.
- **Order matching:** Crossing orders execute at the resting maker order's price. Any unfilled quantity remains on the book.
- **Order identification:** An order-ID lookup structure supports efficient order lookup and cancellation.
- **Price levels:** Orders are organized into price levels, with FIFO queues maintained at each level.
- **Memory management:** Preallocated pools store order nodes and price levels, reducing repeated system allocations when the configured capacity is sufficient.
- **Concurrency:** Core matching runs inside Cython `nogil` sections, protected by a per-book mutex.
- **Batch processing:** `submit_batch()` processes multiple wire-format orders within a single GIL-free section.
- **Integrity validation:** The benchmark calls `book.check_integrity()` after processing the workload.

### Cython Optimization Techniques

The compiled engine uses C-level structures and typed variables to reduce Python-object overhead in the matching path. Matching logic operates on native order and price-level structures, while preallocated fill buffers reduce repeated allocation during execution. Batch submission and direct ring-buffer consumption further reduce Python-call overhead.

### Benchmark Methodology

The existing `engine/bench_engine.py` benchmark:

1. Creates an `OrderBook`.
2. Generates paired sell and buy limit orders at price 180.00, each with quantity 25.
3. Submits the orders sequentially and accumulates the number of fills.
4. Measures total elapsed time with `time.perf_counter()`.
5. Calculates throughput in orders per second and average latency in microseconds per submitted order.
6. Verifies final order-book integrity.

The script defaults to 100,000 submitted orders. Its reported throughput and average latency depend on the machine, build configuration, workload, and runtime conditions.

### Recorded Python-Visible Submit Benchmark

The existing workload records two runs of 20,000 crossing buy submissions against 20,000 resting sell orders at price 100.0, with quantity 1 per order.

| Metric | Run 1 | Run 2 |
|---|---:|---:|
| Submissions | 20,000 | 20,000 |
| Fills | 20,000 | 20,000 |
| p50 latency (µs) | 0.6 | 0.7 |
| p99 latency (µs) | 1.0 | 1.5 |
| Maximum observed latency (µs) | 16.2 | 20.6 |

Both recorded runs had a maximum observed Python-visible submit latency below 50 µs.

**Measurement scope:** These figures describe the existing Python-visible `OrderBook.submit()` workload, including returned-fill handling. They do not represent end-to-end ring-buffer ingestion-to-fill-consumer latency. The separate `bench_engine.py` benchmark uses a different workload and timing method, so its results must be recorded independently after execution.

### Reproducibility and Limitations

Run the compiled-engine benchmark from the repository root:

```cmd
python engine\bench_engine.py
```

The benchmark requires the Cython extension to be built and importable. Record the machine, compiler/build configuration, workload size, elapsed time, throughput, average latency, and integrity result when reporting a new run.

Do not interpret the recorded results as a universal latency guarantee. No Python-versus-Cython speedup ratio is claimed here because a comparable baseline measurement has not been included.

## Day 20 — Final Sub-50 Microsecond Validation Tables

### 1. Python-Visible Submit Latency Validation

**Workload:** 20,000 resting sell orders followed by 20,000 crossing buy submissions, all at price 100.0 with quantity 1. Each incoming order produced a fill.

| Validation metric | Run 1 | Run 2 | Acceptance target |
|---|---:|---:|---:|
| Submissions | 20,000 | 20,000 | 20,000 |
| Confirmed fills | 20,000 | 20,000 | 20,000 |
| Median latency (p50) | 0.6 µs | 0.7 µs | Reference metric |
| 99th-percentile latency (p99) | 1.0 µs | 1.5 µs | Reference metric |
| Maximum observed latency | 16.2 µs | 20.6 µs | Below 50 µs |
| Maximum-latency target | PASS | PASS | < 50 µs |

**Result:** Both recorded runs satisfy the sub-50 µs maximum-latency target for the measured Python-visible `OrderBook.submit()` workload.

### 2. Matching Correctness and Determinism Validation

| Validation metric | Recorded result | Status |
|---|---:|---|
| Stress scenarios | 5 | PASS |
| Total messages processed | 2,500,000 | PASS |
| Total trades | 1,797,383 | PASS |
| Validation checks | 75 of 75 passed | PASS |
| Fill fingerprints matched the reference engine | All tested scenarios and entry paths | PASS |
| Final-book fingerprints matched the reference engine | All tested scenarios and entry paths | PASS |
| Order-book integrity and quantity-conservation checks | Passed at checkpoints | PASS |
| Engine errors during recorded validation | 0 | PASS |

Source: `reports/match_accuracy_report.md`, generated on 2026-10-21. These results establish matching correctness and determinism, not a per-order latency guarantee.

### 3. Cython Hot-Path Audit

| Audit metric | Recorded result | Status |
|---|---:|---|
| Hot-path C functions audited | 52 | PASS |
| Hot-path C functions without Python C-API calls | 52 of 52 | PASS |
| `with nogil:` blocks inspected | 11 | PASS |
| Yellow annotation lines inside inspected blocks | 0 | PASS |
| Reported audit violations | 0 | PASS |

Source: `reports/python_api_audit.md`, generated on 2026-10-21. The audit permits a documented GIL-state probe outside per-order loops; it does not claim that every Python-facing wrapper is free of Python API calls.

### 4. Ring-Consumer Microbenchmark Evidence

| Operation | Recorded time per call | Measurement scope |
|---|---:|---|
| `drain()` with latency tracking | 471 ns (0.471 µs) | One waiting message; includes matching |
| `drain()` without latency clock | 325 ns (0.325 µs) | One waiting message; includes matching |
| `drain_into()` returning a dictionary | 846 ns (0.846 µs) | One waiting message; includes matching |
| `drain_into()` on an empty ring | 326 ns (0.326 µs) | Empty-ring polling |

Source: `reports/python_api_audit.md`. These are measurements from the recorded test machine and workload. They must not be substituted for the submit-latency results or interpreted as full end-to-end pipeline latency.

### 5. Final Acceptance Summary

| Requirement | Evidence available | Conclusion |
|---|---|---|
| Sub-50 µs submit latency | Two recorded runs; maximums 16.2 µs and 20.6 µs | PASS for the measured workload |
| Matching correctness | 75 validation checks passed across five scenarios | PASS |
| Deterministic matching output | Fill and final-book fingerprints matched the reference engine | PASS |
| Python C-API-free matching hot path | 52 of 52 audited C functions passed | PASS |
| Full ring-buffer ingestion-to-fill-consumer latency below 50 µs | No complete end-to-end latency distribution recorded in these reports | NOT VERIFIED |
| Zero Python garbage-collection pauses | No GC-pause measurement in these reports | NOT VERIFIED |
| One-million-order IPC audit latency | The accuracy report includes a one-million-message scenario, but not a dedicated IPC latency audit | NOT VERIFIED |

**Conclusion:** The available evidence supports the sub-50 µs target for the measured Python-visible submit workload and documents successful matching correctness, determinism, and hot-path API audits. End-to-end latency, zero GC pauses, and a dedicated one-million-order IPC latency audit remain unverified by these records.

**Reproducibility:** Preserve the original benchmark and audit reports. Any new performance claim should include the test environment, workload, timing boundaries, sample count, percentile measurements, and validation outcome. Do not present results from different timing methods as directly interchangeable.
