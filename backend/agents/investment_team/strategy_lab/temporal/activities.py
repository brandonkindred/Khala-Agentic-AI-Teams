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


@activity.defn(name="strategy_lab_resolve_readiness_prices")
def resolve_readiness_prices_activity(symbols: List[str], asset_class: str) -> Dict[str, float]:
    """Fetch a short recent-close price sample for ``SpecReadinessGate`` Rule 5.

    Preconditions:
        ``symbols`` is the resolved trading universe for a spec (the same
        list ``resolve_symbols_activity`` returns for that spec);
        ``asset_class`` is the spec's asset class.
    Postconditions:
        Returns ``{symbol: last_close}`` for every symbol with a fetchable
        recent bar; a symbol with no data or a fetch error is simply omitted
        (the caller's ``market_sample_provider`` closure treats a missing key
        as ``float("nan")``, matching
        ``StrategyLabOrchestrator._readiness_price_provider``'s fail-closed
        contract exactly — this activity never raises for a per-symbol fetch
        failure, only for an unexpected exception outside the per-symbol loop).
    """
    from investment_team.market_data_service import MarketDataService

    service = MarketDataService()
    prices: Dict[str, float] = {}
    try:
        for symbol in symbols:
            try:
                bars = service.fetch_ohlcv(symbol, asset_class, days=5)
            except Exception:  # noqa: BLE001 — per-symbol fail-closed, mirrors the orchestrator
                continue
            if bars:
                close = float(bars[-1].close)
                if close > 0:
                    prices[symbol] = close
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return prices


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


# ---------------------------------------------------------------------------
# Composite activities — wrap a whole orchestrator sub-pipeline verbatim
# rather than decomposing it further. Each covers a phase with either no
# bounded per-round retry loop of its own (verification/analysis: a single
# linear pass) or an I/O callback bound deep inside a synchronous gate
# (the alignment audit's near-miss adjudicator) — in both cases decomposing
# further would mean re-deriving internal gate-wiring logic instead of
# reusing it, for no durability benefit (see the plan's Stage 3 design notes).
# Each constructs its own throwaway ``StrategyLabOrchestrator()`` purely to
# call the existing instance method unmodified.
# ---------------------------------------------------------------------------


@activity.defn(name="strategy_lab_run_alignment_audit")
def run_alignment_audit_activity(
    spec: dict,
    code: str,
    trades: List[dict],
    metrics: dict,
    prior_attempts: List[str],
    market_data: Dict[str, List[dict]],
    config: dict,
) -> Dict[str, Any]:
    """Run one round of ``StrategyLabOrchestrator._run_alignment_audit``.

    Preconditions:
        ``spec`` is a ``StrategySpec`` JSON dump; ``trades`` is a list of
        ``TradeRecord`` JSON dumps; ``metrics`` is a ``BacktestResult`` JSON
        dump; ``market_data`` maps symbol to a list of ``OHLCVBar`` JSON
        dumps; ``config`` is a ``BacktestConfig`` JSON dump.
    Postconditions:
        Returns ``{"report": TradeAlignmentReport JSON dump, "gate_results":
        [QualityGateResult JSON dump, ...]}``. Wraps the deterministic
        alignment gate (whose near-miss adjudication calls
        ``TradeAlignmentAgent.adjudicate_near_miss`` as a *synchronous
        callback* mid-check — a shape that cannot be driven from workflow
        code, since a plain Python callback cannot ``await`` an activity)
        plus the fail-closed ``propose_code_fix`` LLM call. Raises
        ``ApplicationError`` only on a genuinely unexpected exception —
        the method itself already falls closed on any agent/parse failure.
    """
    from investment_team.market_data_service import OHLCVBar
    from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec_obj = StrategySpec.parse_persisted(spec)
    trade_objs = [TradeRecord(**t) for t in trades]
    metrics_obj = BacktestResult(**metrics)
    config_obj = BacktestConfig(**config)
    market_data_bars = {sym: [OHLCVBar(**bar) for bar in bars] for sym, bars in market_data.items()}
    orch = StrategyLabOrchestrator()
    try:
        report, gate_results = orch._run_alignment_audit(
            spec_obj,
            code,
            trade_objs,
            metrics_obj,
            prior_attempts,
            market_data=market_data_bars,
            config=config_obj,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "report": report.model_dump(mode="json"),
        "gate_results": [g.model_dump(mode="json") for g in gate_results],
    }


@activity.defn(name="strategy_lab_run_verification_and_analysis")
def run_verification_and_analysis_activity(
    spec: dict,
    trades: List[dict],
    metrics: dict,
    market_data: Optional[Dict[str, List[dict]]],
    config: dict,
    execution_succeeded: bool,
    trades_aligned: bool,
    alignment_reports: List[dict],
    all_gate_results: List[dict],
    runtime_lookahead_violation: bool,
    open_position_entry_reasons: List[str],
    refinement_attempts: List[str],
    rationale: str,
    convergence_tracker_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Run ``StrategyLabOrchestrator._orchestrate_verification_and_analysis`` whole.

    Preconditions:
        ``spec``/``trades``/``metrics``/``config`` are the corresponding
        models' JSON dumps; ``market_data`` (may be ``None``) maps symbol to
        a list of ``OHLCVBar`` JSON dumps; ``alignment_reports`` is a list of
        ``TradeAlignmentReport`` JSON dumps; ``all_gate_results`` is a list
        of ``QualityGateResult`` JSON dumps already accumulated this attempt;
        ``convergence_tracker_state`` is
        ``dto.convergence_tracker_to_wire``'s output for the batch-level
        tracker (this phase both reads ``trial_count`` and increments it via
        ``increment_trials`` before the acceptance gate runs).
    Postconditions:
        Returns ``{"metrics": ..., "is_winning": bool, "narrative": str,
        "all_gate_results": [...] (extended), "convergence_tracker_state":
        ...} (updated)``. Verification never raises (gate/walk-forward
        failures degrade to a fallback internally); the analysis call
        likewise falls back to a deterministic summary on any LLM failure.
        Raises ``ApplicationError`` only on a genuinely unexpected exception.
    """
    from investment_team.market_data_service import OHLCVBar
    from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult
    from investment_team.strategy_lab.temporal.dto import (
        convergence_tracker_from_wire,
        convergence_tracker_to_wire,
    )

    spec_obj = StrategySpec.parse_persisted(spec)
    trade_objs = [TradeRecord(**t) for t in trades]
    metrics_obj = BacktestResult(**metrics)
    config_obj = BacktestConfig(**config)
    market_data_bars = (
        {sym: [OHLCVBar(**bar) for bar in bars] for sym, bars in market_data.items()}
        if market_data
        else None
    )
    alignment_report_objs = [TradeAlignmentReport.model_validate(r) for r in alignment_reports]
    gate_result_objs = [QualityGateResult.model_validate(g) for g in all_gate_results]

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(convergence_tracker_state)
    try:
        new_metrics, is_winning, narrative = orch._orchestrate_verification_and_analysis(
            spec=spec_obj,
            trades=trade_objs,
            metrics=metrics_obj,
            market_data=market_data_bars,
            config=config_obj,
            execution_succeeded=execution_succeeded,
            trades_aligned=trades_aligned,
            alignment_reports=alignment_report_objs,
            all_gate_results=gate_result_objs,
            runtime_lookahead_violation=runtime_lookahead_violation,
            open_position_entry_reasons=open_position_entry_reasons,
            refinement_attempts=refinement_attempts,
            rationale=rationale,
            emit=lambda *_a, **_kw: None,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "metrics": new_metrics.model_dump(mode="json"),
        "is_winning": is_winning,
        "narrative": narrative,
        "all_gate_results": [g.model_dump(mode="json") for g in gate_result_objs],
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
    }


@activity.defn(name="strategy_lab_assemble_record")
def assemble_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run ``StrategyLabOrchestrator._assemble_record`` to build the final record.

    Preconditions:
        ``params`` carries every keyword ``_assemble_record`` accepts
        (JSON-shaped): ``spec``, ``code``, ``config``, ``metrics``,
        ``trades``, ``narrative``, ``original_spec``, ``original_code``,
        ``rationale``, ``requested_symbols``, ``fetched_symbols``,
        ``provider_used``, ``max_rounds_exhausted``, ``execution_succeeded``,
        ``is_winning``, ``trades_aligned``, ``refinement_rounds``,
        ``alignment_rounds``, ``all_gate_results``,
        ``ran_on_non_conforming_code``, ``design_context`` (a dict with
        ``rounds``/``critiques``/``stop_reason``/``loop_telemetry``),
        ``alignment_findings``, ``phase_back_count``, ``drift_collector`` (a
        dict with ``spec_history``/``code_history``/``gate_timeline``, each a
        list of ``SpecRevision``/``CodeRevision``/``GateEvent`` JSON dumps),
        and ``convergence_tracker_state``
        (``dto.convergence_tracker_to_wire``'s output).
    Postconditions:
        Returns ``{"record": StrategyLabRecord JSON dump,
        "convergence_tracker_state": ...} (updated by the one
        ``self.convergence_tracker.record(...)`` mutation this method
        performs)``. Raises ``ApplicationError`` on an unexpected exception.
    """
    from investment_team.models import (
        AlignmentFinding,
        BacktestConfig,
        BacktestResult,
        CodeRevision,
        GateEvent,
        SpecRevision,
        StrategySpec,
        TradeRecord,
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

    design_context_data = params["design_context"]
    design_context = _DesignPersistContext(
        rounds=design_context_data.get("rounds", 0),
        critiques=[
            SpecCritique.model_validate(c) for c in design_context_data.get("critiques", [])
        ],
        stop_reason=design_context_data.get("stop_reason", ""),
        loop_telemetry=design_context_data.get("loop_telemetry", {}),
    )
    drift_data = params["drift_collector"]
    drift_collector = _DriftCollector(
        spec_history=[SpecRevision(**d) for d in drift_data.get("spec_history", [])],
        code_history=[CodeRevision(**d) for d in drift_data.get("code_history", [])],
        gate_timeline=[GateEvent(**d) for d in drift_data.get("gate_timeline", [])],
    )

    orch = StrategyLabOrchestrator()
    orch.convergence_tracker = convergence_tracker_from_wire(params["convergence_tracker_state"])
    try:
        record = orch._assemble_record(
            spec=StrategySpec.parse_persisted(params["spec"]),
            code=params["code"],
            config=BacktestConfig(**params["config"]),
            metrics=BacktestResult(**params["metrics"]),
            trades=[TradeRecord(**t) for t in params["trades"]],
            narrative=params["narrative"],
            original_spec=StrategySpec.parse_persisted(params["original_spec"]),
            original_code=params["original_code"],
            rationale=params["rationale"],
            requested_symbols=params["requested_symbols"],
            fetched_symbols=params["fetched_symbols"],
            provider_used=params["provider_used"],
            max_rounds_exhausted=params["max_rounds_exhausted"],
            execution_succeeded=params["execution_succeeded"],
            is_winning=params["is_winning"],
            trades_aligned=params["trades_aligned"],
            refinement_rounds=params["refinement_rounds"],
            alignment_rounds=params["alignment_rounds"],
            all_gate_results=[
                QualityGateResult.model_validate(g) for g in params["all_gate_results"]
            ],
            emit=lambda *_a, **_kw: None,
            ran_on_non_conforming_code=params.get("ran_on_non_conforming_code", False),
            design_context=design_context,
            alignment_findings=[
                AlignmentFinding.model_validate(f) for f in (params.get("alignment_findings") or [])
            ],
            phase_back_count=params.get("phase_back_count", 0),
            drift_collector=drift_collector,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_exception_to_application_error(exc) from exc
    return {
        "record": record.model_dump(mode="json"),
        "convergence_tracker_state": convergence_tracker_to_wire(orch.convergence_tracker),
    }


@activity.defn(name="strategy_lab_build_short_circuit_record")
def build_short_circuit_record_activity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run ``StrategyLabOrchestrator._build_short_circuit_record``.

    Preconditions:
        ``params`` carries every keyword ``_build_short_circuit_record``
        accepts (JSON-shaped), mirroring :func:`assemble_record_activity`'s
        ``params`` shape minus the fields a short-circuit never has
        (``metrics``/``trades``/``narrative``/symbol audit/etc.): ``spec``,
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
    resolve_readiness_prices_activity,
    fetch_market_data_activity,
    compute_regime_summary_activity,
    persist_run_state_activity,
    snapshot_prior_records_activity,
    persist_record_activity,
    run_alignment_audit_activity,
    run_verification_and_analysis_activity,
    assemble_record_activity,
    build_short_circuit_record_activity,
]

__all__ = [
    "ACTIVITIES",
    "alignment_near_miss_activity",
    "alignment_propose_fix_activity",
    "analysis_activity",
    "assemble_record_activity",
    "build_short_circuit_record_activity",
    "code_synthesis_activity",
    "compute_regime_summary_activity",
    "design_generate_activity",
    "design_review_activity",
    "design_revise_activity",
    "fetch_market_data_activity",
    "persist_record_activity",
    "persist_run_state_activity",
    "refinement_activity",
    "resolve_readiness_prices_activity",
    "resolve_symbols_activity",
    "run_alignment_audit_activity",
    "run_strategy_code_activity",
    "run_verification_and_analysis_activity",
    "snapshot_prior_records_activity",
    "zero_trade_repair_activity",
]
