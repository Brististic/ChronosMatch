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

