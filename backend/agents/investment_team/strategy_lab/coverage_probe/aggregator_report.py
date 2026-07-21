"""Indicator-coverage report aggregation for Strategy Lab strategies (#448).

Aggregates per-leg and per-group evaluation results from the AST-extracted
predicate groups (see :mod:`.predicate_resolution`) into a partial
:class:`CoverageReport`, via the public entry point
:func:`run_indicator_probe`.

Pure: no I/O, no LLM, no subprocess. Bounded: per-symbol vectorised pandas
evaluation only when at least one recognised subcondition exists. The
probe never raises — malformed input degrades to ``UNKNOWN_LOW_COVERAGE``
with an explanatory summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from investment_team.models import (
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
)
from investment_team.strategy_lab.coverage_probe.indicator_probe import _MAX_LIKELY_BLOCKERS
from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    Leg,
    OrOp,
    PredicateGroup,
    collect_legs,
    eval_tree,
    find_or_groups,
    tree_and_unknown,
    tree_effective_symbols,
    tree_or_unknown,
)
from investment_team.strategy_lab.coverage_probe.predicate_resolution import (
    _extract_subconditions,
    _union_target_symbols,
)

logger = logging.getLogger(__name__)


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
        When the longest per-symbol recognised bar count is below this
        value the probe short-circuits with
        :data:`CoverageCategory.INSUFFICIENT_BARS`.

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
