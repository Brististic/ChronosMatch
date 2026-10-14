"""
ChronosMatch - Cython matching engine build script
Owner : Member 2 (Core Quant Engine Engineer)  |  Branch: feature/cython-engine

Build in place (development):
    python setup.py build_ext --inplace

Optional environment switches:
    CHRONOS_ANNOTATE=1   write <module>.html reports (yellow lines = Python C-API calls;
                         used from Day 11 to strip Python interaction from the hot loop)
    CHRONOS_NATIVE=1     add -march=native (fastest on THIS machine, not portable)
    CHRONOS_DEBUG=1      build with -O0 -g and force a clean rebuild
"""
import os
import sys

from setuptools import Extension, setup

try:
    from Cython.Build import cythonize
except ImportError:  # pragma: no cover
    sys.exit("Cython is required: pip install -r requirements-dev.txt")


def flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


# --------------------------------------------------------------------------
# Compiler flags (C++17, optimised, platform aware)
# --------------------------------------------------------------------------
if sys.platform == "win32":                       # MSVC
    compile_args = ["/std:c++17", "/Od" if flag("CHRONOS_DEBUG") else "/O2"]
    link_args = []
else:                                             # GCC / Clang (Linux, macOS)
    compile_args = ["-std=c++17", "-Wall", "-Wno-unused-function"]
    if flag("CHRONOS_DEBUG"):
        compile_args += ["-O0", "-g"]
    else:
        compile_args += ["-O3", "-fno-strict-aliasing"]
        if flag("CHRONOS_NATIVE"):
            compile_args.append("-march=native")
    link_args = []
    if sys.platform == "darwin":
        compile_args.append("-stdlib=libc++")
        link_args.append("-stdlib=libc++")

# --------------------------------------------------------------------------
# Extension modules  (new .pyx files are added to this list on later days)
# --------------------------------------------------------------------------
extensions = [
    Extension(
        name="chronos_engine._matching",
        sources=["chronos_engine/_matching.pyx"],
        language="c++",
        extra_compile_args=compile_args,
        extra_link_args=link_args,
    ),
]

# --------------------------------------------------------------------------
# Cython compiler directives: strip Python safety checks from C-level code
# --------------------------------------------------------------------------
directives = {
    "language_level": "3",
    "boundscheck": False,        # no index checks on C arrays / memoryviews
    "wraparound": False,         # no negative-index handling
    "cdivision": True,           # C semantics for / and % (no ZeroDivisionError check)
    "initializedcheck": False,   # no "memoryview initialised?" checks
    "nonecheck": False,
    "embedsignature": True,      # keep signatures visible in help()
}

setup(
    name="chronos-engine",
    version="0.4.0",
    description="ChronosMatch - Cython limit order book matching engine",
    packages=["chronos_engine"],
    package_data={"chronos_engine": ["*.pxd"]},   # ship headers so other modules can cimport them
    ext_modules=cythonize(
        extensions,
        compiler_directives=directives,
        annotate=flag("CHRONOS_ANNOTATE"),
        force=flag("CHRONOS_DEBUG"),
    ),
    python_requires=">=3.9",
    zip_safe=False,
)
