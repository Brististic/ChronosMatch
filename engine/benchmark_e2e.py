import time
from chronos_engine import OrderBook

N = 20000
book = OrderBook()

# Seed resting sell orders before measuring.
for i in range(1, N + 1):
    book.insert(i, b"S", 100.0, 1)

samples = []
fill_count = 0

# Time each crossing buy submission.
for i in range(N + 1, 2 * N + 1):
    start = time.perf_counter_ns()
    fills = book.submit(i, b"B", 100.0, 1)
    elapsed = time.perf_counter_ns() - start

    samples.append(elapsed)
    fill_count += len(fills)

samples.sort()

print("submissions:", N)
print("total fills:", fill_count)
print("p50_us:", samples[N // 2] / 1000)
print("p99_us:", samples[int(N * 0.99)] / 1000)
print("max_us:", samples[-1] / 1000)