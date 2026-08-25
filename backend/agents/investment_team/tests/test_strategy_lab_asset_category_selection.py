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


def test_select_degrades_to_full_menu_when_every_class_excluded() -> None:
    """A degenerate exclusion covering every category must not crash the
    cycle: nothing on the ``_run_design_loop`` -> ``run_cycle`` path catches a
    ValueError (they catch only DesignBudgetExhausted / SpecImplementabilityError),
    so raising here would abort the whole run with no persisted record. The
    sibling helper for the same input, ``asset_class_mix_hint``, deliberately
    degrades to the full menu; match that fail-open convention."""
    picked = select_asset_category(list(PROMPT_ASSET_CLASSES))
    assert picked in PROMPT_ASSET_CLASSES


def test_select_normalizes_aliases_in_the_exclusion() -> None:
    """Aliases are accepted everywhere else via normalize_asset_class, and the
    pin turns the recovered allowed set into a HARD constraint — so an alias
    that matched nothing would let the pin land on the very class the caller
    excluded and then mandate it in the prompt."""
    for _ in range(30):
        # "equity" -> stocks, "fx" -> forex; both must actually be excluded.
        assert select_asset_category(["equity", "fx"]) not in ("stocks", "forex")


def test_select_ignores_a_bare_string_rather_than_iterating_characters() -> None:
    """``str`` is itself iterable, so a bare string must not be character-
    iterated into a meaningless exclusion set that silently inverts the
    caller's intent."""
    # No character of "stocks" resolves to a class, so nothing is excluded and
    # every category stays selectable — but crucially the function must not
    # crash or treat {'s','t','o','c','k'} as meaningful.
    picked = select_asset_category(["stocks"])
    assert picked in PROMPT_ASSET_CLASSES and picked != "stocks"


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


def test_design_loop_drops_offcategory_symbols_from_a_relabeled_regeneration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching ``asset_class`` on the regenerated spec is NOT proof of a
    genuine rebuild. ``_REVISION_USER_TEMPLATE`` instructs the designer to
    "preserve every aspect of the spec that was NOT criticised", so the common
    partial-compliance outcome is a spec relabeled to the pinned category that
    still carries the wrong category's tickers. Nothing downstream rejects
    that — ``resolve_strategy_symbols`` honors a non-empty ``target_symbols``
    verbatim (mismatch is only a logged warning) and no readiness rule checks
    symbol-vs-class — so those tickers would be fetched and backtested under
    the pinned label, persisting the exact out-of-category record this whole
    mechanism exists to prevent, on the primary success path."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent,
        "run",
        lambda **_kw: (_spec_dict("crypto", target_symbols=["BTC-USD", "ETH-USD"]), "scripted"),
    )
    # The corrective revise flips only asset_class and copies the crypto
    # tickers through verbatim.
    monkeypatch.setattr(
        orch.design_agent,
        "revise",
        lambda *_a, **_kw: (
            _spec_dict("stocks", target_symbols=["BTC-USD", "ETH-USD"]),
            "relabeled",
        ),
    )
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    # allowed = {stocks} -> exclude everything else.
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["crypto", "forex", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "stocks"
    # The crypto tickers must not survive onto a stocks-labeled spec; clearing
    # them falls back to the stocks default universe.
    assert record.strategy.target_symbols == []


def test_design_loop_keeps_ambiguous_symbols_when_correcting_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-asset ETFs (GLD, QQQ, ...) trade like equities via the same
    provider even when their underlying exposure differs, so ``classify_symbol``
    deliberately returns ``None`` for them. They must be kept, not dropped as
    false positives."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(
        orch.design_agent,
        "run",
        lambda **_kw: (_spec_dict("crypto", target_symbols=["GLD", "BTC-USD"]), "scripted"),
    )
    monkeypatch.setattr(
        orch.design_agent,
        "revise",
        lambda *_a, **_kw: (_spec_dict("stocks", target_symbols=["GLD", "BTC-USD"]), "relabeled"),
    )
    monkeypatch.setattr(
        orch.design_review_agent, "run", lambda *_a, **_kw: SpecCritique(ready=True, rationale="ok")
    )
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["crypto", "forex", "futures", "commodities"],
    )

    assert record.strategy.asset_class == "stocks"
    assert record.strategy.target_symbols == ["GLD"]


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


def test_design_loop_reconciles_asset_category_after_revise_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corrective-regeneration mechanism applies after a mid-loop
    ``revise`` round drifts the category too, not only the initial
    generation — this exercises the reconciliation call site inside the
    round loop, distinct from the pre-loop one every other success-path
    test in this file exercises. A round's own ``revise`` call drifts the
    spec to "forex"; the pin-violation correction's ``revise`` call (a
    second, distinct call, immediately following) then successfully
    regenerates a "stocks" spec, and that regenerated spec/rationale is what
    the loop carries into its next round."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("stocks"), "scripted"))

    review_calls = iter(
        [
            SpecCritique(ready=False, rationale="round-0"),
            SpecCritique(ready=True, rationale="round-1 ok"),
        ]
    )
    monkeypatch.setattr(orch.design_review_agent, "run", lambda *_a, **_kw: next(review_calls))

    revise_calls: List[str] = []

    def _revise(*_a: Any, **_kw: Any) -> Tuple[Dict[str, Any], str]:
        revise_calls.append("call")
        if len(revise_calls) == 1:
            # The round's own revise() drifts off the pin.
            return _spec_dict("forex"), "revised off-pin"
        # The pin-violation correction's revise() call converges.
        return _spec_dict("stocks", target_symbols=["AAPL"]), "corrected back to stocks"

    monkeypatch.setattr(orch.design_agent, "revise", _revise)
    monkeypatch.setattr(orchestrator_module, "compile_strategy", lambda _spec: "VALID_CODE")
    monkeypatch.setattr(orch.code_synthesis_agent, "run", lambda _spec: "VALID_CODE")
    _short_circuit_synthesis(monkeypatch)

    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["forex", "crypto", "futures", "commodities"],
    )

    assert len(revise_calls) == 2
    assert record.strategy.asset_class == "stocks"
    assert record.strategy.target_symbols == ["AAPL"]
    assert record.strategy_rationale == "corrected back to stocks"


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


def test_design_loop_keeps_the_stall_directive_when_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall directive offers three alternatives — a different thesis,
    asset class, OR indicator combination — two of which a pinned attempt can
    satisfy, so it must survive the suppression filter. Dropping it would
    strip anti-repetition steering from exactly the runs most prone to
    stalling: a pinned run also has its prior-results context narrowed to one
    category, and ``is_stalled`` stays true until the designer varies, so a
    suppressed directive would be regenerated and re-dropped every cycle with
    the stall never recovering."""
    orch = StrategyLabOrchestrator()
    monkeypatch.setattr(orch.convergence_tracker, "is_stalled", lambda: True)
    stall_directive = orch.convergence_tracker.get_stall_directive()
    assert stall_directive is not None

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

    orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=["crypto", "forex", "futures", "commodities"],
    )

    assert len(captured) == 1
    convergence_directives = captured[0]["convergence_directives"] or []
    assert any("indicator combination" in d for d in convergence_directives)


def test_unsupported_asset_class_is_not_mislabeled_as_a_pin_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_spec_from_dict`` raises the same ``SpecImplementabilityError``
    type for an unsupported ``asset_class`` (a "bonds" typo). That is a
    different failure from a category-pin non-convergence and must keep its own
    reporting — relabeling it would mis-attribute every typo-redesign in the
    fleet to this feature. Exercised on an UNRESTRICTED run, where no pin is
    even active."""
    orch = StrategyLabOrchestrator()

    monkeypatch.setattr(orch.design_agent, "run", lambda **_kw: (_spec_dict("bonds"), "scripted"))
    _short_circuit_synthesis(monkeypatch)

    events: List[Tuple[str, Dict[str, Any]]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        exclude_asset_classes=None,
        on_phase=lambda phase, data: events.append((phase, data)),
    )

    assert record.backtest.status == "failed: spec_unimplementable"
    design_loop_events = [
        data
        for phase, data in events
        if phase == "telemetry" and data.get("scope") == "design_loop"
    ]
    assert not any(
        data.get("stop_reason") == "asset_category_unconverged" for data in design_loop_events
    )
    assert (record.loop_telemetry or {}).get("stop_reason") != "asset_category_unconverged"


# ---------------------------------------------------------------------------
# Readiness Rule 11 — the asset-category pin is enforced by the gate, so a
# spec outside the pinned category can never be reported ready and therefore
# can never reach code synthesis.
# ---------------------------------------------------------------------------


def _readiness_spec(asset_class: str, target_symbols: Optional[List[str]] = None) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-pin",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
        target_symbols=target_symbols or [],
    )


def _pin_results(spec: StrategySpec, pinned: Optional[str]) -> List[Any]:
    from investment_team.strategy_lab.quality_gates.spec_readiness import SpecReadinessGate

    results = SpecReadinessGate().validate(spec, phase="design", pinned_asset_class=pinned)
    return [r for r in results if (r.rule_id or "").startswith("asset_category:")]


def test_readiness_pin_is_inert_without_a_pin() -> None:
    assert _pin_results(_readiness_spec("crypto"), None) == []


def test_readiness_pin_passes_an_on_category_spec() -> None:
    assert _pin_results(_readiness_spec("stocks"), "stocks") == []


def test_readiness_pin_criticals_on_the_wrong_category() -> None:
    findings = _pin_results(_readiness_spec("crypto"), "stocks")
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].rule_id == "asset_category:pin"
    # The message must demand a rebuild, not a relabel — relabelling crypto
    # logic as stocks is the contamination the pin exists to prevent.
    assert "Rebuild the ENTIRE strategy" in findings[0].details


def test_readiness_pin_accepts_an_alias_spelling_of_the_pinned_class() -> None:
    assert _pin_results(_readiness_spec("equity"), "stocks") == []


def test_readiness_pin_criticals_on_offcategory_symbols() -> None:
    findings = _pin_results(_readiness_spec("stocks", ["AAPL", "BTC"]), "stocks")
    assert len(findings) == 1
    assert findings[0].rule_id == "asset_category:symbols"
    assert "BTC" in findings[0].details


def test_readiness_pin_keeps_ambiguous_cross_asset_etfs() -> None:
    # GLD / QQQ trade like equities via Yahoo even though their underlying
    # exposure is a different class — classify_symbol deliberately returns
    # None for them, so they must not be flagged.
    assert _pin_results(_readiness_spec("stocks", ["AAPL", "GLD", "QQQ"]), "stocks") == []


def test_readiness_pin_reports_class_alone_when_the_class_is_wrong() -> None:
    # The symbol check compares against the *declared* class, which is already
    # wrong — reporting both would be noise.
    findings = _pin_results(_readiness_spec("crypto", ["BTC", "ETH"]), "stocks")
    assert [f.rule_id for f in findings] == ["asset_category:pin"]


def test_readiness_pin_rejects_a_non_canonical_pin() -> None:
    from investment_team.strategy_lab.quality_gates.spec_readiness import SpecReadinessGate

    with pytest.raises(AssertionError):
        SpecReadinessGate().validate(_readiness_spec("stocks"), pinned_asset_class="bonds")


# ---------------------------------------------------------------------------
# Mechanical repair — the *symbol* half of the pin is repaired
# deterministically; the *class* half never is.
# ---------------------------------------------------------------------------


def test_repair_strips_offcategory_symbols_under_a_pin() -> None:
    from investment_team.strategy_lab.mechanical_repair import repair_spec

    out = repair_spec(
        _readiness_spec("stocks", ["AAPL", "BTC", "EURUSD=X"]), pinned_asset_class="stocks"
    )
    assert out.spec.target_symbols == ["AAPL"]
    actions = [a for a in out.actions if a.rule == "asset_category_pin_symbols"]
    assert len(actions) == 1
    assert actions[0].field == "target_symbols"


def test_repair_is_idempotent_under_a_pin() -> None:
    from investment_team.strategy_lab.mechanical_repair import repair_spec

    once = repair_spec(_readiness_spec("stocks", ["AAPL", "BTC"]), pinned_asset_class="stocks")
    twice = repair_spec(once.spec, pinned_asset_class="stocks")
    assert twice.actions == []
    assert twice.spec is once.spec


def test_repair_never_rewrites_asset_class() -> None:
    from investment_team.strategy_lab.mechanical_repair import repair_spec

    # A spec declaring the WRONG class must be left alone: rewriting the label
    # would file crypto logic under stocks. The readiness critical owns it.
    spec = _readiness_spec("crypto", ["BTC", "ETH"])
    out = repair_spec(spec, pinned_asset_class="stocks")
    assert out.spec.asset_class == "crypto"
    assert out.spec.target_symbols == ["BTC", "ETH"]
    assert [a for a in out.actions if a.rule == "asset_category_pin_symbols"] == []


def test_repair_without_a_pin_leaves_symbols_untouched() -> None:
    from investment_team.strategy_lab.mechanical_repair import repair_spec

    out = repair_spec(_readiness_spec("stocks", ["AAPL", "BTC"]))
    assert out.spec.target_symbols == ["AAPL", "BTC"]


def test_repair_keeps_ambiguous_cross_asset_etfs() -> None:
    from investment_team.strategy_lab.mechanical_repair import repair_spec

    out = repair_spec(_readiness_spec("stocks", ["GLD", "QQQ"]), pinned_asset_class="stocks")
    assert out.spec.target_symbols == ["GLD", "QQQ"]


# ---------------------------------------------------------------------------
# select_signal_brief / filter_regime_summary — the two cross-category
# analysis surfaces handed to the designer alongside prior_records.
# ---------------------------------------------------------------------------


def _brief(theme: str) -> Any:
    from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1

    return SignalIntelligenceBriefV1(brief_version=1, macro_themes=[theme])


def test_select_signal_brief_returns_the_pinned_categorys_brief() -> None:
    from investment_team.strategy_lab_context import select_signal_brief

    briefs = {"stocks": _brief("equities"), "crypto": _brief("digital assets")}
    assert select_signal_brief(briefs, "stocks").macro_themes == ["equities"]


def test_select_signal_brief_never_substitutes_another_category() -> None:
    from investment_team.strategy_lab_context import select_signal_brief

    # A missing brief must yield None (design prompt omits its signal
    # section) rather than another category's evidence.
    assert select_signal_brief({"crypto": _brief("digital assets")}, "stocks") is None


def test_select_signal_brief_handles_empty_and_none() -> None:
    from investment_team.strategy_lab_context import select_signal_brief

    assert select_signal_brief(None, "stocks") is None
    assert select_signal_brief({}, "stocks") is None


def _regime_entry(asset_class: str) -> Any:
    from investment_team.strategy_lab.market_regime import RegimeEntry

    return RegimeEntry(
        asset_class=asset_class,
        benchmark_symbol="X",
        trend_direction="up",
        trend_strength="moderate",
        volatility_regime="normal",
        close=1.0,
        sma50=1.0,
        sma200=1.0,
        adx=20.0,
        atr_pct=1.0,
        atr_pct_percentile=50.0,
    )


def _regime_summary(*asset_classes: str) -> Any:
    from investment_team.strategy_lab.market_regime import RegimeSummary

    return RegimeSummary(
        computed_at="2024-01-01T00:00:00Z",
        entries=[_regime_entry(c) for c in asset_classes],
    )


def test_filter_regime_summary_keeps_only_the_pinned_class() -> None:
    from investment_team.strategy_lab.market_regime import filter_regime_summary

    out = filter_regime_summary(_regime_summary("stocks", "crypto", "forex"), "stocks")
    assert [e.asset_class for e in out.entries] == ["stocks"]


def test_filter_regime_summary_returns_none_when_nothing_matches() -> None:
    from investment_team.strategy_lab.market_regime import filter_regime_summary

    # None is the shape every caller already treats as "no regime available",
    # so the prompt's regime section is absent rather than empty.
    assert filter_regime_summary(_regime_summary("crypto"), "stocks") is None


def test_filter_regime_summary_passes_through_without_a_class() -> None:
    from investment_team.strategy_lab.market_regime import filter_regime_summary

    summary = _regime_summary("stocks", "crypto")
    assert filter_regime_summary(summary, None) is summary
    assert filter_regime_summary(None, "stocks") is None


def test_filter_regime_summary_does_not_mutate_its_input() -> None:
    from investment_team.strategy_lab.market_regime import filter_regime_summary

    summary = _regime_summary("stocks", "crypto")
    filter_regime_summary(summary, "stocks")
    assert [e.asset_class for e in summary.entries] == ["stocks", "crypto"]


# ---------------------------------------------------------------------------
# build_spec_from_dict — an OMITTED asset_class defaults to the pin; a
# DIFFERENT one is left as authored for Rule 11 to reject.
# ---------------------------------------------------------------------------


def test_build_spec_defaults_an_omitted_asset_class_to_the_pin() -> None:
    from investment_team.strategy_lab.orchestrator_design import build_spec_from_dict

    payload = _spec_dict()
    payload.pop("asset_class")
    spec = build_spec_from_dict(payload, strategy_id="s1", default_asset_class="crypto")
    assert spec.asset_class == "crypto"


def test_build_spec_defaults_a_blank_asset_class_to_the_pin() -> None:
    from investment_team.strategy_lab.orchestrator_design import build_spec_from_dict

    spec = build_spec_from_dict(
        _spec_dict(asset_class=""), strategy_id="s1", default_asset_class="forex"
    )
    assert spec.asset_class == "forex"


def test_build_spec_preserves_an_authored_offpin_asset_class() -> None:
    from investment_team.strategy_lab.orchestrator_design import build_spec_from_dict

    # Silently reassigning an authored class to the pin is exactly the relabel
    # the design must avoid — Rule 11 rejects it instead.
    spec = build_spec_from_dict(
        _spec_dict(asset_class="crypto"), strategy_id="s1", default_asset_class="stocks"
    )
    assert spec.asset_class == "crypto"


def test_build_spec_rejects_a_non_canonical_default() -> None:
    from investment_team.strategy_lab.orchestrator_design import build_spec_from_dict

    with pytest.raises(AssertionError):
        build_spec_from_dict(_spec_dict(), strategy_id="s1", default_asset_class="bonds")
