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
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
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
        timeframe="1d",
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


def _critical(results) -> list[str]:
    return [r.details for r in results if r.severity == "critical" and not r.passed]


# ---------------------------------------------------------------------------
# Issue #535 — 'options' asset class must be rejected at the validator
# gate. Strategy Lab has no option-chain data, Greeks, or contract
# execution model, so silently treating options as equities produced
# meaningless backtest metrics.
# ---------------------------------------------------------------------------


def test_validator_rejects_options_asset_class() -> None:
    """asset_class='options' fails a critical gate with an explanatory message."""
    spec = _spec(
        hypothesis="Buy ATM puts on SPY when VIX > 25.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70),
            ),
        ],
        asset_class="options",
    )
    results = StrategySpecValidator().validate(spec)
    critical = _critical(results)
    assert any("options" in c.lower() and "not yet supported" in c.lower() for c in critical), (
        f"Expected a critical gate rejecting options, got: {critical}"
    )


def test_validator_rejects_options_via_normalize_alias() -> None:
    """An LLM that emits a canonical 'options' label (uppercase, whitespace)
    is still rejected — the gate runs after ``normalize_asset_class``."""
    spec = _spec(
        hypothesis="Buy ATM puts on SPY when VIX > 25.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70),
            ),
        ],
        asset_class="  OPTIONS  ",
    )
    results = StrategySpecValidator().validate(spec)
    assert any("options" in c.lower() for c in _critical(results)), _critical(results)


def test_validator_does_not_reject_supported_asset_classes() -> None:
    """Supported asset classes (stocks, crypto, forex, futures, commodities)
    must not trip the #535 options gate."""
    for ac in ("stocks", "crypto", "forex", "futures", "commodities"):
        spec = _spec(
            hypothesis="RSI mean reversion",
            entry=[
                EntryRule(
                    side="long",
                    when=Predicate(
                        lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30
                    ),
                ),
            ],
            exit_=[
                SignalExitRule(
                    when=Predicate(
                        lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70
                    ),
                ),
            ],
            asset_class=ac,
        )
        results = StrategySpecValidator().validate(spec)
        assert not any("options" in c.lower() for c in _critical(results)), (
            f"asset_class={ac!r} unexpectedly tripped the options gate: {_critical(results)}"
        )


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
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
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
                when=Predicate(lhs=IndicatorRef(name="macd"), op=">", rhs=0),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="macd"), op="<", rhs=0),
            ),
        ],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("Hypothesis/rules consistency" in w and "macd" in w.lower() for w in warnings), (
        warnings
    )


def test_new_indicator_vocabulary_is_recognised_in_consistency_check() -> None:
    """The channel/volume/momentum indicators added to the catalogue must be
    recognised by the hypothesis-vs-rules consistency vocabulary.

    Otherwise a hypothesis that name-drops e.g. Donchian, Williams %R or money
    flow while the rules use a different indicator would slip past this check —
    the same gap the older nine-indicator vocabulary would have left.
    """
    spec = _spec(
        hypothesis="Donchian breakout confirmed by Williams %R and money flow",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    lowered = " ".join(warnings).lower()
    assert any("Hypothesis/rules consistency" in w for w in warnings), warnings
    assert "donchian" in lowered
    assert "williams" in lowered
    assert "money flow" in lowered


def test_prose_alias_matches_dsl_token_no_spurious_warning() -> None:
    """A prose alias in the hypothesis and the DSL token a rule renders are the
    same concept — no consistency warning.

    ``format_rules_for_prompt`` renders an OBV rule as ``obv(...)`` while the
    hypothesis says "on-balance volume"; resolving both to ``{obv}`` before
    comparing avoids a spurious "each side is orphaned" mismatch. Same idea
    fixes the pre-existing "moving average" vs ``sma`` case.
    """
    obv_spec = _spec(
        hypothesis="On-Balance Volume confirms accumulation before entry",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="obv"), op=">", rhs=0),
            ),
        ],
        exit_=[
            SignalExitRule(when=Predicate(lhs=IndicatorRef(name="obv"), op="<", rhs=0)),
        ],
    )
    warnings = _warnings(StrategySpecValidator().validate(obv_spec))
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings

    ma_spec = _spec(
        hypothesis="A moving average crossover drives the entry",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="sma", params={"period": 20}),
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 50}),
                ),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
    )
    warnings = _warnings(StrategySpecValidator().validate(ma_spec))
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_channel_indicator_rule_matches_hypothesis_no_spurious_warning() -> None:
    """A structured donchian/keltner rule is read from the ref, not the rendered
    text, so an aligned channel-breakout spec draws no consistency warning.

    ``format_rules_for_prompt`` renders these refs band-suffixed (``donchian_upper(20)``),
    which a ``\\b``-anchored concept regex can't match; collecting the rules-side
    concept from the structured ref instead avoids flagging the rule as absent
    from its own hypothesis.
    """
    for ind in ("donchian", "keltner"):
        spec = _spec(
            # No extra strategy-concept words (e.g. "breakout") — just the indicator,
            # so an aligned spec should draw no consistency warning at all.
            hypothesis=f"A {ind} channel strategy",
            entry=[
                EntryRule(
                    side="long",
                    when=Predicate(
                        lhs="bar.close",
                        op=">",
                        rhs=IndicatorRef(name=ind, params={"band": "upper", "period": 20}),
                    ),
                ),
            ],
            exit_=[StopLossRule(pct=0.03)],
        )
        warnings = _warnings(StrategySpecValidator().validate(spec))
        # The fix: the indicator is read from the structured ref, so it is never
        # reported orphaned despite rendering as ``donchian_upper(20)``.
        assert not any("Hypothesis/rules consistency" in w for w in warnings), (ind, warnings)


def test_aligned_hypothesis_and_rules_emit_no_consistency_warning() -> None:
    """When hypothesis and rules share concept vocabulary, no warning fires."""
    spec = _spec(
        hypothesis="RSI signal strategy",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70),
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
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
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
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# Issue #552/#537 — keyword/non-computable scans cover unparseable prose.
#
# After #537 unparseable rules live as raw strings on
# ``StrategySpec.unparsed_rules`` (the previous ``UnparsableRule`` /
# ``UnparsableSizing`` discriminator variants were dropped). The
# validator folds ``unparsed_rules`` into the same text view as the
# structured rules so an asset-class-mismatched or non-computable
# keyword in legacy prose is still caught.
# ---------------------------------------------------------------------------


def _spec_with_unparsed(
    *, unparsed_rules: list[str], asset_class: str, hypothesis: str
) -> StrategySpec:
    return _spec(
        hypothesis=hypothesis,
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
        asset_class=asset_class,
    ).model_copy(update={"unparsed_rules": unparsed_rules, "requires_redesign": True})


def test_asset_class_mismatch_fires_on_unparsed_exit_rule_prose() -> None:
    """Equity keyword 'dividend' in `unparsed_rules` on a forex spec
    trips the asset-class mismatch scan."""
    spec = _spec_with_unparsed(
        unparsed_rules=["exit on dividend ex-date"],
        asset_class="forex",
        hypothesis="trend follow on currency pairs",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("mismatched with asset class 'forex'" in w for w in warnings), warnings


def test_asset_class_mismatch_fires_on_unparsed_sizing_prose() -> None:
    """Equity keyword 'EPS' on a crypto spec trips the asset-class mismatch scan."""
    spec = _spec_with_unparsed(
        unparsed_rules=["size by EPS growth"],
        asset_class="crypto",
        hypothesis="momentum on majors",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("mismatched with asset class 'crypto'" in w for w in warnings), warnings


def test_non_computable_keyword_fires_on_unparsed_exit_rule_prose() -> None:
    """A prose exit rule referencing 'twitter sentiment' trips the
    non-computable-data warning regardless of asset class."""
    spec = _spec_with_unparsed(
        unparsed_rules=["exit when twitter sentiment turns negative"],
        asset_class="stocks",
        hypothesis="mean reversion on retail flow",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("non-computable data" in w for w in warnings), warnings


def test_hypothesis_consistency_scans_unparsed_rules() -> None:
    """Issue #537: indicator concepts that appear only in `unparsed_rules`
    still satisfy the hypothesis-vs-rules consistency check, matching the
    pre-#537 behavior where unparseable prose was rendered through the
    formatters."""
    spec = _spec(
        hypothesis="RSI mean reversion strategy",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            ),
        ],
        exit_=[StopLossRule(pct=0.03)],
    ).model_copy(update={"unparsed_rules": ["exit when rsi(14) > 70"], "requires_redesign": True})
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    consistency_warnings = [w for w in warnings if "Hypothesis/rules consistency" in w]
    # 'rsi' is mentioned in both hypothesis and unparsed_rules → not an orphan.
    # The warning may still fire for 'sma'/'mean reversion' but must NOT cite 'rsi'.
    assert not any("'rsi'" in w for w in consistency_warnings), consistency_warnings


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
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            ),
        ],
        asset_class="forex",
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("mismatched with asset class" in w for w in warnings), warnings
    assert not any("non-computable data" in w for w in warnings), warnings
