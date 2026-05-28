"""Canonical streaming indicator recurrences for the Strategy Lab.

This package is the single source of truth for indicator math used by:

* :mod:`strategy_lab.factors.primitives` — the host-side reference
  implementation imported by unit tests and the alignment audit.
* :mod:`strategy_lab.executor.predicate_evaluator` — ``StreamingHistoryView``
  uses :class:`IndicatorRegistry` to amortise per-bar work to ``O(1)`` after
  the initial cold-start, instead of rebuilding a pandas DataFrame and
  rerunning every indicator series on every appended bar.

The recurrences in :mod:`streaming` and the inline text templates consumed
by :mod:`strategy_lab.synthesis.compiler` and
:mod:`strategy_lab.factors.compiler` (in :mod:`templates`) are kept in
lock-step by ``tests/test_streaming_indicators.py``: any drift between a
template and its canonical Python counterpart fails the parity suite.
"""

from __future__ import annotations

from .streaming import (
    NAN,
    IndicatorRegistry,
    macd_components,
    windowed_ema,
)

__all__ = [
    "IndicatorRegistry",
    "NAN",
    "macd_components",
    "windowed_ema",
]
