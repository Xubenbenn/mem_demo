"""Run the v2 contract and integration regressions: python test_modes.py.

The original order-sensitive v1 characterization suite is preserved verbatim in
 docs/legacy/test_modes_v1.py. New tests assert alias-aware accounting, bounded
coverage and lifecycle behavior instead of a particular greedy root winning.
"""
import os
import unittest

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.defaultTestLoader.discover(os.path.join(here, "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
