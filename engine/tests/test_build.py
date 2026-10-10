"""Day 1 smoke test: does the compiled extension import and behave?

Run from the engine/ folder after building:
    python -m unittest discover -s tests -v
"""
import unittest

import chronos_engine


class TestBuild(unittest.TestCase):
    def test_extension_is_compiled(self):
        import chronos_engine._matching as m
        self.assertTrue(m.__file__.endswith((".so", ".pyd")),
                        "expected a compiled extension, got " + m.__file__)

    def test_cpp17_or_msvc_default(self):
        std = chronos_engine.cpp_standard()
        self.assertTrue(std >= 201703 or std == 199711,  # MSVC reports 199711 unless /Zc:__cplusplus
                        f"unexpected __cplusplus value {std}")

    def test_nogil_function(self):
        self.assertEqual(chronos_engine.selftest_nogil(40, 2), 42)

    def test_build_info(self):
        info = chronos_engine.build_info()
        self.assertEqual(info["abi"], 1)
        self.assertTrue(info["nogil_selftest"])


if __name__ == "__main__":
    unittest.main()
