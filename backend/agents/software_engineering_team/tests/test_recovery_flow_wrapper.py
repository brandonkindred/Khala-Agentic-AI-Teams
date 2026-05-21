"""Wrapper that imports test_recovery_flow.py (in shared/) so its TestCase classes
are picked up by pytest and counted toward coverage.

The original file is a standalone script (`if __name__ == "__main__"`) and lives
under shared/ rather than tests/. This wrapper exercises every test case it
defines plus its `run_tests` helper.
"""

from __future__ import annotations

import sys
import unittest

from software_engineering_team.shared import test_recovery_flow as recovery_mod


def _run_testcase(klass):
    """Run a unittest.TestCase via the unittest loader and assert success."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(klass)
    runner = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w"))
    result = runner.run(suite)
    assert result.wasSuccessful(), f"{klass.__name__} failed: {result.failures + result.errors}"


def test_continuation_module_tests():
    _run_testcase(recovery_mod.TestContinuationModule)


def test_post_mortem_module_tests():
    _run_testcase(recovery_mod.TestPostMortemModule)


def test_decomposition_context_tests():
    _run_testcase(recovery_mod.TestDecompositionContext)


def test_run_tests_helper():
    """Exercises the run_tests() helper that orchestrates the suite."""
    # Redirect output so the test log isn't noisy
    orig_stderr = sys.stderr
    try:
        sys.stderr = open("/dev/null", "w")
        rc = recovery_mod.run_tests()
    finally:
        sys.stderr = orig_stderr
    assert rc == 0
