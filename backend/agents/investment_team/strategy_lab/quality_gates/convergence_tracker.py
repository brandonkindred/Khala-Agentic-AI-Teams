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
            self._signatures = self._signatures[-self._max_history:]
        if len(self._asset_class_history) > self._max_history:
            self._asset_class_history = self._asset_class_history[-self._max_history:]

    # ------------------------------------------------------------------
    # Stall detection
    # ------------------------------------------------------------------

    def is_stalled(self, threshold: float = 0.80) -> bool:
        """Return True if the last ``window_size`` cycles are converging.

        Uses Jaccard similarity between consecutive strategy signature sets.
        """
        if len(self._signatures) < self._window_size:
            return False

        recent = self._signatures[-self._window_size:]
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
    # Snapshot (for parallel wave execution)
    # ------------------------------------------------------------------

    def snapshot(self) -> "ConvergenceTracker":
        """Return a shallow copy suitable for isolated use in a parallel cycle."""
        clone = ConvergenceTracker(window_size=self._window_size, max_history=self._max_history)
        clone._signatures = list(self._signatures)
        clone._failure_modes = Counter(self._failure_modes)
        clone._asset_class_history = list(self._asset_class_history)
        return clone

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
