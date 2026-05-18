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
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .agents.zero_trade_repair import ZeroTradeRepairReport
from .quality_gates.models import QualityGateResult

if TYPE_CHECKING:  # circular at runtime; only needed for type hints.
    from .orchestrator import StrategyLabOrchestrator

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str, Dict[str, Any]], None]


# Issue #530 — zero-trade repair may only mutate ``risk_limits`` via the
# whitelist; the repair agent must fix the **code**, not weaken the **spec**.
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
    assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
    assert isinstance(code, str), "code must be a str"
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
    """

    def __init__(self, orchestrator: "StrategyLabOrchestrator") -> None:
        # Pre: orchestrator is non-None; the repairer reads gate instances
        # and helper methods (``record_gates``, ``build_orchestrator_gate``) off
        # it. No duplication of those collaborators here.
        assert orchestrator is not None, "orchestrator must be supplied"
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

        Asks the :class:`ZeroTradeRepairAgent` (held on the orchestrator)
        for a targeted code fix based on the deterministic execution
        diagnostics, then gates the proposal through code-safety + a fresh
        backtest + :class:`BacktestAnomalyDetector` before signalling
        commit. Mirrors the alignment loop's break-without-commit posture:
        any failed gate appends a record to ``zero_trade_attempts`` and
        returns ``committed=False`` so the caller falls through to the
        generic :class:`RefinementAgent`.

        Pre: ``spec`` is a StrategySpec; ``code`` is the current code;
        ``exec_result.execution_diagnostics`` must be present.
        Post: returns a ZeroTradeRepairOutcome whose ``new_gates`` reflect
        every gate the proposal exercised, regardless of commit status.
        """
        orch = self._orch
        diagnostics = exec_result.execution_diagnostics
        # Caller is responsible for the routing guard, but be defensive.
        if diagnostics is None or diagnostics.zero_trade_category is None:
            return ZeroTradeRepairOutcome(
                committed=False,
                failure_reason="no zero_trade_category on diagnostics envelope",
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

        try:
            report: ZeroTradeRepairReport = orch.zero_trade_repair_agent.run(
                spec=spec,
                code=code,
                diagnostics=diagnostics,
                coverage_report=coverage_report,
                prior_attempts=zero_trade_attempts,
            )
        except Exception as exc:
            logger.exception("Zero-trade repair agent raised; falling through to refinement")
            zero_trade_attempts.append(
                f"agent_error: {type(exc).__name__}: {str(exc)[:160]}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_skipped",
                    "refinement_round": round_num,
                    "reason": "agent_error",
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False, failure_reason=f"agent_error: {exc}"
            )

        if not report.proposed_code:
            zero_trade_attempts.append(
                f"no_proposal ({report.root_cause_category}): "
                f"{report.evidence[:160] or 'agent declined to propose'}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_skipped",
                    "refinement_round": round_num,
                    "reason": "no_proposed_code",
                    "root_cause_category": report.root_cause_category,
                },
            )
            return ZeroTradeRepairOutcome(committed=False, failure_reason="no_proposed_code")

        # ── Code-safety gate on the proposed code ────────────────────
        # Stamp-only: the caller persists ``safety_gates`` via the outcome's
        # ``new_gates=`` so the orchestrator's running list is not extended here.
        safety_gates = orch.record_gates(
            orch.code_safety_checker.check(report.proposed_code, spec),
            refinement_round=round_num,
            gate_name_prefix="zero_trade_repair_",
        )
        critical_safety = [
            g for g in safety_gates if not g.passed and g.severity == "critical"
        ]
        if critical_safety:
            zero_trade_attempts.append(
                f"unsafe_code ({report.root_cause_category}): "
                f"{'; '.join(g.details for g in critical_safety)[:160]}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_rejected",
                    "refinement_round": round_num,
                    "reason": "unsafe_code",
                    "details": "; ".join(g.details for g in critical_safety)[:400],
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False,
                new_gates=safety_gates,
                failure_reason="unsafe_code",
            )

        # ── Surface any off-list spec keys the agent tried to mutate ─
        # Union the agent's own filter (``report.dropped_spec_update_keys``)
        # with any keys still on ``proposed_spec_updates`` so the warning
        # and the ``zero_trade_repair_dropped_spec_keys`` quality gate fire
        # in both flows and the drift is auditable.
        dropped_spec_keys: List[str] = sorted(
            set(report.dropped_spec_update_keys)
            | {
                k
                for k in (report.proposed_spec_updates or {})
                if k not in _ZERO_TRADE_SPEC_UPDATE_KEYS
            }
        )
        dropped_keys_gates: List[QualityGateResult] = []
        if dropped_spec_keys:
            logger.warning(
                "Zero-trade repair discarded spec-mutating keys %s for round=%s "
                "(post-#530 repair may only adjust risk_limits; fix the code, "
                "not the spec).",
                dropped_spec_keys,
                round_num,
            )
            # Build the gate once so every early-return path carries it
            # forward (the ValidationError path below would otherwise drop
            # the audit trail even though the warning was logged).
            dropped_keys_gates = [
                orch.build_orchestrator_gate(
                    "zero_trade_repair_dropped_spec_keys",
                    phase="synthesis",
                    severity="warning",
                    details=(
                        "Zero-trade repair proposed off-list spec keys "
                        f"{dropped_spec_keys}; dropped per #530 "
                        "(risk_limits only)."
                    ),
                    refinement_round=round_num,
                )
            ]

        # ── Fresh backtest of the proposed code ──────────────────────
        try:
            proposed_spec = _apply_zero_trade_spec_updates(
                spec, report.proposed_spec_updates, report.proposed_code
            )
        except ValidationError as exc:
            # Whitelisted keys can still arrive with the wrong shape (e.g.
            # ``risk_limits`` as a list). Reject the proposal as we would
            # for unsafe code and let the caller fall through to generic
            # refinement instead of aborting the Strategy Lab cycle.
            logger.warning(
                "Zero-trade repair proposal had invalid spec updates: %s", exc
            )
            zero_trade_attempts.append(
                f"invalid_spec_updates ({report.root_cause_category}): "
                f"{str(exc).splitlines()[0][:160]}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_rejected",
                    "refinement_round": round_num,
                    "reason": "invalid_spec_updates",
                    "details": str(exc).splitlines()[0][:400],
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False,
                new_gates=safety_gates + dropped_keys_gates,
                failure_reason="invalid_spec_updates",
            )

        # ── Re-validate the spec after repair-driven mutation ────────
        # The pre-synthesis gate only runs once at ideation. Post-#530,
        # zero-trade repair may only mutate ``risk_limits`` via the whitelist;
        # that single mutation still bypasses the pre-synthesis gate. A
        # Pydantic-valid risk_limits value (e.g. max_position_pct=99) can
        # still be a critical spec failure under StrategySpecValidator.
        # Re-validate before committing; on accept, carry the gates forward
        # in ``new_gates`` so warnings reach the persisted record.
        post_repair_spec_gates: List[QualityGateResult] = []
        if report.proposed_spec_updates:
            # Zero-trade repair runs inside the synthesis refinement loop —
            # re-validate the patched spec under that phase rather than design.
            # Stamp-only; the caller persists via the outcome's ``new_gates=``.
            post_repair_spec_gates = orch.record_gates(
                orch.strategy_validator.validate(proposed_spec, phase="synthesis"),
                refinement_round=round_num,
                gate_name_prefix="zero_trade_repair_",
            )
        # Extend with the pre-built dropped-keys gate so the persisted
        # ``quality_gate_results`` records the attempted spec mutation even
        # when no whitelisted key was present.
        post_repair_spec_gates.extend(dropped_keys_gates)
        spec_criticals = [
            g
            for g in post_repair_spec_gates
            if not g.passed and g.severity == "critical"
        ]
        if spec_criticals:
            zero_trade_attempts.append(
                f"invalid_spec_after_repair ({report.root_cause_category}): "
                f"{'; '.join(g.details for g in spec_criticals)[:160]}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_rejected",
                    "refinement_round": round_num,
                    "reason": "invalid_spec_after_repair",
                    "details": "; ".join(g.details for g in spec_criticals)[:400],
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False,
                new_gates=safety_gates + post_repair_spec_gates,
                failure_reason="invalid_spec_after_repair",
            )

        repair_exec = run_strategy_code(
            report.proposed_code, market_data, config, strategy=proposed_spec
        )
        if not repair_exec.success:
            failure_gate = orch.build_orchestrator_gate(
                "zero_trade_repair_code_execution",
                phase="synthesis",
                details=(
                    f"Re-execution after zero-trade repair failed "
                    f"({repair_exec.error_type}): {repair_exec.stderr[:400]}"
                ),
                refinement_round=round_num,
            )
            zero_trade_attempts.append(
                f"reexec_failed ({report.root_cause_category}): {repair_exec.error_type}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_rejected",
                    "refinement_round": round_num,
                    "reason": "re_execution_failed",
                    "error_type": repair_exec.error_type,
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False,
                new_gates=safety_gates + post_repair_spec_gates + [failure_gate],
                failure_reason=f"re_execution_failed: {repair_exec.error_type}",
            )

        new_trades = repair_exec.trades
        new_metrics = compute_metrics(
            new_trades, config.initial_capital, config.start_date, config.end_date
        )

        # Repair re-execution path also attaches a CoverageReport when the
        # proposed fix produces zero/low trades.
        from .orchestrator import _maybe_attach_coverage_report  # local import — avoids cycle

        _maybe_attach_coverage_report(
            metrics=new_metrics,
            spec=proposed_spec,
            market_data=market_data,
            config=config,
            exec_result=repair_exec,
        )

        # ── Anomaly recheck ──────────────────────────────────────────
        # Stamp-only; ``new_gates`` is persisted by the caller via the outcome.
        new_anomaly_gates = orch.record_gates(
            orch.anomaly_detector.check(
                new_metrics,
                new_trades,
                dsr_aware=config.walk_forward_enabled,
                diagnostics=repair_exec.execution_diagnostics,
                coverage_report=new_metrics.coverage_report,
            ),
            refinement_round=round_num,
            gate_name_prefix="zero_trade_repair_",
        )

        new_critical = [
            g for g in new_anomaly_gates if not g.passed and g.severity == "critical"
        ]
        if new_critical:
            zero_trade_attempts.append(
                f"anomaly_after_repair ({report.root_cause_category}): "
                f"{'; '.join(g.details for g in new_critical)[:160]}"
            )
            emit(
                "coding",
                {
                    "sub_phase": "zero_trade_repair_rejected",
                    "refinement_round": round_num,
                    "reason": "anomaly_after_repair",
                    "details": "; ".join(g.details for g in new_critical)[:400],
                },
            )
            return ZeroTradeRepairOutcome(
                committed=False,
                new_gates=safety_gates + post_repair_spec_gates + new_anomaly_gates,
                failure_reason="anomaly_after_repair",
            )

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
