"""Convergence detection across strategy lab cycles.

Modeled on the blogging team's FeedbackTracker
(backend/agents/blogging/blog_writer_agent/feedback_tracker.py).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import List, Optional, Set

from ...models import StrategySpec
from .models import QualityGateResult


class ConvergenceTracker:
    """Track strategy diversity and failure repetition across batch cycles.

    Call ``record()`` after each cycle.  Between cycles, call
    ``get_diversity_directive()`` and ``get_failure_directives()`` to inject
    mandatory steering constraints into the next ideation prompt.
    """

    def __init__(self, window_size: int = 5, max_history: int = 50):
        self._window_size = window_size
        self._signatures: List[Set[str]] = []
        self._failure_modes: Counter[str] = Counter()
        self._asset_class_history: List[str] = []
        self._max_history = max_history
        # Issue #247 — every refinement round across every prior strategy on
        # the same evaluation window counts as one trial for DSR deflation.
        # Incremented explicitly by the orchestrator after each refinement
        # loop completes; ``record()`` does not touch this so parallel cycle
        # snapshots can keep their accounting independent of diversity state.
        self._trial_count: int = 0
        # Issue #269 — baseline captured inside ``snapshot()`` so that
        # ``merge_from`` folds only the delta accumulated during the cycle
        # back into the primary, avoiding double-counting the pre-snapshot
        # trial total. Zero on directly-constructed instances.
        self._trial_count_at_snapshot: int = 0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, spec: StrategySpec, gate_results: List[QualityGateResult]) -> None:
        """Record one completed cycle's strategy and gate outcomes."""
        sig = self._strategy_signature(spec)
        self._signatures.append(sig)
        self._asset_class_history.append(spec.asset_class.lower())

        for g in gate_results:
            if not g.passed:
                self._failure_modes[g.gate_name] += 1

        # Trim to max history
        if len(self._signatures) > self._max_history:
            self._signatures = self._signatures[-self._max_history :]
        if len(self._asset_class_history) > self._max_history:
            self._asset_class_history = self._asset_class_history[-self._max_history :]

    # ------------------------------------------------------------------
    # Stall detection
    # ------------------------------------------------------------------

    def is_stalled(self, threshold: float = 0.80) -> bool:
        """Return True if the last ``window_size`` cycles are converging.

        Uses Jaccard similarity between consecutive strategy signature sets.
        """
        if len(self._signatures) < self._window_size:
            return False

        recent = self._signatures[-self._window_size :]
        for i in range(len(recent) - 1):
            j = _jaccard(recent[i], recent[i + 1])
            if j < threshold:
                return False
        return True

    # ------------------------------------------------------------------
    # Directives for ideation
    # ------------------------------------------------------------------

    def get_diversity_directive(self, tail: int = 10) -> Optional[str]:
        """Return a steering directive if asset-class distribution is skewed."""
        if len(self._asset_class_history) < 3:
            return None

        recent = self._asset_class_history[-tail:]
        counts = Counter(recent)
        total = len(recent)

        over_represented = [ac for ac, c in counts.items() if c / total > 0.4]
        if not over_represented:
            return None

        return (
            f"MANDATORY: The last {total} strategies are heavily skewed toward "
            f"{', '.join(over_represented)}. You MUST choose a DIFFERENT asset class. "
            f"Consider: {', '.join(ac for ac in ['stocks', 'crypto', 'forex', 'commodities', 'futures'] if ac not in over_represented)}."
        )

    def get_failure_directives(self, min_occurrences: int = 3) -> List[str]:
        """Return mandatory constraints for repeatedly failing gate categories."""
        directives: List[str] = []
        for mode, count in self._failure_modes.most_common():
            if count < min_occurrences:
                break
            directives.append(
                f"MANDATORY: Gate '{mode}' has failed {count} times. "
                f"Address this in your strategy design."
            )
        return directives

    def get_stall_directive(self) -> Optional[str]:
        """Return a directive if the tracker detects convergence."""
        if not self.is_stalled():
            return None
        return (
            "WARNING: Strategy ideation is converging — recent strategies are too similar. "
            "MANDATORY: Try a fundamentally different trading thesis, asset class, "
            "or indicator combination."
        )

    # ------------------------------------------------------------------
    # Trial counting (issue #247)
    # ------------------------------------------------------------------

    @property
    def trial_count(self) -> int:
        """Number of refinement rounds observed on the same evaluation window.

        Used as ``n_trials`` in the Deflated Sharpe Ratio computation. See
        issue #247 and the follow-up issue #269 for parallel-batch trial-count
        merging across cycle snapshots.
        """
        return self._trial_count

    def increment_trials(self, n: int = 1) -> None:
        """Add ``n`` refinement rounds to the trial counter.

        Orchestrator should call this after each refinement loop exits so
        the deflation signal reflects every attempt that touched this window.
        """
        if n < 0:
            raise ValueError(f"increment must be non-negative, got {n}")
        self._trial_count += n

    # ------------------------------------------------------------------
    # Snapshot (for parallel wave execution)
    # ------------------------------------------------------------------

    def snapshot(self) -> "ConvergenceTracker":
        """Return a shallow copy suitable for isolated use in a parallel cycle."""
        clone = ConvergenceTracker(window_size=self._window_size, max_history=self._max_history)
        clone._signatures = list(self._signatures)
        clone._failure_modes = Counter(self._failure_modes)
        clone._asset_class_history = list(self._asset_class_history)
        clone._trial_count = self._trial_count
        clone._trial_count_at_snapshot = self._trial_count
        return clone

    def merge_from(self, other: "ConvergenceTracker") -> None:
        """Fold a cycle snapshot's trial-count delta back into this tracker.

        Called at parallel-batch wave completion so the primary tracker
        accumulates the refinement rounds each cycle observed on its own
        snapshot. Without this, DSR deflation during concurrent waves
        sees only prior-wave trials and under-deflates by the current
        wave's sibling increments.

        Only ``_trial_count`` is merged. Diversity state (signatures,
        asset-class history, failure-mode counters) flows back via the
        wave-completion ``record()`` loop in the orchestration layer;
        merging it here would double-count.

        The delta is computed against ``other._trial_count_at_snapshot``
        (captured in ``snapshot()``), so ``self`` need not equal the
        baseline at merge time.
        """
        baseline = other._trial_count_at_snapshot
        delta = other._trial_count - baseline
        if delta < 0:
            raise ValueError(
                f"snapshot trial_count ({other._trial_count}) is below its "
                f"baseline ({baseline}); merge_from expects monotonic increments"
            )
        self._trial_count += delta

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _strategy_signature(spec: StrategySpec) -> Set[str]:
        """Compute a set of hashable tokens representing the strategy's core identity."""
        tokens: Set[str] = set()
        tokens.add(f"ac:{spec.asset_class.lower()}")
        for rule in sorted(spec.entry_rules):
            tokens.add(f"entry:{hashlib.sha256(rule.encode()).hexdigest()[:12]}")
        for rule in sorted(spec.exit_rules):
            tokens.add(f"exit:{hashlib.sha256(rule.encode()).hexdigest()[:12]}")
        return tokens


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)
