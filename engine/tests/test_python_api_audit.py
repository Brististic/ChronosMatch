"""Day 11 tests: the matching hot path must contain no Python C-API calls.

validation/check_python_api_free.py translates the engine with Cython and inspects the C++ it generates.
These tests run that audit (so a future change that adds a Python call to the hot path fails here), and run
it against deliberately broken code to prove the audit can fail.

Run from the engine/ folder (needs Cython installed, as for building):
    python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "validation", "check_python_api_free.py")

try:
    import Cython                                  # noqa: F401
    HAVE_CYTHON = True
except ImportError:                                 # pragma: no cover
    HAVE_CYTHON = False


def load_audit():
    spec = importlib.util.spec_from_file_location("check_python_api_free", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BROKEN_PYX = '''
# cython: language_level=3
cdef int clean_add(int a, int b) noexcept nogil:
    return a + b

cdef int sneaky_python(int x) noexcept nogil:
    with gil:                       # re-acquires the GIL and builds a Python list: NOT allowed on the hot path
        lst = [x, x]
        return <int> len(lst)

def run(int x):
    cdef int r
    with nogil:
        r = clean_add(x, 1)
    return r

def run_bad(int x):
    cdef int r
    with nogil:
        with gil:                   # Python inside a nogil block
            r = len([x])
    return r
'''


@unittest.skipUnless(HAVE_CYTHON, "Cython is not installed")
class TestHotPathIsFreeOfPythonApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_audit()
        cls.audit = cls.mod.run()

    def test_audit_passes(self):
        self.assertEqual(self.audit.violations, [])

    def test_audit_actually_looked_at_the_engine(self):
        names = {short for _, short, _, _ in self.audit.functions}
        for must in ("book_submit_ex", "book_insert", "book_cancel", "idmap_find", "side_find_tick",
                     "level_push_back", "_ring_take", "price_to_tick", "node_alloc"):
            self.assertIn(must, names)
        self.assertGreaterEqual(len(self.audit.functions), 25)
        owners = " ".join(b[0] for b in self.audit.blocks)
        for must in ("drain_core", "run_submit", "submit_batch"):
            self.assertIn(must, owners)

    def test_every_hot_function_is_pure_c(self):
        self.assertTrue(all(not calls for _, _, _, calls in self.audit.functions))

    def test_nogil_blocks_have_at_most_one_gil_probe_and_no_yellow_lines(self):
        for owner, first, last, calls, probes, yellow in self.audit.blocks:
            self.assertEqual(calls, [], owner)
            self.assertLessEqual(probes, 1, owner)
            self.assertEqual(yellow, [], owner)

    def test_report_can_be_built(self):
        report, ok = self.mod.build_report(self.audit, "test")
        self.assertTrue(ok)
        self.assertIn("PASS", report)
        self.assertIn("book_submit_ex", report)


@unittest.skipUnless(HAVE_CYTHON, "Cython is not installed")
class TestAuditCanFail(unittest.TestCase):
    """Negative controls: code that really uses the Python runtime must be flagged."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_audit()
        d = tempfile.mkdtemp(prefix="chronos_audit_test_")
        try:
            pkg = os.path.join(d, "badpkg")
            os.makedirs(pkg)
            open(os.path.join(pkg, "__init__.py"), "w").close()
            with open(os.path.join(pkg, "_bad.pyx"), "w") as f:
                f.write(BROKEN_PYX)
            cls.cpp, cls.html, cls.lines = cls.mod.translate(d, module="badpkg._bad", pyx="_bad.pyx", package="badpkg")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_function_that_regains_the_gil_is_flagged(self):
        rows = {short: calls for _, short, _, calls in self.mod.scan_functions(self.cpp, lambda s, m: True)}
        self.assertTrue(any("sneaky_python" in name for name in rows))
        bad = [calls for name, calls in rows.items() if "sneaky_python" in name][0]
        self.assertTrue(bad, "a nogil function that uses a Python list must show Python API calls")

    def test_clean_function_is_not_flagged(self):
        rows = {short: calls for _, short, _, calls in self.mod.scan_functions(self.cpp, lambda s, m: True)}
        clean = [calls for name, calls in rows.items() if "clean_add" in name]
        self.assertEqual(clean, [[]])

    def test_nogil_block_with_python_inside_is_flagged(self):
        blocks = self.mod.scan_nogil_blocks(self.cpp)
        by_owner = {b[0]: b for b in blocks}
        run_owner = [o for o in by_owner if o.endswith("run") or o == "3run"]
        bad_owner = [o for o in by_owner if "run_bad" in o]
        self.assertTrue(bad_owner, list(by_owner))
        self.assertTrue(by_owner[bad_owner[0]][3], "Python call inside a nogil block must be flagged")
        for o in run_owner:
            self.assertEqual(by_owner[o][3], [])


if __name__ == "__main__":
    unittest.main()
