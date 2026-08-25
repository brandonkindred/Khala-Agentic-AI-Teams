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
from typing import Any, Dict, List, Optional, Tuple

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
    excluded = ["stocks", "futures", "commodities"]
    allowed = set(PROMPT_ASSET_CLASSES) - set(excluded)
    for _ in range(20):
        picked = select_asset_category(excluded)
        assert picked in allowed


def test_select_none_exclude_picks_from_every_prompt_class() -> None:
    seen: set[str] = set()
    rng = random.Random(0)
    for _ in range(200):
        seen.add(select_asset_category(None, rng=rng))
    assert seen == set(PROMPT_ASSET_CLASSES)


def test_select_single_allowed_class_always_returns_it() -> None:
    excluded = list(PROMPT_ASSET_CLASSES[:-1])
    (single_allowed,) = PROMPT_ASSET_CLASSES[-1:]
    for _ in range(10):
        assert select_asset_category(excluded) == single_allowed


def test_select_is_deterministic_with_a_seeded_rng() -> None:
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    picks_a = [select_asset_category(None, rng=rng_a) for _ in range(10)]
    picks_b = [select_asset_category(None, rng=rng_b) for _ in range(10)]
    assert picks_a == picks_b


def test_select_raises_when_every_class_excluded() -> None:
    with pytest.raises(ValueError, match="no asset category remains"):
        select_asset_category(list(PROMPT_ASSET_CLASSES))


def test_select_avoid_steers_away_from_over_represented_classes() -> None:
    excluded = ["futures", "commodities"]  # allowed = {stocks, crypto, forex}
    avoid = {"stocks", "crypto"}
    for _ in range(20):
        assert select_asset_category(excluded, avoid=avoid) == "forex"


def test_select_avoid_falls_back_to_allowed_when_avoid_covers_everything() -> None:
    # allowed = {forex} only; avoid also names forex — the pin must win
    # rather than raising or silently picking something excluded.
    excluded = ["stocks", "crypto", "futures", "commodities"]
    for _ in range(10):
        assert select_asset_category(excluded, avoid={"forex"}) == "forex"


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


def _spec_dict(
    asset_class: str = "forex", target_symbols: Optional[List[str]] = None
) -> Dict[str, Any]:
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
        "target_symbols": target_symbols or [],
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

    events: List[Tuple[str, Dict[str, Any]]] = []
    orch.run_cycle(
        prior_records=prior_records,
        config=_config(),
        exclude_asset_classes=["stocks", "crypto", "futures", "commodities"],
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["exclude_asset_classes"] == ["stocks", "crypto", "futures", "commodities"]
    assert len(kwargs["prior_records"]) == 2
    assert all(r.strategy.asset_class == "forex" for r in kwargs["prior_records"])

    design_loop_events = [
        data
        for phase, data in events
        if phase == "telemetry" and data.get("scope") == "design_loop"
    ]
    assert design_loop_events
    assert design_loop_events[-1]["asset_category"] == "forex"


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
        # Honor whichever single category the pin left allowed, so this test
        # exercises prior-record scoping without also tripping the
        # asset-category enforcement/regeneration path (covered separately).
        (allowed,) = set(PROMPT_ASSET_CLASSES) - set(kwargs["exclude_asset_classes"])
        return _spec_dict(allowed), "scripted rationale"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    prior_records = [_record("stocks"), _record("crypto"), _record("forex")]

    events: List[Tuple[str, Dict[str, Any]]] = []
    # allowed = {stocks, crypto} -> exclude = {forex, futures, commodities}
    orch.run_cycle(
        prior_records=prior_records,
        config=_config(),
        exclude_asset_classes=["forex", "futures", "commodities"],
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert len(captured) == 1
    kwargs = captured[0]
    excluded = set(kwargs["exclude_asset_classes"])
    allowed_in_prompt_classes = set(PROMPT_ASSET_CLASSES) - excluded
    assert len(allowed_in_prompt_classes) == 1
    (selected_category,) = allowed_in_prompt_classes
    assert selected_category in ("stocks", "crypto")
    assert all(r.strategy.asset_class == selected_category for r in kwargs["prior_records"])

    design_loop_events = [
        data
        for phase, data in events
        if phase == "telemetry" and data.get("scope") == "design_loop"
    ]
    assert design_loop_events
    assert design_loop_events[-1]["asset_category"] == selected_category


# ---------------------------------------------------------------------------
# Deterministic backstop: the design agent's prompt-only exclusion rule is
# not itself a guarantee the LLM honors it. ``_enforce_selected_asset_category``
# must correct a mismatched ``asset_class`` after both the initial generation
# and any ``revise`` round, so a strategy can never persist in a category the
# user did not select for this attempt.
# ---------------------------------------------------------------------------


def test_design_loop_regenerates_full_spec_when_correction_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary correction path: a category-pin violation spends one
    ``DesignAgent.revise`` call asking for a full regeneration (not just a
    relabel). When that regeneration honors the pin, the regenerated spec —
    hypothesis, rationale and all — is used as-is; no deterministic
    overwrite or ``target_symbols`` clearing is needed."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("forex"), "scripted"))

    revise_calls: List[Any] = []

    def _revise(spec: Any, critique: Any, **kwargs: Any) -> Tuple[Dict[str, Any], str]:
        revise_calls.append((spec, critique, kwargs))
        return (
            _spec_dict("stocks", target_symbols=["AAPL"]),
            "regenerated for stocks",
        )

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
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

    assert len(revise_calls) == 1
    _, critique, kwargs = revise_calls[0]
    assert critique.ready is False
    assert "stocks" in critique.issues[0].description
    # The reconciliation call is deliberately kept out of the round loop's
    # own critique lineage — it's a pin-violation correction, not a review
    # round, so it must not be threaded prior_critiques.
    assert kwargs.get("prior_critiques") is None

    assert record.strategy.asset_class == "stocks"
    assert record.strategy.target_symbols == ["AAPL"]
    assert record.strategy_rationale == "regenerated for stocks"


def test_design_loop_enforces_asset_category_when_correction_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When even the corrective regeneration still violates the pin, the
    deterministic relabel backstop (``_enforce_selected_asset_category``)
    is the bounded last resort — the pin is guaranteed to hold within one
    extra LLM call, never an unbounded retry loop.

    Critically, the fallback must relabel the *original* pre-regeneration
    ``(spec, rationale)`` pair, not the discarded regeneration attempt: the
    designer's ``revise`` stub below returns a *different* rationale
    ("revised", for a still-wrong "crypto" spec) than the original ``run``
    call ("scripted", for "forex") — if the fallback paired the relabeled
    ("stocks") spec with the discarded "revised" rationale, the persisted
    record would narrate crypto next to a stocks label, reproducing the
    exact "wrong thesis" bug this whole mechanism exists to prevent.
    """
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("forex"), "scripted"))
    # The corrective regeneration also violates the pin, and with a
    # *different* wrong category than the original — simulating a designer
    # that ignores both the MANDATORY EXCLUSION instruction and the
    # explicit corrective critique.
    monkeypatch.setattr(
        orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict("crypto"), "revised")
    )
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {stocks} -> exclude everything else, including forex/crypto.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "stocks"
    assert record.strategy_rationale == "scripted"


def test_design_loop_clears_stale_target_symbols_when_category_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the deterministic-relabel backstop fires (the corrective
    regeneration also failed), any explicit ``target_symbols`` authored for
    the wrong category must be cleared too — otherwise e.g. crypto tickers
    like "BTC-USD" would be honored verbatim and fetched under the
    corrected (forex) asset class, a structurally inconsistent request."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent,
        "run",
        lambda **_kw: (_spec_dict("crypto", target_symbols=["BTC-USD"]), "scripted"),
    )
    # The corrective regeneration also ignores the pin, forcing the fallback
    # to the deterministic relabel where target_symbols clearing lives.
    monkeypatch.setattr(
        orch.design_agent,
        "revise",
        lambda *_a, **_kw: (_spec_dict("crypto", target_symbols=["BTC-USD"]), "revised"),
    )
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {forex} -> the design agent ignores the pin and returns
    # crypto with explicit crypto symbols.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["stocks", "crypto", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "forex"
    assert record.strategy.target_symbols == []


def test_design_loop_enforces_asset_category_on_budget_exhausted_before_first_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM-call budget trips before ``DesignAgent.run`` ever
    returns a spec, ``_run_design_loop`` falls back to a defaults spec built
    from ``{}`` — which defaults ``asset_class`` to "stocks". That fallback
    bypasses the per-response enforcement below the ``design_agent.run``/
    ``revise`` calls, so the short-circuit record must still be corrected to
    the category pinned for this attempt before it is persisted."""
    from investment_team.strategy_lab.agents._llm_budget import DesignBudgetExhausted

    orch = StrategyLabOrchestrator()

    def _run(**_kw: Any) -> Tuple[Dict[str, Any], str]:
        raise DesignBudgetExhausted(1, 1)

    monkeypatch.setattr(orch.design_agent, "run", _run)

    # allowed = {forex} -> exclude everything else.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["stocks", "crypto", "futures", "commodities"],
    )

    assert record.backtest.status == "failed: budget_exhausted"
    assert record.strategy.asset_class == "forex"


def test_design_loop_enforces_asset_category_when_correction_trips_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A category-pin violation's corrective ``revise`` call is a real LLM
    call and can itself trip the per-cycle budget. The trip must still
    short-circuit with the pin enforced (via the outer budget handler's
    ``_enforce_selected_asset_category`` fallback on the pre-regeneration
    spec), not crash or silently persist the mismatched category."""
    from investment_team.strategy_lab.agents._llm_budget import DesignBudgetExhausted

    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("forex"), "scripted"))

    def _revise(*_a: Any, **_kw: Any) -> Tuple[Dict[str, Any], str]:
        raise DesignBudgetExhausted(1, 1)

    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    # allowed = {stocks} -> exclude everything else, including forex.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
    )

    assert record.backtest.status == "failed: budget_exhausted"
    assert record.strategy.asset_class == "stocks"


def test_reconcile_asset_category_forwards_mechanical_repair_count_to_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget trip inside the corrective ``revise`` call must annotate the
    exception with the caller's real accumulated ``mechanical_repair_count``
    (e.g. repairs already applied earlier in the same design-review round),
    not a hardcoded 0 — otherwise persisted budget-exhausted telemetry
    under-reports repairs that already happened this cycle. Exercised as a
    direct unit test (rather than through ``run_cycle``) since manufacturing
    a real mechanical repair plus a mid-loop pin violation plus a budget
    trip through the full pipeline would require an elaborate multi-round
    setup for a fix that is purely about one forwarded argument."""
    from investment_team.strategy_lab.agents._llm_budget import DesignBudgetExhausted

    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(_spec_dict("forex"), strategy_id="test-strat")

    def _revise(*_a: Any, **_kw: Any) -> Tuple[Dict[str, Any], str]:
        raise DesignBudgetExhausted(1, 1)

    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    with pytest.raises(DesignBudgetExhausted) as exc_info:
        orch._reconcile_asset_category(
            spec,
            "scripted",
            selected_category="stocks",
            review_round=1,
            emit=lambda *_a, **_kw: None,
            drift_collector=None,
            config=_config(),
            strategy_id="test-strat",
            all_gate_results=[],
            mechanical_repair_count=2,
        )

    assert exc_info.value.mechanical_repair_count == 2


def test_reconcile_asset_category_records_readiness_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success-path readiness validation must be appended to
    ``all_gate_results`` via ``record_gates``, not just used locally to
    compute the ``design_repair`` event's ``now_ready`` flag — otherwise the
    persisted record's ``quality_gate_results`` and the convergence tracker
    silently miss this gate's findings for the attempt."""
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(_spec_dict("forex"), strategy_id="test-strat")

    monkeypatch.setattr(
        orch.design_agent, "revise", lambda *_a, **_kw: (_spec_dict("stocks"), "regenerated")
    )

    all_gate_results: List[Any] = []
    orch._reconcile_asset_category(
        spec,
        "scripted",
        selected_category="stocks",
        review_round=1,
        emit=lambda *_a, **_kw: None,
        drift_collector=None,
        config=_config(),
        strategy_id="test-strat",
        all_gate_results=all_gate_results,
    )

    assert all_gate_results


def test_enforce_selected_asset_category_records_readiness_gates() -> None:
    """Same ``record_gates`` requirement for the deterministic-relabel
    backstop — its own fresh readiness validation must also land in
    ``all_gate_results``, not only feed the emitted event's ``now_ready``."""
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(_spec_dict("forex"), strategy_id="test-strat")

    all_gate_results: List[Any] = []
    orch._enforce_selected_asset_category(
        spec,
        selected_category="stocks",
        review_round=1,
        emit=lambda *_a, **_kw: None,
        drift_collector=None,
        config=_config(),
        all_gate_results=all_gate_results,
    )

    assert all_gate_results


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


# ---------------------------------------------------------------------------
# Reconciling the category pin with the convergence tracker's diversity
# directive — the two steering mechanisms must not hand the design agent
# contradictory "MANDATORY" instructions about which asset class to use.
# ---------------------------------------------------------------------------


def _skew_convergence_tracker_toward(orch: StrategyLabOrchestrator, asset_class: str) -> None:
    for _ in range(5):
        orch.convergence_tracker.record(
            StrategySpec(
                strategy_id=f"s-{uuid.uuid4().hex[:6]}",
                authored_by="test",
                asset_class=asset_class,
                hypothesis="h",
                signal_definition="sig",
                timeframe="1d",
                entry_rules=[
                    EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))
                ],
                exit_rules=[StopLossRule(pct=0.03)],
                risk_limits={},
                speculative=False,
            ),
            [],
        )


def test_design_loop_drops_diversity_directive_when_pin_forces_over_represented_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only one category is allowed and it happens to be the class the
    convergence tracker's diversity directive says to avoid, the pin must
    win — the now-impossible-to-satisfy "MUST choose a DIFFERENT asset
    class" directive must be dropped rather than left to contradict the
    "only <category> is allowed" exclusion rule in the same prompt."""
    orch = StrategyLabOrchestrator()
    _skew_convergence_tracker_toward(orch, "stocks")
    assert orch.convergence_tracker.get_diversity_avoid_classes() == {"stocks"}

    captured: List[Dict[str, Any]] = []

    def _run(**kwargs: Any) -> Tuple[Dict[str, Any], str]:
        captured.append(kwargs)
        return _spec_dict("stocks"), "scripted"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {stocks} only -> the pin forces stocks despite it being
    # over-represented.
    orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["crypto", "forex", "futures", "commodities"],
    )

    assert len(captured) == 1
    convergence_directives = captured[0]["convergence_directives"] or []
    assert not any("You MUST choose a DIFFERENT asset class" in d for d in convergence_directives)


def test_design_loop_keeps_diversity_directive_when_pin_avoids_over_represented_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pin can honor the diversity directive (another allowed
    category exists), the directive is preserved verbatim — only a genuine
    conflict (tested above) drops it."""
    orch = StrategyLabOrchestrator()
    _skew_convergence_tracker_toward(orch, "stocks")
    assert orch.convergence_tracker.get_diversity_avoid_classes() == {"stocks"}

    captured: List[Dict[str, Any]] = []

    def _run(**kwargs: Any) -> Tuple[Dict[str, Any], str]:
        captured.append(kwargs)
        return _spec_dict("forex"), "scripted"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {stocks, forex} -> the bias in select_asset_category can (and
    # given the stub above, does) avoid the over-represented "stocks".
    orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["crypto", "futures", "commodities"],
    )

    assert len(captured) == 1
    convergence_directives = captured[0]["convergence_directives"] or []
    assert any("You MUST choose a DIFFERENT asset class" in d for d in convergence_directives)
