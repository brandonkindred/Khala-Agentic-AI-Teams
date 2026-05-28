"""Indicator-coverage probe for Strategy Lab strategies (#448).

Walks the strategy's ``on_bar`` (or equivalent entry path) for ``if``
predicates whose subconditions reference a recognised OHLCV column or
one of the indicator helpers in
:mod:`investment_team.strategy_lab.executor.indicators`, evaluates each
subcondition over the fetched market data, and aggregates per-bar hit
rates plus a conjunction hit-rate into a partial :class:`CoverageReport`.

Pure: no I/O, no LLM, no subprocess. Bounded: a single ``ast.parse`` per
strategy and per-symbol vectorised pandas evaluation only when at least
one recognised subcondition exists. The probe never raises — malformed
input degrades to ``UNKNOWN_LOW_COVERAGE`` with an explanatory summary.
"""

from __future__ import annotations

import ast
import logging
import operator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as _field
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    Union,
)

import pandas as pd

from investment_team.models import (
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
)
from investment_team.strategy_lab.executor.indicators import INDICATORS

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})
_MAX_SUBCONDITIONS = 16
_MAX_LIKELY_BLOCKERS = 6
_MAX_LABEL_LEN = 80


@dataclass(frozen=True)
class _CombinatorOps:
    """Strategy object parameterising AND vs OR compound-subcond building."""

    reduce: Callable[[pd.Series, pd.Series], pd.Series]
    identity: bool
    combine_symbols: Callable[[frozenset, frozenset], frozenset]
    on_unknown_term: Literal["abort", "track"]
    expose_or_legs: bool


_AND_OPS = _CombinatorOps(
    reduce=operator.and_,
    identity=True,
    combine_symbols=frozenset.__and__,
    on_unknown_term="abort",
    expose_or_legs=False,
)

_OR_OPS = _CombinatorOps(
    reduce=operator.or_,
    identity=False,
    combine_symbols=frozenset.__or__,
    on_unknown_term="track",
    expose_or_legs=True,
)


_CMP_OPS: Dict[type, Callable[[pd.Series, pd.Series], pd.Series]] = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


@dataclass(frozen=True)
class _Operand:
    """Compiled half of a comparison.

    ``data_dependent`` is True iff the operand reads the DataFrame (column
    or indicator). Subconditions whose *both* operands are pure literals
    are rejected — they are constant-truth and carry no coverage signal.
    """

    fn: Callable[[pd.DataFrame], pd.Series]
    data_dependent: bool


# ---------------------------------------------------------------------------
# BarPredicate IR
# ---------------------------------------------------------------------------
#
# The extractor produces a tree of these nodes; the aggregator pattern-matches
# the tree to compute hit rates, classify blockers, and assemble the report.
# Tree shape replaces the flag-based encoding that used ``combinator``,
# ``ancestor_count``, ``has_unknown_or_leg``, ``has_unknown_and_conjunct``,
# and ``has_unknown_leg`` on the old ``_Group`` / ``_Subcond`` dataclasses.


@dataclass(frozen=True)
class MaskLeaf:
    """A terminal: a single evaluable comparison or truthiness mask."""

    label: str
    evaluator: Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class Static:
    """A terminal whose mask is a constant.

    ``Static(True)`` is used as the inner body of a pure symbol-gate leg
    (e.g. ``bar.symbol == "AAPL"`` as a standalone OR alternative — the
    enclosing :class:`SymbolGate` restricts the leg to AAPL bars where
    the predicate is unconditionally true).
    """

    value: bool


@dataclass(frozen=True)
class SymbolGate:
    """Wraps an inner predicate, restricting it to specific symbols.

    Replaces ``_Subcond.target_symbols`` / ``_Group.target_symbols``.
    During evaluation, when the current symbol is not in ``syms`` the
    inner mask is forced to all-False.
    """

    syms: frozenset
    inner: "BarPredicate"


@dataclass(frozen=True)
class AndOp:
    """Conjunction: all legs must hold.

    ``unknown=True`` when at least one original conjunct was un-modellable
    (e.g. ``self.custom_ok(bar)``). The recognised legs' mask is then
    only a SUPERSET of the real predicate, so the aggregator must not
    conclude ``COVERAGE_OK`` from them alone. Replaces
    ``_Group.has_unknown_and_conjunct``.
    """

    legs: Tuple["BarPredicate", ...]
    unknown: bool = False


@dataclass(frozen=True)
class OrOp:
    """Disjunction: at least one leg must hold.

    ``unknown=True`` when at least one original alternative was
    un-modellable. With an un-modelled alternative present we can't
    prove the OR is unreachable, so the aggregator must suppress
    ``or_group_never_fires`` / zero-hit blockers under this node.
    Replaces ``_Group.has_unknown_or_leg`` and ``_Subcond.has_unknown_leg``.
    """

    legs: Tuple["BarPredicate", ...]
    unknown: bool = False


@dataclass(frozen=True)
class Leg:
    """One reportable subcondition in :class:`CoverageReport.subconditions`.

    Wraps a sub-tree with the human-readable label used in coverage
    output. The wrapped sub-tree is what's evaluated for the leg's
    hits; the label is what's displayed. Leg boundaries also define
    the granularity of blocker classification: a leg in AND context
    can be flagged zero-hit, alternatives directly under an :class:`OrOp`
    are classified as an OR group, and deeper structure inside a leg
    is internal (not separately reported).
    """

    label: str
    inner: "BarPredicate"


BarPredicate = Union[AndOp, OrOp, SymbolGate, MaskLeaf, Static, Leg]


@dataclass(frozen=True)
class PredicateGroup:
    """One ``if``-predicate's worth of coverage-relevant content.

    ``tree`` is the :data:`BarPredicate` IR — its shape encodes the
    combinator (AND vs OR), AND-required ancestors of an OR (as
    ``AndOp(legs=(anc, OrOp(alts)))``), symbol filters (``SymbolGate``
    wrapping), and un-modelled conjuncts/alternatives (``AndOp.unknown``
    / ``OrOp.unknown``). Empty-symbol intersections (e.g. two
    ``bar.symbol == "X"`` and ``bar.symbol == "Y"`` conjoined) collapse
    to a ``SymbolGate(frozenset(), ...)`` and the group is dropped
    before emission.

    ``denied_symbols`` carries the exclude-shaped early-return denylist
    (``if bar.symbol == "AAPL": return``) — symbols dropped from
    evaluation regardless of any positive ``SymbolGate``. Independent
    of the tree's positive filters: a group can have an allowlist gate
    AND a denylist; effective scope is
    ``tree_symbols ∩ (universe - denied_symbols)``.
    """

    tree: BarPredicate
    denied_symbols: Optional[frozenset] = None


# ---------------------------------------------------------------------------
# Tree utilities
# ---------------------------------------------------------------------------


def _strip_symbol_gates(node: BarPredicate) -> Tuple[BarPredicate, Optional[frozenset]]:
    """Peel any outer :class:`SymbolGate` nodes off *node* and return the inner
    predicate plus the intersected gate symbols. ``None`` symbols means
    no gate at this level. Used to inspect the structural shape of a
    sub-tree without fragile dispatch on ``SymbolGate``.
    """
    syms: Optional[frozenset] = None
    while isinstance(node, SymbolGate):
        syms = node.syms if syms is None else (syms & node.syms)
        node = node.inner
    return node, syms


def _tree_and_unknown(node: BarPredicate) -> bool:
    """Return True if any :class:`AndOp` in the tree has ``unknown=True``."""
    if isinstance(node, AndOp):
        if node.unknown:
            return True
        return any(_tree_and_unknown(leg) for leg in node.legs)
    if isinstance(node, OrOp):
        return any(_tree_and_unknown(leg) for leg in node.legs)
    if isinstance(node, (SymbolGate, Leg)):
        return _tree_and_unknown(node.inner)
    return False


def _tree_or_unknown(node: BarPredicate) -> bool:
    """Return True if any :class:`OrOp` in the tree has ``unknown=True``."""
    if isinstance(node, OrOp):
        if node.unknown:
            return True
        return any(_tree_or_unknown(leg) for leg in node.legs)
    if isinstance(node, AndOp):
        return any(_tree_or_unknown(leg) for leg in node.legs)
    if isinstance(node, (SymbolGate, Leg)):
        return _tree_or_unknown(node.inner)
    return False


def _collect_legs(
    node: BarPredicate, accumulated_syms: Optional[frozenset] = None
) -> List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]]:
    """Walk the tree, collecting each :class:`Leg` along with its
    effective symbol filter (intersection of all enclosing
    :class:`SymbolGate` syms), whether it sits directly under an
    :class:`OrOp` (i.e. is an OR alternative rather than an AND-required
    leg), and an integer identifier of the closest enclosing
    :class:`OrOp` (used to group OR alternatives for the "all-zero OR"
    blocker). Stops at each :class:`Leg` — does *not* descend into
    ``Leg.inner``.
    """
    return _collect_legs_walk(node, accumulated_syms, in_or=False, or_id=None, next_or_id=[0])


def _collect_legs_walk(
    node: BarPredicate,
    accumulated_syms: Optional[frozenset],
    in_or: bool,
    or_id: Optional[int],
    next_or_id: List[int],
) -> List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]]:
    if isinstance(node, Leg):
        return [(node, accumulated_syms, in_or, or_id)]
    if isinstance(node, SymbolGate):
        new_syms = node.syms if accumulated_syms is None else (accumulated_syms & node.syms)
        return _collect_legs_walk(node.inner, new_syms, in_or, or_id, next_or_id)
    if isinstance(node, AndOp):
        results: List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]] = []
        for leg in node.legs:
            results.extend(
                _collect_legs_walk(
                    leg, accumulated_syms, in_or=False, or_id=None, next_or_id=next_or_id
                )
            )
        return results
    if isinstance(node, OrOp):
        this_or_id = next_or_id[0]
        next_or_id[0] += 1
        results = []
        for leg in node.legs:
            results.extend(
                _collect_legs_walk(
                    leg, accumulated_syms, in_or=True, or_id=this_or_id, next_or_id=next_or_id
                )
            )
        return results
    return []


def _tree_effective_symbols(node: BarPredicate) -> Optional[frozenset]:
    """Return the union of symbols any leg in the tree could fire on, or
    ``None`` when at least one leg is symbol-unconstrained (universal).

    AND combinator: a single universal leg leaves the AND unconstrained
    only if no other AND leg gates the symbol space — but for warmup
    sizing we conservatively treat the AND as universal whenever any
    leg is universal (the predicate could fire on any symbol from a
    universal leg's perspective). OR combinator: a single universal
    alternative makes the disjunction universal.

    This function powers warmup sizing in :func:`_union_target_symbols`
    and group-level symbol resolution.
    """
    legs = _collect_legs(node)
    union: set = set()
    saw_universal = False
    for _leg, syms, _in_or, _or_id in legs:
        if syms is None:
            saw_universal = True
        else:
            union |= syms
    if saw_universal:
        return None
    return frozenset(union) if union else None


def _find_or_groups(node: BarPredicate) -> List[Tuple[int, OrOp]]:
    """Return a list of (or_id, OrOp) pairs in the same order that
    :func:`_collect_legs` assigns or_ids — used so the classifier can
    look up the :class:`OrOp` (for ``unknown`` flag) of each OR group
    it sees in a leg's metadata.
    """
    out: List[Tuple[int, OrOp]] = []
    _find_or_groups_walk(node, out, next_or_id=[0])
    return out


def _find_or_groups_walk(
    node: BarPredicate, out: List[Tuple[int, OrOp]], next_or_id: List[int]
) -> None:
    if isinstance(node, OrOp):
        this_id = next_or_id[0]
        next_or_id[0] += 1
        out.append((this_id, node))
        for leg in node.legs:
            _find_or_groups_walk(leg, out, next_or_id)
        return
    if isinstance(node, AndOp):
        for leg in node.legs:
            _find_or_groups_walk(leg, out, next_or_id)
        return
    if isinstance(node, (SymbolGate, Leg)):
        _find_or_groups_walk(node.inner, out, next_or_id)


def _eval_tree(node: BarPredicate, df: pd.DataFrame, symbol: str) -> pd.Series:
    """Recursively evaluate *node* against *df* for *symbol*, returning a
    boolean :class:`pandas.Series` indexed by ``df.index``.

    The :class:`SymbolGate` dispatch is the per-symbol filter: when
    ``symbol`` is not in the gate's ``syms``, the entire sub-tree below
    it evaluates to all-False without invoking the inner mask.
    """
    if isinstance(node, MaskLeaf):
        try:
            series = node.evaluator(df)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover — defensive
            logger.debug("subcondition %r failed on %s: %s", node.label, symbol, exc)
            return pd.Series(False, index=df.index, dtype=bool)
        return pd.Series(series, index=df.index).fillna(False).astype(bool)
    if isinstance(node, Static):
        return pd.Series(node.value, index=df.index, dtype=bool)
    if isinstance(node, SymbolGate):
        if symbol not in node.syms:
            return pd.Series(False, index=df.index, dtype=bool)
        return _eval_tree(node.inner, df, symbol)
    if isinstance(node, Leg):
        return _eval_tree(node.inner, df, symbol)
    if isinstance(node, AndOp):
        if not node.legs:
            return pd.Series(True, index=df.index, dtype=bool)
        result = _eval_tree(node.legs[0], df, symbol)
        for leg in node.legs[1:]:
            result = result & _eval_tree(leg, df, symbol)
        return result
    if isinstance(node, OrOp):
        if not node.legs:
            return pd.Series(False, index=df.index, dtype=bool)
        result = _eval_tree(node.legs[0], df, symbol)
        for leg in node.legs[1:]:
            result = result | _eval_tree(leg, df, symbol)
        return result
    raise AssertionError(f"unknown BarPredicate variant: {type(node)!r}")  # pragma: no cover


def _leg_gate_symbols(leg: Leg) -> Optional[frozenset]:
    """Return the outermost :class:`SymbolGate` ``syms`` of *leg*'s inner
    sub-tree, or ``None`` if the leg has no outer gate. Used by the
    visitor to propagate a compound leg's effective symbol scope to the
    group level so sibling AND-conjuncts inherit it.
    """
    inner = leg.inner
    if isinstance(inner, SymbolGate):
        return inner.syms
    return None


def _build_and_group(
    legs: List[Leg],
    effective_symbols: Optional[set],
    effective_unknown: bool,
    denied_symbols: Optional[frozenset],
) -> PredicateGroup:
    """Assemble a :class:`PredicateGroup` whose root predicate is an
    :class:`AndOp` over *legs*, optionally wrapped in a
    :class:`SymbolGate` when ``effective_symbols`` constrains the
    group.

    ``effective_unknown`` becomes ``AndOp.unknown`` — replaces the old
    ``_Group.has_unknown_and_conjunct`` flag.
    """
    tree: BarPredicate = AndOp(legs=tuple(legs), unknown=effective_unknown)
    if effective_symbols is not None:
        tree = SymbolGate(syms=frozenset(effective_symbols), inner=tree)
    return PredicateGroup(tree=tree, denied_symbols=denied_symbols)


def _build_or_group(
    ancestor_legs: List[Leg],
    or_alt_legs: List[Leg],
    or_unknown: bool,
    effective_symbols: Optional[set],
    ancestor_unknown: bool,
    denied_symbols: Optional[frozenset],
) -> PredicateGroup:
    """Assemble a :class:`PredicateGroup` for an ``if A or B or C:`` shape,
    optionally with AND-required ancestors from enclosing ``if``s.

    Tree shape:

    * No ancestors → root is :class:`OrOp` over *or_alt_legs*.
    * With ancestors → ``AndOp(legs=(*ancestor_legs, OrOp(legs=or_alt_legs)))``.

    ``or_unknown`` becomes the :class:`OrOp` ``unknown`` flag (replaces
    the old ``_Group.has_unknown_or_leg``); ``ancestor_unknown`` becomes
    the :class:`AndOp` ``unknown`` flag when ancestors are present
    (replaces ``_Group.has_unknown_and_conjunct``). Optional outer
    :class:`SymbolGate` wraps the root when *effective_symbols* is set.
    """
    or_node = OrOp(legs=tuple(or_alt_legs), unknown=or_unknown)
    if ancestor_legs:
        tree: BarPredicate = AndOp(
            legs=tuple(ancestor_legs) + (or_node,),
            unknown=ancestor_unknown,
        )
    else:
        tree = or_node
    if effective_symbols is not None:
        tree = SymbolGate(syms=frozenset(effective_symbols), inner=tree)
    return PredicateGroup(tree=tree, denied_symbols=denied_symbols)


def run_indicator_probe(
    *,
    strategy_code: str,
    market_data: Dict[str, pd.DataFrame],
    warmup_bars_required: int = 0,
) -> CoverageReport:
    """Return a partial :class:`CoverageReport` from indicator-coverage analysis.

    Parameters
    ----------
    strategy_code:
        Source of the generated strategy. The probe scans the
        ``on_bar`` (or equivalent) method body for ``if`` predicates.
    market_data:
        Dict of ``symbol -> DataFrame`` with at least the standard
        OHLCV columns. Index is treated opaquely; ``last_true_bar``
        is rendered with ``str(...)``.
    warmup_bars_required:
        When the total recognised bars is below this value the probe
        short-circuits with :data:`CoverageCategory.INSUFFICIENT_BARS`.

    The probe is deterministic and never raises.
    """
    symbols_checked = sum(1 for df in market_data.values() if isinstance(df, pd.DataFrame))
    bars_checked = sum(len(df) for df in market_data.values() if isinstance(df, pd.DataFrame))
    base_kwargs = {
        "symbols_checked": symbols_checked,
        "bars_checked": bars_checked,
        "warmup_bars_required": int(max(0, warmup_bars_required)),
    }

    try:
        subconds = _extract_subconditions(strategy_code)
    except Exception as exc:  # noqa: BLE001 — never raise from probe
        logger.debug("indicator_probe AST extraction failed: %s", exc)
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="strategy_code did not parse for indicator probe",
            **base_kwargs,
        )

    if not subconds:
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="no recognized indicator subconditions found",
            **base_kwargs,
        )

    # Warmup is a per-symbol time-series property: a 150-period SMA on
    # a 100-bar DataFrame is all NaN regardless of how many other
    # symbols are present. Compare against the longest single symbol's
    # bar count so a multi-symbol universe with no individually
    # warm-enough series correctly classifies as INSUFFICIENT_BARS
    # rather than letting the all-NaN indicators silently flow through
    # to a false INDICATOR_FILTER_TOO_RESTRICTIVE.
    #
    # When every extracted group is symbol-gated (``bar.symbol == "X"``
    # or per-leg OR gates), restrict the warmup denominator to the
    # union of those gates: an unrelated symbol with plenty of history
    # cannot rescue the warmup check for the gated symbols, so its
    # bar count must not be counted. If any group is universal (no
    # symbol filter), every DataFrame is potentially in scope and the
    # check stays over the full universe.
    universe = {sym for sym, df in market_data.items() if isinstance(df, pd.DataFrame)}
    target_symbols = _union_target_symbols(subconds, universe)
    if target_symbols is None:
        warmup_dfs = [df for df in market_data.values() if isinstance(df, pd.DataFrame)]
    else:
        warmup_dfs = [
            df
            for sym, df in market_data.items()
            if sym in target_symbols and isinstance(df, pd.DataFrame)
        ]
    max_per_symbol_bars = max((len(df) for df in warmup_dfs), default=0)

    if warmup_bars_required > 0 and max_per_symbol_bars < warmup_bars_required:
        evidence = f"max_per_symbol_bars={max_per_symbol_bars} < warmup={warmup_bars_required}"
        if target_symbols is not None:
            evidence = f"{evidence} [{','.join(sorted(target_symbols))}]"
        return CoverageReport(
            coverage_category=CoverageCategory.INSUFFICIENT_BARS,
            summary=(
                f"insufficient per-symbol history: longest series has "
                f"{max_per_symbol_bars} bars, {warmup_bars_required} required"
            ),
            likely_blockers=[
                LikelyBlocker(
                    reason="insufficient_bars",
                    evidence=evidence,
                )
            ],
            **base_kwargs,
        )

    try:
        return _aggregate(subconds, market_data, base_kwargs)
    except Exception as exc:  # noqa: BLE001 — never raise from probe
        logger.debug(
            "indicator_probe evaluation failed: %s", exc
        )  # pragma: no cover — defensive catch-all; aggregator is internally robust
        return CoverageReport(  # pragma: no cover — defensive fallback
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="indicator probe evaluation failed",
            **base_kwargs,
        )


@dataclass(frozen=True)
class _LeafResult:
    """Per-leg evaluation rollup for one :class:`Leg` in a group's tree.

    Carries the data the report and classifier stages need for one
    reportable subcondition, with the per-leg structural context
    (AND-required vs OR-alternative, which OR group, effective symbol
    filter from enclosing ``SymbolGate`` nodes).

    Invariants:
        ``hits >= 0``.
        ``effective_symbols`` is ``None`` (universal) or a frozenset.
        ``in_or`` implies ``or_id is not None``.
    """

    leg: Leg
    hits: int
    last_true: Optional[str]
    effective_symbols: Optional[frozenset]
    in_or: bool
    or_id: Optional[int]


@dataclass(frozen=True)
class _GroupResult:
    """Per-group evaluation rollup produced by ``CoverageAggregator``.

    Carries the per-leg rollups plus the conjunction (whole-tree) hits
    and a flag tracking whether the group was evaluated against any
    symbol at all. Structural information lives on ``group.tree``.

    Invariants:
        ``conjunction_hits >= 0``.
    """

    group: PredicateGroup
    leaf_results: Tuple[_LeafResult, ...]
    conjunction_hits: int
    evaluated: bool


@dataclass(frozen=True)
class _BlockerResult:
    """Classification output from ``CoverageAggregator._classify_blockers``.

    Invariants:
        If ``conjunction_group`` is not ``None``, ``conjunction_blocker``
        is also not ``None``.
    """

    zero_hit_blockers: List[LikelyBlocker]
    conjunction_blocker: Optional[LikelyBlocker]
    conjunction_group: Optional[PredicateGroup]


class CoverageAggregator:
    """Three-stage pipeline for indicator-coverage aggregation.

    Replaces the monolithic ``_aggregate`` function with structured per-group
    evaluation, blocker classification, and report assembly. The three
    stages must be called in order via :meth:`run`.

    Preconditions:
        ``groups`` is non-empty (the caller checks for empty groups
        before constructing the aggregator).
    Postconditions:
        :meth:`run` returns a valid ``CoverageReport``.
    Invariants:
        After :meth:`_evaluate_groups`, ``_total_eval_bars`` and
        ``_per_symbol_bars`` are populated and used by subsequent stages.
    """

    def __init__(
        self,
        groups: List[PredicateGroup],
        market_data: Dict[str, pd.DataFrame],
        base_kwargs: Dict[str, object],
    ) -> None:
        self._groups = groups
        self._market_data = market_data
        self._base_kwargs = base_kwargs
        self._total_eval_bars: int = 0
        self._per_symbol_bars: Dict[str, int] = {}
        # Pre-collect legs per group (with their effective symbols and
        # OR-group ids) so each stage walks the same per-leg view.
        self._group_legs: List[List[Tuple[Leg, Optional[frozenset], bool, Optional[int]]]] = [
            _collect_legs(g.tree) for g in groups
        ]
        # Pre-collect or-group records per group so the classifier can
        # look up the originating ``OrOp`` (for ``unknown`` and label
        # assembly) of any or_id reported on a leaf.
        self._group_or_groups: List[Dict[int, OrOp]] = [
            dict(_find_or_groups(g.tree)) for g in groups
        ]

    def run(self) -> CoverageReport:
        """Execute the three-stage pipeline and return a ``CoverageReport``.

        Postconditions:
            Returns a ``CoverageReport`` with one of ``UNKNOWN_LOW_COVERAGE``,
            ``INDICATOR_FILTER_TOO_RESTRICTIVE``, ``CONJUNCTION_NEVER_TRUE``,
            or ``COVERAGE_OK``.
        """
        results = self._evaluate_groups()

        if self._total_eval_bars == 0:
            return CoverageReport(
                coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
                summary="no bars evaluated",
                subconditions=[],
                **self._base_kwargs,
            )

        subcoverages = self._build_subcoverages(results)
        blocker_result = self._classify_blockers(results)
        return self._assemble_report(results, blocker_result, subcoverages)

    # ------------------------------------------------------------------
    # Stage 1: per-symbol × per-group evaluation
    # ------------------------------------------------------------------

    def _evaluate_groups(self) -> List[_GroupResult]:
        """Evaluate each group's predicate tree across all symbols and
        return per-group rollups.

        Preconditions:
            ``self._groups`` is non-empty.
        Postconditions:
            ``self._total_eval_bars`` and ``self._per_symbol_bars`` are set.
            Returned list has one ``_GroupResult`` per group, even if
            unevaluated (``evaluated=False``).
        """
        groups = self._groups
        n_groups = len(groups)

        # Per-group, per-leg accumulators.
        leg_hits: List[List[int]] = [[0] * len(legs) for legs in self._group_legs]
        leg_last_true: List[List[Optional[str]]] = [[None] * len(legs) for legs in self._group_legs]
        group_conjunction_hits: List[int] = [0] * n_groups
        group_evaluated: List[bool] = [False] * n_groups
        total_eval_bars = 0
        per_symbol_bars: Dict[str, int] = {}

        for symbol, df in self._market_data.items():
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            symbol_contributed = False
            for group_idx, group in enumerate(groups):
                if group.denied_symbols is not None and symbol in group.denied_symbols:
                    continue
                legs = self._group_legs[group_idx]
                # Per-leg masks (used for both per-leg hits and the group
                # conjunction). Computing each leg's mask separately gives
                # us symmetric data for the report; ``_eval_tree`` against
                # the whole tree would conflate them.
                per_leg_masks: List[pd.Series] = []
                any_leg_evaluated = False
                for leg_idx, (leg, eff_syms, _in_or, _or_id) in enumerate(legs):
                    if eff_syms is not None and symbol not in eff_syms:
                        per_leg_masks.append(pd.Series(False, index=df.index, dtype=bool))
                        continue
                    mask = _eval_tree(leg, df, symbol)
                    per_leg_masks.append(mask)
                    any_leg_evaluated = True
                    hits = int(mask.sum())
                    leg_hits[group_idx][leg_idx] += hits
                    if hits:
                        last_bar = str(mask[mask].index[-1])
                        prior = leg_last_true[group_idx][leg_idx]
                        if prior is None or last_bar > prior:
                            leg_last_true[group_idx][leg_idx] = last_bar
                if not any_leg_evaluated:
                    continue
                # Whole-tree conjunction mask: a single ``_eval_tree``
                # call against the group's root captures AND/OR/SymbolGate
                # semantics in one place — including the implicit gating
                # of legs whose ``SymbolGate`` excludes this symbol.
                conjunction_mask = _eval_tree(group.tree, df, symbol)
                group_conjunction_hits[group_idx] += int(conjunction_mask.sum())
                group_evaluated[group_idx] = True
                symbol_contributed = True
            if symbol_contributed:
                total_eval_bars += len(df)
                per_symbol_bars[symbol] = per_symbol_bars.get(symbol, 0) + len(df)

        self._total_eval_bars = total_eval_bars
        self._per_symbol_bars = per_symbol_bars

        results: List[_GroupResult] = []
        for group_idx, group in enumerate(groups):
            legs = self._group_legs[group_idx]
            leaf_rs: List[_LeafResult] = []
            for leg_idx, (leg, eff_syms, in_or, or_id) in enumerate(legs):
                leaf_rs.append(
                    _LeafResult(
                        leg=leg,
                        hits=leg_hits[group_idx][leg_idx],
                        last_true=leg_last_true[group_idx][leg_idx],
                        effective_symbols=eff_syms,
                        in_or=in_or,
                        or_id=or_id,
                    )
                )
            results.append(
                _GroupResult(
                    group=group,
                    leaf_results=tuple(leaf_rs),
                    conjunction_hits=group_conjunction_hits[group_idx],
                    evaluated=group_evaluated[group_idx],
                )
            )
        return results

    # ------------------------------------------------------------------
    # Subcondition coverage deduplication
    # ------------------------------------------------------------------

    def _build_subcoverages(self, results: List[_GroupResult]) -> List[SubconditionCoverage]:
        """Deduplicate subconditions by ``(label, effective_symbols)`` and compute hit rates.

        Preconditions:
            ``self._total_eval_bars > 0``, ``self._per_symbol_bars`` populated.
        Postconditions:
            No duplicate ``(label, effective_symbols)`` keys in the returned list.
            Every ``hit_rate`` is in ``[0.0, 1.0]``.
        """
        subcoverages: List[SubconditionCoverage] = []
        seen_keys: set = set()
        for result in results:
            for leaf in result.leaf_results:
                effective_syms = leaf.effective_symbols
                key = (leaf.leg.label, effective_syms)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if effective_syms is not None:
                    denom = sum(self._per_symbol_bars.get(s, 0) for s in effective_syms)
                else:
                    denom = self._total_eval_bars
                rate = (leaf.hits / denom) if denom > 0 else 0.0
                label = leaf.leg.label
                if effective_syms:
                    label = f"{label} [{','.join(sorted(effective_syms))}]"
                subcoverages.append(
                    SubconditionCoverage(
                        label=label,
                        hit_count=leaf.hits,
                        hit_rate=min(max(rate, 0.0), 1.0),
                        last_true_bar=leaf.last_true,
                    )
                )
        return subcoverages

    # ------------------------------------------------------------------
    # Stage 2: blocker classification (zero-hit + conjunction-never-true)
    # ------------------------------------------------------------------

    def _classify_blockers(self, results: List[_GroupResult]) -> _BlockerResult:
        """Detect zero-hit blockers and conjunction-never-true groups.

        Preconditions:
            ``results`` is the output of ``_evaluate_groups()``.
        Postconditions:
            Returned ``_BlockerResult.zero_hit_blockers`` contains at most
            one entry per ``(label, symbols_key)`` pair.
            If ``conjunction_group`` is set, ``conjunction_blocker`` is set.
        """
        blockers: List[LikelyBlocker] = []
        flagged_keys: set = set()

        for result_idx, result in enumerate(results):
            if not result.evaluated:
                continue
            group = result.group
            or_groups = self._group_or_groups[result_idx]
            group_syms = _tree_effective_symbols(group.tree)
            symbols_key = group_syms

            def _flag_zero_hit(leaf: _LeafResult, _sym_key=symbols_key) -> None:
                key = (leaf.leg.label, _sym_key)
                if key in flagged_keys:
                    return
                flagged_keys.add(key)
                evidence = leaf.leg.label
                if group_syms:
                    evidence = f"{evidence} [{','.join(sorted(group_syms))}]"
                blockers.append(
                    LikelyBlocker(
                        reason="indicator_filter_zero_hits",
                        evidence=evidence,
                        hit_rate=0.0,
                    )
                )

            # AND-required legs: any zero-hit leaf is a blocker unless
            # its inner sub-tree carries an OR with un-modelled
            # alternatives (which shielded it under the old
            # ``_Subcond.has_unknown_leg`` flag).
            for leaf in result.leaf_results:
                if leaf.in_or:
                    continue
                if leaf.hits != 0:
                    continue
                if _tree_or_unknown(leaf.leg.inner):
                    continue
                _flag_zero_hit(leaf)

            # OR groups: flag "or_group_never_fires" only when every
            # alternative under one ``OrOp`` has 0 hits AND that
            # ``OrOp.unknown`` is False (replaces ``_Group.has_unknown_or_leg``).
            or_buckets: Dict[int, List[_LeafResult]] = {}
            for leaf in result.leaf_results:
                if leaf.in_or and leaf.or_id is not None:
                    or_buckets.setdefault(leaf.or_id, []).append(leaf)
            for or_id, leaves in or_buckets.items():
                or_op = or_groups.get(or_id)
                if or_op is None or or_op.unknown:
                    continue
                if not all(lf.hits == 0 for lf in leaves):
                    continue
                evidence = " OR ".join(lf.leg.label for lf in leaves)
                if group_syms:
                    evidence = f"{evidence} [{','.join(sorted(group_syms))}]"
                blockers.append(
                    LikelyBlocker(
                        reason="or_group_never_fires",
                        evidence=evidence,
                        hit_rate=0.0,
                    )
                )

        # Conjunction-never-true: find any single predicate whose individual
        # legs all fire but whose bar-wise conjunction is empty.
        conjunction_blocker: Optional[LikelyBlocker] = None
        conjunction_group: Optional[PredicateGroup] = None

        for result_idx, result in enumerate(results):
            if not result.evaluated or result.conjunction_hits != 0:
                continue
            group = result.group
            # Any unknown anywhere in the tree suppresses the conjunction
            # blocker — we can't prove the conjunction never fires when
            # part of the predicate is un-modelled.
            if _tree_or_unknown(group.tree):
                continue
            # AND-only group (no OrOp): require >= 2 legs and all fire.
            # AND-with-nested-OR: require AND-required ancestors all
            # fire AND at least one OR alternative fires (matches the
            # legacy ``or_tail_any_fire`` rule).
            and_leaves = [lf for lf in result.leaf_results if not lf.in_or]
            or_leaves = [lf for lf in result.leaf_results if lf.in_or]
            if or_leaves:
                if not and_leaves:
                    continue
                if not all(lf.hits > 0 for lf in and_leaves):
                    continue
                if not any(lf.hits > 0 for lf in or_leaves):
                    continue
                conjunction_group = group
                break
            else:
                if len(and_leaves) >= 2 and all(lf.hits > 0 for lf in and_leaves):
                    conjunction_group = group
                    break

        if conjunction_group is not None:
            conj_leaves = _collect_legs(conjunction_group.tree)
            conjunction_blocker = LikelyBlocker(
                reason="conjunction_never_true",
                evidence=" AND ".join(leg.label for leg, _es, _io, _oi in conj_leaves),
                hit_rate=0.0,
            )

        return _BlockerResult(
            zero_hit_blockers=blockers,
            conjunction_blocker=conjunction_blocker,
            conjunction_group=conjunction_group,
        )

    # ------------------------------------------------------------------
    # Stage 3: final category classification + report assembly
    # ------------------------------------------------------------------

    def _assemble_report(
        self,
        results: List[_GroupResult],
        blocker_result: _BlockerResult,
        subcoverages: List[SubconditionCoverage],
    ) -> CoverageReport:
        """Classify the coverage category and build the final report.

        Preconditions:
            ``results``, ``blocker_result``, and ``subcoverages`` are
            outputs of the preceding pipeline stages.
        Postconditions:
            Returns a ``CoverageReport`` with one of
            ``INDICATOR_FILTER_TOO_RESTRICTIVE``,
            ``CONJUNCTION_NEVER_TRUE``, ``UNKNOWN_LOW_COVERAGE``, or
            ``COVERAGE_OK``.
        """
        if blocker_result.zero_hit_blockers:
            blockers = blocker_result.zero_hit_blockers
            if any(b.reason == "indicator_filter_zero_hits" for b in blockers):
                summary = (
                    f"{len(blockers)} of {len(subcoverages)} indicator subconditions never fired"
                )
            else:
                summary = "or-predicate has no firing leg"
            return CoverageReport(
                coverage_category=CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
                summary=summary,
                subconditions=subcoverages,
                likely_blockers=blockers[:_MAX_LIKELY_BLOCKERS],
                **self._base_kwargs,
            )

        if blocker_result.conjunction_blocker is not None:
            return CoverageReport(
                coverage_category=CoverageCategory.CONJUNCTION_NEVER_TRUE,
                summary="individual subconditions fire but their conjunction is never true",
                subconditions=subcoverages,
                likely_blockers=[blocker_result.conjunction_blocker][:_MAX_LIKELY_BLOCKERS],
                **self._base_kwargs,
            )

        # Unknown-evidence polarity check. OR-unknown widens the recognised
        # mask (recognised firing = positive evidence); AND-unknown narrows
        # it (recognised AND = superset, not proof).
        has_unknown_evidence = False
        has_recognised_evidence = False
        for result in results:
            if not result.evaluated:
                continue
            tree = result.group.tree
            group_or_unknown = _tree_or_unknown(tree)
            group_and_unknown = _tree_and_unknown(tree)
            group_unknown = group_or_unknown or group_and_unknown
            any_leaf_fired = any(lf.hits > 0 for lf in result.leaf_results)
            if group_unknown:
                has_unknown_evidence = True
                if group_and_unknown:
                    # AND-unknown: recognised AND is a superset, not proof.
                    # Don't accept its hits as recognised evidence.
                    pass
                elif result.conjunction_hits > 0 and any_leaf_fired:
                    has_recognised_evidence = True
            else:
                if any_leaf_fired:
                    has_recognised_evidence = True

        if has_unknown_evidence and not has_recognised_evidence:
            return CoverageReport(
                coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
                summary=(
                    "predicate has un-modellable alternative(s) and recognised legs "
                    "produced no firing bars — coverage is unknown"
                ),
                subconditions=subcoverages,
                likely_blockers=[],
                **self._base_kwargs,
            )

        return CoverageReport(
            coverage_category=CoverageCategory.COVERAGE_OK,
            summary="indicator subconditions fired at least once",
            subconditions=subcoverages,
            likely_blockers=[],
            **self._base_kwargs,
        )


def _aggregate(
    groups: List[PredicateGroup],
    market_data: Dict[str, pd.DataFrame],
    base_kwargs: Dict[str, object],
) -> CoverageReport:
    """Aggregate indicator-coverage evaluation into a ``CoverageReport``.

    Preconditions:
        ``groups`` may be empty (returns ``UNKNOWN_LOW_COVERAGE``).
        ``market_data`` values are DataFrames with OHLCV columns.
    Postconditions:
        Returns a valid ``CoverageReport``.
    """
    has_any_leg = any(_collect_legs(g.tree) for g in groups)
    if not has_any_leg:
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="no recognized indicator subconditions found",
            **base_kwargs,
        )
    return CoverageAggregator(groups, market_data, base_kwargs).run()


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


_BLOCK_FIELDS = ("body", "orelse", "finalbody")


class SubconditionVisitor:
    """Statement-list-driven walker that produces coverage groups.

    Replaces the closure-soup form of ``_extract_subconditions``
    (#467). Each former nested ``def`` is now a method; the formerly-
    captured ``name_evaluators`` / ``name_periods`` / ``name_strings``
    dicts become instance attributes; the budget counter (formerly
    ``state["total"]``) is ``self._budget``; the transactional
    save/restore in ``_visit`` is the ``_snapshot`` context manager.

    Deliberately NOT a subclass of :class:`ast.NodeVisitor` — ``_visit``
    is statement-list-driven (it iterates a ``List[ast.stmt]`` and
    applies assignments in source order between siblings), which does
    not fit the per-node dispatch ``NodeVisitor`` provides. The
    function-body / class-body short-circuit in ``_visit`` is
    explicit; subclassing ``NodeVisitor`` would silently re-enable
    descent into nested helper functions.
    """

    def __init__(self, tree: ast.Module, on_bar: ast.FunctionDef) -> None:
        # Outer-scope (module / strategy class / __init__) period bindings
        # only. Function-local ``WINDOW = 5`` shadowing (and all
        # ``Name = <indicator>`` bindings inside on_bar) are applied
        # **flow-sensitively** in :meth:`_visit` so a later reassignment
        # can't shadow a predicate that lexically precedes it.
        # ``strategy_class`` confines the outer-scope walk to the strategy's
        # own ``ClassDef`` so a sibling helper class can't pre-empt the
        # strategy's bare-name attribute bindings.
        self._tree = tree
        self._on_bar = on_bar
        self._strategy_class = _find_strategy_class(tree, on_bar)
        # ``bar_name`` is the actual third positional parameter name on
        # ``on_bar``. The symbol recognisers historically hard-coded
        # ``"bar"`` and silently dropped the gate when the strategy named
        # it ``candle`` / ``b`` — see :func:`_bar_param_name`.
        self._bar_name = _bar_param_name(on_bar)
        self._name_periods = _collect_name_periods(
            tree, function_node=None, strategy_class=self._strategy_class
        )
        # String-constant bindings (``TARGET_SYMBOL = "BBB"``) — used by
        # ``_symbol_gate`` so ``bar.symbol == TARGET_SYMBOL`` resolves to
        # the same gated subcondition as ``bar.symbol == "BBB"``.
        self._name_strings = _collect_name_strings(tree, strategy_class=self._strategy_class)
        # Local name → indicator evaluator bindings start empty. The
        # walker fills them as it encounters assignments in source order.
        self._name_evaluators: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}
        self._groups: List[PredicateGroup] = []
        # Budget counter — formerly ``state["total"]`` in the closure.
        self._budget = 0

    def walk(self, on_bar: ast.FunctionDef) -> List[PredicateGroup]:
        body = getattr(on_bar, "body", None)
        if isinstance(body, list):
            self._visit(body, [], None)
        return self._groups

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        """Save / restore the three name-binding dicts across a ``_visit`` call.

        ``self._groups`` and ``self._budget`` are deliberately NOT rolled
        back: the early-emit blocks in :meth:`_process_if` and
        :meth:`_process_or_if` append partial groups when the budget is
        hit mid-walk and that emission is observable behaviour the
        robustness suite anchors. The restore order mirrors the legacy
        try/finally at the same point in :meth:`_visit`.
        """
        saved_evals = dict(self._name_evaluators)
        saved_periods = dict(self._name_periods)
        saved_strings = self._name_strings.copy()
        try:
            yield
        finally:
            self._name_evaluators.clear()
            self._name_evaluators.update(saved_evals)
            self._name_periods.clear()
            self._name_periods.update(saved_periods)
            self._name_strings.restore_from(saved_strings)

    def _apply_assign_inplace(self, stmt: ast.stmt) -> None:
        """Update self._name_evaluators / self._name_periods from a single assignment.

        Mirrors the per-target logic of the previous global pre-pass
        (``_collect_name_evaluators`` and the function-local pass of
        ``_collect_name_periods``) but applied **flow-sensitively** —
        the walker calls this in source order so an assignment only
        affects predicates that lexically follow it. Without this,
        ``ma = sma(close, 5); if close > ma; ma = 999`` evaluated the
        predicate against the later 999 binding instead of the SMA.
        """
        if isinstance(stmt, ast.Assign):
            value = stmt.value
            targets = stmt.targets
        elif (
            isinstance(stmt, ast.AnnAssign) and stmt.value is not None
        ):  # pragma: no cover — flow-sensitive annotated-assignment shape; rare in generated strategies
            value = stmt.value
            targets = [stmt.target]
        else:
            return
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                _bind_tuple_unpack(target, value, self._name_periods, self._name_evaluators)
                continue
            if isinstance(target, ast.Name):
                evaluator = _resolve_assign_evaluator(
                    value, self._name_periods, self._name_evaluators
                )
                if evaluator is not None:
                    self._name_evaluators[target.id] = evaluator
                else:
                    # RHS is a scalar / unsupported call — drop any
                    # prior indicator binding so downstream lookups
                    # fall through to numeric-literal / OHLCV
                    # resolution.
                    self._name_evaluators.pop(target.id, None)
                # Numeric-scalar side: record any numeric value
                # (including zero and negatives), preserving int-ness
                # when the value is integer-valued so period-use sites
                # stay clean. Non-integer floats and zero/negative
                # thresholds must also be preserved here:
                # ``_build_operand`` resolves ``Name`` literals through
                # this dict, so without it ``ZERO_LINE = 0; if
                # macd(close)[0] > ZERO_LINE:`` and similar predicates
                # would be dropped and the probe would degenerate to
                # ``UNKNOWN_LOW_COVERAGE``. Indicator dispatch in
                # :func:`_indicator_call` forwards these literals
                # straight to the helper (matching the runtime), so a
                # threshold-shaped binding (e.g. ``ZERO_LINE = 0``)
                # only matters in operand comparisons, not in window
                # arguments — strategies that pass non-positive or
                # float values to a helper will fail identically in
                # the probe and the runtime.
                v = _numeric_literal(value, self._name_periods)
                if v is not None:
                    self._name_periods[target.id] = int(v) if float(v).is_integer() else float(v)
                else:
                    # Non-literal RHS (e.g. ``LIMIT = self.dynamic_limit()``).
                    # Drop any prior scalar binding so downstream
                    # ``_build_operand`` lookups treat the comparison
                    # as unmodelled rather than evaluating against the
                    # stale literal that the previous assignment set.
                    self._name_periods.pop(target.id, None)
                # String-scalar side: a function-local ``target = "BBB"``
                # is a bare-name binding inside ``on_bar`` and must be
                # visible to bare-``Name`` resolution in ``_symbol_gate``
                # (e.g. ``if bar.symbol == target:``). Function-local
                # names take precedence over module-level globals via
                # overwrite — Python's lexical scope chain. Writes to
                # ``globals_`` rather than ``attrs`` because a bare name
                # never resolves through the class.
                #
                # RHS aliases (``target = OTHER`` / ``target = self.X``)
                # resolve through the current bindings — bare ``Name``
                # via ``globals_`` (method scope = module scope),
                # ``self.X`` via ``attrs``.
                str_value = _resolve_string_in_method(value, self._name_strings)
                if str_value is not None:
                    self._name_strings.globals_[target.id] = str_value
                else:
                    self._name_strings.globals_.pop(target.id, None)
            elif isinstance(
                target, ast.Attribute
            ):  # pragma: no cover — flow-sensitive `self.X = ...` inside on_bar; rare in generated strategies (most attribute writes live in __init__)
                # ``self.WINDOW = N`` — record by attribute name.
                v = _numeric_literal(value, self._name_periods)
                if v is not None:
                    self._name_periods[target.attr] = int(v) if float(v).is_integer() else float(v)
                else:
                    # Same drop-stale rule for ``self.X = <non-literal>``.
                    self._name_periods.pop(target.attr, None)
                # ``self.TARGET = "BBB"`` (or alias from a module/global
                # constant) — flow-sensitive instance-attr binding
                # routed through ``attrs`` so ``self.TARGET`` /
                # ``cls.TARGET`` resolution sees it without leaking into
                # bare-name lookups.
                str_value = _resolve_string_in_method(value, self._name_strings)
                if str_value is not None:
                    self._name_strings.attrs[target.attr] = str_value
                else:
                    self._name_strings.attrs.pop(target.attr, None)

    def _budgeted_extend(self, group_legs: List[Leg], extras: List[Leg]) -> bool:
        """Append extras into group within the global leg budget.

        Returns False when the global cap is hit (caller should stop).
        """
        for leg in extras:
            if self._budget >= _MAX_SUBCONDITIONS:
                return False
            group_legs.append(leg)
            self._budget += 1
        return True

    def _process_if(
        self,
        test: ast.expr,
        body: List[ast.stmt],
        orelse: List[ast.stmt],
        ancestors: List[Leg],
        ancestor_symbols: Optional[set],
        ancestor_unknown: bool,
        ancestor_denied: Optional[set] = None,
    ) -> bool:
        """Process a single if-shape (test + body + orelse) given an
        ancestor stack. Used both for real ``ast.If`` statements and for
        synthesised ifs after stripping a position-gate conjunct.

        ``ancestor_unknown`` is True when any enclosing ``if`` test had
        an un-modellable AND conjunct. Body recursion inherits the flag
        because the descendant predicate only fires when the unknown
        ancestor conjunct is also true; the descendant group's
        recognised mask is therefore still only an upper bound. Without
        this, ``if close > 0 and self.custom_ok(bar): if volume > 0:
        ...`` would emit a clean nested group whose recognised legs
        carried the report to ``COVERAGE_OK`` even though the unknown
        ancestor could narrow it to zero.

        ``ancestor_denied`` carries the symbol denylist accumulated
        from enclosing exclude-shaped early-return guards (``if
        bar.symbol == "AAPL": return``); the aggregator drops those
        symbols from every emitted group's evaluation.
        """
        # Top-level OR predicate: each leg becomes an independent
        # subcondition row but the group's blocker classification uses
        # disjunction (only too-restrictive when ALL legs are zero).
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return self._process_or_if(
                test, body, orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied
            )

        # Statically-unreachable AND short-circuit. If any conjunct is
        # a literal-falsy ``Constant`` (``False`` / ``0`` / ``None`` /
        # ``""``) or a statically-false ``Compare`` (e.g. ``1 < 0``,
        # ``LIMIT == 0`` after ``LIMIT = 1``), the whole predicate is
        # unreachable. Emitting a group from the surviving recognised
        # siblings would let them carry the report to ``COVERAGE_OK``
        # even though no bar can satisfy the real entry path. Skip
        # body recursion entirely; ``orelse`` runs unconditionally so
        # we still recurse into it.
        for term in _flatten_top_terms(test):
            truth = _evaluate_static_predicate(term, self._name_periods, self._name_evaluators)
            if truth is False:
                if not self._visit(
                    orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied
                ):
                    return False
                return True

        own_subs: List[Leg] = []
        own_symbols: Optional[set] = None
        # Track whether any AND-conjunct could not be statically modelled.
        # When set, the recognised mask is a SUPERSET of the real
        # predicate so the aggregator must not conclude ``COVERAGE_OK``
        # from the recognised legs alone — surfaces as ``AndOp.unknown``
        # in the emitted IR.
        has_unknown_conjunct = False
        for term in _flatten_top_terms(test):
            # Statically-true literal conjunct (``True``, non-zero
            # number, non-empty string, etc.) is a no-op AND-gate. The
            # recognised siblings' mask is exact in its presence, so
            # don't taint the group as unknown. Statically-false
            # literals make the predicate dead — also not "unknown
            # narrowing" in the sense the aggregator needs to suppress
            # COVERAGE_OK; the surviving recognised legs simply don't
            # describe a reachable path.
            #
            # Unified static-evaluation skip. ``True`` means the term
            # is a statically-decidable no-op (literal ``True``,
            # ``1 < 2``, ``1 + 1 == 2``, ...) — drop it from the
            # group's recognised set without tagging the group as
            # unknown. ``False`` was already short-circuited by the
            # pre-scan above so we don't expect to see it here, but
            # treating it like ``True`` is safe (the surviving
            # recognised siblings can't carry the report; the group
            # will still be empty). ``None`` (un-decidable) falls
            # through to the regular type dispatch so an
            # almost-static-but-unevaluable Compare (e.g.
            # ``(5 % 2 == 0)`` whose ``Mod`` operand isn't in the
            # constant-folding scope) lands in the unknown-conjunct
            # path rather than slipping through as a silent no-op.
            truth = _evaluate_static_predicate(term, self._name_periods, self._name_evaluators)
            if truth is not None:
                continue
            if isinstance(term, ast.Compare):
                sym = _symbol_gate(term, self._name_strings, self._bar_name)
                if sym is not None:
                    # Multiple ``bar.symbol == X`` gates within a single
                    # ``and`` are conjoined, so a second different literal
                    # *contradicts* the first — they must be intersected,
                    # not unioned. ``bar.symbol == "AAPL" and
                    # bar.symbol == "MSFT"`` collapses to an empty filter,
                    # which downstream drops as unreachable.
                    if own_symbols is None:
                        own_symbols = set(sym)
                    else:
                        own_symbols &= sym
                    continue
                sub = _build_subcond(term, self._name_periods, self._name_evaluators)
                if sub is not None:
                    own_subs.append(sub)
                else:
                    # Compare term we couldn't model. Either an opaque
                    # comparison (``self.flag == True``) or a static-
                    # constant compare we couldn't actually fold
                    # (``_build_operand`` accepted both BinOp operands
                    # but ``_evaluate_static_predicate`` returned None
                    # — see its docstring). In both cases the
                    # recognised siblings' mask is at best an upper
                    # bound on the real predicate, so tag the group
                    # as unknown.
                    has_unknown_conjunct = True
                continue
            # A nested OR inside the top-level AND, e.g.
            # ``if close > 0 and (volume < 0 or close < -1):`` — flatten
            # the disjunction into a single AND-conjunct subcond whose
            # evaluator is the bar-wise OR of the inner legs' masks.
            # Without this the OR was sent to _build_truthy_subcond,
            # returned None, and the whole disjunction was dropped —
            # leaving the AND predicate's coverage decision based on
            # only the surviving Compare conjuncts.
            if isinstance(term, ast.BoolOp) and isinstance(term.op, ast.Or):
                or_compound = _build_compound_subcond(
                    term,
                    self._name_periods,
                    _OR_OPS,
                    self._name_evaluators,
                    self._name_strings,
                    self._bar_name,
                )
                if or_compound is not None:
                    own_subs.append(or_compound)
                    # If the OR is fully symbol-gated (every leg restricted
                    # via ``bar.symbol == "X"``), the OR-compound's outer
                    # ``SymbolGate`` is the union of those gates.
                    # Propagate that allowlist to the GROUP level so
                    # sibling AND-conjuncts are evaluated only against
                    # the gated symbols. Without this, a predicate like
                    # ``(bar.symbol == "AAPL" or bar.symbol == "MSFT")
                    # and close > 100`` lets the sibling ``close > 100``
                    # count hits from unrelated symbols (GOOG); the
                    # report then flags ``CONJUNCTION_NEVER_TRUE``
                    # instead of the actionable
                    # ``INDICATOR_FILTER_TOO_RESTRICTIVE`` on the
                    # gated symbols.
                    or_compound_gate = _leg_gate_symbols(or_compound)
                    if (
                        or_compound_gate is not None
                    ):  # pragma: no cover — fully-symbol-gated nested-OR within AND rare in generated strategies
                        if own_symbols is None:
                            own_symbols = set(or_compound_gate)
                        else:
                            own_symbols &= or_compound_gate
                else:
                    # Couldn't model any leg of the inner OR — the whole
                    # disjunction is opaque. Treat it as an unknown
                    # conjunct so a sibling ``close > 0`` doesn't carry
                    # the group to ``COVERAGE_OK`` on its own.
                    has_unknown_conjunct = True
                continue
            # Truthiness term — ``bool(x)`` or a bare ``Name`` referencing
            # a precomputed indicator. Required for the ideation/codegen
            # shape ``_entry = sma(close, 200) > bar.close`` followed by
            # ``if pos is None and bool(_entry):``. When ``Name`` doesn't
            # resolve to a recognised indicator helper (e.g. compiler-
            # emitted ``self._n_X`` factor methods), we leave the term
            # unhandled rather than silently treating it as always-true.
            truthy = _build_truthy_subcond(term, self._name_periods, self._name_evaluators)
            if truthy is not None:
                own_subs.append(truthy)
            else:
                # Un-modellable term (e.g. ``self.custom_ok(bar)``,
                # ``some_function()``, attribute lookup that isn't a
                # known indicator series). Tag the group so the
                # aggregator knows the recognised mask is only a
                # superset of the real predicate.
                has_unknown_conjunct = True

        effective_symbols = _intersect_symbols(ancestor_symbols, own_symbols)
        # Effective unknown narrowing for self._groups emitted at this level:
        # the union of any inherited unknown ancestor and a locally-
        # detected unknown conjunct. Body recursion uses the same flag
        # so descendants remain tainted; orelse uses the bare inherited
        # value because the negation of an unknown isn't an unknown
        # gate on the orelse path.
        effective_unknown = ancestor_unknown or has_unknown_conjunct
        effective_denied = frozenset(ancestor_denied) if ancestor_denied else None

        group_legs: List[Leg] = []
        if not self._budgeted_extend(
            group_legs, ancestors
        ):  # pragma: no cover — budget exhaustion (>16 legs) unreachable in production
            if group_legs:
                self._groups.append(
                    _build_and_group(
                        group_legs, effective_symbols, effective_unknown, effective_denied
                    )
                )
            return False
        if not self._budgeted_extend(
            group_legs, own_subs
        ):  # pragma: no cover — budget exhaustion (>16 legs) unreachable in production
            if group_legs:
                self._groups.append(
                    _build_and_group(
                        group_legs, effective_symbols, effective_unknown, effective_denied
                    )
                )
            return False
        if group_legs and not (effective_symbols is not None and not effective_symbols):
            self._groups.append(
                _build_and_group(group_legs, effective_symbols, effective_unknown, effective_denied)
            )
        if not self._visit(
            body,
            ancestors + own_subs,
            effective_symbols,
            effective_unknown,
            ancestor_denied,
        ):
            return False
        if not self._visit(orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied):
            return False
        return True

    def _process_or_if(
        self,
        test: ast.BoolOp,
        body: List[ast.stmt],
        orelse: List[ast.stmt],
        ancestors: List[Leg],
        ancestor_symbols: Optional[set],
        ancestor_unknown: bool,
        ancestor_denied: Optional[set] = None,
    ) -> bool:
        """Process ``if A or B or C:`` — each leg becomes an independent
        subcondition row, classified disjunctively at aggregation time.

        Body recursion runs with bare ancestors rather than
        ``ancestors + or_legs`` because we don't have a single conjunct
        to attach: any one of the legs being true is sufficient for the
        body, and modelling the OR as an extra ancestor would amount to
        building a synthetic merged-mask we can't represent in the
        per-Subcond ``evaluate`` callback. Conservative under-flagging
        on the body's nested coverage is preferable to over-flagging.

        ``orelse`` recursion remains bare-ancestor (consistent with the
        AND path).

        ``ancestor_unknown`` (an inherited AND-side unknown narrowing,
        not the OR-side leg uncertainty) is propagated unchanged to
        body and orelse: an OR test does not introduce its own AND
        narrowing, so descendants only inherit what was already in
        place at this node's entry.
        """
        own_subs: List[Leg] = []
        # Track legs we couldn't statically model (e.g. an unrecognised
        # method call like ``self.custom_ok(bar)``). When at least one
        # leg is unknown the OR's "all known legs zero" rule must NOT
        # flag a blocker — the un-modelled alternative may make the
        # entry reachable, so flagging would be a false positive.
        # Surfaces as ``OrOp.unknown=True`` on the emitted IR.
        has_unknown_leg = False
        for leg in test.values:
            if isinstance(leg, ast.Compare):
                # Standalone ``bar.symbol == "X"`` legs are symbol
                # allowlists: the leg is true exactly on bars from "X".
                # Without this branch ``_build_subcond`` rejects the gate
                # (no data-dependent operand), the leg is dropped, and a
                # predicate like ``bar.symbol == "AAPL" or close > 100``
                # collapses to just ``close > 100`` with disjunction
                # semantics — if ``close > 100`` has zero hits the probe
                # falsely flags ``INDICATOR_FILTER_TOO_RESTRICTIVE`` even
                # though every AAPL bar satisfies the predicate. Mirror
                # the nested-OR helper: emit an always-true mask scoped
                # by the leg's symbol so the aggregator counts AAPL bars
                # as a firing leg.
                sym = _symbol_gate(leg, self._name_strings, self._bar_name)
                if sym is not None:
                    own_subs.append(
                        Leg(
                            label=_format_label(leg),
                            inner=SymbolGate(syms=frozenset(sym), inner=Static(True)),
                        )
                    )
                    continue
                sub = _build_subcond(leg, self._name_periods, self._name_evaluators)
                if sub is not None:
                    own_subs.append(sub)
                else:
                    has_unknown_leg = True
                continue
            if isinstance(leg, ast.BoolOp) and isinstance(leg.op, ast.And):
                # Compound OR leg, e.g. ``(close > 100 and volume > 0)``
                # in ``(A and B) or (C and D)``. Each conjunct is built
                # individually and the leg's evaluator is the bar-wise
                # AND of all inner masks — that compound mask is what
                # the disjunction needs to test. Drops cleanly to None
                # when no inner term is recognisable.
                compound = _build_compound_subcond(
                    leg,
                    self._name_periods,
                    _AND_OPS,
                    self._name_evaluators,
                    self._name_strings,
                    self._bar_name,
                )
                if compound is not None:
                    own_subs.append(compound)
                else:
                    has_unknown_leg = True
                continue
            truthy = _build_truthy_subcond(leg, self._name_periods, self._name_evaluators)
            if truthy is not None:
                own_subs.append(truthy)
            else:
                has_unknown_leg = True

        denied_frozen = frozenset(ancestor_denied) if ancestor_denied else None

        if not own_subs:  # pragma: no cover — OR with no recognised legs is rare; fall-through descent path not exercised by current corpus
            # No recognised legs — fall through to body / orelse without
            # emitting a group, so nested ``if`` analysis still runs.
            if not self._visit(
                body, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied
            ):
                return False
            if not self._visit(
                orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied
            ):
                return False
            return True

        # Ancestors stay AND-required; OR legs are alternatives. We
        # carry both in one tree: ``AndOp(legs=(ancestors..., OrOp(alts...)))``,
        # which directly encodes the AND-required prefix + OR-tail split
        # the OLD ``_Group.ancestor_count`` integer used to encode.
        group_ancestors: List[Leg] = []
        if not self._budgeted_extend(
            group_ancestors, ancestors
        ):  # pragma: no cover — budget exhaustion (>16 legs) unreachable in production
            if group_ancestors:
                self._groups.append(
                    _build_and_group(
                        group_ancestors, ancestor_symbols, ancestor_unknown, denied_frozen
                    )
                )
            return False
        group_or_legs: List[Leg] = []
        if not self._budgeted_extend(
            group_or_legs, own_subs
        ):  # pragma: no cover — budget exhaustion (>16 legs) unreachable in production
            if group_ancestors or group_or_legs:
                self._groups.append(
                    _build_or_group(
                        group_ancestors,
                        group_or_legs,
                        has_unknown_leg,
                        ancestor_symbols,
                        ancestor_unknown,
                        denied_frozen,
                    )
                )
            return False
        if group_ancestors or group_or_legs:
            self._groups.append(
                _build_or_group(
                    group_ancestors,
                    group_or_legs,
                    has_unknown_leg,
                    ancestor_symbols,
                    ancestor_unknown,
                    denied_frozen,
                )
            )
        # Carry the OR predicate into the body recursion as a single
        # compound ancestor: the body only fires on bars where some
        # OR leg also fired, so any nested ``if`` predicate must AND
        # against the OR's bar-wise mask. Without this, a shape like
        # ``if close > 100 or close < 0: if volume < 0: pass`` was
        # reported ``COVERAGE_OK`` whenever ``close > 100`` and
        # ``volume < 0`` each fired on at least one bar, even when
        # never on the same bar — the live entry path was empty but
        # the probe had no representation of the OR mask at the
        # nested level.
        #
        # ``_build_compound_subcond`` already does the leg
        # synthesis (compound OR-of-masks evaluator + per-leg symbol
        # gates rolled into ``target_symbols`` when every leg is
        # symbol-gated). Reuse it here so the nested body is
        # evaluated against the same OR semantics the aggregator
        # uses for the immediate group.
        body_ancestors = ancestors
        body_symbols = ancestor_symbols
        body_unknown = ancestor_unknown or has_unknown_leg
        or_compound = _build_compound_subcond(
            test,
            self._name_periods,
            _OR_OPS,
            self._name_evaluators,
            self._name_strings,
            self._bar_name,
        )
        if or_compound is not None:
            body_ancestors = ancestors + [or_compound]
            or_compound_gate = _leg_gate_symbols(or_compound)
            if or_compound_gate is not None:
                body_symbols = _intersect_symbols(ancestor_symbols, set(or_compound_gate))
            if _tree_or_unknown(or_compound.inner):
                body_unknown = True
        else:  # pragma: no cover — fully-unmodellable OR ancestor descent rare
            # OR was fully un-modellable — every nested predicate is
            # gated by an unknown ancestor, so descendants can't
            # supply positive evidence on their own.
            body_unknown = True
        if not self._visit(
            body, body_ancestors, body_symbols, body_unknown, ancestor_denied
        ):  # pragma: no cover — budget exhaustion propagation
            return False
        if not self._visit(
            orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied
        ):  # pragma: no cover — budget exhaustion propagation
            return False
        return True

    def _visit(
        self,
        stmts: List[ast.stmt],
        ancestors: List[Leg],
        ancestor_symbols: Optional[set],
        ancestor_unknown: bool = False,
        ancestor_denied: Optional[set] = None,
    ) -> bool:
        # Implicit symbol filter accumulated from early-return guards
        # at this scope. ``if bar.symbol != "BBB": return`` excludes
        # all symbols other than BBB for any statement that follows in
        # the same block — sibling predicates must be evaluated under
        # this implied gate, otherwise an unrelated symbol could
        # satisfy a price filter and the report would falsely flip to
        # COVERAGE_OK even though the live entry path is unreachable
        # for the target.
        current_symbols: Optional[set] = ancestor_symbols
        # Sibling-scope denylist accumulated from exclude-shaped
        # early-return guards (``if bar.symbol == "AAPL": return``).
        # Mirrors ``current_symbols`` but with the opposite polarity:
        # any symbol in this set is excluded from subsequent siblings'
        # evaluation. Independent of ``current_symbols`` so a strategy
        # that combines an allowlist and an exclude on different
        # symbols composes correctly.
        current_denied: Optional[set] = set(ancestor_denied) if ancestor_denied else None
        with self._snapshot():
            for stmt in stmts:
                # Apply assignments in source order so each predicate
                # sees only the bindings established by lexically
                # preceding statements. Without this a later
                # reassignment leaks back to earlier predicates via the
                # shared dicts.
                if isinstance(stmt, ast.Assign) or isinstance(stmt, ast.AnnAssign):
                    self._apply_assign_inplace(stmt)
                    continue

                if isinstance(stmt, ast.If):
                    # Early-return symbol guard: ``if bar.symbol != "X":
                    # return`` / ``not in (...)`` → allowlist update;
                    # ``if bar.symbol == "X": return`` / ``in (...)`` →
                    # denylist update. Both shapes update the implicit
                    # symbol filter for subsequent siblings rather than
                    # emitting a coverage row for the guard itself.
                    guard = _early_return_symbol_guard(stmt, self._name_strings, self._bar_name)
                    if guard is not None:
                        polarity, syms = guard
                        if polarity == "allow":
                            current_symbols = _intersect_symbols(current_symbols, syms)
                        else:  # "deny"
                            if current_denied is None:
                                current_denied = set(syms)
                            else:
                                current_denied |= syms
                        continue

                    # ``if pos is None: ... else: ...`` (and the inverted
                    # ``if pos is not None: <exit> else: <entry>``) is the
                    # documented entry/exit gate. The codegen also produces
                    # combined forms like ``if pos is None and <entry>:`` /
                    # ``elif pos is not None and <exit>:`` — the ``elif`` is
                    # represented as a nested ``if`` inside the parent's
                    # orelse, so we must strip the position-gate conjunct
                    # from the test and route the rest accordingly.
                    position_check, gate_residual = _strip_position_gate(stmt.test)
                    if position_check == "vacant":  # pos is None — body is entry
                        if gate_residual is None:
                            if not self._visit(
                                stmt.body,
                                ancestors,
                                current_symbols,
                                ancestor_unknown,
                                current_denied,
                            ):  # pragma: no cover — budget exhaustion propagation
                                return False
                            # Vacant guard-clause: ``if pos is None:
                            # return`` (or any single ``return``).
                            # Subsequent siblings only execute when
                            # ``pos is not None`` — the exit path.
                            # Skip them so a follow-up ``if close < 0:
                            # sell()`` doesn't get classified as
                            # entry coverage.
                            if _is_return_only_body(stmt.body):
                                break
                        else:
                            if not self._process_if(
                                gate_residual,
                                stmt.body,
                                [],
                                ancestors,
                                current_symbols,
                                ancestor_unknown,
                                current_denied,
                            ):  # pragma: no cover — budget exhaustion propagation
                                return False
                        continue
                    if position_check == "occupied":  # pos is not None — orelse is entry
                        if not self._visit(
                            stmt.orelse,
                            ancestors,
                            current_symbols,
                            ancestor_unknown,
                            current_denied,
                        ):  # pragma: no cover — budget exhaustion propagation
                            return False
                        continue

                    if not self._process_if(
                        stmt.test,
                        stmt.body,
                        stmt.orelse,
                        ancestors,
                        current_symbols,
                        ancestor_unknown,
                        current_denied,
                    ):  # pragma: no cover — budget exhaustion propagation
                        return False
                else:
                    # Skip nested function / class bodies — they only
                    # execute if explicitly invoked, and we don't model
                    # arbitrary calls. Without this guard a local
                    # helper such as ``def debug_helper(): if close <
                    # 0: ...`` defined inside ``on_bar`` would have its
                    # ``if`` predicates analysed as if they were on the
                    # entry path, producing spurious
                    # ``INDICATOR_FILTER_TOO_RESTRICTIVE`` blockers
                    # from dead helper code. ``ClassDef`` is included
                    # for symmetry — a strategy-defined inner class's
                    # methods don't run on the entry path either.
                    if isinstance(
                        stmt,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    ):
                        continue
                    # Descend into compound statements (For, While, With,
                    # Try) but pass through ancestors so
                    # ``for x in ...: if close > 100: ...`` still inherits
                    # nothing, which is correct.
                    for field in _BLOCK_FIELDS:
                        inner = getattr(stmt, field, None)
                        if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                            if not self._visit(
                                inner,
                                ancestors,
                                current_symbols,
                                ancestor_unknown,
                                current_denied,
                            ):  # pragma: no cover — budget exhaustion propagation
                                return False
                    # ast.Try has handlers; each handler.body is a stmt list.
                    handlers = getattr(stmt, "handlers", None)
                    if isinstance(
                        handlers, list
                    ):  # pragma: no cover — rare except-handler descent for AST shapes not generated by Strategy Lab
                        for h in handlers:
                            h_body = getattr(h, "body", None)
                            if isinstance(h_body, list) and h_body:
                                if not self._visit(
                                    h_body,
                                    ancestors,
                                    current_symbols,
                                    ancestor_unknown,
                                    current_denied,
                                ):
                                    return False
            return True


def _extract_subconditions(strategy_code: str) -> List[PredicateGroup]:
    """Return one group of subconditions per ``if`` predicate.

    Subconditions are grouped by their parent ``if`` so the conjunction
    hit-rate check stays scoped to a single predicate. Two **sibling**
    branches like ``if close > 100: enter`` and ``if close < 50: exit``
    are returned as separate groups and are never ANDed together.

    A **nested** ``if`` inherits the subconditions of every enclosing
    ``if`` on its positive control-flow path: ``if close > 100: if close
    < 50: pass`` produces a single group containing both legs.

    Position checks (``if pos is None: ... else: ...``) are special-cased:
    the documented strategy template uses this to gate the entry logic
    in ``body`` and the exit logic in ``orelse``. We only recurse into
    ``body`` so exit predicates aren't mis-reported as entry-coverage
    blockers.

    Symbol gates (``bar.symbol == "AAPL"``) attach a per-group symbol
    filter so the indicator condition is only evaluated against that
    DataFrame — otherwise an unrelated symbol's data could satisfy a
    ``close > 1000`` filter and mask the actual zero-coverage on the
    target symbol.

    The positive branch (``body``) propagates the ancestor predicate;
    ``orelse`` does not, since negating an arbitrary indicator subcond
    is generally ambiguous and we'd rather under-flag than over-flag.
    """
    if not strategy_code:
        return []
    tree = ast.parse(strategy_code)
    on_bar = _find_on_bar(tree)
    if on_bar is None:
        return []
    visitor = SubconditionVisitor(tree, on_bar)
    return visitor.walk(on_bar)


def _strip_position_gate(test: ast.expr) -> tuple:
    """Detect a position-gate inside (or as) a boolean entry test.

    Generated strategies often combine the position check with the entry
    rule in one predicate: ``if pos is None and <entry>:`` and the
    matching ``elif pos is not None and <exit>:``. The ``elif`` is
    parsed as a nested ``if`` inside the outer ``orelse``, so without
    this helper the exit predicate would be treated as another entry
    coverage subcond.

    Returns ``(direction, residual)`` where:

    - ``direction`` is ``"vacant"`` / ``"occupied"`` / ``None``.
    - ``residual`` is the remaining test expression after the
      position-gate conjunct is removed, or ``None`` if no further
      conjuncts remain (bare position check).

    For combined gates with three or more conjuncts the residual is the
    AND of the surviving values, preserving any indicator subconditions
    that legitimately gate the entry alongside the position check.
    """
    direction = _classify_position_check(test)
    if direction is not None:
        return direction, None

    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        position_dir: Optional[str] = None
        survivors: List[ast.expr] = []
        for value in test.values:
            d = _classify_position_check(value)
            if d is not None and position_dir is None:
                # First gate wins; stop matching against further conjuncts
                # so a same-test repeated by accident isn't reclassified.
                position_dir = d
                continue
            survivors.append(value)
        if position_dir is not None:
            if not survivors:
                return position_dir, None
            if len(survivors) == 1:
                return position_dir, survivors[0]
            return (
                position_dir,
                ast.BoolOp(op=ast.And(), values=survivors),
            )  # pragma: no cover — multi-survivor position-gate residual rare in generated strategies
    return None, None


def _classify_position_check(test: ast.expr) -> Optional[str]:
    """Classify a position-check ``if`` test direction.

    Returns:
      - ``"vacant"`` — the test means "no open position" (``pos is None``,
        ``position == None``, ``ctx.position(...) is None``). The ``body``
        branch is the entry path; ``orelse`` is the exit path.
      - ``"occupied"`` — the test means "position exists" (``pos is not
        None``, ``position != None``). The ``orelse`` branch is the entry
        path; ``body`` is the exit path.
      - ``None`` — not a position check at all.

    The caller routes the recursion accordingly so exit predicates never
    surface as entry-coverage blockers regardless of which polarity the
    strategy uses.
    """
    if not isinstance(test, ast.Compare):
        return None
    if len(test.ops) != 1:  # pragma: no cover — chained comparison in position-check predicate rare
        return None
    op = test.ops[0]
    rhs = test.comparators[0]
    if not (isinstance(rhs, ast.Constant) and rhs.value is None):
        return None
    left = test.left
    if isinstance(left, ast.Name) and left.id in {"pos", "position"}:
        pass
    elif (  # pragma: no cover — ``ctx.position()`` call shape rare in generated strategies
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Attribute)
        and left.func.attr == "position"
    ):
        pass
    else:
        return None
    if isinstance(op, (ast.Is, ast.Eq)):
        return "vacant"
    if isinstance(op, (ast.IsNot, ast.NotEq)):
        return "occupied"
    return None  # pragma: no cover — non-equality op on None comparator declined


def _is_return_only_body(stmts: List[ast.stmt]) -> bool:
    """True iff ``stmts`` is a single ``return`` (with or without value).

    Used by :func:`_visit` to detect guard-clause shapes like
    ``if pos is None: return``. The reviewer pointed out that after
    such a guard, subsequent siblings only execute on the opposite
    branch — they're exit-only logic and shouldn't be analysed as
    entry coverage.
    """
    return len(stmts) == 1 and isinstance(stmts[0], ast.Return)


def _early_return_symbol_guard(
    stmt: ast.If,
    name_strings: Optional["_NameStrings"] = None,
    bar_name: str = "bar",
) -> Optional[Tuple[str, set]]:
    """Detect a ``if <bar>.symbol <op> ...: return`` symbol guard.

    Returns ``("allow", syms)`` for guards that *retain* a symbol set
    (the live code path continues only on those symbols) or
    ``("deny", syms)`` for guards that *exclude* a symbol set (the
    live code path continues on everything except those). ``None``
    means the if isn't a recognised guard shape and the caller should
    process it as a normal predicate.

    The guard's ``body`` must consist of a single bare ``return`` (or
    a ``return None``). Compound bodies, conditional returns, or
    side-effecting bodies aren't recognised because the implication
    isn't unambiguous.

    ``bar_name`` is the actual third positional parameter name of the
    strategy's ``on_bar``. The safety gate only enforces arity, so
    valid strategies may name it ``candle`` or ``b``; hard-coding
    ``"bar"`` would silently drop the guard for those.

    Recognised shapes (with ``bar_name='bar'`` shown for brevity):

    Allowlist (retain) shapes:
    - ``if bar.symbol != "X": return`` → ``("allow", {"X"})``
    - ``if bar.symbol != TARGET_SYMBOL: return`` (with
      ``TARGET_SYMBOL = "BBB"`` resolved via ``name_strings``) →
      ``("allow", {"BBB"})``
    - ``if bar.symbol not in ("X", "Y"): return`` →
      ``("allow", {"X", "Y"})``

    Denylist (exclude) shapes:
    - ``if bar.symbol == "X": return`` → ``("deny", {"X"})``
    - ``if bar.symbol == TARGET_SYMBOL: return`` →
      ``("deny", {<resolved>})``
    - ``if bar.symbol in ("X", "Y"): return`` →
      ``("deny", {"X", "Y"})``

    Without the deny shapes, exclude-guards left subsequent siblings
    free to count hits from the excluded symbol — the probe could
    report ``COVERAGE_OK`` from data the live entry path never sees.

    Returns ``None`` for anything else; the caller then processes the
    if as a normal predicate.
    """
    # Body must be a single bare return.
    if len(stmt.body) != 1:
        return None
    body0 = stmt.body[0]
    if not isinstance(body0, ast.Return):
        return None
    if (
        body0.value is not None
    ):  # pragma: no cover — value-bearing return rare in early-return symbol guards
        # ``return None`` is equivalent to bare return; anything else
        # (a value-bearing return) is too suggestive of a real path
        # we'd rather not assume nothing about.
        if not (isinstance(body0.value, ast.Constant) and body0.value.value is None):
            return None
    # An ``orelse`` here means there's a follow-up branch the strategy
    # cares about, which doesn't fit the simple "early return" guard
    # shape. Skip.
    if stmt.orelse:  # pragma: no cover — early-return-with-orelse shape declined
        return None

    test = stmt.test

    def _is_bar_symbol(n: ast.expr) -> bool:
        return (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == bar_name
            and n.attr == "symbol"
        )

    def _resolve_string(n: ast.expr) -> Optional[str]:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if (
            name_strings is None
        ):  # pragma: no cover — name_strings always provided in live call path
            return None
        # Bare ``Name`` resolves through the module/global scope only —
        # class-body bare names are NOT in lexical scope for methods.
        if isinstance(n, ast.Name):
            return name_strings.globals_.get(n.id)
        # ``self.X`` / ``cls.X`` resolves through the class chain
        # (instance dict via ``__init__`` shadowing class body), never
        # through module scope.
        if (  # pragma: no cover — ``self.X``/``cls.X`` in early-return guard rare in generated strategies
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in {"self", "cls"}
        ):
            return name_strings.attrs.get(n.attr)
        return None

    # ``bar.symbol <op> X`` / ``X <op> bar.symbol`` → allow / deny
    # depending on the operator polarity.
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        if isinstance(op, (ast.NotEq, ast.Eq)):
            polarity = "allow" if isinstance(op, ast.NotEq) else "deny"
            left, right = test.left, test.comparators[0]
            if _is_bar_symbol(left):
                sym = _resolve_string(right)
                if sym is not None:
                    return polarity, {sym}
            if _is_bar_symbol(
                right
            ):  # pragma: no cover — reversed-operand early-return guard rare in generated strategies
                sym = _resolve_string(left)
                if sym is not None:
                    return polarity, {sym}
        # ``bar.symbol not in (X, Y)`` → allow {X, Y}; the matching
        # ``in`` form is the deny variant — both keep the same
        # element-resolution rules and only differ on polarity.
        if isinstance(op, (ast.NotIn, ast.In)):
            polarity = "allow" if isinstance(op, ast.NotIn) else "deny"
            left, right = test.left, test.comparators[0]
            if _is_bar_symbol(left) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                syms: set = set()
                for elt in right.elts:
                    s = _resolve_string(elt)
                    if (
                        s is None
                    ):  # pragma: no cover — unresolvable element in symbol-list guard rare
                        return None
                    syms.add(s)
                if syms:
                    return polarity, syms
    return None


def _symbol_gate(
    node: ast.Compare,
    name_strings: Optional["_NameStrings"] = None,
    bar_name: str = "bar",
) -> Optional[set]:
    """Detect a symbol gate on ``<bar>.symbol``.

    Returns the set of allowed symbols when the comparison constrains
    the live symbol; ``None`` otherwise. Used to scope a group's
    evaluation to the matching DataFrames rather than evaluating
    against every symbol in the universe.

    ``bar_name`` is the actual third positional parameter name of the
    strategy's ``on_bar`` (the safety gate only enforces arity, so
    valid strategies may name it ``candle`` or ``b``). Hard-coding
    ``"bar"`` here silently dropped the gate for those strategies and
    a sibling price predicate would then evaluate against every
    fetched DataFrame, letting an unrelated symbol satisfy the
    predicate and falsely flag ``COVERAGE_OK``.

    Recognised shapes (with ``bar_name='bar'`` shown for brevity):

    - ``bar.symbol == "X"`` / ``"X" == bar.symbol`` → ``{"X"}``
    - ``bar.symbol == TARGET`` (with a string-constant binding via
      ``name_strings``) → ``{<resolved value>}``
    - ``bar.symbol in ("X", "Y")`` (positive allow-list) →
      ``{"X", "Y"}``. Without this, a strategy that allow-lists with
      ``in`` had the gate dropped and a sibling indicator condition
      silently evaluated against every fetched DataFrame.

    Inline string constants and named-string-constant references both
    resolve in the ``in`` form. An ``in`` operator with any
    unresolvable element returns ``None`` (don't constrain on a
    partially-known list).
    """
    if len(node.ops) != 1:  # pragma: no cover — chained comparison rare in symbol-gate position
        return None
    op = node.ops[0]
    left, right = node.left, node.comparators[0]

    def _is_bar_symbol(n: ast.expr) -> bool:
        return (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == bar_name
            and n.attr == "symbol"
        )

    def _string_const(n: ast.expr) -> Optional[str]:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if (
            name_strings is None
        ):  # pragma: no cover — name_strings always provided in live call path
            return None
        # Bare ``Name`` resolves through the module/global scope only —
        # class-body bare names are NOT in lexical scope for methods.
        if isinstance(n, ast.Name):
            return name_strings.globals_.get(n.id)
        # ``self.X`` / ``cls.X`` resolves through the class chain
        # (instance dict via ``__init__`` shadowing class body), never
        # through module scope.
        if (  # pragma: no cover — self.X/cls.X in symbol-gate position rare in generated strategies
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in {"self", "cls"}
        ):
            return name_strings.attrs.get(n.attr)
        return None

    if isinstance(op, (ast.Eq, ast.Is)):
        if _is_bar_symbol(left):
            sym = _string_const(right)
            return {sym} if sym is not None else None
        if _is_bar_symbol(
            right
        ):  # pragma: no cover — reversed operand symbol gate rare in generated strategies
            sym = _string_const(left)
            return {sym} if sym is not None else None
        return None

    if isinstance(op, ast.In):
        if not _is_bar_symbol(left):
            return None
        if not isinstance(
            right, (ast.Tuple, ast.List, ast.Set)
        ):  # pragma: no cover — non-literal-collection right operand rare in ``in`` symbol gates
            return None
        syms: set = set()
        for elt in right.elts:
            s = _string_const(elt)
            if s is None:  # pragma: no cover — unresolvable element in symbol-list gate rare
                # Partial allow-list: refuse to gate. Better to leave
                # the predicate unconstrained than apply a wrong filter.
                return None
            syms.add(s)
        return syms if syms else None

    return None


def _union_target_symbols(
    groups: List[PredicateGroup], universe: Optional[set] = None
) -> Optional[set]:
    """Return the union of symbols any group could possibly fire on, or ``None``.

    Used by :func:`run_indicator_probe` to size the warmup check to the
    symbols that can actually satisfy a predicate. Returns ``None`` when
    at least one group is **fully unconstrained** — i.e. no positive
    :class:`SymbolGate` narrows the symbol space anywhere along the
    path to a leaf, and the group carries no exclude-shaped early-return
    denylist — so the warmup check stays over every fetched DataFrame.

    ``universe`` is the set of symbol keys present in ``market_data``.
    It's required to express "universal except for these" when a group
    has ``denied_symbols`` but no positive :class:`SymbolGate` anywhere
    in its tree (e.g. ``if bar.symbol == "AAPL": return`` followed by
    an indicator-only predicate).

    Tree-walk semantics:

    * :class:`AndOp`: a leg's symbol space contributes via union of
      each conjunct's gate (conservative — for warmup we want every
      symbol that could conceivably contribute so we don't over-flag
      ``INSUFFICIENT_BARS``). A leg with no gate anywhere is universal
      and short-circuits.

    * :class:`OrOp`: the predicate fires when any alternative holds.
      An unrestricted alternative makes the OR universal at this level.

    * :class:`SymbolGate`: tightens the accumulated symbol filter via
      intersection (its ``syms`` are the only symbols that can satisfy
      the inner sub-tree).

    Denylists (``group.denied_symbols``) are subtracted from each
    group's effective set before union'ing. A group with no allowlist
    but a denylist resolves to ``universe - denied_symbols`` rather
    than ``universe``.
    """
    union: set = set()
    saw_universal = False
    for g in groups:
        group_syms = _tree_symbol_scope(g.tree)
        denied = set(g.denied_symbols) if g.denied_symbols else set()

        if group_syms is None:
            # Universal allowlist. If the denylist is empty the group is
            # fully universal and short-circuits the warmup denominator.
            if not denied:
                saw_universal = True
                continue
            if (
                universe is None
            ):  # pragma: no cover — universe-less denied-only group rare in current corpus
                saw_universal = True
                continue
            group_syms = set(universe) - denied
        else:
            group_syms = set(group_syms) - denied

        union.update(group_syms)

    if saw_universal:
        return None
    return union if union else None


def _tree_symbol_scope(node: BarPredicate) -> Optional[set]:
    """Return the set of symbols any leaf in *node* could fire on under
    the warmup-sizing semantics described in :func:`_union_target_symbols`,
    or ``None`` when the tree is universal at this level.
    """
    if isinstance(node, Leg):
        return _tree_symbol_scope(node.inner)
    if isinstance(node, SymbolGate):
        inner_scope = _tree_symbol_scope(node.inner)
        if inner_scope is None:
            return set(node.syms)
        return set(node.syms) & inner_scope
    if isinstance(node, AndOp):
        # Conservative: union of conjunct scopes, treating a universal
        # conjunct as contributing nothing extra to the narrowing.
        scope: Optional[set] = None
        saw_universal = False
        for leg in node.legs:
            leg_scope = _tree_symbol_scope(leg)
            if leg_scope is None:
                saw_universal = True
                continue
            if scope is None:
                scope = set(leg_scope)
            else:
                scope.update(leg_scope)
        if saw_universal and scope is None:
            return None
        return scope
    if isinstance(node, OrOp):
        # Any universal alternative makes the OR universal.
        scope = None
        for leg in node.legs:
            leg_scope = _tree_symbol_scope(leg)
            if leg_scope is None:
                return None
            if scope is None:
                scope = set(leg_scope)
            else:
                scope.update(leg_scope)
        return scope
    if isinstance(node, (MaskLeaf, Static)):
        return None
    return None  # pragma: no cover — defensive


def _intersect_symbols(a: Optional[set], b: Optional[set]) -> Optional[set]:
    """Combine ancestor and own symbol filters under conjunction.

    None means "no constraint introduced at this level". A real set of
    symbols overrides None. When both sides constrain, the effective
    filter is the intersection.
    """
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def _bar_param_name(on_bar: ast.AST) -> str:
    """Return the parameter name the strategy uses for the bar argument.

    The safety gate only enforces ``on_bar`` arity (self/cls + ctx +
    bar) and the harness calls positionally, so a valid strategy may
    write ``def on_bar(self, ctx, candle)`` and reference
    ``candle.symbol`` / ``candle.close`` throughout. The symbol
    recognisers (:func:`_symbol_gate`,
    :func:`_early_return_symbol_guard`) match the receiver's ``Name``
    id and historically hard-coded ``"bar"`` — for a strategy that
    renamed it, the symbol gate was silently dropped while
    :func:`_column_from` (which only checks the attribute) still
    treated ``candle.close`` as data, so an unrelated DataFrame could
    satisfy a price predicate and the report falsely flipped to
    ``COVERAGE_OK``.

    Returns the third positional parameter name when present (after
    ``self``/``cls`` and ``ctx``). Falls back to ``"bar"`` for module-
    level helper functions (which have no ``self``) where the bar
    parameter is the second positional argument, and ultimately for
    free functions / fewer-args shapes the gate doesn't recognise as
    canonical entry points anyway.
    """
    args = getattr(on_bar, "args", None)
    if args is None:  # pragma: no cover — defensive: every ast.FunctionDef has an args attribute
        return "bar"
    posargs = list(getattr(args, "args", []) or [])
    if len(posargs) >= 3:
        # Method form ``def on_bar(self, ctx, bar):``
        return posargs[2].arg
    if (
        len(posargs) == 2
    ):  # pragma: no cover — free-function on_bar shape rare; safety gate enforces method form
        # Free-function form ``def on_bar(ctx, bar):``
        return posargs[1].arg
    return "bar"  # pragma: no cover — under-arity on_bar shape declined by safety gate before this point


def _find_on_bar(tree: ast.AST) -> Optional[ast.AST]:
    """Prefer ``on_bar`` — the real Strategy contract — when present.

    Only fall back to ``entry`` / ``signal`` / ``generate_signal`` if no
    ``on_bar`` is found. Otherwise a module-level helper named ``signal``
    placed before the strategy class would shadow the real entry path.
    """
    fallback: Optional[ast.AST] = None
    fallback_names = ("entry", "signal", "generate_signal")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name.lower()
        if name == "on_bar":
            return node
        if (
            fallback is None and name in fallback_names
        ):  # pragma: no cover — fallback entry-name (entry/signal/generate_signal) rare in generated strategies
            fallback = node
    return fallback


def _iter_entry_path_assigns(
    node: ast.AST,
):  # pragma: no cover — legacy AST walker; current call sites pass function_node=None so this path is unreachable from the live entry path
    """Yield ``Assign`` / ``AnnAssign`` nodes on the entry control-flow path.

    Skips the non-entry branch of any ``if`` whose test is (or is gated
    by) a position check — the same routing :func:`_visit` applies on
    the main traversal. Without this filter, an exit-branch reassignment
    like ``ma = sma(close, 200)`` would shadow the entry-branch's
    ``ma = sma(close, 5)`` because the binding pass uses overwrite
    semantics; the probe would then evaluate the entry comparison
    against the exit-path indicator and falsely flag
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``.

    Module/class scope (where there is no entry/exit distinction) calls
    :func:`ast.walk` directly; this helper is for the function-local
    pass only.
    """
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        yield node

    if isinstance(node, ast.If):
        # ``_strip_position_gate`` handles both bare ``if pos is None:``
        # and combined ``if pos is None and <entry>:`` shapes — same
        # logic _visit uses to route the main traversal.
        position_check, _residual = _strip_position_gate(node.test)
        if position_check == "vacant":
            for child in node.body:
                yield from _iter_entry_path_assigns(child)
            return
        if position_check == "occupied":
            for child in node.orelse:
                yield from _iter_entry_path_assigns(child)
            return
        for child in node.body:
            yield from _iter_entry_path_assigns(child)
        for child in node.orelse:
            yield from _iter_entry_path_assigns(child)
        return

    # Non-if compound statements: descend through standard block fields.
    for field in _BLOCK_FIELDS:
        children = getattr(node, field, None)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, ast.AST):
                    yield from _iter_entry_path_assigns(child)
    handlers = getattr(node, "handlers", None)
    if isinstance(handlers, list):
        for h in handlers:
            h_body = getattr(h, "body", None)
            if isinstance(h_body, list):
                for child in h_body:
                    if isinstance(child, ast.AST):
                        yield from _iter_entry_path_assigns(child)


def _flatten_test(
    test: ast.expr,
) -> List[
    ast.Compare
]:  # pragma: no cover — legacy AST helper superseded by _flatten_top_terms; kept for external import compatibility
    """Flatten ``a and b and (c < d)`` into individual ``Compare`` nodes."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        out: List[ast.Compare] = []
        for value in test.values:
            out.extend(_flatten_test(value))
        return out
    if isinstance(test, ast.Compare):
        return [test]
    return []


def _flatten_top_terms(test: ast.expr) -> List[ast.expr]:
    """Split a top-level ``and`` chain into individual term expressions.

    Unlike :func:`_flatten_test`, this returns the raw expression nodes
    (not just ``Compare``), so callers can recognise truthiness terms
    such as ``bool(_entry)`` or a bare ``Name`` reference to a
    precomputed indicator series alongside ordinary comparisons.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        out: List[ast.expr] = []
        for value in test.values:
            out.extend(_flatten_top_terms(value))
        return out
    return [test]


_BINOP_FOLDERS: Dict[type, Callable[[float, float], float]] = {
    ast.Mult: lambda a, b: a * b,
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
}


def _static_scalar_value(
    node: ast.expr,
    name_periods: Dict[str, int],
) -> Optional[float]:
    """Resolve ``node`` to a scalar ``float`` when it's a constant
    expression, or ``None`` otherwise.

    Mirrors the non-data-dependent scope of :func:`_build_operand`
    (literals, ``USub``, named numeric bindings, and
    ``Mult``/``Add``/``Sub`` ``BinOp`` chains over the same), so
    :func:`_evaluate_static_predicate` can actually fold every
    constant-only comparison that ``_build_operand`` would accept.
    Without arithmetic ``BinOp`` folding, ``(1 + 1 == 3)`` was
    rejected by :func:`_numeric_literal` and slipped through as an
    "accepted but unevaluable" no-op skip even though the real
    comparison is statically false.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _static_scalar_value(node.operand, name_periods)
        if (
            inner is None
        ):  # pragma: no cover — nested unary-minus of an unresolvable operand rare in current corpus
            return None
        return -inner
    if isinstance(node, ast.BinOp):
        folder = _BINOP_FOLDERS.get(type(node.op))
        if folder is None:
            return None
        left = _static_scalar_value(node.left, name_periods)
        right = _static_scalar_value(node.right, name_periods)
        if left is None or right is None:
            return None
        try:
            return float(folder(left, right))
        except Exception:  # noqa: BLE001  # pragma: no cover — defensive: arithmetic on validated scalars cannot raise
            return None
    return _numeric_literal(node, name_periods)


_STATIC_CMP_OPS: Dict[type, Callable[[float, float], bool]] = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _evaluate_static_predicate(
    node: ast.expr,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]],
) -> Optional[bool]:
    """Return ``True`` / ``False`` when ``node`` is a statically-decidable
    boolean term, ``None`` otherwise.

    Recognised shapes:
      - bare ``ast.Constant`` — Python's truthiness on the literal
        value (``False``/``None``/``0``/``""`` → False, everything
        else → True).
      - 1-op ``Compare`` whose both operands resolve via
        :func:`_static_scalar_value` (literals, ``USub``, named
        numeric bindings, and arithmetic ``BinOp`` chains over the
        same). The op is then applied directly on the folded scalars.

    Used by ``_process_if`` to (a) short-circuit the AND chain when
    any term evaluates to ``False`` (predicate unreachable, recurse
    into ``orelse`` only), and (b) silently skip ``True`` terms as
    no-op gates. Returning ``None`` for any other shape — including a
    constant-only Compare we couldn't actually fold (e.g. one whose
    operands escape :func:`_static_scalar_value`'s constant-folding
    scope) — sends the term through the unknown-conjunct path so the
    aggregator treats recognised siblings' hits as upper-bound only,
    rather than silently dropping the term as if it were a no-op.
    """
    if isinstance(node, ast.Constant):
        try:
            return bool(node.value)
        except Exception:  # noqa: BLE001  # pragma: no cover — defensive: bool() on a Constant value cannot raise
            return None
    if not isinstance(node, ast.Compare):
        return None
    if (
        len(node.ops) != 1 or len(node.comparators) != 1
    ):  # pragma: no cover — chained-compare shape (e.g. ``0 < x < 1``) declined
        return None
    left_val = _static_scalar_value(node.left, name_periods)
    right_val = _static_scalar_value(node.comparators[0], name_periods)
    if left_val is None or right_val is None:
        return None
    op_fn = _STATIC_CMP_OPS.get(type(node.ops[0]))
    if (
        op_fn is None
    ):  # pragma: no cover — non-arithmetic comparator (Is/IsNot/In/NotIn) on static scalars rare in generated strategies
        return None
    try:
        return bool(op_fn(left_val, right_val))
    except Exception:  # noqa: BLE001  # pragma: no cover — defensive: comparison on validated scalars cannot raise
        return None


def _find_strategy_class(tree: ast.AST, on_bar: ast.AST) -> Optional[ast.ClassDef]:
    """Return the ``ClassDef`` that lexically contains ``on_bar``, if any.

    Used by :func:`_collect_name_periods` to skip unrelated helper
    classes when collecting attribute / class-variable bindings. Without
    this, ``Helper.PERIOD = 2`` declared before ``class Strategy:
    PERIOD = 20`` would seed ``setdefault("PERIOD", 2)`` and Strategy's
    own constant would never bind — flipping zero-hit / NaN-window
    diagnostics into ``COVERAGE_OK`` or vice versa for valid
    multi-class strategy code.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in ast.walk(node):
            if child is on_bar:
                return node
    return None


def _constructor_param_defaults(func: ast.AST) -> Dict[str, ast.Constant]:
    """Map ``__init__``'s parameter names to their default ``Constant``.

    Strategies generated by the ideation pipeline routinely guard
    ``__init__`` blocks with a default-true parameter, e.g.::

        def __init__(self, enabled=True):
            if enabled:
                self.TARGET = "AAPL"

    The default-construction path unconditionally takes the
    ``enabled``-True branch, so the assignment is guaranteed for every
    real strategy invocation. Skipping it (because the predicate isn't
    a literal ``Constant``) drops the symbol gate and lets the
    indicator condition silently evaluate against every fetched
    DataFrame. By looking up the parameter's default in this table,
    :func:`_iter_unconditional_constructor_assigns` can resolve the
    guard the same way it resolves a literal ``if True:``.

    Only parameters with a *constant* default are recorded — anything
    else (a call, a name, a complex expression) stays opaque and the
    guard remains conservatively skipped.
    """
    defaults: Dict[str, ast.Constant] = {}
    args = getattr(func, "args", None)
    if args is None:  # pragma: no cover — defensive: every ast.FunctionDef has an args attribute
        return defaults
    posargs = list(getattr(args, "args", []) or [])
    pos_defaults = list(getattr(args, "defaults", []) or [])
    # ``args.defaults`` aligns with the trailing positional args.
    offset = len(posargs) - len(pos_defaults)
    for idx, param in enumerate(posargs):
        if idx < offset:
            continue
        default = pos_defaults[idx - offset]
        if isinstance(default, ast.Constant):
            defaults[param.arg] = default
    kwonly = list(getattr(args, "kwonlyargs", []) or [])
    kw_defaults = list(getattr(args, "kw_defaults", []) or [])
    for param, default in zip(
        kwonly, kw_defaults
    ):  # pragma: no cover — kwonly defaults rare in generated strategy __init__
        if isinstance(default, ast.Constant):
            defaults[param.arg] = default
    return defaults


def _iter_unconditional_constructor_assigns(
    stmts: List[ast.stmt],
    param_defaults: Optional[Dict[str, ast.Constant]] = None,
) -> List[Union[ast.Assign, ast.AnnAssign]]:
    """Yield ``Assign`` / ``AnnAssign`` nodes guaranteed to execute on
    every constructor invocation.

    A blanket ``ast.walk(child)`` over ``__init__`` records nested
    assignments unconditionally — including dead branches like
    ``if False: self.TARGET = "MSFT"`` — and those overwrite the
    class attribute with a value the runtime never sets. A blanket
    "top-level statements only" rule is too conservative the other
    way: ``if True: self.TARGET = "AAPL"`` IS unconditionally
    executed, and skipping it lets the probe lose a real symbol
    gate.

    This walker descends into branches that are statically guaranteed
    to run while still skipping branches whose predicate isn't a
    constant we can resolve:

    - ``Assign`` / ``AnnAssign`` at the current level → yield
    - ``if <Constant>: body else: orelse`` → yield from the branch
      Python's truthiness on ``Constant.value`` selects (the other is
      dead code at runtime)
    - ``if <Name>:`` where ``Name`` is a constructor parameter with a
      ``Constant`` default → resolve via ``param_defaults`` and yield
      from the live branch. Strategies routinely use this shape
      (``def __init__(self, enabled=True): if enabled: ...``); the
      default-construction path is guaranteed.
    - ``if <unknown>: ...`` → skip both branches conservatively
    - ``with <ctx>: body`` / ``async with`` → yield from ``body``
      (the context manager unconditionally executes the body unless
      ``__enter__`` raises, which we treat as a runtime error path
      not relevant to static binding)
    - ``for`` / ``while`` / ``try`` / nested function defs → skip
      conservatively (``for`` may iterate zero times; ``try``'s body
      may be interrupted; nested defs aren't constructor logic)
    """
    param_defaults = param_defaults or {}
    out: List[Union[ast.Assign, ast.AnnAssign]] = []
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            out.append(stmt)
        elif (
            isinstance(stmt, ast.AnnAssign) and stmt.value is not None
        ):  # pragma: no cover — annotated __init__ assignment shape rare in generated strategies
            out.append(stmt)
        elif isinstance(stmt, ast.If):
            resolved = _resolve_constant_predicate(stmt.test, param_defaults)
            if resolved is not None:
                # Literal-or-default-resolved predicate — only the live
                # branch contributes.
                branch = stmt.body if resolved else stmt.orelse
                out.extend(_iter_unconditional_constructor_assigns(branch, param_defaults))
            # Unknown predicate — skip both branches; the class-body
            # binding (already recorded) acts as the runtime fallback.
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            out.extend(_iter_unconditional_constructor_assigns(stmt.body, param_defaults))
        # For / While / Try / etc.: conservatively skip — execution
        # isn't statically guaranteed.
    return out


def _resolve_constant_predicate(
    test: ast.expr, param_defaults: Dict[str, ast.Constant]
) -> Optional[bool]:
    """Resolve a constructor ``if`` predicate to a static bool, if possible.

    Returns:
      - ``True`` / ``False`` if the predicate is a ``Constant`` literal,
        a parameter ``Name`` whose default is a ``Constant``, or a
        ``UnaryOp(Not, ...)`` over either of the above.
      - ``None`` if the predicate can't be resolved statically.

    Resolving ``Not`` is cheap and covers the symmetric guard shape
    ``if not enabled: self.TARGET = "..."`` strategies sometimes use.
    """
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.Name):
        default = param_defaults.get(test.id)
        if default is not None:
            return bool(default.value)
        return None  # pragma: no cover — unbound-name constructor predicate rare
    if isinstance(test, ast.UnaryOp) and isinstance(
        test.op, ast.Not
    ):  # pragma: no cover — ``if not enabled:`` constructor predicate rare in generated strategies
        inner = _resolve_constant_predicate(test.operand, param_defaults)
        if inner is None:
            return None
        return not inner
    return None


@dataclass
class _NameStrings:
    """Two-namespace string-constant table for symbol-gate resolution.

    Python's name resolution treats class-body bare names as class
    attributes — they are NOT in lexical scope for methods. So a
    module-level ``TARGET = "X"`` and a class-body ``TARGET = "Y"``
    resolve to different values inside ``on_bar``: bare ``TARGET``
    sees ``"X"`` (the module/global binding), while ``self.TARGET``
    sees ``"Y"`` (the class attribute, with instance dict shadowing
    via ``__init__`` taking precedence). The probe needs separate
    dicts so a single ``_collect_name_strings`` call can serve both
    lookup paths without one overwriting the other.

    - ``globals_`` — bare-``Name`` lookups: module-level ``Name``
      targets (``setdefault`` so cross-scope module constants stay
      isolated) plus function-local ``Name`` targets from inside
      ``on_bar`` (overwrite, applied flow-sensitively in
      :func:`_apply_assign_inplace`).
    - ``attrs`` — ``self.X`` / ``cls.X`` lookups: class-body
      ``Name`` targets (overwrite, source order — last wins) plus
      class ``__init__`` / ``__post_init__`` ``self.X = "..."``
      assignments (overwrite). Module-level bare names do **not**
      contribute here because ``self.X`` doesn't fall through to
      module scope at runtime.
    """

    globals_: Dict[str, str] = _field(default_factory=dict)
    attrs: Dict[str, str] = _field(default_factory=dict)

    def copy(self) -> "_NameStrings":
        return _NameStrings(globals_=dict(self.globals_), attrs=dict(self.attrs))

    def restore_from(self, other: "_NameStrings") -> None:
        """In-place reset to ``other``'s contents — used by the
        flow-sensitive walker's transactional snapshot/restore.
        """
        self.globals_.clear()
        self.globals_.update(other.globals_)
        self.attrs.clear()
        self.attrs.update(other.attrs)


def _resolve_string_in_method(value: ast.expr, name_strings: "_NameStrings") -> Optional[str]:
    """Resolve an assignment RHS to a string from inside a method body.

    Used by :func:`_apply_assign_inplace` so flow-sensitive
    function-local writes can honour aliases like ``target = OTHER``
    or ``self.TARGET = SOME_NAME`` (where ``SOME_NAME`` is a module
    constant). Method-scope bare-``Name`` references resolve through
    the module/global dict only — Python's class body is not in scope
    for methods. ``self.X`` / ``cls.X`` resolves through ``attrs``.
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(
        value, ast.Name
    ):  # pragma: no cover — bare-name string alias in method body rare in generated strategies
        return name_strings.globals_.get(value.id)
    if (  # pragma: no cover — self/cls string alias in method body rare in generated strategies
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id in {"self", "cls"}
    ):
        return name_strings.attrs.get(value.attr)
    return None


class _BindingRecorder(Protocol):
    """Protocol for type-specific constant recorders.

    Preconditions: ``target`` is an ``ast.expr`` from an ``Assign`` or
    ``AnnAssign`` node; ``value`` is the corresponding RHS expression.
    Postconditions: the recorder's internal accumulator reflects the
    binding, or the call is a no-op when the RHS cannot be resolved.
    """

    def record_module(self, target: ast.expr, value: ast.expr) -> None: ...
    def record_class_body(self, target: ast.expr, value: ast.expr) -> None: ...
    def record_constructor(self, target: ast.expr, value: ast.expr) -> None: ...


class _StringRecorder:
    """Collects ``NAME = "<string>"`` bindings into a :class:`_NameStrings`.

    Invariants:
    - ``result.globals_`` holds bare-``Name`` module-scope bindings.
    - ``result.attrs`` holds class-body and constructor ``self.X`` bindings.
    - The two namespaces never cross-pollute.
    """

    __slots__ = ("result",)

    def __init__(self) -> None:
        self.result = _NameStrings()

    def _resolve(self, value: ast.expr, *, in_method: bool) -> Optional[str]:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            if (
                in_method
            ):  # pragma: no cover — method-body bare-name string alias rare in generated strategies
                return self.result.globals_.get(value.id)
            cls_local = self.result.attrs.get(value.id)
            if (
                cls_local is not None
            ):  # pragma: no cover — class-local bare-name alias resolution rare
                return cls_local
            return self.result.globals_.get(value.id)
        if (  # pragma: no cover — self/cls string alias rare in generated strategies
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id in {"self", "cls"}
        ):
            return self.result.attrs.get(value.attr)
        return None

    def record_module(self, target: ast.expr, value: ast.expr) -> None:
        resolved = self._resolve(value, in_method=True)
        if resolved is None:
            return
        if isinstance(target, ast.Name):
            self.result.globals_.setdefault(target.id, resolved)

    def record_class_body(self, target: ast.expr, value: ast.expr) -> None:
        resolved = self._resolve(value, in_method=False)
        if resolved is None:
            return
        if isinstance(target, ast.Name):
            self.result.attrs[target.id] = resolved
        elif isinstance(target, ast.Attribute):
            self.result.attrs[target.attr] = resolved

    def record_constructor(self, target: ast.expr, value: ast.expr) -> None:
        resolved = self._resolve(value, in_method=True)
        if resolved is None:
            return
        if isinstance(target, ast.Attribute):
            self.result.attrs[target.attr] = resolved


class _PeriodRecorder:
    """Collects ``NAME = <numeric>`` bindings into a flat dict.

    Invariants:
    - Keys are bare names or attribute names (never dotted paths).
    - Values are ``int`` when the literal is integer-valued, else ``float``.
    """

    __slots__ = ("result",)

    def __init__(self) -> None:
        self.result: Dict[str, Union[int, float]] = {}

    def _record(self, target: ast.expr, value: ast.expr, *, overwrite: bool) -> None:
        v = _numeric_literal(value, self.result)
        if v is None:
            return
        ivalue: Union[int, float] = int(v) if float(v).is_integer() else float(v)
        if isinstance(target, ast.Name):
            if overwrite:
                self.result[target.id] = ivalue
            else:
                self.result.setdefault(target.id, ivalue)
        elif isinstance(target, ast.Attribute):
            if overwrite:
                self.result[target.attr] = ivalue
            else:  # pragma: no cover — non-overwrite Attribute target rare in current corpus
                self.result.setdefault(target.attr, ivalue)

    def record_module(self, target: ast.expr, value: ast.expr) -> None:
        self._record(target, value, overwrite=False)

    def record_class_body(self, target: ast.expr, value: ast.expr) -> None:
        self._record(target, value, overwrite=True)

    def record_constructor(self, target: ast.expr, value: ast.expr) -> None:
        self._record(target, value, overwrite=True)


def _collect_name_bindings(
    tree: ast.AST,
    recorder: _BindingRecorder,
    *,
    strategy_class: Optional[ast.ClassDef] = None,
) -> None:
    """Walk module → class → constructor collecting name bindings via *recorder*.

    Preconditions:
    - ``tree`` is a parsed ``ast.Module`` (or rooted subtree).
    - ``recorder`` implements the :class:`_BindingRecorder` protocol.
    Postconditions:
    - ``recorder``'s internal accumulator contains all statically-resolvable
      constant bindings from the guaranteed-execution paths of ``tree``.
    """
    _CONSTRUCTOR_NAMES = {"__init__", "__post_init__"}

    def _dispatch(node: Union[ast.Assign, ast.AnnAssign], hook: str) -> None:
        record_fn = getattr(recorder, hook)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                record_fn(t, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            record_fn(node.target, node.value)

    def _walk(node: ast.AST) -> None:
        if strategy_class is not None and isinstance(node, ast.ClassDef):
            if node is not strategy_class:
                return
            class_param_defaults: Dict[str, ast.Constant] = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in _CONSTRUCTOR_NAMES:
                        param_defaults = _constructor_param_defaults(child)
                        for sub in _iter_unconditional_constructor_assigns(
                            child.body, param_defaults
                        ):
                            _dispatch(sub, "record_constructor")
                    continue
                for sub in _iter_unconditional_constructor_assigns([child], class_param_defaults):
                    _dispatch(sub, "record_class_body")
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            _dispatch(node, "record_module")
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)


def _collect_name_strings(
    tree: ast.AST,
    strategy_class: Optional[ast.ClassDef] = None,
) -> _NameStrings:
    """Bind ``NAME = "<string>"`` for string-constant resolution.

    Delegates to :func:`_collect_name_bindings` with a :class:`_StringRecorder`.

    Preconditions: ``tree`` is a parsed ``ast.Module``.
    Postconditions: returns a :class:`_NameStrings` with ``globals_``
    (bare-name lookups) and ``attrs`` (``self.X`` / ``cls.X`` lookups).
    """
    recorder = _StringRecorder()
    _collect_name_bindings(tree, recorder, strategy_class=strategy_class)
    return recorder.result


def _collect_name_periods(
    tree: ast.AST,
    function_node: Optional[ast.AST] = None,
    strategy_class: Optional[ast.ClassDef] = None,
) -> Dict[str, Union[int, float]]:
    """Bind ``NAME = <int>`` for later ``Name`` / ``self.NAME`` resolution.

    Delegates to :func:`_collect_name_bindings` with a :class:`_PeriodRecorder`.

    Preconditions: ``tree`` is a parsed ``ast.Module``.
    Postconditions: returns a flat dict mapping bare names and attribute
    names to their resolved numeric values.
    """
    recorder = _PeriodRecorder()
    _collect_name_bindings(tree, recorder, strategy_class=strategy_class)
    return recorder.result


def _build_subcond(
    node: ast.Compare,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Leg]:
    # Only support simple a <op> b shape — chained comparisons are rare in
    # generated strategies and ambiguous for hit-rate semantics.
    if (
        len(node.ops) != 1 or len(node.comparators) != 1
    ):  # pragma: no cover — chained comparison declined; rare in generated strategies
        return None
    op = type(node.ops[0])
    op_fn = _CMP_OPS.get(op)
    if (
        op_fn is None
    ):  # pragma: no cover — non-arithmetic Compare op (Is/IsNot/In/NotIn) declined for hit-rate semantics
        return None

    left = _build_operand(node.left, name_periods, name_evaluators)
    right = _build_operand(node.comparators[0], name_periods, name_evaluators)
    if left is None or right is None:
        return None
    if not (left.data_dependent or right.data_dependent):
        return None

    label = _format_label(node)
    l_fn = left.fn
    r_fn = right.fn

    def _eval(df: pd.DataFrame) -> pd.Series:
        return op_fn(l_fn(df), r_fn(df))

    return Leg(label=label, inner=MaskLeaf(label=label, evaluator=_eval))


def _build_truthy_subcond(
    node: ast.expr,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Leg]:
    """Build a coverage subcond for a truthiness term like ``bool(x)`` or ``x``.

    Recognised shapes:

    - ``bool(<Compare>)`` — delegates to :func:`_build_subcond` so e.g.
      ``bool(close > 100)`` produces the same row as ``close > 100``.
    - ``bool(<Name>)`` and bare ``<Name>`` — resolves the name to a
      previously-bound indicator evaluator (see
      :func:`_collect_name_evaluators`) and treats the resulting series
      as truthy where it is non-NaN and non-zero.

    Returns ``None`` when the inner expression is neither a recognised
    comparison nor a Name with an indicator binding — in particular the
    factor-tree codegen pattern ``_entry = self._n_X(bars)`` falls in
    this bucket because ``self._n_X(...)`` is not a recognised helper,
    so those strategies still surface as ``UNKNOWN_LOW_COVERAGE`` rather
    than being silently treated as always-true.
    """
    inner = node
    if (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "bool"
        and len(inner.args) == 1
        and not inner.keywords
    ):
        inner = inner.args[0]

    if isinstance(inner, ast.Compare):
        return _build_subcond(inner, name_periods, name_evaluators)

    if not isinstance(inner, ast.Name) or name_evaluators is None:
        return None

    evaluator = name_evaluators.get(inner.id)
    if evaluator is None:
        return None

    try:
        label = ast.unparse(node).strip()
    except Exception:  # noqa: BLE001  # pragma: no cover — defensive: ast.unparse on a valid AST node cannot raise
        label = inner.id
    if (
        len(label) > _MAX_LABEL_LEN
    ):  # pragma: no cover — label-truncation branch rare for truthy subcond names
        label = label[: _MAX_LABEL_LEN - 1] + "…"

    def _eval(df: pd.DataFrame) -> pd.Series:
        s = evaluator(df)
        return s.fillna(0).astype(bool)

    return Leg(label=label, inner=MaskLeaf(label=label, evaluator=_eval))


def _build_compound_subcond(
    node: ast.BoolOp,
    name_periods: Dict[str, int],
    ops: _CombinatorOps,
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
    name_strings: Optional["_NameStrings"] = None,
    bar_name: str = "bar",
) -> Optional[Leg]:
    """Build a single :class:`Leg` wrapping a compound AND or OR sub-tree.

    Parameterised by *ops* (``_AND_OPS`` or ``_OR_OPS``).

    **AND mode** (``_AND_OPS``): produces ``Leg(label, AndOp(legs=...))``
    (optionally wrapped in a :class:`SymbolGate` when intra-leg symbol
    gates apply). Returns ``None`` when any inner conjunct is
    un-modellable — the AND-of-known-conjuncts would be a superset of
    the actual mask, which is too permissive.

    **OR mode** (``_OR_OPS``): produces ``Leg(label, OrOp(legs=...,
    unknown=...))``, optionally wrapped in a :class:`SymbolGate` when
    every alternative carries its own symbol gate (the OR can fire on
    the union of those symbols). Tracks ``OrOp.unknown=True`` when a
    leg is un-modellable (rather than aborting) so the parent AND group
    can suppress false-positive blockers.
    """
    inner: List[Leg] = []
    intra_and_symbols: Optional[set] = None
    has_unknown_leg = False

    terms = _flatten_top_terms(node) if not ops.expose_or_legs else node.values

    for term in terms:
        if isinstance(term, ast.Compare):
            sym = _symbol_gate(term, name_strings, bar_name)
            if sym is not None:
                if ops.expose_or_legs:
                    inner.append(
                        Leg(
                            label=_format_label(term),
                            inner=SymbolGate(syms=frozenset(sym), inner=Static(True)),
                        )
                    )
                else:
                    if intra_and_symbols is None:
                        intra_and_symbols = set(sym)
                    else:  # pragma: no cover — multiple symbol gates inside one AND leg rare
                        intra_and_symbols &= sym
                continue
            sub = _build_subcond(term, name_periods, name_evaluators)
        elif ops.expose_or_legs and isinstance(term, ast.BoolOp) and isinstance(term.op, ast.And):
            sub = _build_compound_subcond(
                term, name_periods, _AND_OPS, name_evaluators, name_strings, bar_name
            )
        else:
            sub = _build_truthy_subcond(term, name_periods, name_evaluators)

        if sub is not None:
            inner.append(sub)
        elif ops.on_unknown_term == "abort":
            return None
        else:
            has_unknown_leg = True

    if not ops.expose_or_legs:
        and_gate = frozenset(intra_and_symbols) if intra_and_symbols is not None else None
        if (
            and_gate is not None and not and_gate
        ):  # pragma: no cover — empty intra-leg symbol-intersection unreachable
            return None
    else:
        and_gate = None  # OR mode collects per-leg gates below

    if not inner:  # pragma: no cover — fully-unmodellable term list declines in current corpus
        return None

    if not ops.expose_or_legs:
        # AND mode collapses to the single inner leg when only one
        # conjunct survived and no symbol gate applies — avoids a
        # redundant ``AndOp(legs=(only_leg,))`` wrapper.
        if len(inner) == 1 and and_gate is None:
            return inner[0]
    else:
        if (
            len(inner) == 1 and not has_unknown_leg
        ):  # pragma: no cover — single-recognised-leg OR rare
            return inner[0]

    label = _format_compound_label(node)

    if not ops.expose_or_legs:
        and_node: BarPredicate = AndOp(legs=tuple(inner), unknown=False)
        if and_gate is not None:
            and_node = SymbolGate(syms=and_gate, inner=and_node)
        return Leg(label=label, inner=and_node)

    # OR mode: compute the outer gate as the union of per-leg gates
    # when *every* leg carries one. The aggregator uses this for
    # propagating the OR's effective symbol scope to sibling AND
    # conjuncts at the GROUP level.
    leg_gates = [_leg_gate_symbols(lg) for lg in inner]
    if leg_gates and all(g is not None for g in leg_gates):
        union: frozenset = frozenset()
        for g in leg_gates:
            assert g is not None
            union = union | g
        outer_or_gate: Optional[frozenset] = union if union else None
    else:
        outer_or_gate = None

    or_node: BarPredicate = OrOp(legs=tuple(inner), unknown=has_unknown_leg)
    if outer_or_gate is not None:
        or_node = SymbolGate(syms=outer_or_gate, inner=or_node)
    return Leg(label=label, inner=or_node)


def _format_compound_label(node: ast.expr) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001  # pragma: no cover — defensive: ast.unparse on a valid AST node cannot raise
        text = "<compound>"
    text = text.strip()
    if (
        len(text) > _MAX_LABEL_LEN
    ):  # pragma: no cover — compound-label truncation branch rare in current corpus
        text = text[: _MAX_LABEL_LEN - 1] + "…"
    return text


def _format_label(node: ast.Compare) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001  # pragma: no cover — defensive: ast.unparse on a valid AST node cannot raise
        text = "<expr>"
    text = text.strip()
    if (
        len(text) > _MAX_LABEL_LEN
    ):  # pragma: no cover — single-expression-label truncation rare in current corpus
        text = text[: _MAX_LABEL_LEN - 1] + "…"
    return text


def _build_operand(
    node: ast.expr,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[_Operand]:
    """Compile an AST sub-expression into a ``df -> Series`` callable.

    Returns ``None`` for expressions whose evaluation we can't faithfully
    model (e.g. function calls into user code, attribute chains we don't
    recognise). Such subconditions are silently dropped.
    """
    # Resolve a Name to a previously-bound indicator-call evaluator
    # (e.g. ``sma_var = sma(close, 200)`` then ``if x > sma_var``).
    # This must be checked BEFORE :func:`_column_from` so a local
    # assignment that intentionally shadows an OHLCV name takes
    # precedence over the bare-column shortcut. Without this, a
    # strategy like ``close = sma(open, 2); if close > 100:`` would
    # have its predicate evaluated against the raw ``close`` column at
    # probe time even though the runtime compares the SMA value, and
    # the report could falsely flip to ``COVERAGE_OK`` /
    # ``INDICATOR_FILTER_TOO_RESTRICTIVE`` based on the wrong series.
    if isinstance(node, ast.Name) and name_evaluators is not None:
        evaluator = name_evaluators.get(node.id)
        if evaluator is not None:
            return _Operand(fn=evaluator, data_dependent=True)

    column = _column_from(node)
    if column is not None:

        def _col(df: pd.DataFrame, c: str = column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(float("nan"), index=df.index)

        return _Operand(fn=_col, data_dependent=True)

    literal = _numeric_literal(node, name_periods)
    if literal is not None:
        return _Operand(
            fn=lambda df, v=literal: pd.Series(v, index=df.index, dtype=float),
            data_dependent=False,
        )

    indicator_fn = _indicator_call(node, name_periods, name_evaluators)
    if indicator_fn is not None:
        return _Operand(fn=indicator_fn, data_dependent=True)

    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left = _build_operand(node.left, name_periods, name_evaluators)
        right = _build_operand(node.right, name_periods, name_evaluators)
        if left is not None and right is not None:
            l_fn, r_fn = left.fn, right.fn
            if isinstance(node.op, ast.Mult):

                def combined(df: pd.DataFrame) -> pd.Series:
                    return l_fn(df) * r_fn(df)
            elif isinstance(node.op, ast.Add):

                def combined(df: pd.DataFrame) -> pd.Series:
                    return l_fn(df) + r_fn(df)
            else:

                def combined(df: pd.DataFrame) -> pd.Series:
                    return l_fn(df) - r_fn(df)

            return _Operand(
                fn=combined,
                data_dependent=left.data_dependent or right.data_dependent,
            )

    return None


def _column_from(node: ast.expr) -> Optional[str]:
    """Resolve a node to an OHLCV column name, if possible.

    Strategy attributes such as ``self.close`` (a stored threshold)
    must NOT be misread as the market ``close`` column. The Attribute
    branch therefore excludes owners ``self`` / ``cls``: they belong
    to instance/class state and resolve via ``_numeric_literal``'s
    ``self.X`` / ``cls.X`` path (or are dropped). Bar attributes
    (``bar.close`` / ``candle.close`` / ``b.close``) and any other
    non-instance owner remain valid column accesses.
    """
    if isinstance(node, ast.Name) and node.id in _OHLCV_COLUMNS:
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and node.attr in _OHLCV_COLUMNS
        and not (isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"})
    ):
        return node.attr
    if isinstance(node, ast.Subscript):
        slc = node.slice
        if isinstance(slc, ast.Constant) and isinstance(
            slc.value, str
        ):  # pragma: no cover — ``df["close"]`` subscript shape rare in generated strategies
            if slc.value in _OHLCV_COLUMNS:
                return slc.value
    # ``[b.volume for b in history]`` — strategies routinely pass a
    # history comprehension into a single-series helper. Recognise the
    # element's OHLCV attribute when the comprehension target name
    # matches the element's value (i.e. ``b`` in both places); we don't
    # need to validate ``history`` itself.
    if isinstance(node, ast.ListComp) and len(node.generators) == 1:
        elt = node.elt
        target = node.generators[0].target
        if (
            isinstance(elt, ast.Attribute)
            and elt.attr in _OHLCV_COLUMNS
            and isinstance(elt.value, ast.Name)
            and isinstance(target, ast.Name)
            and elt.value.id == target.id
        ):
            return elt.attr
    return None


def _numeric_literal(node: ast.expr, name_periods: Dict[str, int]) -> Optional[float]:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric_literal(node.operand, name_periods)
        if inner is not None:
            return -inner
    if isinstance(node, ast.Name):
        # Bare OHLCV column names (``close``, ``open``, ...) are
        # data-dependent column references and must NOT be resolved
        # as static numeric literals — even when ``self.close = 100``
        # has happened to record ``name_periods["close"] = 100`` for
        # the matching ``self.close`` Attribute lookup. ``_build_operand``
        # already takes the column path for these Names; the static
        # evaluator must agree, otherwise a predicate like
        # ``close > self.close`` folds to ``100 > 100 = False`` and
        # the AND short-circuit drops a real data-dependent comparison.
        if node.id in _OHLCV_COLUMNS:
            return None
        period = name_periods.get(node.id)
        if period is not None:
            return float(period)
    # ``self.WINDOW`` / ``cls.WINDOW`` — strategies routinely pass class
    # tuning knobs to indicator helpers. Record the attr name in
    # _collect_name_periods so this lookup matches.
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"self", "cls"}
    ):
        period = name_periods.get(node.attr)
        if period is not None:
            return float(period)
    return None


def _indicator_call(
    node: ast.expr,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Resolve an AST call (or ``Subscript[Call, idx]``) to a per-bar evaluator.

    Tuple-returning helpers are only recognised inside a ``Subscript``
    with a constant non-negative int slice — bare calls are ambiguous
    because the user hasn't picked which leg to compare against. The
    inverse holds for single-Series helpers: subscripting them is a
    user error we don't model. Returns ``None`` on any unresolvable
    input so the caller drops the comparison instead of silently
    substituting the helper's default (which would describe coverage
    of a different indicator from the runtime).
    """
    if isinstance(node, ast.Subscript):
        if not isinstance(
            node.value, ast.Call
        ):  # pragma: no cover — non-call subscript value declined for tuple-indicator dispatch
            return None
        slc = node.slice
        if not (
            isinstance(slc, ast.Constant)
            and isinstance(slc.value, int)
            and not isinstance(slc.value, bool)
        ):  # pragma: no cover — non-int subscript on tuple-indicator declined
            return None
        call = node.value
        idx: Optional[int] = slc.value
    elif isinstance(node, ast.Call):
        call = node
        idx = None
    else:
        return None

    func_name = _func_name(call.func)
    if func_name is None:  # pragma: no cover — non-Name/non-Attribute call expression declined
        return None
    spec = INDICATORS.get(func_name)
    if spec is None:
        return None

    is_tuple_call = idx is not None
    if is_tuple_call != (
        spec.tuple_arity is not None
    ):  # pragma: no cover — single-vs-tuple mismatch (e.g. sma()[0]) declined
        # ``sma(close, 20)[0]`` (single-Series subscripted) and
        # ``macd(close, 12, 26, 9)`` (tuple bare-called) are both
        # rejected — we'd be guessing the user's intent.
        return None
    if is_tuple_call and not (
        0 <= idx < spec.tuple_arity
    ):  # pragma: no cover — out-of-range tuple subscript declined
        return None

    resolved_inputs: List[Callable[[pd.DataFrame], pd.Series]] = []
    for slot_idx, kind in enumerate(spec.data_inputs):
        if kind == "series":
            resolved = _resolve_series_input(call, name_evaluators)
        else:
            resolved = _positional_series_input(call, slot_idx, kind, name_evaluators)
        if resolved is None:
            # Explicit but un-modellable input (e.g. ``atr(low, low,
            # close, 14)`` — second arg is ``low`` but we can't model
            # synthesised series). Decline rather than substitute the
            # default OHLCV column.
            return None
        resolved_inputs.append(resolved)

    extra_pos = _trailing_numeric_args(call, name_periods, start_index=len(spec.data_inputs))
    if extra_pos is None:
        # Strategy passed an explicit trailing positional config the
        # probe can't reduce to a literal (e.g. ``sma(close, PERIOD +
        # 1)`` or ``macd(close, dynamic_window)``). Drop rather than
        # silently use the helper's default.
        return None
    extra_kwargs = _resolve_known_kwargs(call, name_periods, spec.kwarg_names)
    if extra_kwargs is None:
        # Same guard for unresolvable known kwargs, e.g.
        # ``bollinger_bands(close, 20, num_std=self.band_width)``.
        return None
    if not _validate_scalar_args(spec, extra_pos, extra_kwargs):
        # An explicit but invalid scalar like ``sma(close, 0)`` or
        # ``sma(close, 2.5)`` would TypeError inside pandas. Decline
        # the indicator so the predicate becomes UNMODELABLE rather
        # than letting the helper raise (which the aggregator turns
        # into an all-False mask and the report misclassifies as
        # ``INDICATOR_FILTER_TOO_RESTRICTIVE`` — same bug the old
        # ``_resolve_period_arg`` ``> 0 and is_integer()`` check
        # protected against).
        return None

    helper = spec.helper
    inputs = tuple(resolved_inputs)
    if idx is None:

        def _eval(df: pd.DataFrame) -> pd.Series:
            return helper(*(fn(df) for fn in inputs), *extra_pos, **extra_kwargs)

    else:

        def _eval(df: pd.DataFrame) -> pd.Series:
            return helper(*(fn(df) for fn in inputs), *extra_pos, **extra_kwargs)[idx]

    return _eval


def _trailing_numeric_args(
    call: ast.Call,
    name_periods: Dict[str, int],
    *,
    start_index: int,
) -> Optional[List[Union[int, float]]]:
    """Collect positional numeric args from ``start_index`` onwards.

    Returns the resolved values when every positional arg from
    ``start_index`` to the end is a numeric literal we can interpret.
    Returns ``None`` when any positional arg in that range can't be
    resolved (e.g. ``macd(close, PERIOD + 1)`` — the user supplied an
    explicit value but the probe can't reduce it to a literal). The
    caller treats ``None`` as "decline this indicator" rather than
    substituting the helper's default, which would silently evaluate
    a different indicator from the runtime.

    An empty positional tail (``start_index >= len(call.args)``)
    returns ``[]`` — the user simply omitted these args and the
    helper's default applies.

    Trailing numeric args after the data inputs (``num_std`` /
    ``slow`` / ``signal`` / etc.) are preserved in source order and
    int-ness is preserved so helpers like ``rolling(window=N)`` get
    an int rather than a float.
    """
    out: List[Union[int, float]] = []
    for i in range(start_index, len(call.args)):
        v = _numeric_literal(call.args[i], name_periods)
        if v is None:
            return None
        out.append(int(v) if float(v).is_integer() else v)
    return out


def _resolve_known_kwargs(
    call: ast.Call,
    name_periods: Dict[str, int],
    known: tuple,
) -> Optional[Dict[str, Union[int, float]]]:
    """Pick out keyword arguments the helper actually accepts.

    Unknown kwargs are dropped — passing them through would TypeError
    inside the helper. Numeric values preserve int-ness for the same
    reason as :func:`_trailing_numeric_args`.

    Returns ``None`` when any **known** kwarg has a value the probe
    can't reduce to a numeric literal (e.g. ``bollinger_bands(close,
    20, num_std=self.band_width)`` where ``self.band_width`` isn't a
    constant). The caller treats ``None`` as "decline this indicator"
    rather than substituting the helper's default for the unresolved
    kwarg, which would silently evaluate a different indicator from
    the runtime. Unknown kwargs are still dropped without declining
    because the runtime would raise on them.
    """
    out: Dict[str, Union[int, float]] = {}
    for kw in call.keywords:
        if kw.arg not in known:
            continue
        v = _numeric_literal(kw.value, name_periods)
        if v is None:
            return None
        out[kw.arg] = int(v) if float(v).is_integer() else v
    return out


def _validate_scalar_args(
    spec,
    extra_pos: List[Union[int, float]],
    extra_kwargs: Dict[str, Union[int, float]],
) -> bool:
    """Reject zero / negative / non-integer scalars unless the helper
    declares the slot as float-allowed.

    Restores the ``> 0 and is_integer()`` check the old
    ``_resolve_period_arg`` performed before the registry refactor —
    without it ``sma(close, 0)`` and ``sma(close, 2.5)`` flow into the
    helper, pandas raises during evaluation, the aggregator forces an
    all-False mask, and the report misclassifies a runtime-config
    error as ``INDICATOR_FILTER_TOO_RESTRICTIVE``. Declining here
    drops the indicator instead, so the predicate is removed from the
    recognised set and the report falls through to
    ``UNKNOWN_LOW_COVERAGE``.

    ``spec.float_kwargs`` opts specific slots out of the integer
    requirement (currently only ``bollinger_bands.num_std``). Every
    other scalar must be a positive integer; positional args at
    indexes ``len(spec.kwarg_names) ..`` are treated as overflow and
    decline the call (the helper would TypeError on them anyway).
    """
    float_slots = spec.float_kwargs
    for i, value in enumerate(extra_pos):
        if i >= len(
            spec.kwarg_names
        ):  # pragma: no cover — overflow positional arg declined (helper would TypeError)
            return False
        slot_name = spec.kwarg_names[i]
        if not _is_valid_scalar(
            value, slot_name in float_slots
        ):  # pragma: no cover — invalid positional scalar declined
            return False
    for name, value in extra_kwargs.items():
        if not _is_valid_scalar(
            value, name in float_slots
        ):  # pragma: no cover — invalid kwarg scalar declined
            return False
    return True


def _is_valid_scalar(value: Union[int, float], allow_float: bool) -> bool:
    if value is None:  # pragma: no cover — _trailing_numeric_args already filters None
        return False
    try:
        v = float(value)
    except (
        TypeError,
        ValueError,
    ):  # pragma: no cover — _numeric_literal already returns float; float(float) cannot raise
        return False
    if v <= 0:
        return False
    if allow_float:
        return True
    return v.is_integer()


def _collect_name_evaluators(  # pragma: no cover
    on_bar: ast.AST, name_periods: Dict[str, int]
) -> Dict[str, Callable[[pd.DataFrame], pd.Series]]:
    """Bind local ``Name = <expr>`` assignments inside ``on_bar`` whose RHS
    resolves to a data-dependent operand.

    Legacy pre-pass kept for documentation / external import compatibility.
    The runtime walker uses ``_apply_assign_inplace`` flow-sensitively; this
    function is no longer reached from the live entry path and is pragma'd
    out of coverage as a deprecated AST walker helper.

    Walks ``on_bar``'s body for simple ``name = <expr>`` and
    ``name: T = <expr>`` assignments and compiles each RHS through the
    same ``_build_operand`` pipeline used for predicate operands. This
    covers two canonical generated-strategy shapes:

    1. Bare indicator call ::

           sma_var = sma(close, 200)
           if bar.close > sma_var:
               ...

    2. Derived threshold (binop with a literal) ::

           threshold = sma(close, 5) * 1.02
           if close > threshold:
               ...

    Without (2), a refactor that pulls the constant out of the
    comparison into a Name binding silently dropped the entire
    subcondition and the report degenerated to UNKNOWN_LOW_COVERAGE.

    Bindings are accumulated progressively so a chain like
    ``a = sma(close, 5); b = a * 1.02; if x > b:`` resolves the second
    binding through the first. Source order isn't formally guaranteed
    by ``ast.walk`` but is stable for the simple, top-level assignment
    chains generated strategies actually produce.
    """
    bindings: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}
    # Entry-path-only walk: skip exit branches of position-check ifs so
    # an exit reassignment like ``ma = sma(close, 200)`` can't shadow
    # the entry's ``ma = sma(close, 5)``.
    for node in _iter_entry_path_assigns(on_bar):
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue

        # Tuple / list unpacking targets — ``upper, mid, lower =
        # bollinger_bands(closes, 20)`` is the documented usage pattern
        # for the tuple-returning helpers, and binding each name to
        # its corresponding output series lets a downstream
        # ``if bar.close > upper:`` resolve normally.
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                _bind_tuple_unpack(target, value, name_periods, bindings)
                continue
            if isinstance(target, ast.Name):
                evaluator = _resolve_assign_evaluator(value, name_periods, bindings)
                if evaluator is not None:
                    # Overwrite — Python semantics use the latest
                    # assignment. A first-wins ``setdefault`` lets a
                    # stale earlier binding survive a reassignment in
                    # the same scope, which can mask zero-coverage
                    # filters when the second assignment is the one
                    # the entry test actually uses.
                    bindings[target.id] = evaluator
                else:
                    # Reassignment whose RHS doesn't resolve to a
                    # data-dependent evaluator (e.g. a scalar literal,
                    # an unsupported call). Drop any prior indicator
                    # binding so downstream lookups fall through to
                    # numeric-literal / OHLCV resolution. Without this,
                    # ``threshold = sma(close, 5); threshold = 150``
                    # leaves the stale SMA binding in place and the
                    # predicate ``close > threshold`` evaluates against
                    # the SMA instead of the literal 150.
                    bindings.pop(target.id, None)
    return bindings


def _bind_tuple_unpack(
    target: ast.expr,
    value: ast.expr,
    name_periods: Dict[str, int],
    bindings: Dict[str, Callable[[pd.DataFrame], pd.Series]],
) -> None:
    """Bind ``a, b, c = <tuple_indicator_call>`` element-wise.

    The tuple-returning helpers (``macd``, ``bollinger_bands``,
    ``stochastic``) emit one Series per element. Pre-existing code only
    recognised the ``[idx]`` subscript form (``bollinger_bands(close,
    20)[0]``); this also handles the unpacked-assignment form so the
    documented pattern ::

        upper, mid, lower = bollinger_bands(closes, 20)
        if bar.close > upper:
            ...

    no longer drops to UNKNOWN_LOW_COVERAGE.

    Always clears any prior indicator binding for each unpack target
    name first. Without that, a sequence like ::

        upper = sma(close, 2)
        upper, lower = self.custom_levels(bar)   # un-modellable RHS
        if close > upper:

    would leave ``upper`` bound to the SMA from the first assignment
    and the probe would evaluate the predicate against the wrong
    indicator. The drop-stale rule mirrors the Name-target path in
    ``_apply_assign_inplace``.
    """
    elements = list(getattr(target, "elts", []))
    # Drop stale bindings up front: every Name in the tuple/list target
    # is being reassigned, so any prior binding on those names is no
    # longer current. Subsequent recognition logic re-establishes
    # bindings on success; on any early-return path the names stay
    # cleared so downstream lookups fall through.
    for elem in elements:
        if isinstance(elem, ast.Name):
            bindings.pop(elem.id, None)
            name_periods.pop(elem.id, None)
    if not isinstance(
        value, ast.Call
    ):  # pragma: no cover — non-call tuple-unpack RHS already declined by _resolve_assign_evaluator
        return
    func_name = _func_name(value.func)
    spec = INDICATORS.get(func_name) if func_name else None
    if (
        spec is None or spec.tuple_arity is None
    ):  # pragma: no cover — non-tuple indicator on tuple-unpack target declined
        return
    if not elements:  # pragma: no cover — empty tuple target declined
        return
    if (
        len(elements) > spec.tuple_arity
    ):  # pragma: no cover — over-long unpack would TypeError at runtime
        # Unpacking would TypeError at runtime — don't bind anything.
        return

    extra_pos = _trailing_numeric_args(value, name_periods, start_index=len(spec.data_inputs))
    if (
        extra_pos is None
    ):  # pragma: no cover — unresolved positional config on tuple-unpack declined
        # Unpacked tuple-indicator with an unresolved positional config
        # (e.g. ``upper, _, _ = bollinger_bands(close, PERIOD + 1)``).
        # Don't bind anything — downstream lookups fall through and the
        # comparison gets dropped rather than evaluating against a
        # different indicator from the runtime.
        return
    extra_kwargs = _resolve_known_kwargs(value, name_periods, spec.kwarg_names)
    if extra_kwargs is None:  # pragma: no cover — unresolved known kwargs on tuple-unpack declined
        # Same guard for unresolvable known kwargs in the unpack form.
        return
    if not _validate_scalar_args(
        spec, extra_pos, extra_kwargs
    ):  # pragma: no cover — invalid scalar arg on tuple-unpack declined
        # ``upper, _, _ = bollinger_bands(close, 0)`` — same decline
        # rule as the indicator-call dispatcher; without it the bound
        # name would later evaluate to all-NaN and the comparison
        # would be misclassified as a zero-hit filter.
        return

    resolved_inputs: List[Callable[[pd.DataFrame], pd.Series]] = []
    for slot_idx, kind in enumerate(spec.data_inputs):
        if kind == "series":
            resolved = _resolve_series_input(value, bindings)
        else:  # pragma: no cover — HLC tuple-unpack rare in generated strategies
            # HLC slot for ``stochastic``: honour explicit positional
            # series args the same way ``_indicator_call`` does so
            # ``k, d = stochastic(low, low, close, 3)`` declines rather
            # than silently probing the default high/low/close columns.
            resolved = _positional_series_input(value, slot_idx, kind, bindings)
        if resolved is None:  # pragma: no cover — unresolved series input on tuple-unpack declined
            return
        resolved_inputs.append(resolved)

    helper = spec.helper
    inputs = tuple(resolved_inputs)
    for idx, elem in enumerate(elements):
        if not isinstance(elem, ast.Name):
            continue

        def _make(idx=idx, helper=helper, ins=inputs, ep=extra_pos, ek=extra_kwargs):
            def _eval(df: pd.DataFrame) -> pd.Series:
                return helper(*(fn(df) for fn in ins), *ep, **ek)[idx]

            return _eval

        bindings[elem.id] = _make()


def _resolve_assign_evaluator(
    value: ast.expr,
    name_periods: Dict[str, int],
    bindings: Dict[str, Callable[[pd.DataFrame], pd.Series]],
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Compile an assignment RHS into a ``df -> Series`` evaluator.

    Handles three flavours, in order:

    1. **Data-dependent operand expression** (indicator call, indicator
       BinOp, column reference) via :func:`_build_operand` — covers
       ``threshold = sma(close, 5) * 1.02``.
    2. **Cached comparison** (``_entry = close > sma(close, 5)``) — the
       boolean mask becomes the evaluator so a downstream ``bool(_entry)``
       in :func:`_build_truthy_subcond` resolves to the original
       comparison's coverage.
    3. **Cached truthy expression** (``_entry = bool(close > 0)``) —
       same as (2) after unwrapping the ``bool(...)``.
    """
    operand = _build_operand(value, name_periods, bindings)
    if operand is not None and operand.data_dependent:
        return operand.fn

    inner = value
    if (  # pragma: no cover — ``bool(...)`` wrapper on assignment RHS rare in generated strategies
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "bool"
        and len(inner.args) == 1
        and not inner.keywords
    ):
        inner = inner.args[0]

    if isinstance(inner, ast.Compare):
        sub = _build_subcond(inner, name_periods, bindings)
        if sub is not None and isinstance(sub.inner, MaskLeaf):
            return sub.inner.evaluator
    return None


def _func_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id.lower()
    if isinstance(
        func, ast.Attribute
    ):  # pragma: no cover — ``self.indicator(...)``-style call name extraction rare in generated strategies
        return func.attr.lower()
    return None


def _positional_series_input(
    call: ast.Call,
    positional_index: int,
    default_column: str,
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Resolve one positional series-input arg of an HLC / OHLCV helper.

    HLC helpers (``atr``, ``adx``) take ``(high, low, close, period)``
    and OHLCV helpers (``vwap``) take ``(high, low, close, volume)``.
    Each input slot defaults to the same-named column when omitted,
    but if the strategy supplied an explicit positional arg the probe
    must honour it — substituting the default would silently evaluate
    coverage against a different indicator than the runtime
    (``atr(low, low, close, 14)`` is meaningfully different from
    ``atr(high, low, close, 14)``).

    Returns a ``(df) -> Series`` callable when the slot resolves
    cleanly (omitted → default column; explicit OHLCV column or
    bound local series → that input). Returns ``None`` when the
    user supplied an explicit arg that can't be reduced to a known
    column or bound name; the caller declines the indicator.
    """
    if positional_index >= len(call.args):
        # Slot not supplied — use the default OHLCV column.
        def _default(df: pd.DataFrame, c: str = default_column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(
                float("nan"), index=df.index
            )  # pragma: no cover — defensive missing-column NaN fallback

        return _default

    arg = call.args[positional_index]
    column = _column_from(arg)
    if column is not None:

        def _from_column(df: pd.DataFrame, c: str = column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(
                float("nan"), index=df.index
            )  # pragma: no cover — defensive missing-column NaN fallback

        return _from_column

    if (
        isinstance(arg, ast.Name) and name_evaluators is not None
    ):  # pragma: no cover — bound-local positional HLC inputs rare in generated strategies
        evaluator = name_evaluators.get(arg.id)
        if evaluator is not None:

            def _from_binding(df: pd.DataFrame, ev=evaluator) -> pd.Series:
                return ev(df).astype(float)

            return _from_binding

    return None


def _resolve_series_input(
    call: ast.Call,
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Resolve the series-indicator call's input to a ``df -> Series`` callable.

    Resolution paths in order:

    1. **Positional or ``series=`` / ``data=`` kwarg** — recognise the
       same set of expression shapes (OHLCV column references and
       bound local names) regardless of how the strategy passed the
       input. Strategies routinely use the kwarg form
       (``sma(series=volume, period=20)``); without this, ``call.args``
       is empty and we'd fall through to the bare-call default
       (``close``), reporting a volume-based filter against prices.
    2. **OHLCV column reference** — ``close``, ``bar.volume``,
       ``df['close']``, or ``[b.X for b in history]`` — pinned via
       :func:`_column_from`.
    3. **Bound local Name** — when the strategy did
       ``closes = [b.close for b in history]`` (or any other shape that
       :func:`_collect_name_evaluators` already understood) and then
       passed the local into the indicator, look up the binding and use
       its callable directly.
    4. **Bare call** (``sma()``) — defaults to the close column. Rare
       in practice but harmless since no other column is implied.

    Returns ``None`` when an explicit argument can't be resolved by any
    of those paths — the caller then drops the indicator rather than
    silently substituting ``close``, which would mis-evaluate volume /
    OHLC filters and produce false ``COVERAGE_OK`` reports.
    """
    arg0: Optional[ast.expr] = None
    if call.args:
        arg0 = call.args[0]
    else:
        # Kwarg-only form: look for a recognised series keyword. Both
        # ``series=`` and ``data=`` are common in indicator helpers.
        for kw in call.keywords:
            if kw.arg in {"series", "data"}:
                arg0 = kw.value
                break

    if (
        arg0 is None
    ):  # pragma: no cover — bare ``sma()`` shape rare in generated strategies; default-close fallback unreached
        # Bare call ``sma()`` with no positional or recognised series
        # kwarg — default to the close column. Harmless since no other
        # column is implied.
        def _default_close(df: pd.DataFrame) -> pd.Series:
            if "close" in df.columns:
                return df["close"].astype(float)
            return pd.Series(float("nan"), index=df.index)

        return _default_close

    column = _column_from(arg0)
    if column is not None:

        def _from_column(df: pd.DataFrame, c: str = column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(
                float("nan"), index=df.index
            )  # pragma: no cover — defensive missing-column NaN fallback

        return _from_column

    if isinstance(arg0, ast.Name) and name_evaluators is not None:
        evaluator = name_evaluators.get(arg0.id)
        if evaluator is not None:

            def _from_binding(df: pd.DataFrame, ev=evaluator) -> pd.Series:
                return ev(df).astype(float)

            return _from_binding

    return None
