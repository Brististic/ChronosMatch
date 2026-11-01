# ChronosMatch: Ultra-Low Latency Zero-Copy IPC Matching Engine

[![Tests](https://img.shields.io/badge/pytest-39%20passed-brightgreen.svg)]()
[![Language](https://img.shields.io/badge/language-Python%20%7C%20Cython%20%7C%20C%2B%2B-blue.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)]()
[![SLA Target](https://img.shields.io/badge/SLA-%3C50%C2%B5s-success.svg)]()

ChronosMatch is an ultra-low latency, zero-copy inter-process communication (IPC) electronic trading matching engine implemented in Python and Cython. In high-frequency trading (HFT), unpredictable Garbage Collection (GC) pauses and serialization bottlenecks (JSON, Pickle, Protocol Buffers) introduce latency spikes exceeding hundreds of milliseconds. ChronosMatch completely bypasses these bottlenecks through shared-memory memory-mapped circular ring buffers, packed binary wire protocols, and a compiled C++ Limit Order Book executing in `nogil` blocks with pre-allocated static memory pools.

---

## 1. System Architecture
+---------------------------------------------------------------------------------------+
|                                CHRONOSMATCH ARCHITECTURE                              |
+---------------------------------------------------------------------------------------+[ Process A: Ingestion Gateway & Market Feed ] (Member 3)
│
│  29-byte binary wire struct (<IIIIQ) ## ### & 'nogil' (FIFO (Little-endian, (Member (Multiplied (WAL) (Zero (.pyx/.pxd), (1="Buy," (ORDER_STRUCT_SIZE="29" (fills_{sym}.bin) (runtime_wal/{SYM}.wal) (time.perf_counter_ns()) (ui/dashboard.py) (mmap)="========================" (order Member - --- / 1 1) 1 100, 100k+ 2) 2 2. 3) 3 3. 4 4) 4 53-byte 64-bit Allocations) Ask Asynchronous Atomic B: BUS Best Bid Binary Book Branch Buffer Bytes C++ C++, C-slab C: CPU Checkpointed ChronosMatch Circular Clocks Code Compiled Component Core Cython Dashboard Deliverables Dereference Description Direct Engine Feed Field Fills Fixed Fixed-point Format GC Hardware Head Heap ID IPC Inbound Ingestion Ingress Inter-process Layout Ledger Limit Log MEMORY Market Matching Member Memory Memory-mapped Modules Monotonic Monotonically Mutex O(1) OS Observability Observability, Order Outbound P50/P90/P99 Packet Pointer Pre-allocated Price Price-Time Priority Process Protocols Python Quant Quantity Real-time Responsibilities Ring Role SHARED SLA Side Size Stack Systemic Systems TUI TUI, Tail Tech Telemetry Timestamp] To Top-of-Book Top-of-book Type WAL WebSockets Wire Write-Ahead Zero-Copy Zero-lag [ [Order ] $100.50 2="Sell)" <IIIIQ ``` asyncio, `feature/async-firehose` `feature/curses-latency-ui` `feature/cython-engine` `feature/ipc-ringbuffer` `mmap`, `nogil` `order_id` `price` `side` `struct`, `uint32` alerts aligned alignment) and arrays atomic atomicity, binary bitmask blocks boundary breach buffer buffer, burst by byte bytes) cancel crash crisis cross-process curses, dashboard depth durability e.g. eliminate elimination engine exclusively execution firehose, fixed-width flash gap hardware highlights identifier increasing index indexed indices injection, inside ladder, ladders latency locks log lookup maps matching, memory monitor, monitoring msg/s mutex nanosecond node order over overhead, packed padding pointer pool pools, pre-allocated price priority profiler queues) raw recovery regimes regions. ring rolling routines schemas, sequence serialization shared-memory slabs) snapshot spread string: structs sweep synthetic terminal tracking transaction transfers unique via whale wire with wrapping | |---|---|---|---| |---|---|---|---|---| │ └── ├── ▼> `10050`) |
| `quantity` | `uint32` | 4 Bytes | Lot size or shares requested |
| `timestamp` | `uint64` | 8 Bytes | Ingress timestamp recorded in nanoseconds via hardware timer |
| Padding | `pad` | 5 Bytes | Alignment padding ensuring 64-bit CPU cacheline boundaries |Outbound Execution Fill Struct (53 bytes)Format string: <IIIIQQc16s ## ### $185.00\,\mu\text{s}$ $22.40\,\mu\text{s}$ $24{,}000\,\mu\text{s}$ $35.00\,\mu\text{s}$ $79.10\,\mu\text{s}$ $< & ( (Cython (GC (Median) (`b'B'` $0.50\,\mu\text{s}$ $0.62\,\mu\text{s}$ $1.20\,\mu\text{s}$ $12.40\,\mu\text{s}$ $3.85\,\mu\text{s}$ 1,935x 44.8x 48.0x 56.4x 65.9x Max P50 P90 P99 P99.9 SLA + --- / 1 1,000,000 16 4 4. 50.0\,\mu\text{s}$ 8 Aggressive All Benchmark Byte Bytes ChronosMatch Delta Description Distribution Engine Evaluated Executed Field Fixed-point ID IPC) Latency Maker Passive Pause) Peak Percentile Performance Pure Python SLA Size Spike Target Ticker Type Unique Validation Violations b'S') char[16] char exec_id gc.disable(). maker_id maker_side maker_ts price quantity symbol taker_id taker_ts time.perf_counter_ns() uint32 uint64 across and arrival boundaries: bytes clocks consecutive egress executed execution faster fill hardware incoming ingress latencies loads measured monotonic null or order orders padded price quantity reduction resting running side stress submission sustained symbol timestamp trade under using volume with | |---|---|---|---| |---|---|---|---|---| —> 50µs) | 0.00% | 3.42% | 0.000% (0 breaches) | Deterministic |Throughput & Memory Stress MetricsMetricPure Python BaselineChronosMatch EngineImprovementSustained Matching Speed~24,000 orders/sec> 1,250,000 orders/sec52x capacityCross-Process Transfer (1M pkts)4.82s (Pickle CPU time)0.00s (Zero-Copy mmap)100% CPU overhead eliminatedRuntime Heap Allocation per TradeDynamic heap objects (dict, list)Static pre-allocated C-slab pool0 heap allocationsGarbage Collector AuditPeriodic multi-millisecond spikes0 GC pauses (nogil thread safety)100% deterministic5. Directory StructureChronosMatch/
├── .gitignore                      # Ignore build, cache, IPC & WAL runtime files
├── pyproject.toml                  # Engine packaging definition
├── requirements.txt                # Production environment dependencies
├── README.md                       # Comprehensive architecture and runbook
├── run_chronos_pipeline.py         # End-to-end integration orchestration runner
├── run_demo.py                     # Member 4 automated live demonstration driver
├── run_demo.ps1                    # One-command Windows PowerShell launcher
├── run_demo.sh                     # One-command POSIX/Linux launcher
├── data/                           # Data storage root
├── docs/                           # Architectural design documents & specs
├── engine/                         # Member 2 Cython matching engine
│   ├── setup.py                    # Cython C++ extension compilation configuration
│   ├── benchmark_e2e.py            # Microsecond execution latency profiler
│   ├── chronos_engine/             # Compiled extension package
│   │   ├── _matching.pyx           # Cython matching loop and ring consumer
│   │   ├── order_book.pxd          # C++ Limit Order Book definitions
│   │   ├── pool.pxd                # Static pre-allocated C memory pool
│   │   ├── matching.pxd            # Price-time priority function signatures
│   │   └── fill_wire.py            # Outbound 53-byte fill serialization
│   └── tests/                      # Engine verification test suites
├── src/                            # Core Python infrastructure modules
│   ├── ring_buffer.py              # Zero-copy circular memory-mapped ring buffer
│   ├── ipc_hub.py                  # Multi-asset ring coordinator
│   ├── ipc_sync.py                 # Cross-process mutex locks & atomic synchronization
│   ├── ipc_recovery.py             # WAL replay & state reconstruction
│   ├── schema.py                   # 29-byte order struct packing/unpacking
│   ├── wal.py                      # Binary Write-Ahead Logging & checkpointing
│   ├── market_feed.py              # Correlated multi-asset streaming & crisis burst feed
│   ├── multiprocess_runner.py      # Multi-process decoupled execution harness
│   ├── benchmarks.py               # Statistical latency percentile calculator
│   └── risk_filter.py              # Pre-trade risk verification
├── tests/                          # 39 integration and unit test suites
└── ui/                             # Member 4 telemetry and observability
    └── dashboard.py                # Multi-asset TUI order book dashboard
6. Setup & Build InstructionsPrerequisitesPython 3.10+ (Tested through Python 3.14)C/C++ Build Tools:Windows: Microsoft Visual C++ Build Tools (MSVC 14.X+)Linux / macOS: GCC or Clang (build-essential)1. Clone Repository & Install DependenciesBashgit clone [https://github.com/Brististic/ChronosMatch.git](https://github.com/Brististic/ChronosMatch.git)
cd ChronosMatch
pip install -r requirements.txt
2. Compile Cython ExtensionsBuild the C++ matching engine extension in place:Bashcd engine
python setup.py build_ext --inplace
cd ..
3. Run Test SuitesExecute the complete 39-suite regression test pack:PowerShell# PowerShell
$env:PYTHONPATH = "engine"
python -m pytest tests/ -v
Bash# POSIX / Bash
export PYTHONPATH="engine"
python -m pytest tests/ -v
7. Launching Live Evaluation DemonstrationsOne-Command Automated Demo LaunchChronosMatch includes an automated demonstration orchestrating market streaming, zero-copy IPC transmission, compiled Cython matching, and real-time terminal UI rendering:Windows PowerShell:PowerShell.\run_demo.ps1
Linux / macOS Terminal:Bashchmod +x run_demo.sh
./run_demo.sh
Decoupled Multi-Process PipelineTo run the fully decoupled multi-process architecture across separate operating system processes:PowerShell$env:PYTHONPATH = "engine"
+---------------------------------------------------------------------------------------+
|                                CHRONOSMATCH ARCHITECTURE                              |
+---------------------------------------------------------------------------------------+[ Process A: Ingestion Gateway & Market Feed ] (Member 3)
│
│  29-byte binary wire struct (<IIIIQ) ## ### & 'nogil' (FIFO (Little-endian, (Member (Multiplied (WAL) (Zero (.pyx/.pxd), (1="Buy," (ORDER_STRUCT_SIZE="29" (fills_{sym}.bin) (runtime_wal/{SYM}.wal) (time.perf_counter_ns()) (ui/dashboard.py) (mmap)="========================" (order Member - --- / 1 1) 1 100, 100k+ 2) 2 2. 3) 3 3. 4 4) 4 53-byte 64-bit Allocations) Ask Asynchronous Atomic B: BUS Best Bid Binary Book Branch Buffer Bytes C++ C++, C-slab C: CPU Checkpointed ChronosMatch Circular Clocks Code Compiled Component Core Cython Dashboard Deliverables Dereference Description Direct Engine Feed Field Fills Fixed Fixed-point Format GC Hardware Head Heap ID IPC Inbound Ingestion Ingress Inter-process Layout Ledger Limit Log MEMORY Market Matching Member Memory Memory-mapped Modules Monotonic Monotonically Mutex O(1) OS Observability Observability, Order Outbound P50/P90/P99 Packet Pointer Pre-allocated Price Price-Time Priority Process Protocols Python Quant Quantity Real-time Responsibilities Ring Role SHARED SLA Side Size Stack Systemic Systems TUI TUI, Tail Tech Telemetry Timestamp] To Top-of-Book Top-of-book Type WAL WebSockets Wire Write-Ahead Zero-Copy Zero-lag [ [Order ] $100.50 2="Sell)" <IIIIQ ``` asyncio, `feature/async-firehose` `feature/curses-latency-ui` `feature/cython-engine` `feature/ipc-ringbuffer` `mmap`, `nogil` `order_id` `price` `side` `struct`, `uint32` alerts aligned alignment) and arrays atomic atomicity, binary bitmask blocks boundary breach buffer buffer, burst by byte bytes) cancel crash crisis cross-process curses, dashboard depth durability e.g. eliminate elimination engine exclusively execution firehose, fixed-width flash gap hardware highlights identifier increasing index indexed indices injection, inside ladder, ladders latency locks log lookup maps matching, memory monitor, monitoring msg/s mutex nanosecond node order over overhead, packed padding pointer pool pools, pre-allocated price priority profiler queues) raw recovery regimes regions. ring rolling routines schemas, sequence serialization shared-memory slabs) snapshot spread string: structs sweep synthetic terminal tracking transaction transfers unique via whale wire with wrapping | |---|---|---|---| |---|---|---|---|---| │ └── ├── ▼> `10050`) |
| `quantity` | `uint32` | 4 Bytes | Lot size or shares requested |
| `timestamp` | `uint64` | 8 Bytes | Ingress timestamp recorded in nanoseconds via hardware timer |
| Padding | `pad` | 5 Bytes | Alignment padding ensuring 64-bit CPU cacheline boundaries |Outbound Execution Fill Struct (53 bytes)Format string: <IIIIQQc16s ## ### $185.00\,\mu\text{s}$ $22.40\,\mu\text{s}$ $24{,}000\,\mu\text{s}$ $35.00\,\mu\text{s}$ $79.10\,\mu\text{s}$ $< & ( (Cython (GC (Median) (`b'B'` $0.50\,\mu\text{s}$ $0.62\,\mu\text{s}$ $1.20\,\mu\text{s}$ $12.40\,\mu\text{s}$ $3.85\,\mu\text{s}$ 1,935x 44.8x 48.0x 56.4x 65.9x Max P50 P90 P99 P99.9 SLA + --- / 1 1,000,000 16 4 4. 50.0\,\mu\text{s}$ 8 Aggressive All Benchmark Byte Bytes ChronosMatch Delta Description Distribution Engine Evaluated Executed Field Fixed-point ID IPC) Latency Maker Passive Pause) Peak Percentile Performance Pure Python SLA Size Spike Target Ticker Type Unique Validation Violations b'S') char[16] char exec_id gc.disable(). maker_id maker_side maker_ts price quantity symbol taker_id taker_ts time.perf_counter_ns() uint32 uint64 across and arrival boundaries: bytes clocks consecutive egress executed execution faster fill hardware incoming ingress latencies loads measured monotonic null or order orders padded price quantity reduction resting running side stress submission sustained symbol timestamp trade under using volume with | |---|---|---|---| |---|---|---|---|---| —> 50µs) | 0.00% | 3.42% | 0.000% (0 breaches) | Deterministic |Throughput & Memory Stress MetricsMetricPure Python BaselineChronosMatch EngineImprovementSustained Matching Speed~24,000 orders/sec> 1,250,000 orders/sec52x capacityCross-Process Transfer (1M pkts)4.82s (Pickle CPU time)0.00s (Zero-Copy mmap)100% CPU overhead eliminatedRuntime Heap Allocation per TradeDynamic heap objects (dict, list)Static pre-allocated C-slab pool0 heap allocationsGarbage Collector AuditPeriodic multi-millisecond spikes0 GC pauses (nogil thread safety)100% deterministic

5. Directory StructureChronosMatch/
├── .gitignore                      # Ignore build, cache, IPC & WAL runtime files
├── pyproject.toml                  # Engine packaging definition
├── requirements.txt                # Production environment dependencies
├── README.md                       # Comprehensive architecture and runbook
├── run_chronos_pipeline.py         # End-to-end integration orchestration runner
├── run_demo.py                     # Member 4 automated live demonstration driver
├── run_demo.ps1                    # One-command Windows PowerShell launcher
├── run_demo.sh                     # One-command POSIX/Linux launcher
├── data/                           # Data storage root
├── docs/                           # Architectural design documents & specs
├── engine/                         # Member 2 Cython matching engine
│   ├── setup.py                    # Cython C++ extension compilation configuration
│   ├── benchmark_e2e.py            # Microsecond execution latency profiler
│   ├── chronos_engine/             # Compiled extension package
│   │   ├── _matching.pyx           # Cython matching loop and ring consumer
│   │   ├── order_book.pxd          # C++ Limit Order Book definitions
│   │   ├── pool.pxd                # Static pre-allocated C memory pool
│   │   ├── matching.pxd            # Price-time priority function signatures
│   │   └── fill_wire.py            # Outbound 53-byte fill serialization
│   └── tests/                      # Engine verification test suites
├── src/                            # Core Python infrastructure modules
│   ├── ring_buffer.py              # Zero-copy circular memory-mapped ring buffer
│   ├── ipc_hub.py                  # Multi-asset ring coordinator
│   ├── ipc_sync.py                 # Cross-process mutex locks & atomic synchronization
│   ├── ipc_recovery.py             # WAL replay & state reconstruction
│   ├── schema.py                   # 29-byte order struct packing/unpacking
│   ├── wal.py                      # Binary Write-Ahead Logging & checkpointing
│   ├── market_feed.py              # Correlated multi-asset streaming & crisis burst feed
│   ├── multiprocess_runner.py      # Multi-process decoupled execution harness
│   ├── benchmarks.py               # Statistical latency percentile calculator
│   └── risk_filter.py              # Pre-trade risk verification
├── tests/                          # 39 integration and unit test suites
└── ui/                             # Member 4 telemetry and observability
    └── dashboard.py                # Multi-asset TUI order book dashboard

6. Setup & Build Instructions
Prerequisites
Python 3.10+ (Tested through Python 3.14)
C/C++ Build Tools:
Windows: Microsoft Visual C++ Build Tools (MSVC 14.X+)Linux / macOS: GCC or Clang (build-essential)

1. Clone Repository & Install DependenciesBashgit clone [https://github.com/Brististic/ChronosMatch.git](https://github.com/Brististic/ChronosMatch.git)
cd ChronosMatch
pip install -r requirements.txt

2. Compile Cython ExtensionsBuild the C++ matching engine extension in place:Bashcd engine
python setup.py build_ext --inplace
cd ..

3. Run Test SuitesExecute the complete 39-suite regression test pack:
PowerShell
# PowerShell
$env:PYTHONPATH = "engine"
python -m pytest tests/ -v
Bash
# POSIX / Bash
export PYTHONPATH="engine"
python -m pytest tests/ -v

7. Launching Live Evaluation DemonstrationsOne-Command Automated Demo Launch
ChronosMatch includes an automated demonstration orchestrating market streaming, zero-copy IPC transmission, compiled Cython matching, and real-time terminal UI rendering:
Windows PowerShell:
PowerShell
.\run_demo.ps1

Linux / macOS Terminal:
Bash
chmod +x run_demo.sh
./run_demo.sh


Decoupled Multi-Process PipelineTo run the fully decoupled multi-process architecture across separate operating system processes:

PowerShell
$env:PYTHONPATH = "engine"
python -c "from src.multiprocess_runner import MultiProcessCoordinator; coord = MultiProcessCoordinator(['AAPL','MSFT','TSLA'], 'data/chronos_ipc_runtime'); print(coord.run_concurrent_session(cycles=20, batch_size=20))"