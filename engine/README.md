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
