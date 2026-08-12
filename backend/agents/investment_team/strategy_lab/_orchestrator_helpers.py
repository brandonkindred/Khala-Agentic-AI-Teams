"""Pure helpers + outcome dataclasses shared by :mod:`orchestrator` and its
extracted mixins.

These types and functions all live "below" :class:`StrategyLabOrchestrator`
and every mixin module in the dependency graph — ``orchestrator_design.py``,
``orchestrator_synthesis.py``, ``orchestrator_alignment.py``,
``orchestrator_verification.py``, ``orchestrator_record_assembly.py`` — they
take primitive inputs (specs, bar lists, metrics) and return fresh values,
and none of them import from ``orchestrator.py`` or any mixin. Hosting them
in a sibling module keeps each of those files focused on its own cluster's
surface instead of re-deriving these helpers.

Only dataclasses that are genuinely constructed in one cluster and consumed
in another live here (``_DesignAttemptState``, ``_MarketDataFetch``,
``_VerificationOutcome``, ``_AlignmentLoopOutcome``, ``_DesignPersistContext``,
``_DriftCollector``, ``RefinementStallTracker``, ``_CodeSynthesisPhaseResult``,
``_RefinementAlignmentResult``, ``_SynthesisLoopOutcome``). ``_DesignAttemptState``
is the shared ``spec``/``code``/``trades``/``metrics`` base that
``_AlignmentLoopOutcome`` and ``_SynthesisLoopOutcome`` here, plus
``_AnomalyRecoveryOutcome``/``_SynthesisEvaluateResult`` in
``orchestrator_synthesis.py`` and ``_AlignmentRoundOutcome`` in
``orchestrator_alignment.py``, all inherit. Dataclasses used
by only a single mixin file live in that file instead (e.g.
``_DesignLoopOutcome``/``_DesignPhaseResult`` in ``orchestrator_design.py``,
``_AnomalyRecoveryOutcome``/``_SynthesisEvaluateResult`` in
``orchestrator_synthesis.py``, ``_AlignmentRoundOutcome`` in
``orchestrator_alignment.py``) — see ``MIXIN_BOUNDARIES.md`` for the full
audit of which dataclasses were merged, relocated, or left as-is and why.

External callers (``zero_trade_repair.py``, the test suite,
``agents/refinement.py``'s docstring reference) historically imported
these names via ``investment_team.strategy_lab.orchestrator``.
``orchestrator.py`` re-exports them (see the "Re-exports" block near the
end of that file) so existing import sites keep working.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..execution.metrics import build_equity_curve_from_trades
from ..execution.risk_filter import _RISK_LIMIT_TIGHTEN_DIRECTION, RiskLimits
from ..market_data_service import OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .alignment_findings import entry_rule_id, signal_exit_rule_id
from .coverage_probe import run_coverage_stage, should_run_probes
from .exceptions import OrchestratorContractError
from .quality_gates.models import QualityGateResult

if TYPE_CHECKING:
    from .phases import Phase

logger = logging.getLogger(__name__)


def publishability_skip_reason(
    *,
    exit_rule_conformance_passed: bool,
    realism_passed: bool,
    trades_aligned: bool,
    runtime_lookahead_violation: bool,
) -> Optional[str]:
    """Join failing publishability gate codes in veto order.

    Preconditions:
      - Each argument is the boolean verdict from the verification-phase
        gate of the same name (``runtime_lookahead_violation`` is True when
        the harness trapped a forward-field access).
    Postconditions:
      - Returns ``None`` when every gate passes (conformance, realism,
        alignment all True and no runtime look-ahead violation).
      - Otherwise returns a comma-joined string of failing codes in the
        fixed order matching ``_apply_publication_vetoes``:
        ``exit_rule_conformance_failed``, ``realism_failed``,
        ``alignment_unresolved``, ``lookahead_violation``.
    Invariants:
      - Pure: no I/O, no mutation of inputs.
      - Deterministic for the same four booleans.
    """
    parts: List[str] = []
    if not exit_rule_conformance_passed:
        parts.append("exit_rule_conformance_failed")
    if not realism_passed:
        parts.append("realism_failed")
    if not trades_aligned:
        parts.append("alignment_unresolved")
    if runtime_lookahead_violation:
        parts.append("lookahead_violation")
    return ",".join(parts) if parts else None


def _env_flag(name: str, *, default: bool = True) -> bool:
    """Resolve a boolean on/off env toggle.

    Pre: ``name`` is a non-empty env-var name.
    Post: returns ``default`` when the var is unset; otherwise ``True`` only
    for the recognised truthy values ``true``/``1``/``yes`` (case-insensitive),
    ``False`` for anything else. Centralises the truthy-env idiom that the
    strategy-lab toggles share.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _emit_phase_transition(
    emit: Callable[[str, Dict[str, Any]], None],
    *,
    from_phase: "Phase",
    to_phase: Optional["Phase"],
    spec: StrategySpec,
    code: str,
    attempt: int,
) -> None:
    """Emit a :class:`PhaseTransition` event through the orchestrator callback.

    Preconditions:
      - ``emit`` is a no-op-safe phase callback.
      - ``from_phase`` is a member of :data:`PHASES`.
      - ``to_phase`` is the next member of :data:`PHASES` after
        ``from_phase``, or ``None`` for the terminal boundary.
      - ``spec`` is the spec as it exists at the boundary; ``code`` is
        the strategy code as it exists at the boundary (empty string
        before ``CODE_SYNTHESIS`` exits).
      - ``attempt`` is the zero-indexed ``run_cycle`` design-attempt
        counter.

    Postconditions:
      - Emits exactly one event via ``emit`` with name
        :data:`PHASE_TRANSITION_EVENT_NAME` and payload equal to
        ``PhaseTransition.model_dump(mode="json")``.
      - The payload carries SHA-256 ``spec_hash`` and ``code_hash``
        computed by :func:`hash_spec` / :func:`hash_code`.
    """
    from .phases import PHASE_TRANSITION_EVENT_NAME, PhaseTransition, hash_code, hash_spec

    transition = PhaseTransition(
        from_phase=from_phase,
        to_phase=to_phase,
        spec_hash=hash_spec(spec),
        code_hash=hash_code(code),
        attempt=attempt,
    )
    emit(PHASE_TRANSITION_EVENT_NAME, transition.model_dump(mode="json"))


# Cap on how many ``last_order_events`` entries the diagnostics block
# carries through to the refinement prompt. The diagnostics model already
# trims to 20; 10 is enough signal for the LLM to spot the failure pattern
# while keeping the JSON line under ~1 KB.
_DIAGNOSTICS_LAST_EVENTS_CAP = 10


# ──────────────────────────────────────────────────────────────────────────
# Outcome dataclasses returned by the orchestrator's phase methods.
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _MarketDataFetch:
    """Issue #525 — return envelope for ``_fetch_market_data``.

    Carries the OHLCV payload alongside the audit trail of the symbols the
    fetch was asked to retrieve and the symbols that actually returned
    usable bars. Both lists feed ``BacktestRecord`` so reviewers can see
    when a fetch silently dropped tickers without re-running the cycle.

    Issue #533 — ``provider_used`` is a snapshot of
    ``MarketDataService.provider_used`` taken at fetch-completion time and
    filtered to the just-fetched symbols. Snapshotting here (rather than
    reading the service attribute later) is required because the service's
    ``provider_used`` dict is mutable shared state that accumulates across
    fetches; a later cycle's fetch would otherwise pollute earlier rows.

    ``should_break`` is unused by the base fetch (``_fetch_market_data``,
    always ``False``) and is set by the synthesis loop's fetch
    (``_fetch_market_data_for_synthesis``) to signal that the loop should
    short-circuit — no data came back or a critical fetch-coverage failure
    fired. Shared here rather than in a separate envelope since both
    call sites otherwise carry identical fields.
    """

    data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]
    provider_used: Dict[str, str] = field(default_factory=dict)
    should_break: bool = False


@dataclass
class _VerificationOutcome:
    """Bundle of state mutated by ``_run_verification_phase``.

    The verification phase runs walk-forward (or its fallback anomaly
    recheck), exit-rule conformance, resolves ``is_winning`` and
    ``is_publishable``, and augments ``metrics.acceptance_reason`` with
    any veto causes. Returning a dataclass keeps the boundary explicit
    without forcing ``_run_design_attempt`` to learn the internal branches.
    """

    metrics: BacktestResult
    is_winning: bool
    is_publishable: bool
    upstream_admitted: bool
    acceptance_results: List[QualityGateResult]
    walk_forward_failed: bool
    exit_rule_conformance_passed: bool
    publishability_skip_reason: Optional[str] = None


@dataclass
class _DesignAttemptState:
    """Shared spec/code/trades/metrics 4-tuple carried by every per-round
    and per-loop outcome bundle in the design-attempt pipeline.

    This is the minimal common base extracted from five outcome dataclasses
    that each independently declared the same four fields
    (``_AlignmentLoopOutcome``, ``_SynthesisLoopOutcome`` here;
    ``_AnomalyRecoveryOutcome`` / ``_SynthesisEvaluateResult`` in
    ``orchestrator_synthesis.py``; ``_AlignmentRoundOutcome`` in
    ``orchestrator_alignment.py``). A later phase of this refactor may
    extend or rename this class into a fuller threaded per-design-attempt
    context object that orchestrator methods construct and pass directly;
    until that is scoped, this stays the narrow 4-tuple base.

    Preconditions:
      - ``spec`` is the ``StrategySpec`` in effect, or just proposed for the
        next round, at the point this state was captured.
      - ``code`` is the strategy Python source associated with ``spec`` at
        that same point (empty string only before code synthesis has run).
      - ``trades`` and ``metrics`` reflect the most recent completed
        execution known to the caller. In the common case they were
        produced by executing this same ``code`` against this same
        ``spec``; some transitional states instead pair a next-round
        ``spec``/``code`` proposal with the prior round's ``trades``/
        ``metrics`` (e.g. ``_AnomalyRecoveryOutcome``'s generic-refinement
        return, where the refined ``spec``/``code`` haven't executed yet).
        Each subclass's own docstring is authoritative on which case
        applies to its construction sites.

    Postconditions:
      - None beyond dataclass field-type declarations; this is a plain
        data container and performs no validation.

    Invariants:
      - Not frozen: every subclass in this module and its sibling mixin
        modules is a plain (non-frozen) dataclass, and a frozen base
        cannot be subclassed by a non-frozen dataclass.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult


@dataclass
class _AlignmentLoopOutcome(_DesignAttemptState):
    """Bundle of state mutated by ``_run_trade_alignment_loop``.

    The trade-alignment loop can replace the run's known-good
    ``spec`` / ``code`` / ``trades`` / ``metrics`` if it commits a fix,
    and tracks attempt strings + per-round reports the caller consumes.
    Returning a single dataclass keeps ``_run_design_attempt``'s
    unpacking explicit and small.
    """

    alignment_attempts: List[str] = field(default_factory=list)
    alignment_reports: List[Any] = field(default_factory=list)
    trades_aligned: bool = False
    rejection_reason: Optional[str] = None
    # True when the persisted backtest ran on custom code whose
    # predicate-conformance check is non-conforming. Initialised from the
    # synthesis-loop value and re-derived whenever an alignment round commits
    # new code (which replaces the persisted trades but is not otherwise
    # conformance-gated), so it always tracks the code that produced the
    # returned ``trades``/``metrics``.
    ran_on_non_conforming_code: bool = False

    @property
    def alignment_rounds(self) -> int:
        return len(self.alignment_attempts)


@dataclass(frozen=True)
class _DesignPersistContext:
    """Bundle of design-phase audit fields persisted onto the final record.

    Threaded through both ``_build_short_circuit_record`` and
    ``_assemble_record`` so the two persistence sites don't drift apart.
    Empty (rounds=0, critiques=[]) on legacy paths that bypass the
    design loop — pre-design short-circuits, callers from older tests.
    """

    rounds: int = 0
    # ``Any`` to avoid a cycle with ``agents/design_review``; the orchestrator
    # passes a ``List[SpecCritique]`` through.
    critiques: List[Any] = field(default_factory=list)
    # Why the design loop stopped ("ready" | "round_cap" | "stalled" |
    # "budget_exhausted"); empty on legacy paths that bypass the loop.
    stop_reason: str = ""
    # Design-loop telemetry slice carried from ``_DesignLoopOutcome``; merged
    # with gate counts at record-build time. Empty on legacy paths.
    loop_telemetry: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _DriftCollector:
    """Mutable accumulator for spec/code revision history and gate events.

    Threaded through the orchestrator pipeline alongside
    ``_DesignPersistContext``. Each mutation site calls one of the
    ``record_*`` helpers; the orchestrator drains the lists into the
    final ``StrategyLabRecord`` at record-build time.

    Invariants:
      - ``record_spec_change`` is a no-op when before/after hashes match
        (no-op mutation). Same for ``record_code_change``.

    Retry isolation:
      The records are append-only and immutable, so isolating one retry
      attempt does not require deep-copying history — it means handing the
      attempt its own empty collector (``snapshot``) and folding it back into
      the parent commit log (``merge``) once the attempt's fate is known. A
      failed attempt's drift can then be preserved for the diagnostic record
      without poisoning the next attempt's working collector. See
      ``RETRY_STATE_ISOLATION.md``.
    """

    spec_history: list = field(default_factory=list)
    code_history: list = field(default_factory=list)
    gate_timeline: list = field(default_factory=list)

    def snapshot(self) -> "_DriftCollector":
        """Return a fresh, empty collector for an isolated retry attempt.

        Records are append-only and immutable, so a clean working copy is an
        empty collector rather than a deep copy of this one's history. The
        attempt records into the returned child; the caller folds it back in
        with ``merge`` once the attempt succeeds or fails.

        Postconditions:
          - The returned collector's three lists are empty.
          - It shares no list object with ``self`` (mutating the child never
            mutates the parent, and vice versa).
        """
        return _DriftCollector()

    def merge(self, child: "_DriftCollector") -> None:
        """Fold a child collector's records into this one, in order.

        Used at a retry boundary to commit an attempt's drift into the parent
        commit log (on success, so the record reflects the converged attempt;
        or on failure, so the short-circuit diagnostic record still shows what
        every failed attempt tried).

        Preconditions:
          - ``child`` is a ``_DriftCollector``.
        Postconditions:
          - ``self``'s histories contain their prior entries followed by all
            of ``child``'s, preserving order.
          - ``child`` is left unmodified.
        """
        if not isinstance(child, _DriftCollector):
            raise OrchestratorContractError("merge() requires a _DriftCollector")
        self.spec_history.extend(child.spec_history)
        self.code_history.extend(child.code_history)
        self.gate_timeline.extend(child.gate_timeline)

    def record_spec_change(
        self,
        *,
        phase: str,
        agent: str,
        before_spec: "StrategySpec",
        after_spec: "StrategySpec",
        reason: str,
        gate_failures: Optional[List[str]] = None,
    ) -> None:
        """Append a ``SpecRevision`` if the spec actually changed.

        Preconditions:
          - Both specs are constructed ``StrategySpec`` instances.
        Postconditions:
          - If ``hash_spec(before) == hash_spec(after)``, no entry is appended.
          - Otherwise exactly one ``SpecRevision`` is appended.
        """
        from ..models import SpecRevision
        from .phases import hash_spec

        before_hash = hash_spec(before_spec)
        after_hash = hash_spec(after_spec)
        if before_hash == after_hash:
            return

        diff = _unified_diff_json(before_spec, after_spec)
        self.spec_history.append(
            SpecRevision(
                phase=phase,
                agent=agent,
                timestamp=_now_iso(),
                before_hash=before_hash,
                after_hash=after_hash,
                diff=diff,
                reason=reason,
                gate_failures=list(gate_failures or []),
            )
        )

    def record_code_change(
        self,
        *,
        phase: str,
        agent: str,
        before_code: str,
        after_code: str,
        reason: str,
        gate_failures: Optional[List[str]] = None,
    ) -> None:
        """Append a ``CodeRevision`` if the code actually changed.

        Preconditions:
          - ``before_code`` and ``after_code`` are strings (possibly empty).
        Postconditions:
          - If ``hash_code(before) == hash_code(after)``, no entry is appended.
          - Otherwise exactly one ``CodeRevision`` is appended.
        """
        from ..models import CodeRevision
        from .phases import hash_code

        before_hash = hash_code(before_code)
        after_hash = hash_code(after_code)
        if before_hash == after_hash:
            return

        diff = _unified_diff_code(before_code, after_code)
        self.code_history.append(
            CodeRevision(
                phase=phase,
                agent=agent,
                timestamp=_now_iso(),
                before_hash=before_hash,
                after_hash=after_hash,
                diff=diff,
                reason=reason,
                gate_failures=list(gate_failures or []),
            )
        )

    def record_gate(
        self,
        *,
        phase: str,
        gate_name: str,
        passed: bool,
        severity: str,
        details: str,
    ) -> None:
        from ..models import GateEvent

        self.gate_timeline.append(
            GateEvent(
                phase=phase,
                gate_name=gate_name,
                passed=passed,
                severity=severity,
                details=details,
                timestamp=_now_iso(),
            )
        )


def _has_short_period_stall(window: Sequence[Any]) -> bool:
    """True when ``window`` is exactly periodic with some short period.

    Detects both the original "all entries identical" case (period 1) and
    short-period oscillation (e.g. an A/B/A/B... 2-cycle) — any period ``p``
    for which the window repeats with at least two full cycles of evidence.

    Preconditions:
      ``window`` is an ordered, finite sequence of comparable (``==``-able)
      entries, oldest first.
    Postconditions:
      Returns ``False`` for an empty window. Returns ``True`` for a
      single-entry window (trivially "unchanged"). Otherwise returns
      ``True`` iff there exists a period ``p`` in ``[1, len(window) // 2]``
      such that ``window[i] == window[i - p]`` for every ``i >= p`` — i.e.
      the window consists of at least two full repetitions of a length-``p``
      cycle. Returns ``False`` if no such period exists.
    """
    n = len(window)
    if n == 0:
        return False
    if n == 1:
        return True
    for period in range(1, n // 2 + 1):
        if all(window[i] == window[i - period] for i in range(period, n)):
            return True
    return False


@dataclass
class RefinementStallTracker:
    """Rolling-window ``(hash(code), hash(failure_details))`` signature
    tracker for the code-refinement loop (``_run_synthesis_loop``).

    Mirrors ``agents.design_review.CritiqueLedger.is_stalled`` in shape and
    intent — round-over-round non-progress detection — but is not built on
    ``CritiqueLedger`` (no open-issue-id set exists here; refinement
    failures are free-text gate/exec/anomaly details, not structured
    critique issues) and deliberately not built on
    ``quality_gates.convergence_tracker.ConvergenceTracker`` (that tracker
    compares whole-``StrategySpec`` Jaccard signatures *across separate
    ``run_cycle`` invocations* to steer the next cycle's ideation prompt;
    it has no within-loop round history and no notion of "this round's
    code plus this round's failure text"). This class is the documented
    lightweight analogue #1569 asks for: a small, purpose-built,
    ``CritiqueLedger``-shaped tracker scoped to one
    ``_run_synthesis_loop`` invocation.

    Unlike ``CritiqueLedger.is_stalled`` there is no "empty signature never
    counts as stalled" carve-out: ``record`` is only ever called on a
    failing round (the loop only reaches ``_refine_or_exhaust`` on
    failure), so there is no analogous "converged" empty state to guard.
    """

    _history: List[Tuple[str, str]] = field(default_factory=list)

    def record(self, code_hash: str, failure_hash: str) -> None:
        """Append this round's signature to the rolling history.

        Preconditions:
          Called at most once per refinement round, before that round's
          ``is_stalled`` check.
        Postconditions:
          ``rounds_recorded`` increments by exactly 1.
        """
        self._history.append((code_hash, failure_hash))

    def is_stalled(self, n: int) -> bool:
        """True when the last ``n`` recorded signatures show no real progress.

        Recognizes both windows of identical repeats and short-period
        oscillating signatures (e.g. an A/B/A/B... 2-cycle) — see
        :func:`_has_short_period_stall`.

        Preconditions:
          ``n`` is the consecutive-round stall threshold (sub-1 floored).
        Postconditions:
          Returns True only when at least ``n`` rounds have been recorded
          and the last ``n`` ``(code_hash, failure_hash)`` pairs are exactly
          periodic with some period ``p <= n // 2`` (or the window is a
          single round).
        """
        n = max(n, 1)
        if len(self._history) < n:
            return False
        return _has_short_period_stall(self._history[-n:])

    @property
    def rounds_recorded(self) -> int:
        return len(self._history)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _unified_diff_json(before_spec: "StrategySpec", after_spec: "StrategySpec") -> str:
    """Unified diff of sorted-key pretty-printed JSON of two specs."""
    import difflib

    def _canon(spec: "StrategySpec") -> str:
        d = spec.model_dump(mode="json")
        d.pop("strategy_code", None)
        return json.dumps(d, sort_keys=True, indent=2, default=str)

    a = _canon(before_spec).splitlines(keepends=True)
    b = _canon(after_spec).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after"))


def _unified_diff_code(before: str, after: str) -> str:
    """Unified diff of raw code strings."""
    import difflib

    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after"))


def _build_rule_implementation_map(
    spec: "StrategySpec",
    findings: List[Any],
    code: str,
) -> list:
    """Build per-rule trade coverage from alignment findings.

    Preconditions:
      - ``findings`` is a list of ``AlignmentFinding`` instances (or empty).
      - ``spec`` has ``entry_rules``, ``exit_rules`` attributes.
    Postconditions:
      - Returns a list of ``RuleImplementationMap`` instances, one per
        known rule ID derived from the spec plus ``"sizing"``.
      - ``traded_count`` counts distinct trades where ``passed=True``
        for that ``rule_id``.
      - ``code_line_refs`` is best-effort AST; empty on parse failure.
    """
    from collections import defaultdict

    from ..models import RuleImplementationMap

    rule_ids: List[str] = []
    for i, _ in enumerate(getattr(spec, "entry_rules", None) or []):
        rule_ids.append(entry_rule_id(i))
    kind_counts: Dict[str, int] = defaultdict(int)
    # Map suffixed finding IDs to the specific rule instance based on
    # distinguishing attributes (e.g. StopLossRule.basis).
    _suffix_to_instance: Dict[str, str] = {}
    for absolute_idx, er in enumerate(getattr(spec, "exit_rules", None) or []):
        if hasattr(er, "kind"):
            if er.kind == "signal_exit":
                # Signal-exit findings are keyed by the rule's ABSOLUTE
                # ``spec.exit_rules`` index (``signal_exit_rule_id``,
                # shared with ``alignment_checks.py`` and
                # ``RuleFiringRateGate``) — it must match here too, or a
                # spec mixing exit-rule kinds (e.g. ``[StopLossRule,
                # SignalExitRule]``) desyncs this map's canonical
                # ``exit:signal_exit[0]`` (per-kind count) from the
                # findings' ``exit:signal_exit[1]`` (absolute index),
                # silently reporting ``traded_count=0`` for a rule that
                # fired on every trade. Other kinds keep the per-kind
                # counter: the engine's own stop_loss/take_profit
                # reasons are never index-stamped (always ambiguous
                # among same-kind rules), so an arbitrary per-instance id
                # is all any consumer can offer for those anyway.
                canonical = signal_exit_rule_id(absolute_idx)
            else:
                idx = kind_counts[er.kind]
                kind_counts[er.kind] += 1
                canonical = f"exit:{er.kind}[{idx}]"
            rule_ids.append(canonical)
            basis = getattr(er, "basis", None)
            if basis:
                _suffix_to_instance[f"exit:{er.kind}:{basis}"] = canonical
        else:
            rule_ids.append(f"exit[{len([r for r in rule_ids if r.startswith('exit')])}]")
    rule_ids.append("sizing")

    # Canonical keys are now per-instance (e.g. "exit:stop_loss[0]",
    # "exit:signal_exit[1]"). Alignment findings may use unindexed
    # ("exit:stop_loss") or suffixed ("exit:stop_loss:trailing") IDs.
    # Normalise to the canonical form before counting.
    canonical_set = set(rule_ids)
    # Map unindexed kind → first ([0]) canonical instance.
    _kind_to_first: Dict[str, str] = {}
    for rid in rule_ids:
        m = re.match(r"^(exit:\w+)\[(\d+)\]$", rid)
        if m:
            base_kind = m.group(1)
            if base_kind not in _kind_to_first:
                _kind_to_first[base_kind] = rid

    def _normalise(rid: str) -> str:
        if rid in canonical_set:
            return rid
        # Suffixed form with a known instance mapping
        # e.g. "exit:stop_loss:trailing_high" → "exit:stop_loss[1]"
        if rid in _suffix_to_instance:
            return _suffix_to_instance[rid]
        # Strip ":suffix" → "exit:stop_loss:trailing" → "exit:stop_loss"
        parts = rid.split(":")
        base = rid
        if len(parts) >= 3:
            base = ":".join(parts[:2])
            if base in canonical_set:
                return base
        # Try stripping "[N]" index → already-indexed non-canonical
        stripped = re.sub(r"\[\d+\]$", "", rid)
        if stripped != rid and stripped in canonical_set:
            return stripped
        # Unindexed form → first ([0]) canonical instance as best-effort
        first = _kind_to_first.get(base)
        if first:
            return first
        return rid

    passed_trades: Dict[str, set] = defaultdict(set)
    for f in findings:
        if f.rule_id and f.passed and f.computed_value is not None:
            passed_trades[_normalise(f.rule_id)].add(f.trade_num)

    all_rule_ids = list(dict.fromkeys(rule_ids))

    code_refs = _extract_code_line_refs(code, all_rule_ids)

    return [
        RuleImplementationMap(
            rule_id=rid,
            code_line_refs=code_refs.get(rid, []),
            traded_count=len(passed_trades.get(rid, set())),
        )
        for rid in all_rule_ids
    ]


def _extract_code_line_refs(code: str, rule_ids: List[str]) -> Dict[str, List[List[int]]]:
    """Best-effort AST analysis to find code regions matching rule IDs."""
    import ast
    import re

    if not code or not code.strip():
        return {}

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}

    refs: Dict[str, List[List[int]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name_lower = node.name.lower()
        start = node.lineno
        end = node.end_lineno or start
        for rid in rule_ids:
            tag = rid.replace("[", "").replace("]", "").replace(":", "_").lower()
            if tag in name_lower or re.search(re.escape(tag), name_lower):
                refs.setdefault(rid, []).append([start, end])
    return refs


@dataclass
class _CodeSynthesisPhaseResult:
    """Return envelope for ``_run_design_attempt``'s initial code-synthesis.

    ``record`` is a short-circuit ``StrategyLabRecord`` (typed ``Any``) when the
    custom-code synthesis agent failed; the caller returns it. Otherwise it is
    ``None`` and ``code`` / ``original_spec`` / ``original_code`` / ``config``
    carry the synthesized code, the pre-refinement snapshot, and the
    fee-adjusted config into the refinement loop.
    """

    record: Optional[Any]
    code: str = ""
    original_spec: Optional[StrategySpec] = None
    original_code: str = ""
    config: Optional[BacktestConfig] = None


@dataclass
class _RefinementAlignmentResult:
    """Return envelope for ``_run_design_attempt``'s refinement + alignment.

    ``record`` is a short-circuit ``StrategyLabRecord`` (typed ``Any``) when the
    pre-synthesis spec gate failed critically; the caller returns it. Otherwise
    it is ``None`` and the two existing outcome bundles carry every downstream
    field: the post-alignment ``spec`` / ``code`` / ``trades`` / ``metrics`` and
    ``ran_on_non_conforming_code`` / ``alignment_rounds`` / ``trades_aligned`` /
    ``alignment_reports`` live on ``alignment``; the fetched ``market_data`` /
    symbol audit / ``execution_succeeded`` / ``max_rounds_exhausted`` /
    ``open_position_entry_reasons`` / ``runtime_lookahead_violation`` live on
    ``synthesis``.
    """

    record: Optional[Any]
    synthesis: Optional[_SynthesisLoopOutcome] = None
    alignment: Optional[_AlignmentLoopOutcome] = None


@dataclass
class _SynthesisLoopOutcome(_DesignAttemptState):
    """Bundle of state mutated by ``_run_synthesis_loop``.

    The synthesis refinement loop iterates up to ``MAX_CODE_REFINEMENT_ROUNDS``
    rounds of (validate → fetch → execute → trade-collect → evaluate),
    refining ``spec``/``code`` between rounds and short-circuiting on
    fatal failures (market-data unavailable, target-symbol coverage,
    max-rounds exhaustion).

    Returning the full final state keeps the boundary explicit so
    ``_run_design_attempt`` doesn't need to inspect loop internals.

    Invariants on return:
    - ``execution_succeeded=True`` implies ``trades`` reflects a clean
      run with no critical anomalies and ``metrics`` was computed from
      those trades.
    - ``execution_succeeded=False`` implies the loop short-circuited or
      exhausted its rounds; ``trades``/``metrics`` may be empty defaults
      or carry the last failed round's partials.
    - ``market_data`` is ``None`` only when the first ``_fetch_market_data``
      call returned an empty payload — the loop breaks immediately in
      that case and downstream phases skip alignment.
    - ``max_rounds_exhausted`` is mutually exclusive with
      ``execution_succeeded=True``.
    """

    market_data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]
    execution_succeeded: bool
    max_rounds_exhausted: bool
    # Issue #533 — per-symbol provider id, snapshotted at fetch time so it
    # survives later fetches in the same orchestrator run.
    provider_used: Dict[str, str] = field(default_factory=dict)
    open_position_entry_reasons: List[str] = field(default_factory=list)
    # True iff the last code-execution attempt surfaced a runtime
    # ``lookahead_violation`` (the harness's ``AttributeError`` trap on a
    # forward-field access). Threaded into the verification phase so a
    # max-rounds-exhausted run with a persistent lookahead can stamp the
    # cause onto ``acceptance_reason`` rather than the generic
    # ``publication_disabled`` message. Cleared (False) on a clean final
    # round, even when intermediate rounds tripped the trap and were
    # repaired by refinement.
    runtime_lookahead_violation: bool = False
    # True iff the round that produced the persisted ``trades``/``metrics`` ran
    # custom code whose predicate-conformance check was demoted (warning) past
    # the retry budget. Captured at trade-collection time so it tracks the
    # backtest that is actually persisted — a later round that passes
    # conformance but fails execution before collecting new trades leaves this
    # reflecting the earlier demoted round whose backtest still stands.
    ran_on_non_conforming_code: bool = False
    # True iff ``max_rounds_exhausted`` was caused by the refinement-loop
    # stall guard (``RefinementStallTracker``) detecting an unchanged
    # ``(code, failure_details)`` signature for consecutive rounds, rather
    # than the loop genuinely running out of rounds. Lets ``_assemble_record``
    # report ``status="failed: refinement_stalled"`` distinctly from
    # ``"failed: max_refinement_rounds"``.
    refinement_stalled: bool = False


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers used by the orchestrator (and a few external callers).
# ──────────────────────────────────────────────────────────────────────────


def _round_demoted_conformance(round_gate_results: List[QualityGateResult]) -> bool:
    """Whether a single round's predicate-conformance check was demoted.

    Pre: ``round_gate_results`` are the gate results for one synthesis round.
    Post: returns ``True`` iff the round contains a ``predicate_conformance``
    result that did not pass and is a *demotion* warning (``severity ==
    "warning"``, excluding the ``"Fixture unsynthesizable:"`` "could-not-check"
    warning). Evaluated per round so the caller can attribute the verdict to the
    round whose backtest is persisted, rather than to any historical round.
    """
    return any(
        g.gate_name == "predicate_conformance"
        and not g.passed
        and g.severity == "warning"
        and not (g.details or "").startswith("Fixture unsynthesizable:")
        for g in round_gate_results
    )


def _critical_failures(results: Sequence[QualityGateResult]) -> List[QualityGateResult]:
    """The subset of ``results`` that are failed, critical-severity gates.

    Centralizes the ``not passed and severity == "critical"`` idiom
    duplicated across the orchestrator's synthesis/alignment/verification/
    design mixins and ``zero_trade_repair.py``.

    Preconditions:
      - ``results`` is a finite sequence of ``QualityGateResult``.
    Postconditions:
      - Returns a new list containing every element ``g`` of ``results``
        for which ``not g.passed and g.severity == "critical"``, in the
        same relative order as ``results``. Returns ``[]`` when ``results``
        is empty or contains no such element. Does not mutate ``results``.
    """
    return [g for g in results if not g.passed and g.severity == "critical"]


def _has_critical_failures(results: Sequence[QualityGateResult]) -> bool:
    """Whether ``results`` contains at least one failed, critical-severity gate.

    Preconditions:
      - ``results`` is a finite sequence of ``QualityGateResult``.
    Postconditions:
      - Returns ``True`` iff ``results`` contains an element ``g`` with
        ``not g.passed and g.severity == "critical"``; ``False`` otherwise
        (including for an empty ``results``). Equivalent to
        ``bool(_critical_failures(results))`` but short-circuits on the
        first match instead of building the full list.
    """
    return any(not g.passed and g.severity == "critical" for g in results)


def _maybe_attach_coverage_report(
    *,
    metrics: BacktestResult,
    spec: StrategySpec,
    market_data: Dict[str, List[OHLCVBar]],
    config: BacktestConfig,
    exec_result: StrategyRunResult,
) -> None:
    """Run the #451 coverage stage and stamp the report onto ``metrics``.

    The ``spec`` argument MUST carry the same ``strategy_code`` that was
    handed to ``run_strategy_code`` to produce ``exec_result``. The
    alignment and zero-trade-repair paths use a ``proposed_spec`` variant
    of the surrounding spec; pass that, not the loop-level ``spec``,
    otherwise the static probe will analyse stale source.

    No-ops when ``should_run_probes`` says the run isn't zero/low-trade —
    successful runs keep ``metrics.coverage_report = None`` and pay no
    probe cost.
    """
    if should_run_probes(exec_result.execution_diagnostics):
        metrics.coverage_report = run_coverage_stage(
            spec=spec,
            market_data=market_data,
            config=config,
            exec_result=exec_result,
            run_strategy_code_fn=run_strategy_code,
        )


def _attach_execution_diagnostics(
    *,
    metrics: BacktestResult,
    exec_result: StrategyRunResult,
) -> None:
    """Stamp engine-only fields onto ledger-derived ``metrics``.

    ``compute_metrics`` derives ``BacktestResult`` from the closed-trade
    ledger alone and leaves ``execution_diagnostics`` /
    ``cost_stress_results`` at their ``None`` defaults. Those fields live
    only on the ``StrategyRunResult`` (forwarded from ``run_backtest``).
    Without this hand-off:

    * ``ExitRuleConformanceGate`` would see ``None`` diagnostics and treat
      every engine-attributed below-floor stop-loss trade as an unaccounted
      leak.
    * ``CostStressRealismGate`` would see missing cost-stress rows on every
      production run (``cost_stress=True``) and emit a critical finding —
      which publishability gating then treats as a paper-trading veto.

    Preconditions:
        * ``metrics`` and ``exec_result`` are the paired output of the SAME
          backtest execution (same closed-trade ledger). Attaching
          fields from a different run would reconcile one ledger against
          another run's diagnostics / stress rows.
    Postconditions:
        * ``metrics.execution_diagnostics`` is set to
          ``exec_result.execution_diagnostics`` when the exec result carries
          diagnostics; otherwise left unchanged (a populated value is never
          overwritten with ``None``).
        * ``metrics.cost_stress_results`` is set to
          ``exec_result.cost_stress_results`` when present; otherwise left
          unchanged.
    """
    if exec_result.execution_diagnostics is not None:
        metrics.execution_diagnostics = exec_result.execution_diagnostics
    # Duck-typed stubs in tests may omit the field; treat missing as None.
    cost_stress_results = getattr(exec_result, "cost_stress_results", None)
    if cost_stress_results is not None:
        metrics.cost_stress_results = cost_stress_results


def _format_execution_diagnostics(
    diagnostics: Optional[BacktestExecutionDiagnostics],
) -> str:
    """Render a compact JSON block of execution diagnostics for the
    refinement prompt (issue #414, part of #404).

    Returns an empty string when diagnostics is missing or the executor
    couldn't classify a zero-trade failure — healthy backtests must not
    bloat the prompt. When a ``zero_trade_category`` is present, returns a
    single line ``"Execution Diagnostics: {<json>}"`` whose JSON payload is
    stable-key-sorted and compact. ``last_order_events`` is capped to the
    most recent ``_DIAGNOSTICS_LAST_EVENTS_CAP`` entries.
    """
    if diagnostics is None or diagnostics.zero_trade_category is None:
        return ""

    payload = diagnostics.model_dump(mode="json", exclude_none=True)
    events = payload.get("last_order_events") or []
    if len(events) > _DIAGNOSTICS_LAST_EVENTS_CAP:
        payload["last_order_events"] = events[-_DIAGNOSTICS_LAST_EVENTS_CAP:]

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"Execution Diagnostics: {encoded}"


def _apply_veto_to_acceptance_reason(
    metrics: BacktestResult,
    suffix: str,
    *,
    upstream_admitted: bool,
) -> Tuple[BacktestResult, bool]:
    """Stamp a publication veto's cause onto ``metrics.acceptance_reason``.

    Both the conformance veto (#527) and the alignment veto (#529)
    follow the same shape: replace a stale success-style upstream
    reason; append to a real upstream rejection. Returns the updated
    ``metrics`` and ``False`` for the new ``upstream_admitted``, so a
    subsequent veto on the same run appends to this one rather than
    overwriting it.

    The delimiter is ``" | "`` (not ``"; "`` which
    :func:`summarize_acceptance_reason` uses between failing gates)
    so downstream parsers can disambiguate the veto boundary from
    gate-internal boundaries.

    Whitespace-only upstream reasons (``None``, ``""``, ``"   "``)
    collapse to the suffix alone — never produces
    ``"   | <suffix>"`` with an empty left side.
    """
    prior = (metrics.acceptance_reason or "").strip()
    if prior and not upstream_admitted:
        combined = f"{prior} | {suffix}"
    else:
        combined = suffix
    return metrics.model_copy(update={"acceptance_reason": combined}), False


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers (formerly @staticmethod on StrategyLabOrchestrator).
# ──────────────────────────────────────────────────────────────────────────


def _merge_risk_limits_tighten_only(
    current: RiskLimits, proposed: Any
) -> Tuple[RiskLimits, List[str], List[str]]:
    """Tighten-only merge of refinement-proposed risk limits (#543).

    Returns ``(merged_limits, loosened_fields, discarded_unknown_keys)``.

    - ``loosened_fields`` lists fields whose proposed value would loosen
      the limit (raise an "lower"-direction cap, lower a "higher"-direction
      floor, or transition ``target_annual_vol`` between ``None`` and a
      value in either direction — which fundamentally changes the sizing
      model and is treated as loosening).
    - ``discarded_unknown_keys`` lists fields the caller proposed that
      either aren't in the ``RiskLimits`` schema or are marked
      immutable in ``_RISK_LIMIT_TIGHTEN_DIRECTION`` (e.g.
      ``vol_lookback_days``).

    Callers raise ``SpecImplementabilityError`` when ``loosened_fields``
    is non-empty; unknown keys are warned but never trip.
    """
    loosened: List[str] = []
    unknown: List[str] = []
    if not isinstance(proposed, dict):
        return current, loosened, unknown

    merged_data = current.model_dump()
    for key, new_value in proposed.items():
        direction = _RISK_LIMIT_TIGHTEN_DIRECTION.get(key)
        if direction is None:
            # Either unknown to RiskLimits or explicitly immutable.
            unknown.append(key)
            continue

        current_value = merged_data.get(key)

        # Special-case ``target_annual_vol``: ``None`` means "no vol
        # target" (flat sizing). Switching to a value or vice-versa
        # changes the sizing model — treat any None↔value transition
        # as loosening.
        if key == "target_annual_vol":
            if current_value is None and new_value is not None:
                loosened.append(key)
                continue
            if current_value is not None and new_value is None:
                loosened.append(key)
                continue

        # Clearing a numeric cap to ``None`` removes the constraint — a
        # loosening. (Every numeric cap has a non-None default, and the only
        # Optional field, ``target_annual_vol``, is special-cased above, so the
        # converse None→value case cannot arise here; a future Optional cap would
        # need its own explicit handling.)
        if current_value is not None and new_value is None:
            loosened.append(key)
            continue

        try:
            cmp_current = float(current_value) if current_value is not None else None
            cmp_new = float(new_value) if new_value is not None else None
        except (TypeError, ValueError):
            unknown.append(key)
            continue

        if cmp_current is None or cmp_new is None:
            # Already handled above; defensive.
            continue

        if direction == "lower":
            if cmp_new < cmp_current:
                merged_data[key] = new_value
            elif cmp_new > cmp_current:
                loosened.append(key)
            # equal: no-op
        elif direction == "higher":
            if cmp_new > cmp_current:
                merged_data[key] = new_value
            elif cmp_new < cmp_current:
                loosened.append(key)
            # equal: no-op

    try:
        merged = RiskLimits.model_validate(merged_data)
    except Exception:
        # Validation failed on the merged limits — bail out without
        # mutating; surface every proposed key as unknown so the caller
        # logs the full set and keeps the original limits.
        logger.warning(
            "Refined risk_limits failed pydantic validation; keeping current limits unchanged."
        )
        return current, loosened, sorted(set(unknown) | set(proposed.keys()))

    return merged, loosened, unknown


def _daily_returns_from_trades(
    trades: Sequence[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> List[float]:
    """Daily log returns from the equity curve implied by the trades.

    Log basis matches :meth:`EquityCurve.daily_returns` and the rest of
    the metrics module, so OOS-Sharpe / DSR / bootstrap CIs computed
    downstream share the same return convention as the in-sample
    ``compute_performance_metrics`` Sharpe.

    If the equity curve crosses zero (portfolio ruin), the series is
    returned **empty** rather than zero-padding the ruin step. Zeroing
    a wipeout would convert it to a neutral day and let the OOS DSR /
    Sharpe CI / moments report misleadingly low risk; an empty series
    falls through every downstream consumer
    (:func:`summarize_return_moments`, :func:`compute_deflated_sharpe`,
    :func:`bootstrap_sharpe_ci`) as their well-defined "no data" path.
    """
    curve = build_equity_curve_from_trades(
        trades, initial_capital, start_date=start_date, end_date=end_date
    )
    if len(curve.equity) < 2:
        return []
    if any(v <= 0 for v in curve.equity):
        # Ruin: invalidate the whole series.
        return []
    out: List[float] = []
    for i in range(1, len(curve.equity)):
        out.append(math.log(curve.equity[i] / curve.equity[i - 1]))
    return out


def _equity_to_returns(equity: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / prev)
    return out


def _closes_to_equity(closes: Sequence[float], initial_capital: float) -> List[float]:
    if not closes or closes[0] <= 0:
        return []
    scale = initial_capital / closes[0]
    return [c * scale for c in closes]


def _parse_bar_date(d: str) -> Any:
    from datetime import date

    return date.fromisoformat(d[:10])


def _resolve_vix_provider() -> Optional[Callable[[Sequence[Any]], List[float]]]:
    """Always returns None today, so :func:`vix_quartile_subwindows` falls
    back to realized-vol on the benchmark series. ``STRATEGY_LAB_VIX_SOURCE``
    is a reserved hook point for a future production VIX provider (e.g. a
    Yahoo ``^VIX`` fetcher); setting it currently has no effect."""
    source = os.environ.get("STRATEGY_LAB_VIX_SOURCE", "").strip().lower()
    if not source:
        return None
    # Hook point for production providers; unset → realized-vol fallback.
    return None
