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

