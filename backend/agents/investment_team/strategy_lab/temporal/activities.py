"""Temporal activities for the Strategy Lab (fine-grained, per-side-effect).

Every activity wraps exactly one side-effecting call already made by
``StrategyLabOrchestrator`` / the strategy-lab agent classes / the
investment team's API layer: an LLM call, a sandboxed backtest execution, a
market-data fetch, or a durable-store write. Activities never re-implement
business logic — each one reconstructs the relevant Pydantic model(s) from a
JSON-shaped payload, calls the existing method verbatim, and translates the
result (and any failure) back to a wire-shaped ``dict`` / a temporalio
``ApplicationError``.

Sandbox-safety note: unlike ``workflows.py``, this module is never replayed
by the temporalio workflow sandbox (activities always run in the activity
executor), so top-level ``os.getenv`` usage would be safe here. Every
activity still uses **lazy imports** for ``investment_team``/``strategy_lab``
modules regardless, mirroring
``market_research_team/temporal/workflows.py`` — this keeps the module
(and its ``ACTIVITIES`` list) importable without pulling in the full
strategy-lab dependency graph (strands, market-data providers, ...) at
worker-process boot.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)


def _map_exception_to_application_error(exc: Exception) -> ApplicationError:
    """Translate an exception raised inside an activity body into an ``ApplicationError``.

    Preconditions:
        ``exc`` was raised by a strategy-lab agent-class method invoked from
        within an activity.
    Postconditions:
        Returns (does not raise) an ``ApplicationError``. ``non_retryable`` is
        ``True`` when ``exc`` is a ``StrategyLabLLMError`` whose ``outcome``
        is ``"fatal"`` (mirrors ``classify_strands_exception``'s fatal
        classification — retrying the same call cannot help) or any
        non-LLM parse/validation failure raised directly by an agent method
        (also not resolved by a bare retry). ``non_retryable`` is ``False``
        only for a ``StrategyLabLLMError`` whose ``outcome`` is
        ``"exhausted"`` or ``"budget_exhausted"`` — the in-activity envelope
        already spent its own retry budget, so a bounded extra
        Temporal-level attempt only helps recover from a genuine worker
        crash mid-envelope, not re-run the whole backoff loop.
    """
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    if isinstance(exc, StrategyLabLLMError):
        non_retryable = exc.outcome == "fatal"
        return ApplicationError(
            str(exc), type=exc.outcome or "StrategyLabLLMError", non_retryable=non_retryable
        )
    return ApplicationError(
        f"{type(exc).__name__}: {exc}", type=type(exc).__name__, non_retryable=True
    )


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_compute_regime_summary")
def compute_regime_summary_activity() -> Optional[Dict[str, Any]]:
    """Derive the current market-regime summary for the designer prompt.

    Preconditions:
        None.
    Postconditions:
        Returns the resulting ``RegimeSummary``'s JSON dump. The caller (the
        cycle workflow) is responsible for the
        ``STRATEGY_LAB_REGIME_SUMMARY_ENABLED`` on/off gate — a resolved,
        workflow-input flag, not an env read inside this activity —
        and for skipping this activity entirely when the feature is
        disabled, mirroring ``StrategyLabOrchestrator._compute_regime_summary``.
        ``compute_regime_summary`` is itself fail-open (never raises; a
        degraded summary is returned instead), so this activity only raises
        ``ApplicationError`` on a genuinely unexpected exception.
    """
    from datetime import datetime, timezone

    from investment_team.market_data_service import MarketDataService
    from investment_team.strategy_lab.market_regime import compute_regime_summary

    service = MarketDataService()
    try:
        summary = compute_regime_summary(
            service.fetch_ohlcv,
            computed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return summary.model_dump(mode="json")


@activity.defn(name="strategy_lab_resolve_workflow_config")
def resolve_workflow_config_activity() -> Dict[str, Any]:
    """Resolve every env var the cycle-workflow control flow needs, once.

    Preconditions:
        None.
    Postconditions:
        Returns ``{"design_review_rounds": int, "design_review_stall_rounds":
        int, "mechanical_repair_enabled": bool, "code_conformance_retries":
        int, "design_max_llm_calls": int, "regime_summary_enabled": bool,
        "max_design_reentries": int}``.
        A workflow may never read these env vars itself (``os.*`` is
        restricted at workflow runtime by the temporalio sandbox) — it calls
        this activity once and threads the resolved values through
        ``cycle_input`` instead of re-reading env vars in any loop body.
        ``max_design_reentries`` is a plain module constant (not env-derived);
        it is surfaced here so the workflow need never import
        ``orchestrator`` (and its heavy transitive graph) into the sandbox.
    """
    from investment_team.strategy_lab.orchestrator import (
        MAX_DESIGN_REENTRIES,
        _design_max_llm_calls,
        _design_review_rounds,
        _design_review_stall_rounds,
        _env_flag,
        _mechanical_repair_enabled,
    )
    from investment_team.strategy_lab.quality_gates.predicate_conformance import (
        _code_conformance_retries,
    )

    return {
        "design_review_rounds": _design_review_rounds(),
        "design_review_stall_rounds": _design_review_stall_rounds(),
        "mechanical_repair_enabled": _mechanical_repair_enabled(),
        "code_conformance_retries": _code_conformance_retries(),
        "design_max_llm_calls": _design_max_llm_calls(),
        "regime_summary_enabled": _env_flag("STRATEGY_LAB_REGIME_SUMMARY_ENABLED"),
        "max_design_reentries": MAX_DESIGN_REENTRIES,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_persist_run_state")
def persist_run_state_activity(run_id: str, state: dict, create: bool = False) -> None:
    """Persist strategy-lab run/batch progress to the durable job store.

    Preconditions:
        ``run_id`` is a non-empty run identifier; ``state`` is a JSON-shaped
        dict of run-state fields.
    Postconditions:
        Delegates to ``investment_team.api.main._persist_run_state``
        verbatim, which never raises (it logs and swallows any job-service
        failure internally) — so this activity likewise never raises.
    """
    from investment_team.api.main import _persist_run_state

    _persist_run_state(run_id, state, create=create)


@activity.defn(name="strategy_lab_snapshot_prior_records")
def snapshot_prior_records_activity(reverse: bool = False) -> List[Dict[str, Any]]:
    """Read the durable strategy-lab record store, sorted by creation time.

    Preconditions:
        None — safe to call against an empty store.
    Postconditions:
        Returns a list of ``StrategyLabRecord`` JSON dumps sorted by
        ``created_at`` (ascending by default, descending when
        ``reverse=True``), delegating to
        ``investment_team.api.main._snapshot_prior_records`` verbatim.
    """
    from investment_team.api.main import _snapshot_prior_records

    records = _snapshot_prior_records(reverse=reverse)
    return [r.model_dump(mode="json") for r in records]


# ---------------------------------------------------------------------------
# Composite activities — wrap a whole orchestrator sub-pipeline verbatim
# rather than decomposing it further, reusing existing gate-wiring logic.
# Each constructs its own throwaway ``StrategyLabOrchestrator()`` purely to
# call the existing instance method unmodified.
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_build_short_circuit_record")
def build_short_circuit_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run ``StrategyLabOrchestrator._build_short_circuit_record``.

    Preconditions:
        ``params`` carries every keyword ``_build_short_circuit_record``
        accepts (JSON-shaped): ``spec``,
        ``config``, ``code``, ``original_spec``, ``original_code``,
        ``rationale``, ``all_gate_results``, ``refinement_attempts``,
        ``short_circuit_status``, ``short_circuit_reason``,
        ``design_context``, ``phase_back_count``, ``drift_collector``, and
        ``convergence_tracker_state``.
    Postconditions:
        Returns ``{"record": ..., "convergence_tracker_state": ...}``
        (updated by the ``count_asset_class=False`` tracker mutation this
        method performs). Raises ``ApplicationError`` on an unexpected
        exception.
    """
    from investment_team.models import (
        BacktestConfig,
        CodeRevision,
        GateEvent,
        SpecRevision,
        StrategySpec,
    )
    from investment_team.strategy_lab._orchestrator_helpers import (
        _DesignPersistContext,
        _DriftCollector,
    )
    from investment_team.strategy_lab.agents.design_review import SpecCritique
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.strategy_lab.temporal.dto import (
        convergence_tracker_from_wire,
        convergence_tracker_to_wire,
    )

    design_context_data = params.get("design_context") or {}
    design_context = _DesignPersistContext(
        rounds=design_context_data.get("rounds", 0),
        critiques=[
            SpecCritique.model_validate(c) for c in design_context_data.get("critiques", [])
        ],
        stop_reason=design_context_data.get("stop_reason", ""),
        loop_telemetry=design_context_data.get("loop_telemetry", {}),
    )
    drift_data = params.get("drift_collector") or {}
    drift_collector = _DriftCollector(
        spec_history=[SpecRevision(**d) for d in drift_data.get("spec_history", [])],
        code_history=[CodeRevision(**d) for d in drift_data.get("code_history", [])],
        gate_timeline=[GateEvent(**d) for d in drift_data.get("gate_timeline", [])],
    )

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(params["convergence_tracker_state"])
    try:
        record = orch._build_short_circuit_record(
            spec=StrategySpec.parse_persisted(params["spec"]),
            config=BacktestConfig(**params["config"]),
            code=params["code"],
            original_spec=StrategySpec.parse_persisted(params["original_spec"]),
            original_code=params["original_code"],
            rationale=params["rationale"],
            all_gate_results=[
                QualityGateResult.model_validate(g) for g in params["all_gate_results"]
            ],
            refinement_attempts=params["refinement_attempts"],
            short_circuit_status=params["short_circuit_status"],
            short_circuit_reason=params["short_circuit_reason"],
            emit=lambda *_a, **_kw: None,
            design_context=design_context,
            phase_back_count=params.get("phase_back_count", 0),
            drift_collector=drift_collector,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "record": record.model_dump(mode="json"),
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
    }


@activity.defn(name="strategy_lab_run_design_attempt")
def run_design_attempt_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one full ``StrategyLabOrchestrator._run_design_attempt`` verbatim.

    This is the single per-attempt boundary the cycle workflow drives. It
    wraps the whole design → synthesis → refinement/alignment →
    verification/analysis → record-assembly sequence
    (``orchestrator._run_design_attempt``) unmodified, inside one activity.
    Running it here — outside the temporalio workflow sandbox — is what makes
    the quality gates, ``datetime.now``/``uuid.uuid4``, and the scattered
    ``os.environ`` reads in those phases legal: none of that is re-expressed as
    sandboxed workflow code. The ``SpecImplementabilityError`` design-re-entry
    control-flow signal is caught at this outermost frame and returned as a
    structured ``{"kind": "reentry", ...}`` outcome rather than crossing the
    activity boundary as an exception; the workflow branches on
    ``outcome["kind"]`` exactly where ``run_cycle`` branches on the ``except``.

    Preconditions:
        ``params`` carries the JSON-shaped attempt inputs:
          - ``prior_records``: list of ``StrategyLabRecord`` JSON dumps.
          - ``config``: ``BacktestConfig`` JSON dump.
          - ``signal_brief``: ``SignalIntelligenceBriefV1`` JSON dump or ``None``.
          - ``exclude_asset_classes``: list of str or ``None``.
          - ``directives``: list of convergence-directive strings.
          - ``design_attempt``: int re-entry index (0-based).
          - ``phase_back_count``: int prior phase-backs.
          - ``drift``: ``{"spec_history", "code_history", "gate_timeline"}``
            (each a list of ``SpecRevision``/``CodeRevision``/``GateEvent`` JSON
            dumps) — normally the empty per-attempt child collector, since
            ``_DriftCollector.snapshot()`` hands each attempt a fresh empty one.
          - ``gate_results``: list of ``QualityGateResult`` JSON dumps
            accumulated across prior attempts (extended in place by the run).
          - ``budget_calls``: int LLM calls charged in prior attempts; the
            per-cycle budget is pre-charged to this so its ceiling spans every
            re-entry, exactly as ``run_cycle``'s single ``with use_budget(...)``
            around the whole loop does.
          - ``regime_summary``: ``RegimeSummary`` JSON dump or ``None``.
          - ``convergence_tracker_state``: ``dto.convergence_tracker_to_wire``'s
            output for the batch-level tracker.
    Postconditions:
        Returns either
        ``{"kind": "record", "record": <StrategyLabRecord JSON dump>,
        "convergence_tracker_state": ..., "gate_results": [...],
        "budget_calls": int, "drift": {...}}`` on a terminal record, or
        ``{"kind": "reentry", "evidence": str, "last_spec": <StrategySpec JSON
        dump or None>, "last_code": str, "failure_phase": str|None,
        "design_context": {...}|None, "convergence_tracker_state": ...,
        "gate_results": [...], "budget_calls": int, "drift": {...}}`` when
        ``_run_design_attempt`` raised ``SpecImplementabilityError``, or
        ``{"kind": "skipped", "reason": "no_market_data",
        "convergence_tracker_state": ..., "gate_results": [...],
        "budget_calls": int, "drift": {...}}`` when this attempt recorded a
        failed ``"market_data"`` gate (no data available for the asset class —
        ``_fetch_market_data``/``_fetch_market_data_for_synthesis`` degrade to
        this gate rather than raising; a bare ``HTTPException(502)`` is also
        still honored as a secondary/defense-in-depth signal, matching thread
        mode's wave loop). Either case is cycle-terminal — no further
        design-attempt retry. Any other exception (including a non-502
        ``HTTPException``) maps to ``ApplicationError`` via
        :func:`_map_exception_to_application_error`.
    Invariants:
        The returned ``convergence_tracker_state``/``gate_results``/
        ``budget_calls`` reflect exactly this attempt's mutations layered on
        the incoming state; ``drift`` reflects only this attempt's own child
        collector — the workflow owns the parent copy-on-entry/merge across
        attempts.
    """
    from fastapi import HTTPException

    from investment_team.models import (
        BacktestConfig,
        CodeRevision,
        GateEvent,
        SpecRevision,
        StrategyLabRecord,
    )
    from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
    from investment_team.strategy_lab._orchestrator_helpers import _DriftCollector
    from investment_team.strategy_lab.agents._llm_budget import LLMCallBudget, use_budget
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.market_regime import RegimeSummary
    from investment_team.strategy_lab.orchestrator import (
        StrategyLabOrchestrator,
        _design_max_llm_calls,
    )
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.strategy_lab.temporal.dto import (
        convergence_tracker_from_wire,
        convergence_tracker_to_wire,
    )

    prior_records = [StrategyLabRecord.parse_persisted(r) for r in params["prior_records"]]
    config = BacktestConfig(**params["config"])
    signal_brief = (
        SignalIntelligenceBriefV1(**params["signal_brief"]) if params.get("signal_brief") else None
    )
    regime_summary = (
        RegimeSummary(**params["regime_summary"]) if params.get("regime_summary") else None
    )
    drift_data = params.get("drift") or {}
    drift_collector = _DriftCollector(
        spec_history=[SpecRevision(**d) for d in drift_data.get("spec_history", [])],
        code_history=[CodeRevision(**d) for d in drift_data.get("code_history", [])],
        gate_timeline=[GateEvent(**d) for d in drift_data.get("gate_timeline", [])],
    )
    # ``_run_design_attempt`` appends to this list in place (its
    # ``all_gate_results``); returning the mutated list threads accumulation
    # across re-entries, matching ``run_cycle``'s single ``cumulative_gate_results``.
    cumulative_gate_results = [
        QualityGateResult.model_validate(g) for g in params.get("gate_results", [])
    ]

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(params["convergence_tracker_state"])

    # Pre-charge the per-cycle budget to what prior attempts already spent so
    # the ceiling is a true whole-cycle cap, not a per-attempt allowance.
    budget = LLMCallBudget(_design_max_llm_calls())
    budget.calls_made = min(int(params.get("budget_calls", 0)), budget.limit)

    def _drift_to_wire(collector: _DriftCollector) -> Dict[str, Any]:
        return {
            "spec_history": [r.model_dump(mode="json") for r in collector.spec_history],
            "code_history": [r.model_dump(mode="json") for r in collector.code_history],
            "gate_timeline": [r.model_dump(mode="json") for r in collector.gate_timeline],
        }

    def _skipped_outcome() -> Dict[str, Any]:
        return {
            "kind": "skipped",
            "reason": "no_market_data",
            "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
            "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
            "budget_calls": budget.calls_made,
            "drift": _drift_to_wire(drift_collector),
        }

    # ``cumulative_gate_results`` is ``all_gate_results`` in
    # ``_run_design_attempt``'s frame — mutated in place, so a "market_data"
    # gate this attempt records lands here too. Snapshot the length first so
    # the post-call scan only sees THIS attempt's own additions, never a
    # market_data failure recorded by an earlier re-entered attempt (whose
    # own outcome already returned separately as "reentry").
    gate_results_len_before = len(cumulative_gate_results)

    try:
        with use_budget(budget):
            record = orch._run_design_attempt(
                prior_records=prior_records,
                config=config,
                signal_brief=signal_brief,
                emit=lambda *_a, **_kw: None,
                exclude_asset_classes=params.get("exclude_asset_classes"),
                directives=list(params.get("directives") or []),
                design_attempt=params.get("design_attempt", 0),
                phase_back_count=params.get("phase_back_count", 0),
                drift_collector=drift_collector,
                cumulative_gate_results=cumulative_gate_results,
                regime_summary=regime_summary,
            )
    except SpecImplementabilityError as exc:
        design_context = exc.design_context
        design_context_wire = (
            None
            if design_context is None
            else {
                "rounds": design_context.rounds,
                "critiques": [c.model_dump(mode="json") for c in design_context.critiques],
                "stop_reason": design_context.stop_reason,
                "loop_telemetry": design_context.loop_telemetry,
            }
        )
        return {
            "kind": "reentry",
            "evidence": exc.evidence,
            "last_spec": (
                exc.last_spec.model_dump(mode="json") if exc.last_spec is not None else None
            ),
            "last_code": exc.last_code or "",
            "failure_phase": exc.failure_phase,
            "design_context": design_context_wire,
            "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
            "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
            "budget_calls": budget.calls_made,
            "drift": _drift_to_wire(drift_collector),
        }
    except HTTPException as exc:
        # Defense-in-depth: no current code path in the design-attempt
        # pipeline raises this (market-data failures degrade to the
        # "market_data" gate checked below instead), but thread mode's wave
        # loop still honors a bare 502 the same way, so this stays a live
        # secondary signal rather than being removed. Any other
        # HTTPException status is still a deep failure.
        if exc.status_code == 502:
            return _skipped_outcome()
        raise _map_exception_to_application_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc

    # Primary "no market data" signal: ``_fetch_market_data``/
    # ``_fetch_market_data_for_synthesis`` never raise on a failed/empty
    # fetch — they record a critical "market_data" gate and the synthesis
    # loop breaks immediately, and ``_run_design_attempt`` still returns a
    # normal (failing) record rather than short-circuiting. Detect that gate
    # among this attempt's own additions and report it as a skip instead of
    # a real record, matching what thread mode's now-dead HTTPException(502)
    # branch was actually meant to catch.
    if any(
        g.gate_name == "market_data" and not g.passed
        for g in cumulative_gate_results[gate_results_len_before:]
    ):
        return _skipped_outcome()

    return {
        "kind": "record",
        "record": record.model_dump(mode="json"),
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
        "gate_results": [g.model_dump(mode="json") for g in cumulative_gate_results],
        "budget_calls": budget.calls_made,
        "drift": _drift_to_wire(drift_collector),
    }


# ---------------------------------------------------------------------------
# Batch-level activities — used by StrategyLabBatchWorkflow (the per-batch /
# per-wave orchestration around the per-cycle child workflows). Each wraps an
# existing thread-mode helper verbatim so the two paths share one implementation.
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_compute_signal_brief")
def compute_signal_brief_activity(benchmark_symbol: str) -> Dict[str, Any]:
    """Build the per-batch signal brief over all currently-persisted prior records.

    Preconditions:
        ``benchmark_symbol`` is the run's benchmark ticker.
    Postconditions:
        Returns ``{"signal_brief": <SignalIntelligenceBriefV1 JSON dump or None>,
        "signal_brief_storage": <dict or None>}`` — the JSON-shaped pair the batch
        workflow threads into each cycle's ``signal_brief`` input and into
        ``finalize_cycle_record_activity``. Delegates to
        ``investment_team.api.main._compute_signal_brief_snapshot``, which is
        fail-open (never raises; returns a skipped/degraded marker instead), so
        this activity likewise only raises ``ApplicationError`` on a genuinely
        unexpected exception.
    """
    from investment_team.api.main import _compute_signal_brief_snapshot

    try:
        brief, storage = _compute_signal_brief_snapshot(benchmark_symbol)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "signal_brief": brief.model_dump(mode="json") if brief is not None else None,
        "signal_brief_storage": storage,
    }


@activity.defn(name="strategy_lab_is_run_cancelled")
def is_run_cancelled_activity(run_id: str) -> bool:
    """Return True if the run has been externally cancelled (terminal job status).

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns ``investment_team.api.main._is_strategy_lab_run_cancelled``'s
        result verbatim — True for a ``cancelled``/``failed``/``interrupted``
        job status, False otherwise. That helper never raises, so this activity
        never raises either.
    """
    from investment_team.api.main import _is_strategy_lab_run_cancelled

    return _is_strategy_lab_run_cancelled(run_id)


@activity.defn(name="strategy_lab_external_terminal_status")
def external_terminal_status_activity(run_id: str) -> Optional[str]:
    """Return the run's persisted external stop status, or None if not stopped.

    Preconditions:
        ``run_id`` is the strategy-lab run identifier.
    Postconditions:
        Returns ``investment_team.api.main._strategy_lab_external_terminal_status``'s
        result verbatim — the exact persisted status string (``cancelled``,
        ``failed``, or ``interrupted``) when the job was externally marked
        terminal, else ``None``. That helper never raises, so this activity never
        raises either. Callers persist the returned status directly so an
        external interrupt/failure is not mislabeled a user cancellation (thread
        mode's ``_strategy_lab_worker`` makes the same distinction).
    """
    from investment_team.api.main import _strategy_lab_external_terminal_status

    return _strategy_lab_external_terminal_status(run_id)


@activity.defn(name="strategy_lab_finalize_cycle_record")
def finalize_cycle_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run the post-``run_cycle`` finalization (signal brief + paper-trade + persist).

    The per-cycle child workflow (``StrategyLabCycleWorkflow``) returns only the
    raw ``run_cycle`` record; this activity reproduces the tail that thread-mode's
    ``_run_one_strategy_lab_cycle`` runs after it, by delegating to the shared
    ``investment_team.api.main._finalize_strategy_lab_cycle_record`` helper.

    Preconditions:
        ``params`` carries ``record`` (a ``StrategyLabRecord`` JSON dump from the
        cycle workflow), and optionally ``signal_brief_storage`` (dict or None),
        ``paper_trading_enabled`` (bool, default True), and
        ``paper_trading_lookback_days`` (int, default 365).
    Postconditions:
        Returns ``{"record": <finalized StrategyLabRecord JSON dump>}`` — the same
        record with ``paper_trading_*`` resolved and durably persisted. Raises
        ``ApplicationError`` on an unexpected exception (paper-trading failures are
        already non-fatal inside the helper).
    """
    from investment_team.api.main import _finalize_strategy_lab_cycle_record
    from investment_team.models import StrategyLabRecord

    record = StrategyLabRecord.parse_persisted(params["record"])
    try:
        finalized = _finalize_strategy_lab_cycle_record(
            record,
            signal_brief_storage=params.get("signal_brief_storage"),
            paper_trading_enabled=params.get("paper_trading_enabled", True),
            paper_trading_lookback_days=params.get("paper_trading_lookback_days", 365),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"record": finalized.model_dump(mode="json")}


@activity.defn(name="strategy_lab_merge_wave_results")
def merge_wave_results_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a completed wave's cycle results into the batch-level convergence tracker.

    Reproduces the deterministic merge thread-mode does after each wave
    (``api/main.py``: sort settled cycles by cycle index, then per record
    ``primary_tracker.record(strategy, gate_results)`` for diversity/failure-mode
    state and ``primary_tracker.merge_from(cycle_tracker)`` for the trial-count
    delta). Runs in an activity — not workflow code — so the Pydantic
    reconstruction of ``StrategyLabRecord``/``QualityGateResult`` and the tracker
    math stay outside the temporalio sandbox.

    Mirrors thread mode's per-cycle isolation of the merge step (``api/main.py``):
    a single record's ``merge_from`` failure is captured, not fatal — the wave's
    other records still merge and the activity still succeeds.

    Preconditions:
        ``params`` = ``{"primary_tracker_state": <dto wire dict>, "wave_results":
        [{"cycle_index": int, "record": <StrategyLabRecord JSON dump>,
        "cycle_tracker_state": <dto wire dict>}, ...]}``.
    Postconditions:
        Returns ``{"primary_tracker_state": <updated dto wire dict>, "merge_errors":
        [{"cycle_index": int, "error": str, "exception_type": str, "reason":
        "tracker_merge_failed"}, ...]}`` — the primary tracker with every settled
        cycle in the wave recorded/merged in cycle-index order (reproducible
        across runs), and one ``merge_errors`` entry per record whose
        ``merge_from`` call raised (that record's ``.record(...)`` call still
        ran). A ``merge_errors`` entry's ``cycle_index`` is ``wr["cycle_index"] +
        1`` — a 1-based, human-friendly cycle number for error reporting, distinct
        from the 0-based ``cycle_index`` used in ``wave_results`` above. Raises
        ``ApplicationError`` on an exception outside the isolated ``merge_from``
        step (e.g. malformed input).
    """
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    try:
        primary = ConvergenceTracker.from_wire_dict(params["primary_tracker_state"])
        merge_errors: List[Dict[str, Any]] = []
        for wr in sorted(params["wave_results"], key=lambda w: w["cycle_index"]):
            record = StrategyLabRecord.parse_persisted(wr["record"])
            gate_results = [
                QualityGateResult(**g) if isinstance(g, dict) else g
                for g in record.quality_gate_results
            ]
            primary.record(record.strategy, gate_results)
            try:
                primary.merge_from(ConvergenceTracker.from_wire_dict(wr["cycle_tracker_state"]))
            except Exception as exc:  # noqa: BLE001
                merge_errors.append(
                    {
                        "cycle_index": wr["cycle_index"] + 1,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "reason": "tracker_merge_failed",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"primary_tracker_state": primary.to_wire_dict(), "merge_errors": merge_errors}


ACTIVITIES = [
    compute_regime_summary_activity,
    persist_run_state_activity,
    snapshot_prior_records_activity,
    build_short_circuit_record_activity,
    run_design_attempt_activity,
    resolve_workflow_config_activity,
    compute_signal_brief_activity,
    is_run_cancelled_activity,
    external_terminal_status_activity,
    finalize_cycle_record_activity,
    merge_wave_results_activity,
]

__all__ = [
    "ACTIVITIES",
    "compute_signal_brief_activity",
    "finalize_cycle_record_activity",
    "is_run_cancelled_activity",
    "external_terminal_status_activity",
    "merge_wave_results_activity",
    "build_short_circuit_record_activity",
    "compute_regime_summary_activity",
    "persist_run_state_activity",
    "resolve_workflow_config_activity",
    "run_design_attempt_activity",
    "snapshot_prior_records_activity",
]
