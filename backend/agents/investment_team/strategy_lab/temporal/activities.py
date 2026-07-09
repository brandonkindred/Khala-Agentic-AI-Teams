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
# Design phase
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_design_generate")
def design_generate_activity(
    prior_records: List[dict],
    signal_brief: Optional[dict] = None,
    convergence_directives: Optional[List[str]] = None,
    exclude_asset_classes: Optional[List[str]] = None,
    regime_summary: Optional[dict] = None,
) -> Dict[str, Any]:
    """Run ``DesignAgent.run`` to author a fresh strategy spec.

    Preconditions:
        ``prior_records`` is a list of ``StrategyLabRecord.model_dump(mode="json")``
        payloads (possibly legacy-shaped — reconstructed via
        ``parse_persisted``); ``signal_brief`` / ``regime_summary``, when
        given, are the corresponding model's JSON dump.
    Postconditions:
        Returns ``{"strategy_dict": dict, "rationale": str}`` — the same pair
        ``DesignAgent.run`` returns, JSON-shaped. Raises ``ApplicationError``
        (see :func:`_map_exception_to_application_error`) on any failure.
    """
    from investment_team.models import StrategyLabRecord
    from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1
    from investment_team.strategy_lab.agents.design import DesignAgent
    from investment_team.strategy_lab.market_regime import RegimeSummary

    records = [StrategyLabRecord.parse_persisted(r) for r in prior_records]
    brief = SignalIntelligenceBriefV1(**signal_brief) if signal_brief else None
    regime = RegimeSummary(**regime_summary) if regime_summary else None
    try:
        strategy_dict, rationale = DesignAgent().run(
            prior_records=records,
            signal_brief=brief,
            convergence_directives=convergence_directives,
            exclude_asset_classes=exclude_asset_classes,
            regime_summary=regime,
        )
    except Exception as exc:  # noqa: BLE001 — translate every failure mode
        raise _map_exception_to_application_error(exc) from exc
    return {"strategy_dict": strategy_dict, "rationale": rationale}


@activity.defn(name="strategy_lab_design_revise")
def design_revise_activity(
    prior_spec: dict,
    critique: dict,
    prior_critiques: Optional[List[dict]] = None,
    regression_notice: str = "",
) -> Dict[str, Any]:
    """Run ``DesignAgent.revise`` to address a design-review critique.

    Preconditions:
        ``prior_spec`` is a ``StrategySpec`` JSON dump; ``critique`` is a
        ``SpecCritique`` JSON dump; ``prior_critiques`` (if given) is a list
        of ``SpecCritique`` JSON dumps.
    Postconditions:
        Returns ``{"strategy_dict": dict, "rationale": str}``. Raises
        ``ApplicationError`` on any failure.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents.design import DesignAgent
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    spec = StrategySpec.parse_persisted(prior_spec)
    crit = SpecCritique.model_validate(critique)
    priors = [SpecCritique.model_validate(c) for c in prior_critiques] if prior_critiques else None
    try:
        strategy_dict, rationale = DesignAgent().revise(
            prior_spec=spec,
            critique=crit,
            prior_critiques=priors,
            regression_notice=regression_notice,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"strategy_dict": strategy_dict, "rationale": rationale}


@activity.defn(name="strategy_lab_design_review")
def design_review_activity(
    spec: dict,
    readiness_results: Optional[List[dict]] = None,
    prior_critiques: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Run ``DesignReviewAgent.run`` for one design-review round.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``readiness_results`` (if
        given) is a list of ``QualityGateResult`` JSON dumps produced by the
        deterministic ``SpecReadinessGate``; ``prior_critiques`` (if given)
        is a list of ``SpecCritique`` JSON dumps.
    Postconditions:
        Returns the resulting ``SpecCritique``'s JSON dump. Never raises for
        a reviewer/transport hiccup (``DesignReviewAgent.run`` falls closed
        internally); may raise ``ApplicationError`` on ``DesignBudgetExhausted``
        or an unexpected failure.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents.design_review import DesignReviewAgent, SpecCritique
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    spec_obj = StrategySpec.parse_persisted(spec)
    readiness = (
        [QualityGateResult.model_validate(r) for r in readiness_results]
        if readiness_results
        else None
    )
    priors = [SpecCritique.model_validate(c) for c in prior_critiques] if prior_critiques else None
    try:
        critique = DesignReviewAgent().run(
            spec_obj, readiness_results=readiness, prior_critiques=priors
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return critique.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Code synthesis / refinement
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_code_synthesis")
def code_synthesis_activity(spec: dict) -> Dict[str, Any]:
    """Run ``CodeSynthesisAgent.run`` to generate Python code from a frozen spec.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump that has already passed
        design review.
    Postconditions:
        Returns ``{"code": str}`` with a non-empty Python source string.
        Raises ``ApplicationError`` on any failure (``CodeSynthesisError`` or
        an LLM envelope failure).
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents.code_synthesis import CodeSynthesisAgent

    spec_obj = StrategySpec.parse_persisted(spec)
    try:
        code = CodeSynthesisAgent().run(spec_obj)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"code": code}


@activity.defn(name="strategy_lab_refinement")
def refinement_activity(
    spec: dict,
    code: str,
    failure_phase: str,
    failure_details: str,
    metrics: Optional[dict] = None,
    prior_attempts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run ``RefinementAgent.run`` to fix strategy code after a gate/execution failure.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``code`` is the current
        strategy source; ``metrics`` (if given) is a ``BacktestResult`` JSON
        dump.
    Postconditions:
        Returns ``{"updated_fields": dict, "updated_code": str}`` — the same
        pair ``RefinementAgent.run`` returns. Raises ``ApplicationError`` on
        any failure.
    """
    from investment_team.models import BacktestResult, StrategySpec
    from investment_team.strategy_lab.agents.refinement import RefinementAgent

    spec_obj = StrategySpec.parse_persisted(spec)
    metrics_obj = BacktestResult(**metrics) if metrics else None
    try:
        updated_fields, updated_code = RefinementAgent().run(
            spec_obj,
            code,
            failure_phase,
            failure_details,
            metrics=metrics_obj,
            prior_attempts=prior_attempts,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"updated_fields": updated_fields, "updated_code": updated_code}


# ---------------------------------------------------------------------------
# Trade alignment
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_alignment_near_miss")
def alignment_near_miss_activity(
    rule_id: str,
    predicate_repr: str,
    computed_value: float,
    threshold: float,
    symbol: str,
    entry_date: str,
) -> Dict[str, Any]:
    """Run ``TradeAlignmentAgent.adjudicate_near_miss`` on one entry-signal near-miss.

    Preconditions:
        ``computed_value`` / ``threshold`` are finite floats; the
        deterministic gate has already confirmed the relative miss is within
        tolerance.
    Postconditions:
        Returns the resulting ``NearMissVerdict``'s JSON dump. Raises
        ``ApplicationError`` (mapped from ``AlignmentAuditError``) on parse
        or transport failure.
    """
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentAgent

    try:
        verdict = TradeAlignmentAgent().adjudicate_near_miss(
            rule_id=rule_id,
            predicate_repr=predicate_repr,
            computed_value=computed_value,
            threshold=threshold,
            symbol=symbol,
            entry_date=entry_date,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return verdict.model_dump(mode="json")


@activity.defn(name="strategy_lab_alignment_propose_fix")
def alignment_propose_fix_activity(
    spec: dict,
    code: str,
    findings: List[dict],
    prior_attempts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run ``TradeAlignmentAgent.propose_code_fix`` to patch a misaligned strategy.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``code`` is the
        most-recently-executed strategy source; ``findings`` is a list of
        ``AlignmentFinding`` JSON dumps containing at least one
        ``severity="critical", passed=False`` row.
    Postconditions:
        Returns the resulting ``TradeAlignmentReport``'s JSON dump
        (``aligned=False``, ``proposed_code`` set). Raises
        ``ApplicationError`` on parse or transport failure.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentAgent
    from investment_team.strategy_lab.alignment_findings import AlignmentFinding

    spec_obj = StrategySpec.parse_persisted(spec)
    finding_objs = [AlignmentFinding.model_validate(f) for f in findings]
    try:
        report = TradeAlignmentAgent().propose_code_fix(
            spec=spec_obj,
            code=code,
            findings=finding_objs,
            prior_attempts=prior_attempts,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return report.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Analysis / zero-trade repair
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_analysis")
def analysis_activity(
    spec: dict,
    metrics: dict,
    trades: List[dict],
    rationale: str,
    is_winning: Optional[bool] = None,
    alignment_report: Optional[dict] = None,
    robustness_caveats: Optional[str] = None,
) -> Dict[str, Any]:
    """Run ``AnalysisAgent.run`` to produce the post-backtest narrative.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``metrics`` is a
        ``BacktestResult`` JSON dump; ``trades`` is a list of
        ``TradeRecord`` JSON dumps; ``alignment_report`` (if given) is a
        ``TradeAlignmentReport`` JSON dump.
    Postconditions:
        Returns ``{"narrative": str}``. ``AnalysisAgent.run`` never raises
        for an LLM/transport failure (it falls back to a deterministic
        auto-summary internally); an unexpected failure still maps to
        ``ApplicationError``.
    """
    from investment_team.models import BacktestResult, StrategySpec, TradeRecord
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.strategy_lab.agents.analysis import AnalysisAgent

    spec_obj = StrategySpec.parse_persisted(spec)
    metrics_obj = BacktestResult(**metrics)
    trade_objs = [TradeRecord(**t) for t in trades]
    alignment_obj = (
        TradeAlignmentReport.model_validate(alignment_report) if alignment_report else None
    )
    try:
        narrative = AnalysisAgent().run(
            spec_obj,
            metrics_obj,
            trade_objs,
            rationale,
            is_winning=is_winning,
            alignment_report=alignment_obj,
            robustness_caveats=robustness_caveats,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {"narrative": narrative}


@activity.defn(name="strategy_lab_zero_trade_repair")
def zero_trade_repair_activity(
    spec: dict,
    code: str,
    diagnostics: dict,
    prior_attempts: Optional[List[str]] = None,
    coverage_report: Optional[dict] = None,
) -> Dict[str, Any]:
    """Run ``ZeroTradeRepairAgent.run`` to diagnose + patch a zero-trade backtest.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``diagnostics`` is a
        ``BacktestExecutionDiagnostics`` JSON dump with a non-null
        ``zero_trade_category``; ``coverage_report`` (if given) is a
        ``CoverageReport`` JSON dump.
    Postconditions:
        Returns the resulting ``ZeroTradeRepairReport``'s JSON dump. Never
        raises for an LLM/parse failure (``ZeroTradeRepairAgent.run`` falls
        back to a no-op report internally); an unexpected failure still maps
        to ``ApplicationError``.
    """
    from investment_team.models import BacktestExecutionDiagnostics, CoverageReport, StrategySpec
    from investment_team.strategy_lab.agents.zero_trade_repair import ZeroTradeRepairAgent

    spec_obj = StrategySpec.parse_persisted(spec)
    diagnostics_obj = BacktestExecutionDiagnostics(**diagnostics)
    coverage_obj = CoverageReport(**coverage_report) if coverage_report else None
    try:
        report = ZeroTradeRepairAgent().run(
            spec_obj,
            code,
            diagnostics_obj,
            prior_attempts=prior_attempts,
            coverage_report=coverage_obj,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return report.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Sandboxed backtest execution
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_run_strategy_code")
def run_strategy_code_activity(
    strategy_code: str,
    market_data: Dict[str, List[dict]],
    config: dict,
    strategy: Optional[dict] = None,
    coverage_probe_mode: bool = False,
) -> Dict[str, Any]:
    """Execute strategy code against market data via ``run_strategy_code``.

    Preconditions:
        ``config`` is a ``BacktestConfig`` JSON dump; ``market_data`` maps
        symbol to a list of ``OHLCVBar`` JSON dumps; ``strategy`` (if given)
        is a ``StrategySpec`` JSON dump.
    Postconditions:
        Returns a JSON-shaped dict mirroring ``StrategyRunResult``'s fields
        (``success``, ``trades``, ``stdout``, ``stderr``,
        ``execution_time_seconds``, ``error_type``, ``execution_diagnostics``,
        ``probe_events``, ``open_position_entry_reasons``). Raises
        ``ApplicationError`` only on an unexpected exception —
        ``run_strategy_code`` itself reports sandbox/runtime failures via
        ``success=False`` rather than raising.
    """
    from investment_team.market_data_service import OHLCVBar
    from investment_team.models import BacktestConfig, StrategySpec
    from investment_team.trading_service.modes.sandbox_compat import run_strategy_code

    config_obj = BacktestConfig(**config)
    strategy_obj = StrategySpec.parse_persisted(strategy) if strategy else None
    market_data_bars = {sym: [OHLCVBar(**bar) for bar in bars] for sym, bars in market_data.items()}
    try:
        result = run_strategy_code(
            strategy_code,
            market_data_bars,
            config_obj,
            strategy=strategy_obj,
            coverage_probe_mode=coverage_probe_mode,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "success": result.success,
        "trades": [t.model_dump(mode="json") for t in result.trades],
        "stdout": result.stdout,
        "stderr": result.stderr,
        "execution_time_seconds": result.execution_time_seconds,
        "error_type": result.error_type,
        "execution_diagnostics": (
            result.execution_diagnostics.model_dump(mode="json")
            if result.execution_diagnostics is not None
            else None
        ),
        "probe_events": result.probe_events,
        "open_position_entry_reasons": list(result.open_position_entry_reasons),
    }


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_resolve_symbols")
def resolve_symbols_activity(spec: dict) -> List[str]:
    """Resolve the symbol universe a strategy should trade.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump.
    Postconditions:
        Returns ``MarketDataService.resolve_strategy_symbols``'s result
        verbatim. Raises ``ApplicationError`` on an unexpected exception.
    """
    from investment_team.market_data_service import MarketDataService
    from investment_team.models import StrategySpec

    spec_obj = StrategySpec.parse_persisted(spec)
    try:
        return MarketDataService().resolve_strategy_symbols(spec_obj)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc


@activity.defn(name="strategy_lab_fetch_market_data")
def fetch_market_data_activity(
    symbols: List[str],
    asset_class: str,
    start_date: str,
    end_date: str,
    as_of: Optional[str] = None,
    intraday_mode: bool = False,
    frequency: str = "1d",
) -> Dict[str, Any]:
    """Fetch OHLCV bars for a symbol universe over an explicit date range.

    Preconditions:
        ``symbols`` is a non-empty list of tickers; ``asset_class`` /
        ``start_date`` / ``end_date`` match
        ``MarketDataService.fetch_multi_symbol_range``'s contract.
    Postconditions:
        Returns ``{"data": {symbol: [bar_dict, ...]}, "provider_used": {symbol: provider}}``.
        Constructs a **fresh** ``MarketDataService`` per call (rather than
        reusing a shared instance) so ``provider_used`` reflects only this
        fetch — the orchestrator's synchronous path instead reuses one
        service instance and must defensively re-filter ``provider_used``
        per call; a fresh instance per activity invocation sidesteps that
        shared-mutable-state hazard entirely. Raises ``ApplicationError`` on
        an unexpected exception.
    """
    from investment_team.market_data_service import MarketDataService

    service = MarketDataService()
    try:
        data = service.fetch_multi_symbol_range(
            symbols,
            asset_class,
            start_date,
            end_date,
            intraday_mode=intraday_mode,
            as_of=as_of,
            frequency=frequency,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "data": {sym: [bar.model_dump(mode="json") for bar in bars] for sym, bars in data.items()},
        "provider_used": dict(service.provider_used),
    }


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


@activity.defn(name="strategy_lab_persist_record")
def persist_record_activity(record: dict) -> None:
    """Durably persist one completed cycle's ``StrategyLabRecord``.

    Preconditions:
        ``record`` is a ``StrategyLabRecord`` JSON dump with ``strategy`` /
        ``backtest`` populated.
    Postconditions:
        Delegates to ``investment_team.api.main._persist_strategy_lab_record``
        verbatim — the identical write thread-mode's
        ``_run_one_strategy_lab_cycle`` makes, already durable via the
        ``JobServiceClient``-backed ``_strategy_lab_records`` /
        ``_strategies`` / ``_backtests`` stores. Raises ``ApplicationError``
        on an unexpected exception.
    """
    from investment_team.api.main import _persist_strategy_lab_record
    from investment_team.models import StrategyLabRecord

    record_obj = StrategyLabRecord.parse_persisted(record)
    try:
        _persist_strategy_lab_record(record_obj)
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc


ACTIVITIES = [
    design_generate_activity,
    design_revise_activity,
    design_review_activity,
    code_synthesis_activity,
    refinement_activity,
    alignment_near_miss_activity,
    alignment_propose_fix_activity,
    analysis_activity,
    zero_trade_repair_activity,
    run_strategy_code_activity,
    resolve_symbols_activity,
    fetch_market_data_activity,
    compute_regime_summary_activity,
    persist_run_state_activity,
    snapshot_prior_records_activity,
    persist_record_activity,
]

__all__ = [
    "ACTIVITIES",
    "alignment_near_miss_activity",
    "alignment_propose_fix_activity",
    "analysis_activity",
    "code_synthesis_activity",
    "compute_regime_summary_activity",
    "design_generate_activity",
    "design_review_activity",
    "design_revise_activity",
    "fetch_market_data_activity",
    "persist_record_activity",
    "persist_run_state_activity",
    "refinement_activity",
    "resolve_symbols_activity",
    "run_strategy_code_activity",
    "snapshot_prior_records_activity",
    "zero_trade_repair_activity",
]
