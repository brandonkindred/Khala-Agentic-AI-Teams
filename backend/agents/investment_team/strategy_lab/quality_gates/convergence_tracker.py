"""Convergence detection across strategy lab cycles.

Modeled on the blogging team's FeedbackTracker
(backend/agents/blogging/blog_writer_agent/feedback_tracker.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import List, Optional, Set

from ...models import StrategySpec
from .models import QualityGateResult

logger = logging.getLogger(__name__)

# The sentence :meth:`ConvergenceTracker.get_diversity_directive` embeds to
# demand a change of asset class. Owned here (rather than duplicated as a string
# literal at the consumer) so a reword cannot silently break the pin's
# suppression filter: both the text and the predicate that recognises it live in
# this one place.
#
# Deliberately does NOT cover ``get_stall_directive``. That directive offers
# three alternatives — "a fundamentally different trading thesis, asset class,
# or indicator combination" — two of which a pinned attempt can satisfy, so
# suppressing it would strip a pinned run of all anti-repetition steering. And
# a pinned run is *more* prone to stalling, since the pin also scopes the
# designer's prior-results context to a single category.
ASSET_CLASS_ONLY_STEERING_PHRASE = "You MUST choose a DIFFERENT asset class"


def is_asset_class_steering_directive(directive: str) -> bool:
    """True when ``directive`` contains the asset-class-only steering phrase.

    A design attempt pinned to a single asset category cannot satisfy an
    asset-class-only instruction — the pin's exclusion list already forbids
    every other class — so the caller drops such a directive rather than
    handing the designer a prompt that mandates both "use only X" and "use
    something other than X". This is a plain substring check, not a parse of
    the directive's structure: it relies on :data:`ASSET_CLASS_ONLY_STEERING_PHRASE`
    only ever appearing in the asset-class-only directive
    (:meth:`ConvergenceTracker.get_diversity_directive`) and never in a
    directive that offers other satisfiable alternatives (e.g.
    :meth:`ConvergenceTracker.get_stall_directive`, whose wording
    deliberately avoids the exact phrase) — it does not itself verify that
    the directive contains nothing else.

    Preconditions:
      - ``directive`` is a string (a rendered convergence-tracker directive).
    Postconditions:
      - Returns ``True`` iff :data:`ASSET_CLASS_ONLY_STEERING_PHRASE` occurs in
        ``directive``.
    """
    return ASSET_CLASS_ONLY_STEERING_PHRASE in directive


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

    def record(
        self,
        spec: StrategySpec,
        gate_results: List[QualityGateResult],
        *,
        count_asset_class: bool = True,
    ) -> None:
        """Record one cycle's strategy and gate outcomes.

        Failure modes count each cycle that failed a gate, not each
        failed row. Gates like ``DeterministicAlignmentChecker`` emit
        one row per trade × per check, so a single misaligned cycle
        can produce dozens of failing rows under the same gate name;
        crediting every row would prematurely trip
        ``get_failure_directives(min_occurrences=3)`` after one bad
        cycle. Deduping by ``gate_name`` per call gives each cycle at
        most one count per distinct failing gate — the right semantic
        for cross-cycle failure frequency.

        ``count_asset_class`` (default ``True``) controls whether the
        spec's asset class feeds the diversity-steering history. Set it
        ``False`` for short-circuited cycles that never reached a
        backtest: an unsupported class (e.g. ``bonds``) is coerced to a
        schema-valid placeholder (``stocks``) before the redesign route,
        so counting it would skew ``get_diversity_directive`` toward a
        false "heavily stocks" signal even though no stock strategy ran.
        Signatures and failure modes are still recorded so stall and
        failure-frequency detection see the failed attempt.
        """
        sig = self._strategy_signature(spec)
        self._signatures.append(sig)
        if count_asset_class:
            self._asset_class_history.append(spec.asset_class.lower())

        # Iterate sorted so insertion order into ``_failure_modes`` is
        # deterministic across interpreter runs. ``Counter.most_common``
        # breaks ties by first-seen order; without sorting, equal-count
        # gates could appear in different order across runs and produce
        # nondeterministic ideation prompts / snapshot-test flakes.
        failed_gate_names = {g.gate_name for g in gate_results if not g.passed}
        for gate_name in sorted(failed_gate_names):
            self._failure_modes[gate_name] += 1

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

    def _recent_asset_class_history(self, tail: int) -> List[str]:
        """The windowed slice both diversity methods key their computation on.

        Pre: none beyond the type constraint.
        Post: the last ``tail`` entries of ``_asset_class_history``, or the
        full history when ``tail <= 0`` (matching the ``[-0:]``-returns-
        everything pitfall documented on the two callers). Centralising this
        one-line slice keeps ``get_diversity_avoid_classes`` and
        ``get_diversity_directive`` from independently recomputing the same
        window — and risking drift between them — every time either is called.
        """
        if tail > 0:
            return self._asset_class_history[-tail:]
        return list(self._asset_class_history)

    def _over_represented_classes(self, recent: List[str]) -> Set[str]:
        """Shared over-representation predicate given an already-sliced window.

        Pre: ``recent`` is the caller's windowed slice (typically
        :meth:`_recent_asset_class_history`'s return).
        Post: returns an empty set when fewer than 3 entries have been
        recorded across the *full* history (not just ``recent``), or when no
        class exceeds the 40% share of ``recent``.
        """
        if len(self._asset_class_history) < 3:
            return set()
        counts = Counter(recent)
        total = len(recent)
        return {ac for ac, c in counts.items() if c / total > 0.4}

    def get_diversity_avoid_classes(self, tail: int = 10) -> Set[str]:
        """Return the asset classes ``get_diversity_directive`` would tell the
        designer to avoid — over-represented in the recent window (>40% share).

        Single source of truth for the over-representation computation, shared
        by :meth:`get_diversity_directive` (renders it as prompt text) and any
        caller (e.g. a per-attempt asset-category pin) that needs to steer
        *around* the same skew without re-deriving it from the raw history.

        Args:
            tail: number of recent asset-class entries to consider.
                Non-positive values are treated as the full history (see
                :meth:`_recent_asset_class_history`) rather than the ``[-0:]``
                slice, which returns everything rather than nothing.

        Postconditions:
          - Returns an empty set when the asset-class history contains fewer
            than 3 entries, or when no class exceeds the 40% share threshold.
        """
        return self._over_represented_classes(self._recent_asset_class_history(tail))

    def get_diversity_directive(self, tail: int = 10) -> Optional[str]:
        """Return a steering directive if asset-class distribution is skewed.

        Args:
            tail: forwarded to :meth:`get_diversity_avoid_classes` (see its
                behavior note on non-positive values).

        Postconditions:
          - Returns ``None`` when no class is over-represented; otherwise a
            directive naming the over-represented classes in alphabetical
            order and containing :data:`ASSET_CLASS_ONLY_STEERING_PHRASE`, so
            :func:`is_asset_class_steering_directive` recognises it and a
            pinned attempt suppresses it as unsatisfiable.
        """
        recent = self._recent_asset_class_history(tail)
        over_represented = self._over_represented_classes(recent)
        if not over_represented:
            return None

        total = len(recent)
        return (
            f"MANDATORY: The last {total} strategies are heavily skewed toward "
            f"{', '.join(sorted(over_represented))}. {ASSET_CLASS_ONLY_STEERING_PHRASE}. "
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
        """Return a directive if the tracker detects convergence.

        Deliberately offers three alternatives rather than demanding an
        asset-class change alone, so it stays satisfiable — and therefore
        survives the suppression filter — under a single-category pin, where
        anti-repetition steering matters most (the pin also narrows the
        designer's prior-results context to one category).

        Preconditions: none.
        Postconditions:
          - Returns ``None`` unless :meth:`is_stalled`; otherwise a directive
            that does NOT contain
            :data:`ASSET_CLASS_ONLY_STEERING_PHRASE`, so
            :func:`is_asset_class_steering_directive` leaves it in place.
        """
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

    # ------------------------------------------------------------------
    # Wire serialization (for crossing a Temporal activity/workflow boundary)
    # ------------------------------------------------------------------

    def to_wire_dict(self) -> dict:
        """Serialize this tracker to a JSON-safe dict.

        Kept on the class (rather than reaching into private attributes from an
        external module) so the wire contract stays co-located with the internal
        representation it depends on.

        Postconditions:
            Returns a dict round-trippable by :meth:`from_wire_dict` into an
            equivalent tracker — same window/history size, signatures,
            failure-mode counts, asset-class history, trial count, and
            snapshot-baseline trial count (the last is required for
            :meth:`merge_from` to fold only the per-cycle delta after a round
            trip). Sets are emitted as sorted lists so the output is
            deterministic across runs.
        """
        return {
            "window_size": self._window_size,
            "max_history": self._max_history,
            "signatures": [sorted(sig) for sig in self._signatures],
            "failure_modes": dict(self._failure_modes),
            "asset_class_history": list(self._asset_class_history),
            "trial_count": self._trial_count,
            "trial_count_at_snapshot": self._trial_count_at_snapshot,
        }

    @classmethod
    def from_wire_dict(cls, data: dict) -> "ConvergenceTracker":
        """Reconstruct a tracker from :meth:`to_wire_dict`'s output.

        Preconditions:
            ``data`` is either ``{}`` (yields a fresh tracker) or a dict
            produced by :meth:`to_wire_dict`.
        Postconditions:
            Returns a tracker with state equivalent to the serialized one.
            ``trial_count_at_snapshot`` defaults to ``trial_count`` for dicts
            predating that field, so a round trip is never worse than treating
            the whole count as the snapshot baseline.
        """
        tracker = cls(
            window_size=data.get("window_size", 5),
            max_history=data.get("max_history", 50),
        )
        tracker._signatures = [set(sig) for sig in data.get("signatures", [])]
        tracker._failure_modes = Counter(data.get("failure_modes", {}))
        tracker._asset_class_history = list(data.get("asset_class_history", []))
        tracker._trial_count = data.get("trial_count", 0)
        tracker._trial_count_at_snapshot = data.get("trial_count_at_snapshot", tracker._trial_count)
        return tracker

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
            logger.warning(
                "ConvergenceTracker.merge_from: snapshot trial_count (%d) is below "
                "its baseline (%d); refusing to apply negative delta",
                other._trial_count,
                baseline,
            )
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
        """Compute a set of hashable tokens representing the strategy's core identity.

        Issue #551/#552: rule fields are now structured DSL nodes. We hash a
        canonical-JSON view (sort_keys, no whitespace) of each rule so the
        token set is deterministic and stable across runs without depending
        on dict ordering. The shape is intentionally narrower than
        ``model_dump_json`` — only ``kind`` + the field values — but Pydantic
        already enforces the schema and ``sort_keys=True`` makes it
        reproducible.
        """
        tokens: Set[str] = set()
        tokens.add(f"ac:{spec.asset_class.lower()}")

        def _canon(rule) -> str:
            return json.dumps(rule.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

        for rule in sorted((_canon(r) for r in spec.entry_rules)):
            tokens.add(f"entry:{hashlib.sha256(rule.encode()).hexdigest()[:12]}")
        for rule in sorted((_canon(r) for r in spec.exit_rules)):
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
