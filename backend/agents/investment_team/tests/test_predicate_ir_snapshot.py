"""Snapshot + unit tests for the ``BarPredicate`` IR.

The extractor (:func:`_extract_subconditions`) turns a strategy's ``on_bar``
predicates into a tree of :data:`BarPredicate` nodes; the aggregator walks that
tree to build the coverage report. The IR is therefore a stable internal
contract whose bugs may not surface at the report level. These snapshot tests
pin the *canonical render* of the extracted IR for a spread of representative
strategies, so any unintended change to the tree shape produced by the
extractor (combinator, symbol gating, unknown-leg flags, denylist, ancestor
nesting) is caught directly at the IR boundary.

To regenerate the golden file after an intentional IR change, run::

    UPDATE_PREDICATE_IR_SNAPSHOTS=1 pytest \
        agents/investment_team/tests/test_predicate_ir_snapshot.py

then commit ``golden/snapshots/predicate_ir.json``.
"""

from __future__ import annotations

import difflib
import json
import os
import textwrap
from pathlib import Path

import pandas as pd
import pytest

from investment_team.strategy_lab.coverage_probe.indicator_probe import _extract_subconditions
from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    AndOp,
    Leg,
    MaskLeaf,
    OrOp,
    PredicateGroup,
    Static,
    SymbolGate,
    build_or_group,
    collect_legs,
    eval_tree,
    find_or_groups,
    leg_gate_symbols,
    render_bar_predicate,
    render_predicate_group,
    tree_and_unknown,
    tree_effective_symbols,
    tree_or_unknown,
)

from ._indicator_probe_fixtures import make_strategy

SNAPSHOT_PATH = Path(__file__).parent / "golden" / "snapshots" / "predicate_ir.json"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_PREDICATE_IR_SNAPSHOTS") == "1"
GROUP_SEP = "\n========\n"


# ---------------------------------------------------------------------------
# Representative strategies — one canonical IR render each.
#
# These mirror known-good inputs already exercised by the indicator-probe
# suite, chosen to span the IR surface: AND vs OR, symbol gates (single /
# multi / empty-intersection), unknown conjuncts/alternatives, ancestor
# nesting, denylists, position gates, indicator subscripts, multi-output
# indicators, binop operands, and assignment-bound indicator variables.
# ---------------------------------------------------------------------------


def _nested(*lines: str) -> str:
    """Wrap *lines* (already-indented ``on_bar`` body) in a strategy class."""
    body = "\n".join(f"            {ln}" for ln in lines)
    return textwrap.dedent("class S:\n    def on_bar(self, ctx, bar):\n") + body + "\n"


CASES: dict[str, str] = {
    "01_simple_compare": make_strategy("close > sma(close, 200)"),
    "02_pure_or": make_strategy("close > 100 or close < 50"),
    "03_nested_and": _nested(
        "if close > sma(close, 50):",
        "    if close < sma(close, 10):",
        "        pass",
    ),
    "04_and_unknown_conjunct": make_strategy("close > 100 and self.custom_ok(bar)"),
    "05_or_unknown_alt": make_strategy("close > 100 or self.custom_ok(bar)"),
    "06_symbol_gate_single": make_strategy('bar.symbol == "AAPL" and close > sma(close, 50)'),
    "07_symbol_gate_multi": make_strategy('bar.symbol in ("AAPL", "MSFT") and rsi(close, 14) < 30'),
    "08_ancestors_plus_or": _nested(
        "if rsi(close, 14) < 30:",
        "    if close > 100 or close < 50:",
        "        pass",
    ),
    "09_symbol_only_or_alt": make_strategy('bar.symbol == "AAPL" or close > 100'),
    "10_bollinger_subscript": make_strategy("close > bollinger_bands(close, 20)[0]"),
    "11_macd_multi_output": make_strategy("macd(close, 5, 10, 4)[0] > 0"),
    "12_stochastic": make_strategy("stochastic(high, low, close, 3)[0] > 0"),
    "13_vwap": make_strategy("close > vwap(high, low, close, volume)"),
    "14_denylist": _nested(
        'if bar.symbol == "AAPL":',
        "    return",
        "if close > sma(close, 150):",
        "    pass",
    ),
    "15_position_gate": _nested(
        "pos = ctx.position(bar.symbol)",
        "if pos is None:",
        "    if close > 0:",
        "        pass",
        "else:",
        "    if close < -50:",
        "        pass",
    ),
    "16_binop_operand": make_strategy("close > sma(close, 50) * 1.02"),
    "17_multi_leg_and": _nested(
        "if close > sma(close, 50):",
        "    if close > ema(close, 20):",
        "        if rsi(close, 14) < 70:",
        "            pass",
    ),
    "18_empty_symbol_intersection": make_strategy(
        'bar.symbol == "AAPL" and bar.symbol == "MSFT" and close > 0'
    ),
    "19_or_of_ands": _nested(
        'if (bar.symbol == "AAPL" and close > 1000) or (bar.symbol == "MSFT" and close < 50):',
        "    pass",
    ),
    "20_custom_var_binding": _nested(
        "threshold = sma(close, 5) + 1000",
        "if close > threshold:",
        "    pass",
    ),
}


def _render_case(code: str) -> str:
    """Extract *code*'s predicate groups and render the canonical IR string."""
    groups = _extract_subconditions(code)
    if not groups:
        return "<no groups>"
    return GROUP_SEP.join(render_predicate_group(g) for g in groups)


def _live_renders() -> dict[str, str]:
    return {case_id: _render_case(code) for case_id, code in CASES.items()}


def _load_snapshots(live: dict[str, str]) -> dict[str, str]:
    """Return the committed golden, writing it only on explicit opt-in.

    Regeneration is gated strictly behind ``UPDATE_PREDICATE_IR_SNAPSHOTS`` so a
    missing or accidentally-deleted golden fails loudly instead of being
    silently re-created from the current (possibly regressed) output.
    """
    if UPDATE_SNAPSHOTS:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(
            f"Snapshot file {SNAPSHOT_PATH} not found. "
            "Run with UPDATE_PREDICATE_IR_SNAPSHOTS=1 to generate it."
        )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def live_renders() -> dict[str, str]:
    """Extract + render every case once per test session (lazy, at run time)."""
    return _live_renders()


@pytest.fixture(scope="session")
def snapshots(live_renders: dict[str, str]) -> dict[str, str]:
    """The committed golden, loaded (or regenerated on opt-in) lazily.

    Deferring this to a fixture keeps the extraction work and the
    missing-golden ``FileNotFoundError`` out of import/collection time, so a
    broken extractor or absent golden surfaces as a test failure rather than a
    collection error.
    """
    return _load_snapshots(live_renders)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_predicate_ir_snapshot(
    case_id: str, live_renders: dict[str, str], snapshots: dict[str, str]
) -> None:
    """The extracted IR render must match the committed golden for each case."""
    expected = snapshots.get(case_id)
    assert expected is not None, (
        f"no stored snapshot for {case_id}; regenerate with "
        "UPDATE_PREDICATE_IR_SNAPSHOTS=1 and commit golden/snapshots/predicate_ir.json"
    )
    live = live_renders[case_id]
    if live != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                live.splitlines(),
                fromfile=f"{case_id} (golden)",
                tofile=f"{case_id} (live)",
                lineterm="",
            )
        )
        raise AssertionError(
            f"IR snapshot drift for {case_id}. Set UPDATE_PREDICATE_IR_SNAPSHOTS=1 to "
            f"accept after verifying the change is intentional.\n{diff}"
        )


def test_every_case_is_snapshotted(snapshots: dict[str, str]) -> None:
    """Guard against a stale golden missing newly-added cases (or vice versa)."""
    assert set(snapshots) == set(CASES)


# ---------------------------------------------------------------------------
# Focused unit tests for the IR layer's pure helpers / renderer branches.
# These lock the contracts of the stable middleman directly, independent of
# the extractor and aggregator.
# ---------------------------------------------------------------------------


def _mask(label: str, value: bool) -> MaskLeaf:
    """A constant-valued mask leaf for deterministic evaluation tests."""
    return MaskLeaf(label=label, evaluator=lambda df, _v=value: pd.Series(_v, index=df.index))


def _df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({"close": [1.0] * n})


def test_render_leaf_variants() -> None:
    assert render_bar_predicate(MaskLeaf("close > 0", lambda df: df["close"])) == "Mask(close > 0)"
    assert render_bar_predicate(Static(True)) == "Static(True)"


def test_render_symbol_gate_sorts_symbols() -> None:
    gate = SymbolGate(frozenset({"MSFT", "AAPL"}), MaskLeaf("close > 0", lambda df: df["close"]))
    assert render_bar_predicate(gate) == "SymbolGate({AAPL, MSFT})\n  Mask(close > 0)"


def test_render_empty_symbol_gate() -> None:
    gate = SymbolGate(frozenset(), Static(True))
    assert render_bar_predicate(gate) == "SymbolGate({})\n  Static(True)"


def test_render_and_or_nesting_with_flags() -> None:
    tree = AndOp(
        legs=(
            Leg("a", MaskLeaf("a", lambda df: df["close"])),
            OrOp(
                legs=(Leg("b", MaskLeaf("b", lambda df: df["close"])),),
                unknown=True,
            ),
        ),
        unknown=False,
    )
    rendered = render_bar_predicate(tree)
    assert rendered == (
        "And(unknown=False)\n  Leg(a)\n    Mask(a)\n  Or(unknown=True)\n    Leg(b)\n      Mask(b)"
    )


def test_render_predicate_group_denied_none_vs_set() -> None:
    leaf = MaskLeaf("close > 0", lambda df: df["close"])
    no_deny = PredicateGroup(tree=AndOp(legs=(Leg("x", leaf),)))
    assert render_predicate_group(no_deny).startswith("denied: none\n")

    with_deny = PredicateGroup(
        tree=AndOp(legs=(Leg("x", leaf),)), denied_symbols=frozenset({"TSLA", "AAPL"})
    )
    assert render_predicate_group(with_deny).startswith("denied: {AAPL, TSLA}\n")

    # An explicit (even empty) denylist is rendered distinctly from None.
    empty_deny = PredicateGroup(tree=AndOp(legs=(Leg("x", leaf),)), denied_symbols=frozenset())
    assert render_predicate_group(empty_deny).startswith("denied: {}\n")


def test_render_indent_precondition() -> None:
    with pytest.raises(AssertionError):
        render_bar_predicate(Static(True), indent=-1)


def test_eval_tree_empty_and_or_and_static() -> None:
    df = _df()
    # Empty AndOp is the conjunction identity (all-True); empty OrOp is all-False.
    assert eval_tree(AndOp(legs=()), df, "AAPL").tolist() == [True, True, True]
    assert eval_tree(OrOp(legs=()), df, "AAPL").tolist() == [False, False, False]
    assert eval_tree(Static(False), df, "AAPL").tolist() == [False, False, False]


def test_eval_tree_symbol_gate_filters_by_symbol() -> None:
    df = _df()
    gate = SymbolGate(frozenset({"AAPL"}), _mask("always", True))
    assert eval_tree(gate, df, "AAPL").tolist() == [True, True, True]
    # Symbol not in the gate → forced all-False without invoking the inner mask.
    assert eval_tree(gate, df, "MSFT").tolist() == [False, False, False]


def test_eval_tree_and_or_combine() -> None:
    df = _df()
    t, f = _mask("t", True), _mask("f", False)
    assert eval_tree(AndOp(legs=(t, f)), df, "AAPL").tolist() == [False, False, False]
    assert eval_tree(OrOp(legs=(t, f)), df, "AAPL").tolist() == [True, True, True]


def test_collect_legs_on_bare_leaf_returns_empty() -> None:
    # A node that is neither a Leg nor a combinator/gate contributes no legs.
    assert collect_legs(_mask("x", True)) == []
    assert collect_legs(Static(True)) == []


def test_build_or_group_wraps_in_symbol_gate() -> None:
    leg = Leg("close > 0", _mask("close > 0", True))
    group = build_or_group(
        ancestor_legs=[],
        or_alt_legs=[leg],
        or_unknown=False,
        effective_symbols={"AAPL"},
        ancestor_unknown=False,
        denied_symbols=None,
    )
    assert isinstance(group.tree, SymbolGate)
    assert group.tree.syms == frozenset({"AAPL"})
    assert isinstance(group.tree.inner, OrOp)


def test_tree_and_unknown_detects_any_and_flag() -> None:
    leg = Leg("x", _mask("x", True))
    # Clean AND → no unknown; AND with unknown=True, or a nested unknown AND, → True.
    assert tree_and_unknown(AndOp(legs=(leg,))) is False
    assert tree_and_unknown(AndOp(legs=(leg,), unknown=True)) is True
    nested = OrOp(legs=(SymbolGate(frozenset({"AAPL"}), AndOp(legs=(leg,), unknown=True)),))
    assert tree_and_unknown(nested) is True
    # An OrOp.unknown does not count as an AND-unknown.
    assert tree_and_unknown(OrOp(legs=(leg,), unknown=True)) is False


def test_tree_or_unknown_detects_any_or_flag() -> None:
    leg = Leg("x", _mask("x", True))
    assert tree_or_unknown(OrOp(legs=(leg,))) is False
    assert tree_or_unknown(OrOp(legs=(leg,), unknown=True)) is True
    nested = AndOp(legs=(SymbolGate(frozenset({"AAPL"}), OrOp(legs=(leg,), unknown=True)),))
    assert tree_or_unknown(nested) is True
    # An AndOp.unknown does not count as an OR-unknown.
    assert tree_or_unknown(AndOp(legs=(leg,), unknown=True)) is False


def test_leg_gate_symbols_returns_outer_gate_only() -> None:
    gated = Leg("x", SymbolGate(frozenset({"AAPL"}), _mask("x", True)))
    assert leg_gate_symbols(gated) == frozenset({"AAPL"})
    # No outer SymbolGate on the leg's inner sub-tree → None.
    assert leg_gate_symbols(Leg("x", _mask("x", True))) is None


def test_find_or_groups_orders_match_collect_legs() -> None:
    leg = Leg("x", _mask("x", True))
    inner_or = OrOp(legs=(leg,))
    outer_or = OrOp(legs=(leg,))
    tree = AndOp(legs=(outer_or, SymbolGate(frozenset({"AAPL"}), inner_or)))
    groups = find_or_groups(tree)
    # Two OR groups, id-ordered to match collect_legs' assignment (outer first).
    assert [oid for oid, _ in groups] == [0, 1]
    assert [op for _, op in groups] == [outer_or, inner_or]
    # No OR anywhere → empty.
    assert find_or_groups(AndOp(legs=(leg,))) == []


def test_tree_effective_symbols_union_and_universal() -> None:
    g_aapl = SymbolGate(frozenset({"AAPL"}), Leg("a", _mask("a", True)))
    g_msft = SymbolGate(frozenset({"MSFT"}), Leg("b", _mask("b", True)))
    # All legs gated → union of their symbols.
    assert tree_effective_symbols(OrOp(legs=(g_aapl, g_msft))) == frozenset({"AAPL", "MSFT"})
    # Any ungated (universal) leg → None (symbol-unconstrained).
    assert tree_effective_symbols(OrOp(legs=(g_aapl, Leg("c", _mask("c", True))))) is None
    # No symbol info at all → None.
    assert tree_effective_symbols(AndOp(legs=(Leg("d", _mask("d", True)),))) is None
