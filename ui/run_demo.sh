#!/usr/bin/env bash
#
# run_demo.sh - one-command launcher for the ChronosMatch evaluation demo.
#
#   bash run_demo.sh                  fake engine + live dashboard (default)
#   bash run_demo.sh --mode mock      dashboard on its built-in mock data
#   bash run_demo.sh --mode real      your real engine (needs ENGINE_CMD)
#   bash run_demo.sh --check          headless self-test, no screen needed
#   bash run_demo.sh --help           all options

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

PY="${PYTHON:-python3}"
RING="${RING_PATH:-/tmp/chronosmatch_ring.bin}"
LOG="/tmp/chronosmatch_engine.log"
MODE="fake"
CHECK=0
DURATION=""
WHALE_EVERY=""
NO_COLOR_FLAG=0
BG_PID=""
PRODUCER_EXTRA=()
DASH_EXTRA=()

usage() {
  cat <<'EOF'
ChronosMatch demo launcher

usage: bash run_demo.sh [options] [-- dashboard options]

options:
  -m, --mode MODE       fake (default), mock or real
  -d, --duration SEC    stop the dashboard automatically after SEC seconds
      --whale-every SEC average seconds between whale sweeps
      --no-color        turn colors off
      --check           run a headless self-test of the pipeline and exit
  -h, --help            show this help

modes:
  fake   starts ui/fake_producer.py in the background, then the dashboard
         reading its shared-memory ring buffer (--source shm)
  mock   starts only the dashboard, on its built-in simulated data
  real   starts the command in ENGINE_CMD in the background, then the
         dashboard reading the ring buffer that engine writes. Example:
           ENGINE_CMD="python3 engine/run_engine.py" bash run_demo.sh --mode real

environment:
  RING_PATH     ring buffer file (default /tmp/chronosmatch_ring.bin)
  ENGINE_CMD    command that starts the real engine (real mode)
  PYTHON        python interpreter to use (default python3)

keys inside the dashboard: q quit, h switch panel, w test whale sweep (mock)
EOF
}

fail() {
  echo "run_demo: $*" >&2
  exit 1
}

# ---------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--mode)
      [ $# -ge 2 ] || fail "--mode needs a value"
      MODE="$2"; shift 2 ;;
    -d|--duration)
      [ $# -ge 2 ] || fail "--duration needs a value"
      DURATION="$2"; shift 2 ;;
    --whale-every)
      [ $# -ge 2 ] || fail "--whale-every needs a value"
      WHALE_EVERY="$2"; shift 2 ;;
    --no-color)
      NO_COLOR_FLAG=1; shift ;;
    --check)
      CHECK=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; DASH_EXTRA=("$@"); break ;;
    *)
      fail "unknown option: $1 (try --help)" ;;
  esac
done

case "$MODE" in
  fake|mock|real) ;;
  *) fail "unknown mode '$MODE' (use fake, mock or real)" ;;
esac

DASH=(ui/dashboard.py)
[ -n "$DURATION" ] && DASH+=(--duration "$DURATION")
[ "$NO_COLOR_FLAG" = 1 ] && DASH+=(--no-color)
[ "$MODE" != mock ] && DASH+=(--source shm --ring "$RING")
if [ -n "$WHALE_EVERY" ]; then
  if [ "$MODE" = mock ]; then
    DASH+=(--whale-every "$WHALE_EVERY")
  else
    PRODUCER_EXTRA=(--whale-every "$WHALE_EVERY")
  fi
fi

# ---------------------------------------------------------- preflight

preflight() {
  command -v "$PY" >/dev/null 2>&1 || fail "$PY not found. Install Python 3."
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || fail "Python 3.8 or newer is required"
  "$PY" -c 'import curses, mmap' 2>/dev/null \
    || fail "Python modules curses/mmap are missing (on Windows: use WSL)"
  [ -f ui/dashboard.py ] || fail "ui/dashboard.py not found (run from the project folder)"
  if [ "$MODE" = fake ]; then
    [ -f ui/fake_producer.py ] || fail "ui/fake_producer.py not found"
  fi
  if [ "$MODE" = real ] && [ -z "${ENGINE_CMD:-}" ]; then
    fail "real mode needs ENGINE_CMD, e.g. ENGINE_CMD=\"python3 engine/run_engine.py\" bash run_demo.sh --mode real"
  fi
}

check_terminal() {
  [ "$CHECK" = 1 ] && return
  if [ ! -t 1 ]; then
    echo "warning: output is not a terminal, the dashboard needs one" >&2
    return
  fi
  local size rows cols
  size="$(stty size 2>/dev/null)"
  [ -n "$size" ] || return
  rows="${size% *}"
  cols="${size#* }"
  if [ "$rows" -lt 28 ] 2>/dev/null || [ "$cols" -lt 70 ] 2>/dev/null; then
    echo "warning: terminal is ${cols}x${rows}, the dashboard needs at least 70x28." >&2
    echo "         Enlarge the window (or go full screen) for the best result." >&2
  elif [ "$cols" -lt 130 ] 2>/dev/null; then
    echo "note: terminal is ${cols} columns wide; 130+ shows every panel at once." >&2
  fi
}

# ------------------------------------------------ background engine

stop_background() {
  if [ -n "$BG_PID" ] && kill -0 "$BG_PID" 2>/dev/null; then
    kill "$BG_PID" 2>/dev/null
    wait "$BG_PID" 2>/dev/null
  fi
  BG_PID=""
}

cleanup() {
  trap - EXIT INT TERM
  stop_background
  [ "$MODE" != mock ] && rm -f "$RING" "$RING.tmp"
}
trap cleanup EXIT INT TERM

start_engine() {
  rm -f "$RING" "$RING.tmp"       # never read a stale ring from an earlier run
  : > "$LOG"
  if [ "$MODE" = fake ]; then
    "$PY" ui/fake_producer.py --path "$RING" ${PRODUCER_EXTRA[@]+"${PRODUCER_EXTRA[@]}"} >"$LOG" 2>&1 &
  else
    bash -c "exec $ENGINE_CMD" >"$LOG" 2>&1 &
  fi
  BG_PID=$!
}

# 0 = ring ready, 1 = timed out, 2 = the engine exited early
wait_for_ring() {
  local i=0
  while [ "$i" -lt 60 ]; do
    kill -0 "$BG_PID" 2>/dev/null || return 2
    if [ -f "$RING" ] && head -c 8 "$RING" 2>/dev/null | grep -aq CHRONOS1; then
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done
  return 1
}

start_and_wait() {
  echo "Starting the $MODE engine..."
  start_engine
  wait_for_ring
  case $? in
    0) echo "Ring buffer ready: $RING" ;;
    1) echo "--- engine log ($LOG) ---" >&2; cat "$LOG" >&2
       fail "timed out waiting for the ring buffer" ;;
    2) echo "--- engine log ($LOG) ---" >&2; cat "$LOG" >&2
       fail "the engine exited before creating the ring buffer" ;;
  esac
}

# ---------------------------------------------------------- self-test

self_test() {
  "$PY" - "$RING" <<'PY'
import sys, time
sys.path.insert(0, "ui")
import dashboard as d

try:
    s = d.ShmEngineStream(sys.argv[1])
except d.RingError as e:
    print("  FAIL  cannot open the ring:", e)
    sys.exit(1)

time.sleep(1.5)
orders, raw = s.read()
n = len(raw) // d.FILL_STRUCT.size
book = s.book
cap, used, peak = s.ring_stats()

print(f"  ring     {cap:,} slots of {s.slot_size} bytes")
print(f"  events   {n} read in 1.5 s")
print(f"  orders   {orders:,} counted")
print(f"  book     {len(book.bids)} bids / {len(book.asks)} asks, spread {book.spread}")
print(f"  dropped  {s.dropped}")
s.close()

ok = n > 0 and orders > 0 and book.spread is not None
print("  PASS" if ok else "  FAIL  no events, no orders or no book data")
sys.exit(0 if ok else 1)
PY
}

# --------------------------------------------------------------- main

preflight

if [ "$CHECK" = 1 ]; then
  echo "ChronosMatch self-test (mode: $MODE)"
  if [ "$MODE" = mock ]; then
    "$PY" ui/dashboard.py --version || fail "dashboard does not start"
    echo "  PASS  dashboard starts (mock mode has no pipeline to test)"
    exit 0
  fi
  start_and_wait
  self_test
  status=$?
  [ "$status" -eq 0 ] && echo "Self-test passed." || echo "Self-test FAILED. Engine log: $LOG"
  exit "$status"
fi

check_terminal

if [ "$MODE" != mock ]; then
  start_and_wait
fi

echo "Starting the dashboard (press q to quit)..."
sleep 1
"$PY" "${DASH[@]}" ${DASH_EXTRA[@]+"${DASH_EXTRA[@]}"}
status=$?

stop_background
echo
echo "Demo finished (exit code $status)."
[ "$MODE" != mock ] && echo "Engine log: $LOG"
exit "$status"
