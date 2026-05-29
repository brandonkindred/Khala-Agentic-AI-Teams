"""Canonical streaming indicator recurrences for the Strategy Lab.

This package houses the host-side indicator math used by:

* :mod:`strategy_lab.factors.primitives` — the reference implementation
  imported by unit tests and the alignment audit. Built from
  :func:`windowed_ema` / :func:`macd_components` directly.
* :mod:`strategy_lab.executor.predicate_evaluator` — ``StreamingHistoryView``
  provides same-bar dedupe over the pandas DataFrame; the
  ``IndicatorRegistry`` is not yet wired through to the view (the
  consumer-side API mismatch is documented in the view's docstring).

The MACD streaming recurrence is implemented in **three independent
sites** (registry's :meth:`IndicatorRegistry._macd_value`, the synthesis
compiler's emitted MACD helper, and the factors compiler's emitted
MACDSignal helper). Each site re-implements the same fingerprint /
classify / single-step / signal-EMA logic — there is no shared
template module today. Parity is enforced by ``tests/test_streaming_indicators.py``
(registry parity vs. legacy reference) and ``tests/test_strategy_compiler.py``
(compiled-output parity vs. fresh cold-compute on the same slice).
Drift between the three implementations IS possible; any change to the
recurrence must land in all three sites in lockstep.
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
