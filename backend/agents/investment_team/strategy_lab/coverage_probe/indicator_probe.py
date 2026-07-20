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
from dataclasses import dataclass
from typing import (
    Callable,
    Dict,
    List,
    Literal,
    Optional,
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
from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    AndOp,
    BarPredicate,
    Leg,
    MaskLeaf,
    OrOp,
    PredicateGroup,
    Static,
    SymbolGate,
    collect_legs,
    eval_tree,
    find_or_groups,
    leg_gate_symbols,
    tree_and_unknown,
    tree_effective_symbols,
    tree_or_unknown,
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
            collect_legs(g.tree) for g in groups
        ]
        # Pre-collect or-group records per group so the classifier can
        # look up the originating ``OrOp`` (for ``unknown`` and label
        # assembly) of any or_id reported on a leaf.
        self._group_or_groups: List[Dict[int, OrOp]] = [
            dict(find_or_groups(g.tree)) for g in groups
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
                # us symmetric data for the report; ``eval_tree`` against
                # the whole tree would conflate them.
                per_leg_masks: List[pd.Series] = []
                any_leg_evaluated = False
                for leg_idx, (leg, eff_syms, _in_or, _or_id) in enumerate(legs):
                    if eff_syms is not None and symbol not in eff_syms:
                        per_leg_masks.append(pd.Series(False, index=df.index, dtype=bool))
                        continue
                    mask = eval_tree(leg, df, symbol)
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
                # Whole-tree conjunction mask: a single ``eval_tree``
                # call against the group's root captures AND/OR/SymbolGate
                # semantics in one place — including the implicit gating
                # of legs whose ``SymbolGate`` excludes this symbol.
                conjunction_mask = eval_tree(group.tree, df, symbol)
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
            group_syms = tree_effective_symbols(group.tree)
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
                if tree_or_unknown(leaf.leg.inner):
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
            if tree_or_unknown(group.tree):
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
            conj_leaves = collect_legs(conjunction_group.tree)
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
            group_or_unknown = tree_or_unknown(tree)
            group_and_unknown = tree_and_unknown(tree)
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
    has_any_leg = any(collect_legs(g.tree) for g in groups)
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
    leg_gates = [leg_gate_symbols(lg) for lg in inner]
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


# The predicate/symbol-gate/name-binding resolution helpers live in
# predicate_resolution.py (#1777); imported back here — after every
# remaining definition in this module — for the handful this file's own
# code still calls: _extract_subconditions/_union_target_symbols (used by
# run_indicator_probe), _flatten_top_terms/_symbol_gate (used by
# _build_compound_subcond), _NameStrings (type hint in
# _build_compound_subcond's signature), and _iter_entry_path_assigns
# (used by _resolve_assign_evaluator). A repo-wide audit (#1978) found no
# caller importing any of the other resolution helpers via this
# indicator_probe path — they're reachable directly from
# predicate_resolution.py, the canonical home for this cluster.
# predicate_resolution.py imports _BLOCK_FIELDS and _numeric_literal back
# from this module at ITS bottom, forming a two-way cycle; keeping both
# cross-imports as each module's last statement is what makes the cycle
# safe regardless of which module is imported first — see
# predicate_resolution.py's module docstring.
from investment_team.strategy_lab.coverage_probe.predicate_resolution import (  # noqa: E402
    _extract_subconditions,
    _flatten_top_terms,
    _iter_entry_path_assigns,
    _NameStrings,
    _symbol_gate,
    _union_target_symbols,
)
