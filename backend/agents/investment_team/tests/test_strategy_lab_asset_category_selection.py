"""Tests for pinning each design attempt to a single, randomly-selected
asset category, and scoping the prior-results context handed to the design
agent to only that category.

Before this behavior, the design agent's "Prior Strategy Results" context
was built from every previously generated strategy across every asset
category, regardless of what the user selected when starting the run — the
design agent could reason over (and drift into generating for) categories
the user never asked for. ``select_asset_category`` pins one category per
design attempt (recovered from ``exclude_asset_classes``, the complement of
the user's ``allowed_asset_classes`` selection — see
``test_strategy_lab_allowed_categories.py``), and
``filter_records_by_asset_class`` scopes ``prior_records`` to that category
before it reaches ``DesignAgent.run``.
"""

from __future__ import annotations

import random
import uuid
from typing import Any, Dict, List, Tuple

import pytest

from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.agents.design_review import SpecCritique
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule
from investment_team.strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    filter_records_by_asset_class,
    select_asset_category,
)

pytestmark = pytest.mark.strategy_lab_integration


# ---------------------------------------------------------------------------
# select_asset_category
# ---------------------------------------------------------------------------


def test_select_returns_a_value_from_the_allowed_set() -> None:
    for _ in range(20):
        picked = select_asset_category(["stocks", "futures", "commodities"])
        assert picked in ("crypto", "forex")


def test_select_none_exclude_picks_from_every_prompt_class() -> None:
    seen: set[str] = set()
    rng = random.Random(0)
    for _ in range(200):
        seen.add(select_asset_category(None, rng=rng))
    assert seen == set(PROMPT_ASSET_CLASSES)


def test_select_single_allowed_class_always_returns_it() -> None:
    for _ in range(10):
        assert select_asset_category(["stocks", "crypto", "futures", "commodities"]) == "forex"


def test_select_is_deterministic_with_a_seeded_rng() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    picks_a = [select_asset_category(None, rng=rng_a) for _ in range(10)]
    picks_b = [select_asset_category(None, rng=rng_b) for _ in range(10)]
    assert picks_a == picks_b


def test_select_raises_when_every_class_excluded() -> None:
    with pytest.raises(ValueError, match="no asset category remains"):
        select_asset_category(list(PROMPT_ASSET_CLASSES))


# ---------------------------------------------------------------------------
# filter_records_by_asset_class
# ---------------------------------------------------------------------------


def _stub_backtest_result() -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=5.0,
        volatility_pct=12.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=-3.0,
        win_rate_pct=55.0,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _record(asset_class: str) -> StrategyLabRecord:
    from investment_team.api.main import _now

    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"s-{suffix}",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = _now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{suffix}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        status="completed",
        result=_stub_backtest_result(),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


def test_filter_keeps_only_matching_category() -> None:
    records = [_record("stocks"), _record("crypto"), _record("stocks"), _record("forex")]
    out = filter_records_by_asset_class(records, "stocks")
    assert len(out) == 2
    assert all(r.strategy.asset_class == "stocks" for r in out)


def test_filter_normalizes_aliases_before_comparing() -> None:
    # A legacy/alias asset_class value on a persisted record must still match
    # the canonical selected category.
    records = [_record("equity")]
    assert filter_records_by_asset_class(records, "stocks") == records


def test_filter_empty_records_returns_empty() -> None:
    assert filter_records_by_asset_class([], "stocks") == []


def test_filter_preserves_input_order() -> None:
    records = [_record("stocks") for _ in range(3)]
    out = filter_records_by_asset_class(records, "stocks")
    assert [r.lab_record_id for r in out] == [r.lab_record_id for r in records]


# ---------------------------------------------------------------------------
# Integration: _run_design_loop pins one category and scopes prior_records
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _spec_dict(asset_class: str = "forex") -> Dict[str, Any]:
    return {
        "asset_class": asset_class,
        "hypothesis": "RSI mean reversion on a small universe",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(side="long", when=Predicate(lhs="bar.close", op="<", rhs=30)).model_dump()
        ],
        "exit_rules": [StopLossRule(pct=0.03).model_dump()],
        "risk_limits": {},
        "speculative": False,
    }


def _short_circuit_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        StrategyLabOrchestrator,
        "_fetch_market_data",
        lambda *_a, **_kw: _MarketDataFetch(data=None, requested_symbols=[], fetched_symbols=[]),
    )


def test_design_loop_pins_single_category_and_scopes_prior_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``allowed_asset_classes=["forex"]`` (expressed downstream as
    ``exclude_asset_classes`` excluding everything else), the design agent
    must be called with exactly ``["stocks", "crypto", "futures",
    "commodities"]`` excluded (i.e. pinned to forex) and with
    ``prior_records`` scoped to forex-only priors — the stocks/crypto priors
    must never reach it.
    """
    orch = StrategyLabOrchestrator()

    captured: List[Dict[str, Any]] = []

    def _run(**kwargs: Any) -> Tuple[Dict[str, Any], str]:
        captured.append(kwargs)
        return _spec_dict(), "scripted rationale"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    prior_records = [_record("forex"), _record("forex"), _record("stocks"), _record("crypto")]

    orch.run_cycle(
        prior_records=prior_records,
        config=_config(),
        exclude_asset_classes=["stocks", "crypto", "futures", "commodities"],
    )

    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["exclude_asset_classes"] == ["stocks", "crypto", "futures", "commodities"]
    assert len(kwargs["prior_records"]) == 2
    assert all(r.strategy.asset_class == "forex" for r in kwargs["prior_records"])


def test_design_loop_pins_one_of_several_allowed_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With multiple allowed categories, the design agent is still pinned to
    exactly one per attempt (a single-category exclude list, not the
    original multi-category exclude list), and ``prior_records`` is scoped
    to that one category only.
    """
    orch = StrategyLabOrchestrator()

    captured: List[Dict[str, Any]] = []

    def _run(**kwargs: Any) -> Tuple[Dict[str, Any], str]:
        captured.append(kwargs)
        return _spec_dict(), "scripted rationale"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    prior_records = [_record("stocks"), _record("crypto"), _record("forex")]

    # allowed = {stocks, crypto} -> exclude = {forex, futures, commodities}
    orch.run_cycle(
        prior_records=prior_records,
        config=_config(),
        exclude_asset_classes=["forex", "futures", "commodities"],
    )

    assert len(captured) == 1
    kwargs = captured[0]
    excluded = set(kwargs["exclude_asset_classes"])
    allowed_in_prompt_classes = set(PROMPT_ASSET_CLASSES) - excluded
    assert len(allowed_in_prompt_classes) == 1
    (selected_category,) = allowed_in_prompt_classes
    assert selected_category in ("stocks", "crypto")
    assert all(r.strategy.asset_class == selected_category for r in kwargs["prior_records"])


# ---------------------------------------------------------------------------
# Deterministic backstop: the design agent's prompt-only exclusion rule is
# not itself a guarantee the LLM honors it. ``_enforce_selected_asset_category``
# must correct a mismatched ``asset_class`` after both the initial generation
# and any ``revise`` round, so a strategy can never persist in a category the
# user did not select for this attempt.
# ---------------------------------------------------------------------------


def test_design_loop_enforces_asset_category_when_generation_ignores_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = StrategyLabOrchestrator()

    # The design agent always returns "forex" regardless of the category
    # actually pinned for this attempt (the LLM ignoring the MANDATORY
    # EXCLUSION instruction in the prompt).
    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("forex"), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {stocks} -> exclude everything else, including forex.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "stocks"


def test_design_loop_enforces_asset_category_after_revise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enforcement backstop applies after a ``revise`` round too, not
    only the initial generation — closing the gap where a mid-loop revision
    could otherwise drift the spec's asset class away from the category
    pinned for this attempt."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("stocks"), "scripted"))

    review_calls = iter(
        [
            SpecCritique(ready=False, rationale="round-0"),
            SpecCritique(ready=True, rationale="round-1 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *_a, **_kw: next(review_calls))
    # revise() drifts the asset_class to "forex" even though "stocks" is pinned.
    monkeypatch.setattr(
        orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict("forex"), "revised")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "stocks"


def test_design_loop_leaves_matching_asset_class_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No spurious correction (and no ``design_repair`` telemetry) when the
    design agent already honors the pinned category."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("stocks"), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    events: List[Tuple[str, Dict[str, Any]]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert record.strategy.asset_class == "stocks"
    assert not any(phase == "design_repair" for phase, _ in events)


def test_design_loop_telemetry_includes_asset_category_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live ``on_phase`` telemetry consumers see ``asset_category`` on the
    ``scope=design_loop`` summary event for a normal (non-budget-exhausted)
    exit — not only in the persisted record's design context."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("stocks"), "scripted"))
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    events: List[Tuple[str, Dict[str, Any]]] = []
    orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    design_loop_events = [
        data
        for phase, data in events
        if phase == "telemetry" and data.get("scope") == "design_loop"
    ]
    assert design_loop_events
    assert design_loop_events[-1]["asset_category"] == "stocks"
