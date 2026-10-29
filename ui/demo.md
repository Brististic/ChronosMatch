# ChronosMatch Terminal Demonstration

Recorded demonstration of the live latency dashboard (Member 4, Observability & UI).

## Recording

- Video: <PASTE VIDEO LINK HERE>
- Length: <about 2.5 minutes>
- Recorded on: <date>, macOS terminal, 130+ columns

## What the recording shows

1. Order book with best bid/ask, spread and a five-level depth ladder
2. Real-time throughput above 100,000 orders per second
3. Latency measured from ingress/egress nanosecond timestamps, with rolling P50, P90, P99 and P99.9 percentiles and a live histogram
4. SLA breach alerts for trades slower than 50 microseconds
5. Whale sweep alert when one order clears several price levels
6. System health panel: CPU, memory, shared-memory ring buffer fill and watermark alert

## How to reproduce

From the project root, in a terminal at least 130 columns wide:

    bash run_demo.sh --check        # headless self-test of the pipeline
    bash run_demo.sh                # fake engine + live dashboard

For the 100k+ orders/sec run shown in the video, start the engine and dashboard in two terminals:

    python3 ui/fake_producer.py --orders-per-sec 120000 --whale-every 15
    python3 ui/dashboard.py --source shm

## Notes

- The engine in this recording is the simulator `ui/fake_producer.py`, writing to the same shared-memory ring buffer format the dashboard reads. The throughput and latency figures demonstrate the dashboard, not the final matching engine.
- The dashboard reads the ring buffer read-only and cannot disturb the engine.
- Dashboard keys: q quit, h switch panels on narrow terminals, f follow live, w test whale sweep (mock source only).
