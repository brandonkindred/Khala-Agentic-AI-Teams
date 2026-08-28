"""Zero-trade repair sub-pipeline extracted from the Strategy Lab orchestrator.

When a backtest produces zero trades, the orchestrator can invoke a
specialised :class:`ZeroTradeRepairAgent` to propose a targeted code fix
based on the deterministic execution diagnostics. The proposal then runs
through the same battery of gates a normal refinement round would face
(code-safety + spec re-validation + fresh backtest + anomaly recheck)
before the orchestrator commits it.

That whole flow used to live inline on :class:`StrategyLabOrchestrator`,
threading ~340 lines of business logic and four collaborating gates
through one private method. :class:`ZeroTradeRepairer` owns the
sub-pipeline now; the orchestrator instantiates one repairer in
``__init__`` and delegates with ``self.zero_trade_repairer.try_repair(...)``
at the single call site.

The orchestrator is passed in by reference and the repairer reads the
gate instances and helper methods (``record_gates``,
``build_orchestrator_gate``) off it — no API surface is duplicated, but the
~340 lines of branching now sit in their own module with their own test
boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from ..market_data_service import OHLCVBar
from ..models import (
    BacktestConfig,
    BacktestResult,
    CoverageReport,
    StrategySpec,
    TradeRecord,
)
from ..trade_simulator import compute_metrics
from ..trading_service.modes.sandbox_compat import StrategyRunResult
from .agents._llm_budget import DesignBudgetExhausted
from .agents.zero_trade_repair import ZeroTradeRepairReport
from .exceptions import OrchestratorContractError
from .quality_gates.models import QualityGateResult, join_gate_details

if TYPE_CHECKING:  # circular at runtime; only needed for type hints.
    from .orchestrator import StrategyLabOrchestrator

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


# Issue #530 — zero-trade repair may only mutate ``risk_limits`` via the
# whitelist; the repair agent must fix the **code**, not weaken the **spec**.
# ``risk_limits`` is the one sanctioned spec adjustment, and unlike
# refinement's tighten-only merge a *loosening* is accepted here on purpose:
# over-tight limits are a diagnosed cause of zero-trade outcomes, and the
# repair prompt authorises a limits update only for that diagnosis (that
# condition is enforced in the prompt, not in this code). The committed value
# is still bounded — ``_revalidate_spec`` re-runs the spec validator's range
# checks and the proposal must survive a fresh backtest plus anomaly gates.
# Off-list keys are dropped with a ``logger.warning`` and a
# ``zero_trade_repair_dropped_spec_keys`` quality gate so the drift is
# visible in the persisted ``quality_gate_results``.
_ZERO_TRADE_SPEC_UPDATE_KEYS = frozenset({"risk_limits"})


@dataclass
class ZeroTradeRepairOutcome:
    """Result of one specialized zero-trade repair attempt.

    ``committed=True`` means the proposed code passed code-safety, ran
    cleanly, and produced trades that no longer trip a critical anomaly
    gate; the orchestrator should swap in the new state. ``False`` means
    the caller should fall through to the generic refinement agent so
    the existing loop semantics are preserved.
    """

    committed: bool
    new_code: str = ""
    new_spec: Optional[StrategySpec] = None
    new_trades: List[TradeRecord] = field(default_factory=list)
    new_metrics: Optional[BacktestResult] = None
    new_exec_result: Optional[StrategyRunResult] = None
    new_gates: List[QualityGateResult] = field(default_factory=list)
    failure_reason: str = ""
    changes_made: str = ""


@dataclass
class _RepairCtx:
    """Per-call context passed through ``ZeroTradeRepairer``'s step methods.

    Holds the immutable inputs from ``try_repair`` plus the gate-list
    accumulator that each step appends to before deciding to commit or
    reject. The flow is linear (each step either succeeds-with-state or
    returns a terminating outcome), so bundling the inputs lets the step
    method signatures stay short.
    """

    spec: StrategySpec
    code: str
    market_data: Dict[str, List[OHLCVBar]]
    config: BacktestConfig
    zero_trade_attempts: List[str]
    round_num: int
    emit: PhaseCallback
    coverage_report: Optional[CoverageReport]


def _apply_zero_trade_spec_updates(
    spec: StrategySpec, updates: Optional[Dict[str, Any]], code: str
) -> StrategySpec:
    """Apply whitelisted spec updates from a zero-trade repair report.

    Pre: ``spec`` is a StrategySpec; ``updates`` is a dict or None;
    ``code`` is a string.
    Post: returns a fresh StrategySpec whose ``strategy_code`` is ``code``
    and whose ``risk_limits`` reflect any whitelisted update. Off-list
    keys in ``updates`` are silently dropped (the caller surfaces them
    via a ``zero_trade_repair_dropped_spec_keys`` warning gate).

    Restricts merges to :data:`_ZERO_TRADE_SPEC_UPDATE_KEYS` so an off-list
    LLM hallucination cannot rewrite arbitrary spec fields.
    """
    if not isinstance(spec, StrategySpec):
        raise OrchestratorContractError("spec must be a StrategySpec")
    if not isinstance(code, str):
        raise OrchestratorContractError("code must be a str")
    data = spec.model_dump()
    for key in _ZERO_TRADE_SPEC_UPDATE_KEYS:
        if updates and key in updates:
            data[key] = updates[key]
    data["strategy_code"] = code
    return StrategySpec.model_validate(data)


class ZeroTradeRepairer:
    """Encapsulates the zero-trade repair sub-pipeline.

    Contract: every call to :meth:`try_repair` returns a
    :class:`ZeroTradeRepairOutcome`. On ``committed=True`` the orchestrator
    swaps in ``new_code`` / ``new_spec`` / ``new_trades`` / ``new_metrics``;
    on ``committed=False`` the caller falls through to the generic
    refinement agent. Either way, ``new_gates`` carries the gate results
    produced during the attempt so they reach the persisted record.

    ``try_repair`` reads as a top-to-bottom sequence of step methods, each
    of which either returns ``None`` (continue) or a fully-built
    ``ZeroTradeRepairOutcome`` (terminate). The ``_reject`` helper collapses
    the rejection-emit-and-outcome pattern that fires at every step's
    failure path.
    """

    def __init__(self, orchestrator: "StrategyLabOrchestrator") -> None:
        # Pre: orchestrator is non-None; the repairer reads gate instances
        # and helper methods (``record_gates``, ``build_orchestrator_gate``) off
        # it. No duplication of those collaborators here.
        if orchestrator is None:
            raise OrchestratorContractError("orchestrator must be supplied")
        self._orch = orchestrator

    def try_repair(
        self,
        *,
        spec: StrategySpec,
        code: str,
        exec_result: StrategyRunResult,
        market_data: Dict[str, List[OHLCVBar]],
        config: BacktestConfig,
        zero_trade_attempts: List[str],
        round_num: int,
        emit: PhaseCallback,
        coverage_report: Optional[CoverageReport] = None,
    ) -> ZeroTradeRepairOutcome:
        """Specialized zero-trade repair attempt.

        Asks the :class:`ZeroTradeRepairAgent` for a targeted code fix
        based on the deterministic execution diagnostics, then gates the
        proposal through code-safety + spec re-validation + a fresh
        backtest + :class:`BacktestAnomalyDetector` before signalling
        commit. Any failed gate appends a record to
        ``zero_trade_attempts`` and returns ``committed=False`` so the
        caller falls through to the generic :class:`RefinementAgent`.

        Pre: ``spec`` is a StrategySpec; ``code`` is the current code;
        ``exec_result.execution_diagnostics`` must be present.
        Post: returns a ZeroTradeRepairOutcome whose ``new_gates`` reflect
        every gate the proposal exercised, regardless of commit status.
        """
        diagnostics = exec_result.execution_diagnostics
        if diagnostics is None or diagnostics.zero_trade_category is None:
            # Caller is responsible for the routing guard; be defensive.
            return ZeroTradeRepairOutcome(
                committed=False,
                failure_reason="no zero_trade_category on diagnostics envelope",
            )

        ctx = _RepairCtx(
            spec=spec,
            code=code,
            market_data=market_data,
            config=config,
            zero_trade_attempts=zero_trade_attempts,
            round_num=round_num,
            emit=emit,
            coverage_report=coverage_report,
        )
        emit(
            "coding",
            {
                "sub_phase": "zero_trade_repair_started",
                "refinement_round": round_num,
                "zero_trade_category": diagnostics.zero_trade_category,
                "prior_attempts": len(zero_trade_attempts),
            },
        )

        # Step 1 — ask the agent for a proposal.
        report_or_outcome = self._fetch_proposal(ctx, diagnostics)
        if isinstance(report_or_outcome, ZeroTradeRepairOutcome):
            return report_or_outcome
        report = report_or_outcome

        # Step 2 — code-safety gate on the proposed code.
        safety_gates_or_outcome = self._gate_proposed_code_safety(ctx, report)
        if isinstance(safety_gates_or_outcome, ZeroTradeRepairOutcome):
            return safety_gates_or_outcome
        safety_gates = safety_gates_or_outcome

        # Off-list spec keys → warning gate, carried forward to every
        # subsequent early-return so the audit trail is intact.
        dropped_keys_gates = self._build_dropped_keys_gates(ctx, report)

        # Step 3 — apply whitelisted spec updates, surface ValidationError.
        spec_or_outcome = self._apply_spec_updates(
            ctx, report, safety_gates=safety_gates, dropped_keys_gates=dropped_keys_gates
        )
        if isinstance(spec_or_outcome, ZeroTradeRepairOutcome):
            return spec_or_outcome
        proposed_spec = spec_or_outcome

        # Step 4 — re-validate the spec after the mutation.
        spec_gates_or_outcome = self._revalidate_spec(
            ctx,
            report,
            proposed_spec=proposed_spec,
            safety_gates=safety_gates,
            dropped_keys_gates=dropped_keys_gates,
        )
        if isinstance(spec_gates_or_outcome, ZeroTradeRepairOutcome):
            return spec_gates_or_outcome
        post_repair_spec_gates = spec_gates_or_outcome

        # Step 5 — fresh backtest of the proposed code.
        exec_or_outcome = self._re_execute(
            ctx,
            report,
            proposed_spec=proposed_spec,
            safety_gates=safety_gates,
            post_repair_spec_gates=post_repair_spec_gates,
        )
        if isinstance(exec_or_outcome, ZeroTradeRepairOutcome):
            return exec_or_outcome
        repair_exec, new_trades, new_metrics = exec_or_outcome

        # Step 6 — anomaly recheck on the post-fix backtest.
        anomaly_gates_or_outcome = self._anomaly_recheck(
            ctx,
            report,
            proposed_spec=proposed_spec,
            repair_exec=repair_exec,
            new_trades=new_trades,
            new_metrics=new_metrics,
            safety_gates=safety_gates,
            post_repair_spec_gates=post_repair_spec_gates,
        )
        if isinstance(anomaly_gates_or_outcome, ZeroTradeRepairOutcome):
            return anomaly_gates_or_outcome
        new_anomaly_gates = anomaly_gates_or_outcome

        # All gates passed — commit the proposal.
        change_summary = report.changes_made or f"repair {report.root_cause_category}"
        zero_trade_attempts.append(
            f"committed ({report.root_cause_category}): {change_summary[:160]}"
        )
        emit(
            "coding",
            {
                "sub_phase": "zero_trade_repair_committed",
                "refinement_round": round_num,
                "root_cause_category": report.root_cause_category,
                "changes_made": change_summary,
                "trades_count": len(new_trades),
            },
        )
        return ZeroTradeRepairOutcome(
            committed=True,
            new_code=report.proposed_code,
            new_spec=proposed_spec,
            new_trades=new_trades,
            new_metrics=new_metrics,
            new_exec_result=repair_exec,
            new_gates=safety_gates + post_repair_spec_gates + new_anomaly_gates,
            changes_made=change_summary,
        )

    # ------------------------------------------------------------------
    # Step methods. Each returns ``None`` / a step-specific success value
    # to continue, or a ``ZeroTradeRepairOutcome`` to terminate.
    # ------------------------------------------------------------------

    def _reject(
        self,
        ctx: _RepairCtx,
        *,
        sub_phase: str,
        attempt: str,
        failure_reason: str,
        new_gates: Optional[List[QualityGateResult]] = None,
        extra_event: Optional[Dict[str, Any]] = None,
    ) -> ZeroTradeRepairOutcome:
        """Build a not-committed outcome with the matching emit + attempt log.

        Every step's rejection path follows the same three-step shape: log
        the attempt, emit the sub-phase event, return the outcome. This
        helper folds the duplication so each step's failure branch is one
        call instead of fifteen lines.
        """
        ctx.zero_trade_attempts.append(attempt)
        event: Dict[str, Any] = {
            "sub_phase": sub_phase,
            "refinement_round": ctx.round_num,
        }
        if extra_event:
            event.update(extra_event)
        ctx.emit("coding", event)
        return ZeroTradeRepairOutcome(
            committed=False,
            new_gates=list(new_gates or []),
            failure_reason=failure_reason,
        )

    def _fetch_proposal(
        self, ctx: _RepairCtx, diagnostics: Any
    ) -> "ZeroTradeRepairReport | ZeroTradeRepairOutcome":
        """Call the repair agent. Returns the report or a rejection outcome."""
        orch = self._orch
        try:
            report: ZeroTradeRepairReport = orch.zero_trade_repair_agent.run(
                spec=ctx.spec,
                code=ctx.code,
                diagnostics=diagnostics,
                coverage_report=ctx.coverage_report,
                prior_attempts=ctx.zero_trade_attempts,
            )
        except DesignBudgetExhausted:
            raise
        except Exception as exc:
            logger.exception("Zero-trade repair agent raised; falling through to refinement")
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_skipped",
                attempt=f"agent_error: {type(exc).__name__}: {str(exc)[:160]}",
                failure_reason=f"agent_error: {exc}",
                extra_event={"reason": "agent_error"},
            )

        if not report.proposed_code:
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_skipped",
                attempt=(
                    f"no_proposal ({report.root_cause_category}): "
                    f"{report.evidence[:160] or 'agent declined to propose'}"
                ),
                failure_reason="no_proposed_code",
                extra_event={
                    "reason": "no_proposed_code",
                    "root_cause_category": report.root_cause_category,
                },
            )
        return report

    def _gate_proposed_code_safety(
        self, ctx: _RepairCtx, report: ZeroTradeRepairReport
    ) -> "list[QualityGateResult] | ZeroTradeRepairOutcome":
        """Run code-safety against the proposed code, return gates or outcome."""
        orch = self._orch
        # Stamp-only: the caller persists ``safety_gates`` via the outcome's
        # ``new_gates=`` so the orchestrator's running list isn't extended.
        safety_gates = orch.record_gates(
            orch.code_safety_checker.check(report.proposed_code, ctx.spec),
            refinement_round=ctx.round_num,
            gate_name_prefix="zero_trade_repair_",
        )
        critical_safety = [g for g in safety_gates if not g.passed and g.severity == "critical"]
        if critical_safety:
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_rejected",
                attempt=(
                    f"unsafe_code ({report.root_cause_category}): "
                    f"{'; '.join(g.details for g in critical_safety)[:160]}"
                ),
                failure_reason="unsafe_code",
                new_gates=safety_gates,
                extra_event={
                    "reason": "unsafe_code",
                    "details": join_gate_details(critical_safety),
                },
            )
        return safety_gates

    def _build_dropped_keys_gates(
        self, ctx: _RepairCtx, report: ZeroTradeRepairReport
    ) -> List[QualityGateResult]:
        """Union the agent's filtered-key list with any off-list keys still on
        ``proposed_spec_updates`` and emit a warning gate when non-empty.

        Both sources are checked because the agent-side filter strips
        off-list keys before we see them in production, but tests / future
        bypasses may leave keys on ``proposed_spec_updates``. Logging both
        keeps the audit trail intact.
        """
        dropped_spec_keys = sorted(
            set(report.dropped_spec_update_keys)
            | {
                k
                for k in (report.proposed_spec_updates or {})
                if k not in _ZERO_TRADE_SPEC_UPDATE_KEYS
            }
        )
        if not dropped_spec_keys:
            return []
        logger.warning(
            "Zero-trade repair discarded spec-mutating keys %s for round=%s "
            "(post-#530 repair may only adjust risk_limits; fix the code, "
            "not the spec).",
            dropped_spec_keys,
            ctx.round_num,
        )
        return [
            self._orch.build_orchestrator_gate(
                "zero_trade_repair_dropped_spec_keys",
                phase="synthesis",
                severity="warning",
                details=(
                    "Zero-trade repair proposed off-list spec keys "
                    f"{dropped_spec_keys}; dropped per #530 "
                    "(risk_limits only)."
                ),
                refinement_round=ctx.round_num,
            )
        ]

    def _apply_spec_updates(
        self,
        ctx: _RepairCtx,
        report: ZeroTradeRepairReport,
        *,
        safety_gates: List[QualityGateResult],
        dropped_keys_gates: List[QualityGateResult],
    ) -> "StrategySpec | ZeroTradeRepairOutcome":
        """Apply whitelisted spec updates; turn ValidationError into a rejection."""
        try:
            return _apply_zero_trade_spec_updates(
                ctx.spec, report.proposed_spec_updates, report.proposed_code
            )
        except ValidationError as exc:
            # Whitelisted keys can still arrive with the wrong shape
            # (``risk_limits`` as a list, e.g.). Reject the proposal as we
            # would for unsafe code and let the caller fall through to
            # generic refinement instead of aborting the cycle.
            logger.warning("Zero-trade repair proposal had invalid spec updates: %s", exc)
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_rejected",
                attempt=(
                    f"invalid_spec_updates ({report.root_cause_category}): "
                    f"{str(exc).splitlines()[0][:160]}"
                ),
                failure_reason="invalid_spec_updates",
                new_gates=safety_gates + dropped_keys_gates,
                extra_event={
                    "reason": "invalid_spec_updates",
                    "details": str(exc).splitlines()[0],
                },
            )

    def _revalidate_spec(
        self,
        ctx: _RepairCtx,
        report: ZeroTradeRepairReport,
        *,
        proposed_spec: StrategySpec,
        safety_gates: List[QualityGateResult],
        dropped_keys_gates: List[QualityGateResult],
    ) -> "list[QualityGateResult] | ZeroTradeRepairOutcome":
        """Re-run the spec validator on the patched spec.

        The pre-synthesis gate runs once at ideation; the whitelisted
        ``risk_limits`` mutation bypasses it. A Pydantic-valid value can
        still be a critical spec failure under ``StrategySpecValidator``
        (e.g. ``max_position_pct=99``). Re-validate before committing.
        """
        orch = self._orch
        post_repair_spec_gates: List[QualityGateResult] = []
        if report.proposed_spec_updates:
            # Zero-trade repair runs inside the synthesis refinement loop —
            # re-validate the patched spec under that phase rather than design.
            # Stamp-only; the caller persists via the outcome's ``new_gates=``.
            post_repair_spec_gates = orch.record_gates(
                orch.strategy_validator.validate(proposed_spec, phase="synthesis"),
                refinement_round=ctx.round_num,
                gate_name_prefix="zero_trade_repair_",
            )
        # Extend with the pre-built dropped-keys gate so the persisted
        # ``quality_gate_results`` records the attempted spec mutation even
        # when no whitelisted key was present.
        post_repair_spec_gates.extend(dropped_keys_gates)
        spec_criticals = [
            g for g in post_repair_spec_gates if not g.passed and g.severity == "critical"
        ]
        if spec_criticals:
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_rejected",
                attempt=(
                    f"invalid_spec_after_repair ({report.root_cause_category}): "
                    f"{'; '.join(g.details for g in spec_criticals)[:160]}"
                ),
                failure_reason="invalid_spec_after_repair",
                new_gates=safety_gates + post_repair_spec_gates,
                extra_event={
                    "reason": "invalid_spec_after_repair",
                    "details": join_gate_details(spec_criticals),
                },
            )
        return post_repair_spec_gates

    def _re_execute(
        self,
        ctx: _RepairCtx,
        report: ZeroTradeRepairReport,
        *,
        proposed_spec: StrategySpec,
        safety_gates: List[QualityGateResult],
        post_repair_spec_gates: List[QualityGateResult],
    ) -> "tuple[StrategyRunResult, List[TradeRecord], BacktestResult] | ZeroTradeRepairOutcome":
        """Run the proposed code in the sandbox.

        Returns the ``(exec_result, trades, metrics)`` triple on success,
        or a rejection outcome on exec failure. Also stamps a coverage
        report onto ``new_metrics`` when the post-fix run produced
        zero/low trades.
        """
        repair_exec = self._orch._cached_run_strategy_code(
            report.proposed_code, ctx.market_data, ctx.config, strategy=proposed_spec
        )
        if not repair_exec.success:
            failure_gate = self._orch.build_orchestrator_gate(
                "zero_trade_repair_code_execution",
                phase="synthesis",
                details=(
                    f"Re-execution after zero-trade repair failed "
                    f"({repair_exec.error_type}): {repair_exec.stderr}"
                ),
                refinement_round=ctx.round_num,
            )
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_rejected",
                attempt=(f"reexec_failed ({report.root_cause_category}): {repair_exec.error_type}"),
                failure_reason=f"re_execution_failed: {repair_exec.error_type}",
                new_gates=safety_gates + post_repair_spec_gates + [failure_gate],
                extra_event={
                    "reason": "re_execution_failed",
                    "error_type": repair_exec.error_type,
                },
            )

        new_trades = repair_exec.trades
        new_metrics = compute_metrics(
            new_trades, ctx.config.initial_capital, ctx.config.start_date, ctx.config.end_date
        )
        from ._orchestrator_helpers import _attach_execution_diagnostics

        _attach_execution_diagnostics(metrics=new_metrics, exec_result=repair_exec)

        # Repair re-execution path also attaches a CoverageReport when the
        # proposed fix produces zero/low trades.
        from .orchestrator import _maybe_attach_coverage_report  # local import — avoids cycle

        _maybe_attach_coverage_report(
            metrics=new_metrics,
            spec=proposed_spec,
            market_data=ctx.market_data,
            config=ctx.config,
            exec_result=repair_exec,
        )
        return repair_exec, new_trades, new_metrics

    def _anomaly_recheck(
        self,
        ctx: _RepairCtx,
        report: ZeroTradeRepairReport,
        *,
        proposed_spec: StrategySpec,  # noqa: ARG002  — kept for symmetry
        repair_exec: StrategyRunResult,
        new_trades: List[TradeRecord],
        new_metrics: BacktestResult,
        safety_gates: List[QualityGateResult],
        post_repair_spec_gates: List[QualityGateResult],
    ) -> "list[QualityGateResult] | ZeroTradeRepairOutcome":
        """Run the anomaly detector on the post-fix backtest.

        Returns the new anomaly gates on success or a rejection outcome on
        critical anomaly.
        """
        orch = self._orch
        # Stamp-only; ``new_gates`` is persisted by the caller via the outcome.
        new_anomaly_gates = orch.record_gates(
            orch.anomaly_detector.check(
                new_metrics,
                new_trades,
                dsr_aware=ctx.config.walk_forward_enabled,
                diagnostics=repair_exec.execution_diagnostics,
                coverage_report=new_metrics.coverage_report,
            ),
            refinement_round=ctx.round_num,
            gate_name_prefix="zero_trade_repair_",
        )
        new_critical = [g for g in new_anomaly_gates if not g.passed and g.severity == "critical"]
        if new_critical:
            return self._reject(
                ctx,
                sub_phase="zero_trade_repair_rejected",
                attempt=(
                    f"anomaly_after_repair ({report.root_cause_category}): "
                    f"{'; '.join(g.details for g in new_critical)[:160]}"
                ),
                failure_reason="anomaly_after_repair",
                new_gates=safety_gates + post_repair_spec_gates + new_anomaly_gates,
                extra_event={
                    "reason": "anomaly_after_repair",
                    "details": join_gate_details(new_critical),
                },
            )
        return new_anomaly_gates
