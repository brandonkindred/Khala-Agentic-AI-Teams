"""Single source of truth for the engine's trailing-history retention ceiling.

The production ``StrategyContext`` (and the conformance shadow context) keeps only
the most recent :data:`STREAMING_WINDOW_BARS` bars per symbol. Every layer that must
compute indicators over the *same* trailing window the runtime traded on references
this one constant instead of a private literal:

* ``trading_service.strategy.contract.StrategyContext._ingest_bar`` — the production
  retention cap (imported sandbox-first / package-fallback, since this module is
  also copied flat into the strategy subprocess by ``StreamingHarness``).
* ``executor.predicate_evaluator`` — the alignment/coverage walk (``_SERIES_WINDOW``)
  and the ``StreamingHistoryView`` deque (``max_bars``).
* ``synthesis.compiler`` — the history depth requested for cumulative indicators
  (``_VWAP_HISTORY``).
* ``quality_gates.predicate_conformance`` — the shadow context's per-symbol history cap.

Cumulative indicators (``vwap``, ``obv``) re-base to the window start, so if any one
site were retuned and another missed, they would silently compute over different
windows in validation vs. runtime. Deriving all sites from this constant makes that
class of divergence unrepresentable.
"""

from __future__ import annotations

# Trailing bars retained per symbol by the engine/shadow context.
STREAMING_WINDOW_BARS: int = 500
