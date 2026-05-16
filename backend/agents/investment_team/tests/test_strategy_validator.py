"""Unit tests for ``StrategySpecValidator`` (#547 item 6).

The gate runs deterministic checks against a ``StrategySpec`` before
code execution. These tests focus on the hypothesis-vs-rules
consistency check added in #547 — every other check has implicit
coverage via the orchestrator integration tests.
"""

from __future__ import annotations

from typing import List

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.strategy_validator import (
    StrategySpecValidator,
)
from investment_team.strategy_lab.spec_dsl import (
    ConstRef,
    EntryRule,
    MACDRef,
    Predicate,
    PriceRef,
    RSIRef,
    SignalExitRule,
    SMARef,
    TimeStopRule,
    UnparsableRule,
    UnparsableSizing,
)


def _spec(
    *,
    hypothesis: str,
    entry: List,
    exit_: List,
    asset_class: str = "stocks",
    sizing=None,
) -> StrategySpec:
    kwargs: dict = dict(
        strategy_id="strat-validator-test",
        authored_by="test",
        asset_class=asset_class,
        hypothesis=hypothesis,
        signal_definition="sig",
        entry_rules=entry,
        exit_rules=exit_,
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )
    if sizing is not None:
        kwargs["sizing"] = sizing
    return StrategySpec(**kwargs)


def _warnings(results) -> list[str]:
    return [r.details for r in results if r.severity == "warning" and not r.passed]


def test_hypothesis_term_missing_from_rules_emits_warning() -> None:
    """Hypothesis names RSI; rules use SMA → consistency warning fires.

    Issue #551/#552: rule consistency now reads through
    ``format_rules_for_prompt``; structured SMARef formats to ``sma(20)``,
    which does not contain ``rsi``.
    """
    spec = _spec(
        hypothesis="RSI mean reversion on oversold conditions",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("Hypothesis/rules consistency" in w and "rsi" in w.lower() for w in warnings), (
        warnings
    )


def test_rules_term_missing_from_hypothesis_emits_warning() -> None:
    """Rules reference MACD; hypothesis doesn't → consistency warning."""
    spec = _spec(
        hypothesis="Catch short-term trend continuation",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=MACDRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=MACDRef(), op="lt", rhs=ConstRef(value=0)),
            ),
        ],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("Hypothesis/rules consistency" in w and "macd" in w.lower() for w in warnings), (
        warnings
    )


def test_aligned_hypothesis_and_rules_emit_no_consistency_warning() -> None:
    """When hypothesis and rules share concept vocabulary, no warning fires."""
    spec = _spec(
        hypothesis="RSI signal strategy",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30)),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)),
            ),
        ],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_no_recognised_terms_emits_no_consistency_warning() -> None:
    """Hypothesis and rules without any recognised vocabulary stay silent.

    ``EntryRule`` with bare ``PriceRef`` / ``ConstRef`` formats to text like
    ``long when close > 0`` — no concept tokens.
    """
    spec = _spec(
        hypothesis="Buy low, sell high on a fixed schedule",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_word_boundary_prevents_thematic_matching_ema() -> None:
    """The compiled regex uses ``\\b`` anchors so 'thematic' does not match 'ema'."""
    spec = _spec(
        hypothesis="Thematic exposure to growth sectors",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# Issue #552 — keyword/non-computable scans on structured rules.
#
# Under the DSL (#551) the asset-class mismatch and non-computable keyword
# scans only see free text through ``UnparsableRule.prose`` /
# ``UnparsableSizing.prose`` — the only escape hatches for prose that
# survived the migration. ``StrategySpec.entry_rules`` is typed
# ``List[EntryRule]`` (no prose escape hatch on entries), so the prose
# surface to exercise is ``exit_rules`` (its union admits ``UnparsableRule``)
# and ``sizing`` (its union admits ``UnparsableSizing``).
# ---------------------------------------------------------------------------


def test_asset_class_mismatch_fires_on_unparsable_exit_rule_prose() -> None:
    """Equity keyword 'dividend' in an ``UnparsableRule.prose`` exit rule on
    a forex spec is surfaced by ``format_rules_for_prompt`` and trips the
    asset-class mismatch scan."""
    spec = _spec(
        hypothesis="trend follow on currency pairs",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[UnparsableRule(prose="exit on dividend ex-date", reason="prose")],
        asset_class="forex",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("mismatched with asset class 'forex'" in w for w in warnings), warnings


def test_asset_class_mismatch_fires_on_unparsable_sizing_prose() -> None:
    """Equity keyword 'EPS' in ``UnparsableSizing.prose`` on a crypto spec
    is surfaced by ``format_sizing_rule`` and trips the asset-class mismatch
    scan."""
    spec = _spec(
        hypothesis="momentum on majors",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
        asset_class="crypto",
        sizing=UnparsableSizing(prose="size by EPS growth", reason="prose"),
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("mismatched with asset class 'crypto'" in w for w in warnings), warnings


def test_non_computable_keyword_fires_on_unparsable_exit_rule_prose() -> None:
    """A prose exit rule referencing 'twitter sentiment' trips the
    non-computable-data warning regardless of asset class."""
    spec = _spec(
        hypothesis="mean reversion on retail flow",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[
            UnparsableRule(
                prose="exit when twitter sentiment turns negative",
                reason="prose",
            ),
        ],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("non-computable data" in w for w in warnings), warnings


def test_structured_rules_do_not_trigger_keyword_scans() -> None:
    """Negative case: purely structured rules format to text like
    ``long when close > sma(20)`` / ``risk 2% per trade``, none of which
    match the asset-class or non-computable keyword regexes — even with a
    mismatch-sensitive asset class set."""
    spec = _spec(
        hypothesis="SMA crossover on equities",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=PriceRef(), op="lt", rhs=SMARef(period=5)),
            ),
        ],
        asset_class="forex",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("mismatched with asset class" in w for w in warnings), warnings
    assert not any("non-computable data" in w for w in warnings), warnings
