"""Pure helpers + outcome dataclasses extracted from :mod:`orchestrator`.

These types and functions all live "below" :class:`StrategyLabOrchestrator`
in the dependency graph — they take primitive inputs (specs, bar lists,
metrics) and return fresh values. Hosting them in a sibling module keeps
``orchestrator.py`` focused on the coordinator's surface.

External callers (``zero_trade_repair.py``, the test suite,
``agents/refinement.py``'s docstring reference) historically imported
these names via ``investment_team.strategy_lab.orchestrator``. The
orchestrator re-exports them so existing import sites keep working.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
from .coverage_probe import run_coverage_stage, should_run_probes
from .quality_gates.models import QualityGateResult

logger = logging.getLogger(__name__)

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
    """

    data: Optional[Dict[str, List[OHLCVBar]]]
    requested_symbols: List[str]
    fetched_symbols: List[str]
    provider_used: Dict[str, str] = field(default_factory=dict)


@dataclass
class _VerificationOutcome:
    """Bundle of state mutated by ``_run_verification_phase``.

    The verification phase runs walk-forward (or its fallback anomaly
    recheck), exit-rule conformance, resolves ``is_winning``, and
    augments ``metrics.acceptance_reason`` with any veto causes.
    Returning a dataclass keeps the boundary explicit without forcing
    ``_run_design_attempt`` to learn the internal branches.
    """

    metrics: BacktestResult
    is_winning: bool
    upstream_admitted: bool
    acceptance_results: List[QualityGateResult]
    walk_forward_failed: bool
    exit_rule_conformance_passed: bool


@dataclass
class _AlignmentLoopOutcome:
    """Bundle of state mutated by ``_run_trade_alignment_loop``.

    The trade-alignment loop can replace the run's known-good
    ``spec`` / ``code`` / ``trades`` / ``metrics`` if it commits a fix,
    and tracks attempt strings + per-round reports the caller consumes.
    Returning a single dataclass keeps ``_run_design_attempt``'s
    unpacking explicit and small.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
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


@dataclass
class _AlignmentRoundOutcome:
    """One iteration of ``_run_trade_alignment_loop``.

    Semantics:
    - ``terminate=True`` ⇒ caller breaks the loop. The spec/code/trades/
      metrics fields carry the pre-iteration state (either because the
      audit reported aligned, the proposal was rejected, or the round
      budget is spent).
    - ``terminate=False`` ⇒ caller continues. The spec/code/trades/
      metrics fields carry the just-committed proposal as the new
      known-good state.

    The helper mutates ``alignment_reports``, ``alignment_attempts``,
    and ``all_gate_results`` in place; callers observe those lists
    directly.
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    terminate: bool
    # Set on a committing round (``terminate=False``) to the conformance
    # verdict of the just-committed code; ignored on terminate rounds (which
    # carry the unchanged pre-iteration state).
    ran_on_non_conforming_code: bool = False


@dataclass
class _AnomalyRecoveryOutcome:
    """Bundle of state returned by ``_handle_critical_anomalies``.

    The synthesis loop's evaluation phase delegates to that helper when
    the backtest produces critical anomaly gates. The helper either
    commits a zero-trade-repair proposal, applies a generic refinement,
    or exhausts the round budget — and the loop body needs to know which
    outcome happened so it can continue or break.

    Invariants on return:
    - ``exhausted=True`` ⇒ caller breaks the synthesis loop with
      ``max_rounds_exhausted=True``; the spec/code/trades/metrics fields
      carry the last failed-round values (callers should not commit them).
    - ``exhausted=False`` ⇒ caller continues to the next round; the
      spec/code/trades/metrics/exec_result fields carry the new known-good
      state (either ZTR-committed proposal or generic-refined source).
    """

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
    exec_result: StrategyRunResult
    exhausted: bool
    # Set only when a zero-trade repair commits new code (which replaces the
    # persisted trades but is not otherwise conformance-gated): the conformance
    # verdict of the committed repair code. ``None`` on the generic-refinement
    # path, which leaves ``trades`` unchanged so the round's existing verdict
    # still applies and must not be overwritten.
    ran_on_non_conforming_code: Optional[bool] = None


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
        assert isinstance(child, _DriftCollector), "merge() requires a _DriftCollector"
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
        rule_ids.append(f"entry[{i}]")
    kind_counts: Dict[str, int] = defaultdict(int)
    # Map suffixed finding IDs to the specific rule instance based on
    # distinguishing attributes (e.g. StopLossRule.basis).
    _suffix_to_instance: Dict[str, str] = {}
    for er in getattr(spec, "exit_rules", None) or []:
        if hasattr(er, "kind"):
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
class _DesignLoopOutcome:
    """Bundle returned by ``_run_design_loop``.

    The design loop iterates (DesignAgent → SpecReadinessGate →
    DesignReviewAgent → DesignAgent.revise) until either the reviewer
    marks the spec ready or the configured round budget is exhausted.

    Invariants on return:
    - ``ready=True`` ⇒ ``spec`` passed both the deterministic readiness
      gate and the LLM reviewer on the most recent round; downstream
      code synthesis may proceed.
    - ``ready=False`` ⇒ the round budget exhausted; ``critique_history``
      carries one entry per round (synthetic critiques produced from
      readiness findings count); the orchestrator must short-circuit
      the cycle rather than running code against a not-ready spec.
    - ``rounds`` equals ``len(critique_history)`` in both branches.
    - ``spec`` is the final candidate the loop produced (whether ready
      or not), so the audit trail always carries the spec the cycle
      stopped on.
    - ``budget_exhausted=True`` ⇒ ``ready=False`` and the per-cycle
      LLM-call budget was hit mid-loop; ``spec`` / ``critique_history``
      carry whatever state existed at the trip. It disambiguates the two
      reasons a ``ready=False`` outcome can arise: round-budget exhaustion
      (``False``) versus LLM-call-budget exhaustion (``True``), which the
      orchestrator maps to ``failed: design_not_ready`` and
      ``failed: budget_exhausted`` respectively.
    """

    spec: StrategySpec
    rationale: str
    ready: bool
    rounds: int
    # NB: typed as ``Any`` to avoid a cycle with ``agents/design_review``.
    # The orchestrator passes a ``List[SpecCritique]`` through.
    critique_history: List[Any]
    budget_exhausted: bool = False
    # Why the loop stopped: "ready" | "round_cap" | "stalled" |
    # "budget_exhausted". Disambiguates the not-ready short-circuit status
    # (stall vs honest round-cap exhaustion).
    stop_reason: str = ""
    # Design-loop slice of the persisted telemetry summary (round count,
    # stop reason, critique-ledger totals). Gate counts + the
    # compiled-vs-custom flag are merged in at record-build time.
    loop_telemetry: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _SynthesisLoopOutcome:
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

    spec: StrategySpec
    code: str
    trades: List[TradeRecord]
    metrics: BacktestResult
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


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers used by the orchestrator (and a few external callers).
# ──────────────────────────────────────────────────────────────────────────


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
    """Stamp the engine's execution diagnostics onto ``metrics``.

    ``compute_metrics`` derives ``BacktestResult`` from the closed-trade
    ledger alone and leaves ``execution_diagnostics`` at its ``None``
    default. The structured exit-rule firing counters
    (``exit_rule_firings`` / ``exit_rule_firings_by_symbol``) the engine
    records live only on the ``StrategyRunResult``. Without this hand-off
    the ``ExitRuleConformanceGate`` — which reads
    ``metrics.execution_diagnostics`` — would see ``None`` and treat every
    engine-attributed below-floor stop-loss trade as an unaccounted leak,
    failing conformance on runs the engine actually enforced correctly.

    Preconditions:
        * ``metrics`` and ``exec_result`` are the paired output of the SAME
          backtest execution (same closed-trade ledger). Attaching
          diagnostics from a different run would let the gate reconcile one
          ledger's trades against another run's firing counts.
    Postconditions:
        * ``metrics.execution_diagnostics`` is set to
          ``exec_result.execution_diagnostics`` when the exec result carries
          diagnostics; otherwise ``metrics`` is left unchanged (a populated
          value is never overwritten with ``None``).
    """
    if exec_result.execution_diagnostics is not None:
        metrics.execution_diagnostics = exec_result.execution_diagnostics


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
      floor, or transition ``target_annual_vol`` from ``None`` to a
      value — which fundamentally changes the sizing model and is
      treated as loosening).
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
    """Return a VIX provider callable when ``STRATEGY_LAB_VIX_SOURCE`` is
    set, otherwise None so :func:`vix_quartile_subwindows` falls back to
    realized-vol on the benchmark series. Production deployments can
    wire in a Yahoo ``^VIX`` fetcher here without touching callers."""
    source = os.environ.get("STRATEGY_LAB_VIX_SOURCE", "").strip().lower()
    if not source:
        return None
    # Hook point for production providers; unset → realized-vol fallback.
    return None
