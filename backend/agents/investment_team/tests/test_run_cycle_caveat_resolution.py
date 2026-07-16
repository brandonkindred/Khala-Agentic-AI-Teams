"""``run_cycle`` resolution of caveats into ``is_winning`` / ``acceptance_reason``.

Under the deterministic verdict, a run whose return clears the benchmark is
WINNING even when it also carries robustness caveats — alignment-loop
failures, a disabled walk-forward check, a fallback-anomaly rejection or
admission, or a zero-trade run. This file drives ``run_cycle`` end-to-end
(via the shared ``wire_run_cycle_stubs`` test double) for each of those
caveat scenarios and asserts the caveat is recorded on ``acceptance_reason``
/ ``quality_gate_results`` without flipping the verdict it shouldn't flip.
Walk-forward evaluation mechanics are covered in
``test_walk_forward_evaluation.py``; acceptance-gate-result composition is
covered in ``test_acceptance_gate_integration.py``.
"""

from __future__ import annotations

from investment_team.models import StrategyLabRecord
from investment_team.strategy_lab.quality_gates.models import QualityGateResult

from ._walk_forward_test_helpers import (
    StubMarketDataService,
    orchestrator,
    raise_walk_forward,
    wire_run_cycle_stubs,
)

# ``config`` keeps its leading-underscore alias: every test below assigns
# its result to a local variable named ``config``, and dropping the alias
# would make ``config = config(...)`` an UnboundLocalError (assigning a name
# anywhere in a function makes every reference to it local within that
# function, including the assignment's own right-hand side).
from ._walk_forward_test_helpers import config as _config


def test_failed_alignment_records_caveat_on_acceptance_path(monkeypatch):
    """A final alignment report with ``aligned=False`` is recorded as a caveat
    (the ``trade_alignment`` gate stays passed=False on ``quality_gate_results``
    and ``alignment_unresolved`` is stamped on ``acceptance_reason``), but under
    the deterministic verdict the 15% (>= 8%) run is still WINNING. The
    resolved verdict is threaded to AnalysisAgent so the narrative surfaces the
    misalignment as a caveat rather than calling the strategy a loss."""

    orch = orchestrator(StubMarketDataService())

    # Stub the acceptance gate to return all-passing (so alignment is the
    # only veto in play). Also stub walk-forward evaluation to leave the
    # metrics untouched so we don't have to construct a full OOS payload.
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                phase="verification",
                details="DSR passes",
            ),
            QualityGateResult(
                gate_name="is_oos_degradation",
                passed=True,
                severity="info",
                phase="verification",
                details="IS->OOS within tolerance",
            ),
        ],
    )

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="entries fire before signal triggers",
    )

    # Pin the orchestrator -> AnalysisAgent wiring: if the call ever drops
    # the ``is_winning`` kwarg the narrative would silently re-derive
    # WINNING from metrics. The wide-stub above (``lambda *a, **k:
    # "narrative"``) would happily absorb that regression, so capture and
    # assert the kwarg directly here.
    captured_analysis_kwargs: dict = {}

    def _recording_analysis(*_args, **kwargs):
        captured_analysis_kwargs.update(kwargs)
        return "narrative"

    monkeypatch.setattr(orch.analysis_agent, "run", _recording_analysis)

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: 15% >= the 8% benchmark on a valid run → WINNING; the
    # alignment failure is recorded as a caveat, not a label-flip.
    assert record.is_winning is True
    alignment_gates = [
        g for g in record.quality_gate_results if g.get("gate_name") == "trade_alignment"
    ]
    assert alignment_gates, "trade_alignment gate must appear in quality_gate_results"
    assert all(g["passed"] is False for g in alignment_gates)
    assert record.analysis_narrative  # narrative still generated for context
    reason = record.backtest.result.acceptance_reason or ""
    assert "alignment_unresolved" in reason
    assert "entries fire before signal triggers" in reason
    # When the acceptance gate fully passed but alignment vetoed, the
    # ``"all four criteria met"`` summary is no longer truthful — the
    # alignment cause must REPLACE it, not appear alongside it.
    assert "all four criteria met" not in reason, (
        "acceptance_reason must not contradict itself: when acceptance "
        "fully passed but alignment vetoed, the alignment cause replaces "
        "the now-stale 'all four criteria met' summary."
    )
    # The orchestrator must thread the resolved verdict through so the
    # narrative template + outcome_label match the persisted record (now the
    # deterministic WINNING verdict, with alignment surfaced as a caveat).
    assert captured_analysis_kwargs.get("is_winning") is True, (
        "orchestrator must pass the deterministic is_winning=True to "
        "AnalysisAgent.run; the alignment failure is a caveat, not a veto."
    )
    # The alignment-augmentation block must not bind a local ``rationale``
    # that shadows the strategy rationale, which would corrupt both the
    # analysis prompt and ``StrategyLabRecord.strategy_rationale`` on every
    # alignment-failure path. The ideation stub returns ``"rationale"`` as
    # the strategy rationale; if the shadowing came back,
    # ``record.strategy_rationale`` would be ``"entries fire before signal
    # triggers"`` instead.
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_failed_alignment_records_caveat_on_walk_forward_fallback(monkeypatch):
    """The walk-forward fallback branch (entered when ``_evaluate_walk_forward``
    raised) records an alignment failure as a caveat too: ``alignment_unresolved``
    replaces the fallback success summary on ``acceptance_reason``. Under the
    deterministic verdict the 15% (>= 8%) run is still WINNING — alignment is a
    caveat, not a veto."""

    orch = orchestrator(StubMarketDataService())

    def _raise(*args, **kwargs):
        raise RuntimeError("walk-forward fold construction failed (synthetic)")

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise)

    # Stub anomaly detector to return only info-severity (no critical) so
    # the fallback branch would have marked is_winning=True absent the
    # alignment check.
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **kw: [
            _QGR(
                gate_name="sharpe_sane",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            )
        ],
    )

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="trades hit wrong symbol",
    )

    # Pin the orchestrator -> AnalysisAgent wiring on this branch too — the
    # call site is shared, so a regression on either path is the same root
    # cause, but defensive duplication makes the failure mode obvious from
    # either test.
    captured_analysis_kwargs: dict = {}

    def _recording_analysis(*_args, **kwargs):
        captured_analysis_kwargs.update(kwargs)
        return "narrative"

    monkeypatch.setattr(orch.analysis_agent, "run", _recording_analysis)

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: 15% >= the 8% benchmark on a valid run → WINNING.
    assert record.is_winning is True
    alignment_gates = [
        g for g in record.quality_gate_results if g.get("gate_name") == "trade_alignment"
    ]
    assert alignment_gates and all(g["passed"] is False for g in alignment_gates)
    reason = record.backtest.result.acceptance_reason or ""
    assert "alignment_unresolved" in reason
    # The anomaly recheck admitted the run (no criticals + return >= threshold),
    # so its ``"walk_forward_fallback_passed: ..."`` summary is superseded once
    # alignment is recorded. The augmentation block REPLACES it (using
    # ``upstream_admitted``) rather than appending.
    assert "walk_forward_fallback_passed" not in reason, (
        "fallback success summary must be replaced (not appended) when "
        "an alignment caveat is recorded on a fallback-admitted run."
    )
    assert captured_analysis_kwargs.get("is_winning") is True, (
        "orchestrator must thread the deterministic is_winning=True on the "
        "walk-forward fallback path too; alignment is a caveat."
    )
    # See the parallel assertion in the acceptance-path test above for the
    # full story on this rationale-shadowing regression guard.
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_acceptance_failures_and_alignment_failure_both_recorded(monkeypatch):
    """When the acceptance gate has real failure reasons AND alignment also
    fails, both caveats must survive on the audit trail joined with ``" | "``
    so a downstream parser can disambiguate the alignment boundary from
    ``summarize_acceptance_reason``'s internal ``"; "`` joiner. Under the
    deterministic verdict the 15% (>= 8%) run is still WINNING — both robustness
    failures are recorded as caveats, not vetoes."""

    orch = orchestrator(StubMarketDataService())

    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    # Acceptance gate returns two failing entries — their ``details``
    # strings get joined with ``"; "`` by ``summarize_acceptance_reason``.
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=False,
                severity="critical",
                phase="verification",
                details="DSR below threshold",
            ),
            QualityGateResult(
                gate_name="oos_trade_count",
                passed=False,
                severity="critical",
                phase="verification",
                details="trade count below floor",
            ),
        ],
    )

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="entries fire on wrong symbol",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: 15% >= the 8% benchmark on a valid run → WINNING, even
    # though acceptance failed. Both failures are preserved as caveats.
    assert record.is_winning is True
    reason = record.backtest.result.acceptance_reason or ""
    # Both acceptance failures preserved (joined internally with "; ")
    assert "DSR below threshold" in reason
    assert "trade count below floor" in reason
    # Alignment cause appended after a " | " boundary so the categories
    # are distinguishable.
    assert " | alignment_unresolved:" in reason, (
        f"expected ' | alignment_unresolved:' boundary in {reason!r}"
    )
    assert "entries fire on wrong symbol" in reason
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_walk_forward_disabled_wins_by_return_records_caveat(monkeypatch):
    """A run with ``walk_forward_enabled=False`` skips the out-of-sample
    acceptance check. Under the deterministic verdict it still WINS when its
    15% return clears the 8% benchmark (a valid run that executed + traded),
    and the audit trail records that walk-forward was disabled as a caveat so
    a reviewer knows the win was not out-of-sample validated."""

    orch = orchestrator(StubMarketDataService())

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=False)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: 15% >= the 8% benchmark on a valid run → WINNING even with
    # walk-forward disabled (the skipped OOS check is a caveat, not a veto).
    assert record.is_winning is True
    assert record.analysis_narrative
    # The persisted record must record that walk-forward was disabled.
    # Permissive substring match so future wording tweaks don't break the test.
    reason = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in reason and "walk_forward_enabled=False" in reason, (
        f"expected publication_disabled / walk_forward_enabled=False in {reason!r}"
    )


def test_walk_forward_fallback_rejected_records_acceptance_reason(monkeypatch):
    """When the walk-forward fallback flags a critical anomaly (e.g. upgraded
    ``Sharpe > 5.0``), the persisted ``acceptance_reason`` must mirror that
    cause — a consumer reading the field alone shouldn't have to grep
    ``quality_gate_results`` for ``fallback_`` entries. Under the deterministic
    verdict the 15% (>= 8%) run is still WINNING; the anomaly is the recorded
    caveat."""

    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    orch = orchestrator(StubMarketDataService())

    monkeypatch.setattr(orch, "_evaluate_walk_forward", raise_walk_forward)

    # ``anomaly_detector.check`` is called twice in this flow: once
    # inside the refinement loop with ``dsr_aware=True`` (because
    # ``walk_forward_enabled=True``), and again on the fallback path
    # with ``dsr_aware=False``. We only want a critical to surface on
    # the second call; on the first, the loop would otherwise refine
    # forever and exhaust the refinement-round budget.
    def _anomaly_stub(*_a, **kw):
        if kw.get("dsr_aware", False):
            return []  # refinement-time: pass through
        return [
            _QGR(
                gate_name="sharpe_sane",
                passed=False,
                severity="critical",
                phase="verification",
                details="Sharpe ratio 6.40 > 5.0 (overfit suspect)",
            )
        ]

    monkeypatch.setattr(orch.anomaly_detector, "check", _anomaly_stub)

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # Caveats-only: 15% >= the 8% benchmark on a valid run → WINNING; the
    # fallback anomaly is recorded on acceptance_reason as the caveat.
    assert record.is_winning is True
    reason = record.backtest.result.acceptance_reason or ""
    assert reason.startswith("walk_forward_fallback_rejected:"), (
        f"expected walk_forward_fallback_rejected prefix in {reason!r}"
    )
    assert "Sharpe ratio 6.40" in reason


def test_walk_forward_fallback_passed_records_provenance(monkeypatch):
    """When the walk-forward fallback admits a run (no critical anomalies,
    return above threshold, alignment passes), the persisted
    ``acceptance_reason`` must record that provenance so the audit trail
    distinguishes a primary-acceptance-gate winner from a fallback-admitted
    winner."""

    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    orch = orchestrator(StubMarketDataService())

    monkeypatch.setattr(orch, "_evaluate_walk_forward", raise_walk_forward)
    # Anomalies all info-severity — fallback_criticals stays empty.
    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **kw: [
            _QGR(
                gate_name="sharpe_sane",
                passed=True,
                severity="info",
                phase="verification",
                details="ok",
            )
        ],
    )

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is True
    reason = record.backtest.result.acceptance_reason or ""
    assert "walk_forward_fallback_passed" in reason, (
        f"expected fallback-passed provenance in {reason!r}"
    )


def test_no_trades_produced_records_acceptance_reason(monkeypatch):
    """A run that drives ``execution_succeeded=True`` with zero trades (e.g.
    when the upstream gates have been stubbed out, or in production when an
    alignment-loop fix produces an empty ledger) can't publish — walk-forward
    and alignment both require trades. The persisted ``acceptance_reason``
    must explain this rather than leave an empty field."""

    orch = orchestrator(StubMarketDataService())

    # Bypass the gates that normally veto zero-trade runs during
    # refinement so we can drive ``execution_succeeded=True`` with
    # ``trades=[]`` and exercise the zero-trade else-branch entry path.
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_trades", lambda *a, **k: [])
    monkeypatch.setattr(orch.anomaly_detector, "check", lambda *a, **kw: [])

    wire_run_cycle_stubs(
        orch,
        monkeypatch,
        # ``alignment_aligned`` is irrelevant — the alignment loop is
        # skipped when ``trades`` is empty.
        alignment_aligned=True,
        alignment_rationale="n/a",
        trades_override=[],
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    reason = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in reason and "no trades produced" in reason, (
        f"expected publication_disabled / no trades produced in {reason!r}"
    )
