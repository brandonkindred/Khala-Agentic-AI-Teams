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
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as _field
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

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


@dataclass(frozen=True)
class _Subcond:
    label: str
    evaluate: Callable[[pd.DataFrame], pd.Series]
    # Optional per-subcond symbol filter. Used by compound OR legs that
    # contain a ``bar.symbol == "X"`` gate alongside their indicator
    # condition: dropping the gate and evaluating the price mask against
    # every DataFrame would let an unrelated symbol satisfy the leg.
    # When set, the aggregator forces the mask to all-False on bars
    # from non-matching symbols and restricts the row's hit-rate
    # denominator to those symbols' bars. ``None`` = applies to all.
    target_symbols: Optional[frozenset] = None
    # Inner OR-leg subconds, exposed for symbol-aware evaluation when
    # this subcond was synthesised by ``_build_compound_or_subcond``.
    # The aggregator iterates these legs per-symbol and skips any whose
    # ``target_symbols`` doesn't include the current symbol — without
    # this an unrelated symbol could satisfy a leg gated to AAPL/MSFT
    # and falsely flip the OR (and the surrounding AND) to true.
    # ``None`` for non-OR-compound subconds.
    or_legs: Optional[Tuple["_Subcond", ...]] = None
    # True when this subcond was synthesised from an OR whose source
    # AST had at least one un-modellable leg (e.g. ``self.custom_ok(bar)``
    # — a custom method call). With an unmodelled alternative present
    # we can't prove the OR is unreachable, so the aggregator must skip
    # the AND zero-hit blocker for this leg even when the recognised
    # legs fired zero times. Mirrors ``_Group.has_unknown_or_leg`` but
    # for nested-OR subconds inside an AND.
    has_unknown_leg: bool = False


@dataclass
class _Group:
    """One ``if``-predicate's worth of coverage-relevant content.

    ``target_symbols`` is ``None`` when the predicate doesn't gate by
    symbol; otherwise it's the set of symbols (from ``bar.symbol == "X"``
    style gates) that may satisfy the entry — DataFrames for any other
    symbol are skipped during aggregation. An empty set means the
    predicate intersects with itself contradictorily (e.g. two
    ``bar.symbol == "X"`` and ``bar.symbol == "Y"`` in one ``and``); the
    group is dropped before emission.

    ``combinator`` is ``"and"`` for the default conjunctive predicate
    and ``"or"`` when the test was a top-level ``BoolOp(Or, ...)``. The
    aggregation classifier flips its zero-hit rule accordingly: AND
    groups flag a blocker as soon as *any* leg is zero, while OR groups
    flag a blocker only when *all* legs are zero (since one firing leg
    is enough to satisfy the disjunction).

    ``ancestor_count`` is the number of leading ``subconds`` that came
    from enclosing AND-conjuncts (ancestors) when the group itself is
    an OR. Those ancestors remain *required* AND-gates, while the
    remaining tail entries are the OR alternatives. The aggregator
    treats positions ``[0:ancestor_count]`` under AND zero-hit rules
    and the tail under OR all-zero rules — so a real ancestor blocker
    still surfaces, but a single dead OR alternative doesn't falsely
    flag a coverage gap. Defaults to ``0`` (all legs in the same
    combinator class).
    """

    subconds: List[_Subcond]
    target_symbols: Optional[set]
    combinator: str = "and"
    ancestor_count: int = 0
    # True when at least one OR-tail leg of this group could not be
    # statically modelled (e.g. a custom method call). The aggregator
    # then suppresses the ``or_group_never_fires`` blocker for the
    # group: with an unmodelled alternative present we can't prove
    # the OR is unreachable, so flagging would be a false positive.
    has_unknown_or_leg: bool = False
    # True when at least one top-level AND-conjunct in this group's
    # source predicate could not be statically modelled (e.g.
    # ``if close > 0 and self.custom_ok(bar):`` — the second conjunct
    # is dropped). The recognised legs' mask is then only a SUPERSET
    # of the real predicate, so the aggregator must not conclude
    # ``COVERAGE_OK`` from the recognised legs firing alone — the
    # un-modelled conjunct may still narrow the actual predicate to
    # zero. Mirrors ``has_unknown_or_leg`` but with the opposite
    # polarity: AND with an unknown conjunct widens the recognised
    # mask, OR with an unknown alternative also widens it.
    has_unknown_and_conjunct: bool = False
    # Symbols the live entry path explicitly excludes via an
    # exclude-shaped early-return guard (e.g. ``if bar.symbol ==
    # "AAPL": return`` or ``if bar.symbol in ("X", "Y"): return``).
    # The aggregator drops these symbols' DataFrames before counting
    # hits. Independent of ``target_symbols``: a group can have an
    # allowlist (everyone in this set is in scope) AND a denylist
    # (anyone in this set is excluded). Effective scope is
    # ``target_symbols ∩ (universe - denied_symbols)``.
    denied_symbols: Optional[frozenset] = None


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
        logger.debug("indicator_probe evaluation failed: %s", exc)
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="indicator probe evaluation failed",
            **base_kwargs,
        )


def _aggregate(
    groups: List[_Group],
    market_data: Dict[str, pd.DataFrame],
    base_kwargs: Dict[str, object],
) -> CoverageReport:
    flat_subconds: List[_Subcond] = [s for g in groups for s in g.subconds]
    # Track each flat subcond's owning-group symbol filter so the
    # SubconditionCoverage dedupe can keep symbol-gated duplicates
    # distinct — otherwise a "close > 50 [AAPL]" branch and a
    # "close > 50 [MSFT]" branch collapse into one entry and a
    # symbol-specific zero-hit blocker is hidden.
    flat_subcond_symbols: List[Optional[frozenset]] = []
    for g in groups:
        syms = frozenset(g.target_symbols) if g.target_symbols is not None else None
        flat_subcond_symbols.extend([syms] * len(g.subconds))
    if not flat_subconds:
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="no recognized indicator subconditions found",
            **base_kwargs,
        )

    sub_hit_counts: List[int] = [0] * len(flat_subconds)
    sub_last_true: List[Optional[str]] = [None] * len(flat_subconds)
    group_conjunction_hits: List[int] = [0] * len(groups)
    group_evaluated: List[bool] = [False] * len(groups)
    total_eval_bars = 0
    # Per-symbol bar count of the symbols that actually contributed to
    # at least one group. Used so a symbol-gated row's hit_rate divides
    # by the matching-symbol bars rather than the full universe — two
    # always-true gated branches would otherwise both report 0.5 instead
    # of 1.0 because each branch's hits come from one symbol's bars but
    # the global denominator includes both.
    per_symbol_bars: Dict[str, int] = {}

    for symbol, df in market_data.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        global_idx = 0
        symbol_contributed = False
        for group_idx, group in enumerate(groups):
            # Symbol-gated groups (``if bar.symbol == "AAPL" and ...``)
            # only consider DataFrames matching one of the gate's symbols.
            if group.target_symbols is not None and symbol not in group.target_symbols:
                global_idx += len(group.subconds)
                continue
            # Exclude-shaped early-return guards
            # (``if bar.symbol == "AAPL": return``) leave a denylist on
            # every group emitted past the guard. Skip those symbols so
            # data the live entry path never reaches can't supply
            # positive coverage.
            if group.denied_symbols is not None and symbol in group.denied_symbols:
                global_idx += len(group.subconds)
                continue
            group_masks: List[pd.Series] = []
            for sub in group.subconds:
                # Per-subcond symbol filter (compound OR legs that
                # captured a ``bar.symbol == "X"`` gate). When the
                # current DataFrame's symbol isn't in the leg's filter,
                # force its mask to all-False so its contribution to
                # both hit count and conjunction is zero — without this
                # an unrelated symbol could appear to satisfy the leg.
                if sub.target_symbols is not None and symbol not in sub.target_symbols:
                    mask = pd.Series(False, index=df.index, dtype=bool)
                elif sub.or_legs is not None:
                    # Compound OR with per-leg symbol gates — evaluate
                    # each leg under its own ``target_symbols`` filter
                    # and OR the masks. Without this the OR wrapper
                    # would drop the per-leg gates and an unrelated
                    # symbol's prices could satisfy a leg restricted to
                    # AAPL/MSFT, falsely flipping the OR (and the
                    # enclosing AND) to true.
                    leg_masks: List[pd.Series] = []
                    for leg in sub.or_legs:
                        if leg.target_symbols is not None and symbol not in leg.target_symbols:
                            leg_masks.append(pd.Series(False, index=df.index, dtype=bool))
                            continue
                        try:
                            leg_series = leg.evaluate(df)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("or-leg %r failed on %s: %s", leg.label, symbol, exc)
                            leg_series = pd.Series(False, index=df.index, dtype=bool)
                        leg_masks.append(
                            pd.Series(leg_series, index=df.index).fillna(False).astype(bool)
                        )
                    if leg_masks:
                        mask = leg_masks[0]
                        for m in leg_masks[1:]:
                            mask = mask | m
                    else:
                        mask = pd.Series(False, index=df.index, dtype=bool)
                else:
                    try:
                        series = sub.evaluate(df)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("subcondition %r failed on %s: %s", sub.label, symbol, exc)
                        series = pd.Series(False, index=df.index, dtype=bool)
                    mask = pd.Series(series, index=df.index).fillna(False).astype(bool)
                hits = int(mask.sum())
                sub_hit_counts[global_idx] += hits
                if hits:
                    last_bar = str(mask[mask].index[-1])
                    if sub_last_true[global_idx] is None or last_bar > sub_last_true[global_idx]:
                        sub_last_true[global_idx] = last_bar
                group_masks.append(mask)
                global_idx += 1
            if group_masks:
                # For AND groups the conjunction is the bar-wise AND
                # of every leg's mask. For OR groups with AND-required
                # ancestors plus an OR tail, the actual predicate is
                # ``ancestors AND (or_tail_1 OR or_tail_2 OR ...)`` —
                # taking the AND of every group mask would require
                # ALL OR legs to fire, which is too restrictive.
                # Combine ancestors-AND with or-tail-OR so the
                # conjunction count reflects the real predicate and
                # downstream classification can flag a never-true
                # nested OR predicate. For plain OR groups (no
                # ancestors), the predicate is just ``or_1 OR or_2
                # OR ...`` so use the bar-wise OR — without this,
                # disjoint firing legs (e.g. ``close > 100 or close
                # < 50``) AND to zero and the unknown-leg fallthrough
                # mis-reports a clearly-firing predicate as
                # ``UNKNOWN_LOW_COVERAGE``.
                if group.combinator == "or":
                    ancestor_masks = group_masks[: group.ancestor_count]
                    or_tail_masks = group_masks[group.ancestor_count :]
                    if ancestor_masks:
                        conjunction_mask = ancestor_masks[0]
                        for m in ancestor_masks[1:]:
                            conjunction_mask = conjunction_mask & m
                    else:
                        conjunction_mask = pd.Series(True, index=df.index, dtype=bool)
                    if or_tail_masks:
                        or_mask = or_tail_masks[0]
                        for m in or_tail_masks[1:]:
                            or_mask = or_mask | m
                        conjunction_mask = conjunction_mask & or_mask
                else:
                    conjunction_mask = group_masks[0]
                    for m in group_masks[1:]:
                        conjunction_mask = conjunction_mask & m
                group_conjunction_hits[group_idx] += int(conjunction_mask.sum())
                group_evaluated[group_idx] = True
                symbol_contributed = True
        if symbol_contributed:
            total_eval_bars += len(df)
            per_symbol_bars[symbol] = per_symbol_bars.get(symbol, 0) + len(df)

    if total_eval_bars == 0:
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary="no bars evaluated",
            subconditions=[],
            **base_kwargs,
        )

    # Deduplicate the SubconditionCoverage list by (label, effective_symbols)
    # so symbol-gated duplicates stay distinct — same predicate text
    # under two different ``bar.symbol == "X"`` branches must surface as
    # two coverage rows so a per-symbol zero-hit blocker is visible.
    # The effective filter is the intersection of the group-level filter
    # (from outer ``bar.symbol == "X"`` ancestors) and the per-subcond
    # filter (from compound OR legs that captured a symbol gate).
    subcoverages: List[SubconditionCoverage] = []
    seen_keys: set = set()
    for sub, group_syms, hits, last in zip(
        flat_subconds, flat_subcond_symbols, sub_hit_counts, sub_last_true
    ):
        if group_syms is None and sub.target_symbols is None:
            effective_syms: Optional[frozenset] = None
        elif group_syms is None:
            effective_syms = sub.target_symbols
        elif sub.target_symbols is None:
            effective_syms = group_syms
        else:
            effective_syms = group_syms & sub.target_symbols
        key = (sub.label, effective_syms)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # Per-row denominator: a symbol-gated row's hits only come from
        # bars in its target_symbols, so dividing by the global total
        # would understate the per-symbol coverage rate. Restrict the
        # denominator to the bars that could have contributed.
        if effective_syms is not None:
            denom = sum(per_symbol_bars.get(s, 0) for s in effective_syms)
        else:
            denom = total_eval_bars
        rate = (hits / denom) if denom > 0 else 0.0
        # Augment the rendered label with the symbol filter so the
        # report distinguishes symbol-gated duplicates without growing
        # the model schema.
        label = sub.label
        if effective_syms:
            label = f"{label} [{','.join(sorted(effective_syms))}]"
        subcoverages.append(
            SubconditionCoverage(
                label=label,
                hit_count=hits,
                hit_rate=min(max(rate, 0.0), 1.0),
                last_true_bar=last,
            )
        )

    # Per-group zero-hit detection. The blocker rule depends on the
    # group's combinator:
    #
    # - ``and`` groups: a single zero-hit leg blocks the predicate
    #   (existing behaviour).
    # - ``or`` groups: only ALL legs being zero blocks the predicate;
    #   any firing leg satisfies the disjunction.
    blockers: List[LikelyBlocker] = []
    flagged_keys: set = set()
    base = 0
    for group_idx, group in enumerate(groups):
        legs = len(group.subconds)
        if not group_evaluated[group_idx]:
            base += legs
            continue
        leg_hits = [sub_hit_counts[base + k] for k in range(legs)]
        leg_labels = [group.subconds[k].label for k in range(legs)]
        # ``target_symbols`` is a regular ``set`` (mutable, unhashable)
        # — freeze it for the dedupe key.
        symbols_key = frozenset(group.target_symbols) if group.target_symbols is not None else None

        def _flag_and_zero(k: int) -> None:
            """Flag a single zero-hit AND-required leg (ancestor or AND-group)."""
            key = (leg_labels[k], symbols_key)
            if key in flagged_keys:
                return
            flagged_keys.add(key)
            evidence = leg_labels[k]
            if group.target_symbols:
                evidence = f"{evidence} [{','.join(sorted(group.target_symbols))}]"
            blockers.append(
                LikelyBlocker(
                    reason="indicator_filter_zero_hits",
                    evidence=evidence,
                    hit_rate=0.0,
                )
            )

        if group.combinator == "and":
            for k in range(legs):
                # Suppress the AND zero-hit blocker for nested-OR
                # subconds whose source AST had at least one
                # un-modellable leg. With an unknown alternative
                # present we can't prove the OR is unreachable, so
                # flagging based on the recognised legs' zero hits
                # would be a false positive.
                if leg_hits[k] == 0 and not group.subconds[k].has_unknown_leg:
                    _flag_and_zero(k)
        else:  # "or"
            # Ancestors are still AND-required even when the group's
            # own predicate is a disjunction — a zero-hit ancestor
            # blocks the predicate regardless of whether any OR leg
            # fires. Apply the AND zero-hit rule to positions
            # [0..ancestor_count) and the disjunction-all-zero rule to
            # the OR-leg tail.
            for k in range(group.ancestor_count):
                if leg_hits[k] == 0 and not group.subconds[k].has_unknown_leg:
                    _flag_and_zero(k)
            or_tail_hits = leg_hits[group.ancestor_count :]
            or_tail_labels = leg_labels[group.ancestor_count :]
            # Suppress the all-zero-OR-tail blocker when at least one
            # leg of the source predicate could not be statically
            # modelled (e.g. ``self.custom_ok(bar)``). With an
            # un-modelled alternative present we can't prove the OR is
            # unreachable, so flagging would be a false positive.
            if or_tail_hits and all(h == 0 for h in or_tail_hits) and not group.has_unknown_or_leg:
                evidence = " OR ".join(or_tail_labels)
                if group.target_symbols:
                    evidence = f"{evidence} [{','.join(sorted(group.target_symbols))}]"
                blockers.append(
                    LikelyBlocker(
                        reason="or_group_never_fires",
                        evidence=evidence,
                        hit_rate=0.0,
                    )
                )
        base += legs

    if blockers:
        if any(b.reason == "indicator_filter_zero_hits" for b in blockers):
            summary = f"{len(blockers)} of {len(subcoverages)} indicator subconditions never fired"
        else:
            summary = "or-predicate has no firing leg"
        return CoverageReport(
            coverage_category=CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
            summary=summary,
            subconditions=subcoverages,
            likely_blockers=blockers[:_MAX_LIKELY_BLOCKERS],
            **base_kwargs,
        )

    # Find any single ``if`` predicate whose component masks all fire
    # individually but whose true conjunction is empty. We flag
    # CONJUNCTION_NEVER_TRUE for two shapes:
    #
    # - **AND groups** with 2+ legs, each individually firing, but
    #   their bar-wise AND is zero — a real per-predicate
    #   contradiction.
    # - **OR groups** with one or more AND-required ancestors PLUS at
    #   least one OR-tail leg, where each ancestor and at least one
    #   OR-tail leg individually fire but the actual predicate
    #   ``ancestors AND (or_tail_1 OR or_tail_2 OR ...)`` is empty.
    #   Without this an ancestor-and-(disjoint OR tail) predicate is
    #   silently classified as ``COVERAGE_OK``.
    #
    # We never flag this for plain OR groups (no ancestors): their
    # bar-wise AND is meaningless under disjunction semantics, and
    # the OR-tail-all-zero rule already covers their unreachability.
    empty_conj_group: Optional[_Group] = None
    base = 0
    for group_idx, group in enumerate(groups):
        legs = len(group.subconds)
        if not group_evaluated[group_idx] or group_conjunction_hits[group_idx] != 0:
            base += legs
            continue
        if group.has_unknown_or_leg or any(group.subconds[k].has_unknown_leg for k in range(legs)):
            # Unknown alternative present — can't prove the predicate
            # is unreachable. Suppress the blocker (mirrors the OR
            # zero-hit suppression elsewhere). The group-level flag
            # covers OR groups whose entire OR-tail leg was un-modellable
            # (and therefore not in ``group.subconds``); the per-subcond
            # flag covers nested-OR ``_Subcond``s synthesised by
            # ``_build_compound_or_subcond`` whose recognised legs DID
            # land in ``group.subconds`` but which had a sibling
            # un-modellable leg.
            base += legs
            continue
        if group.combinator == "and":
            if legs >= 2 and all(sub_hit_counts[base + k] > 0 for k in range(legs)):
                empty_conj_group = group
                break
        else:  # "or"
            if group.ancestor_count >= 1 and legs > group.ancestor_count:
                ancestor_hits_ok = all(
                    sub_hit_counts[base + k] > 0 for k in range(group.ancestor_count)
                )
                or_tail_any_fire = any(
                    sub_hit_counts[base + k] > 0 for k in range(group.ancestor_count, legs)
                )
                if ancestor_hits_ok and or_tail_any_fire:
                    empty_conj_group = group
                    break
        base += legs

    if empty_conj_group is not None:
        return CoverageReport(
            coverage_category=CoverageCategory.CONJUNCTION_NEVER_TRUE,
            summary="individual subconditions fire but their conjunction is never true",
            subconditions=subcoverages,
            likely_blockers=[
                LikelyBlocker(
                    reason="conjunction_never_true",
                    evidence=" AND ".join(s.label for s in empty_conj_group.subconds),
                    hit_rate=0.0,
                )
            ][:_MAX_LIKELY_BLOCKERS],
            **base_kwargs,
        )

    # Final fallthrough check: if every group with an unknown leg /
    # alternative / conjunct has produced no positive recognised
    # evidence, we have no positive evidence the predicate fires —
    # only an un-modellable component that *might* (OR-unknown) or
    # *might not* (AND-unknown). Return ``UNKNOWN_LOW_COVERAGE``
    # rather than ``COVERAGE_OK`` so a sparse / zero-trade backtest
    # isn't mislabelled "healthy" when the probe genuinely doesn't
    # know.
    #
    # Polarity differs by unknown kind:
    #   - OR-leg / nested-OR unknown: an unknown alternative widens
    #     the recognised mask. Recognised conjunction firing is
    #     still sound positive evidence — the predicate fires at
    #     least at those bars, possibly more.
    #   - AND-conjunct unknown: an unknown conjunct narrows the
    #     recognised mask. The recognised AND is only a SUPERSET of
    #     the real predicate, so its conjunction firing does NOT
    #     prove the real predicate fires. Such a group cannot
    #     contribute positive evidence on its own.
    base = 0
    has_unknown_evidence = False
    has_recognised_evidence = False
    for group_idx, group in enumerate(groups):
        legs = len(group.subconds)
        if not group_evaluated[group_idx]:
            base += legs
            continue
        leg_hits = [sub_hit_counts[base + k] for k in range(legs)]
        group_or_unknown = group.has_unknown_or_leg or any(
            group.subconds[k].has_unknown_leg for k in range(legs)
        )
        group_and_unknown = group.has_unknown_and_conjunct
        group_unknown = group_or_unknown or group_and_unknown
        if group_unknown:
            has_unknown_evidence = True
            if group_and_unknown:
                # AND-conjunct unknown: recognised mask is a superset
                # of the real predicate. Conjunction hits only bound
                # the real predicate from above and cannot supply
                # positive evidence — skip without contributing.
                pass
            elif group_conjunction_hits[group_idx] > 0 and any(h > 0 for h in leg_hits):
                # OR-side unknown only: a recognised alternative
                # firing is sufficient positive evidence because the
                # unknown alternative can only widen the disjunction.
                has_recognised_evidence = True
        else:
            # Group has no unknown legs and got past the blocker
            # checks → its mere existence is recognised coverage.
            if any(h > 0 for h in leg_hits):
                has_recognised_evidence = True
        base += legs

    if has_unknown_evidence and not has_recognised_evidence:
        return CoverageReport(
            coverage_category=CoverageCategory.UNKNOWN_LOW_COVERAGE,
            summary=(
                "predicate has un-modellable alternative(s) and recognised legs "
                "produced no firing bars — coverage is unknown"
            ),
            subconditions=subcoverages,
            likely_blockers=[],
            **base_kwargs,
        )

    return CoverageReport(
        coverage_category=CoverageCategory.COVERAGE_OK,
        summary="indicator subconditions fired at least once",
        subconditions=subcoverages,
        likely_blockers=[],
        **base_kwargs,
    )


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
        self._groups: List[_Group] = []
        # Budget counter — formerly ``state["total"]`` in the closure.
        self._budget = 0

    def walk(self, on_bar: ast.FunctionDef) -> List[_Group]:
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
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
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
            elif isinstance(target, ast.Attribute):
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

    def _budgeted_extend(self, group_subs: List[_Subcond], extras: List[_Subcond]) -> bool:
        """Append extras into group within the global subcond budget.

        Returns False when the global cap is hit (caller should stop).
        """
        for sub in extras:
            if self._budget >= _MAX_SUBCONDITIONS:
                return False
            group_subs.append(sub)
            self._budget += 1
        return True

    def _process_if(
        self,
        test: ast.expr,
        body: List[ast.stmt],
        orelse: List[ast.stmt],
        ancestors: List[_Subcond],
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

        own_subs: List[_Subcond] = []
        own_symbols: Optional[set] = None
        # Track whether any AND-conjunct could not be statically modelled.
        # When set, the recognised mask is a SUPERSET of the real
        # predicate so the aggregator must not conclude ``COVERAGE_OK``
        # from the recognised legs alone — see ``_Group.has_unknown_and_conjunct``.
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
                or_compound = _build_compound_or_subcond(
                    term,
                    self._name_periods,
                    self._name_evaluators,
                    self._name_strings,
                    self._bar_name,
                )
                if or_compound is not None:
                    own_subs.append(or_compound)
                    # If the OR is fully symbol-gated (every leg restricted
                    # via ``bar.symbol == "X"``), the OR-compound's
                    # ``target_symbols`` is the union of those gates.
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
                    if or_compound.target_symbols is not None:
                        if own_symbols is None:
                            own_symbols = set(or_compound.target_symbols)
                        else:
                            own_symbols &= or_compound.target_symbols
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

        group_subs: List[_Subcond] = []
        if not self._budgeted_extend(group_subs, ancestors):
            if group_subs:
                self._groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=effective_symbols,
                        has_unknown_and_conjunct=effective_unknown,
                        denied_symbols=effective_denied,
                    )
                )
            return False
        if not self._budgeted_extend(group_subs, own_subs):
            if group_subs:
                self._groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=effective_symbols,
                        has_unknown_and_conjunct=effective_unknown,
                        denied_symbols=effective_denied,
                    )
                )
            return False
        if group_subs and not (effective_symbols is not None and not effective_symbols):
            self._groups.append(
                _Group(
                    subconds=group_subs,
                    target_symbols=effective_symbols,
                    has_unknown_and_conjunct=effective_unknown,
                    denied_symbols=effective_denied,
                )
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
        ancestors: List[_Subcond],
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
        own_subs: List[_Subcond] = []
        # Track legs we couldn't statically model (e.g. an unrecognised
        # method call like ``self.custom_ok(bar)``). When at least one
        # leg is unknown the OR's "all known legs zero" rule must NOT
        # flag a blocker — the un-modelled alternative may make the
        # entry reachable, so flagging would be a false positive.
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
                        _Subcond(
                            label=_format_label(leg),
                            evaluate=_always_true,
                            target_symbols=frozenset(sym),
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
                compound = _build_compound_and_subcond(
                    leg,
                    self._name_periods,
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

        if not own_subs:
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
        # carry both in one group with combinator="or" plus an
        # ``ancestor_count`` so _aggregate knows where the AND-tail
        # ends and the OR-leg head begins. (See _Group docstring for
        # the per-position rule.)
        group_subs: List[_Subcond] = []
        if not self._budgeted_extend(group_subs, ancestors):
            if group_subs:
                self._groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=ancestor_symbols,
                        combinator="and",
                        ancestor_count=len(group_subs),
                        has_unknown_and_conjunct=ancestor_unknown,
                        denied_symbols=denied_frozen,
                    )
                )
            return False
        ancestor_count = len(group_subs)
        if not self._budgeted_extend(group_subs, own_subs):
            if group_subs:
                self._groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=ancestor_symbols,
                        combinator="or",
                        ancestor_count=ancestor_count,
                        has_unknown_or_leg=has_unknown_leg,
                        has_unknown_and_conjunct=ancestor_unknown,
                        denied_symbols=denied_frozen,
                    )
                )
            return False
        if group_subs:
            self._groups.append(
                _Group(
                    subconds=group_subs,
                    target_symbols=ancestor_symbols,
                    combinator="or",
                    ancestor_count=ancestor_count,
                    has_unknown_or_leg=has_unknown_leg,
                    has_unknown_and_conjunct=ancestor_unknown,
                    denied_symbols=denied_frozen,
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
        # ``_build_compound_or_subcond`` already does the leg
        # synthesis (compound OR-of-masks evaluator + per-leg symbol
        # gates rolled into ``target_symbols`` when every leg is
        # symbol-gated). Reuse it here so the nested body is
        # evaluated against the same OR semantics the aggregator
        # uses for the immediate group.
        body_ancestors = ancestors
        body_symbols = ancestor_symbols
        body_unknown = ancestor_unknown or has_unknown_leg
        or_compound = _build_compound_or_subcond(
            test, self._name_periods, self._name_evaluators, self._name_strings, self._bar_name
        )
        if or_compound is not None:
            body_ancestors = ancestors + [or_compound]
            if or_compound.target_symbols is not None:
                body_symbols = _intersect_symbols(ancestor_symbols, set(or_compound.target_symbols))
            if or_compound.has_unknown_leg:
                body_unknown = True
        else:
            # OR was fully un-modellable — every nested predicate is
            # gated by an unknown ancestor, so descendants can't
            # supply positive evidence on their own.
            body_unknown = True
        if not self._visit(body, body_ancestors, body_symbols, body_unknown, ancestor_denied):
            return False
        if not self._visit(orelse, ancestors, ancestor_symbols, ancestor_unknown, ancestor_denied):
            return False
        return True

    def _visit(
        self,
        stmts: List[ast.stmt],
        ancestors: List[_Subcond],
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
                            ):
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
                            ):
                                return False
                        continue
                    if position_check == "occupied":  # pos is not None — orelse is entry
                        if not self._visit(
                            stmt.orelse,
                            ancestors,
                            current_symbols,
                            ancestor_unknown,
                            current_denied,
                        ):
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
                    ):
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
                            ):
                                return False
                    # ast.Try has handlers; each handler.body is a stmt list.
                    handlers = getattr(stmt, "handlers", None)
                    if isinstance(handlers, list):
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


def _extract_subconditions(strategy_code: str) -> List[_Group]:
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
            return position_dir, ast.BoolOp(op=ast.And(), values=survivors)
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
    if len(test.ops) != 1:
        return None
    op = test.ops[0]
    rhs = test.comparators[0]
    if not (isinstance(rhs, ast.Constant) and rhs.value is None):
        return None
    left = test.left
    if isinstance(left, ast.Name) and left.id in {"pos", "position"}:
        pass
    elif (
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
    return None


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
    if body0.value is not None:
        # ``return None`` is equivalent to bare return; anything else
        # (a value-bearing return) is too suggestive of a real path
        # we'd rather not assume nothing about.
        if not (isinstance(body0.value, ast.Constant) and body0.value.value is None):
            return None
    # An ``orelse`` here means there's a follow-up branch the strategy
    # cares about, which doesn't fit the simple "early return" guard
    # shape. Skip.
    if stmt.orelse:
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
        if name_strings is None:
            return None
        # Bare ``Name`` resolves through the module/global scope only —
        # class-body bare names are NOT in lexical scope for methods.
        if isinstance(n, ast.Name):
            return name_strings.globals_.get(n.id)
        # ``self.X`` / ``cls.X`` resolves through the class chain
        # (instance dict via ``__init__`` shadowing class body), never
        # through module scope.
        if (
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
            if _is_bar_symbol(right):
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
                    if s is None:
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
    if len(node.ops) != 1:
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
        if name_strings is None:
            return None
        # Bare ``Name`` resolves through the module/global scope only —
        # class-body bare names are NOT in lexical scope for methods.
        if isinstance(n, ast.Name):
            return name_strings.globals_.get(n.id)
        # ``self.X`` / ``cls.X`` resolves through the class chain
        # (instance dict via ``__init__`` shadowing class body), never
        # through module scope.
        if (
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
        if _is_bar_symbol(right):
            sym = _string_const(left)
            return {sym} if sym is not None else None
        return None

    if isinstance(op, ast.In):
        if not _is_bar_symbol(left):
            return None
        if not isinstance(right, (ast.Tuple, ast.List, ast.Set)):
            return None
        syms: set = set()
        for elt in right.elts:
            s = _string_const(elt)
            if s is None:
                # Partial allow-list: refuse to gate. Better to leave
                # the predicate unconstrained than apply a wrong filter.
                return None
            syms.add(s)
        return syms if syms else None

    return None


def _union_target_symbols(groups: List[_Group], universe: Optional[set] = None) -> Optional[set]:
    """Return the union of symbols any group could possibly fire on, or ``None``.

    Used by :func:`run_indicator_probe` to size the warmup check to the
    symbols that can actually satisfy a predicate. Returns ``None`` when
    at least one group is **fully unconstrained** — i.e. neither the
    group-level filter nor any required subcond filter narrows the
    symbol space, AND the group carries no exclude-shaped early-return
    denylist — so the warmup check stays over every fetched DataFrame.

    ``universe`` is the set of symbol keys present in ``market_data``.
    It's required to express "universal except for these" when a group
    has ``denied_symbols`` but no positive allowlist (e.g. ``if
    bar.symbol == "AAPL": return`` followed by an indicator-only
    predicate). Without it the function would either treat the group
    as universal — letting AAPL's long history rescue warmup even
    though AAPL is never evaluated — or as fully empty, both wrong.

    Combinator-aware:

    * **AND groups**: the predicate fires only when every conjunct
      holds, so the symbol space is the union of each conjunct's gate
      (we use union rather than intersection conservatively — for
      warmup we want every symbol that could conceivably contribute,
      so we don't over-flag ``INSUFFICIENT_BARS``). A nested OR
      subcond (``sub.or_legs``) with any unrestricted leg contributes
      no narrowing because that leg can fire on any symbol.

    * **OR groups**: the predicate fires when any leg holds. AND-required
      ancestors (positions ``[0:ancestor_count)``) still narrow the
      group, but if any OR-tail leg is unrestricted, the OR can fire
      on any symbol — so the group is universal unless ancestors
      narrow it.

    * **Denylists**: ``group.denied_symbols`` (set by an exclude-shaped
      early-return guard like ``if bar.symbol == "AAPL": return``) is
      subtracted from each group's effective set before union'ing. A
      group with no allowlist but a denylist resolves to ``universe -
      denied_symbols`` rather than ``universe``.
    """
    union: set = set()
    saw_universal = False
    for g in groups:
        group_syms: Optional[set] = None
        if g.target_symbols is not None:
            group_syms = set(g.target_symbols)

        if g.combinator == "or":
            and_required = g.subconds[: g.ancestor_count]
            or_tail = g.subconds[g.ancestor_count :]
        else:
            and_required = list(g.subconds)
            or_tail = []

        for sub in and_required:
            if sub.target_symbols is not None:
                if group_syms is None:
                    group_syms = set(sub.target_symbols)
                else:
                    group_syms.update(sub.target_symbols)
            if sub.or_legs is not None:
                # Nested OR inside an AND term. If any leg is
                # unrestricted, the OR subcond is universal — it
                # contributes no narrowing to the AND. Otherwise
                # union the leg gates (the OR can only fire on the
                # union of leg symbols).
                if any(leg.target_symbols is None for leg in sub.or_legs):
                    pass
                else:
                    for leg in sub.or_legs:
                        if leg.target_symbols is not None:
                            if group_syms is None:
                                group_syms = set(leg.target_symbols)
                            else:
                                group_syms.update(leg.target_symbols)

        if or_tail:
            if any(t.target_symbols is None for t in or_tail):
                # OR-tail is universal: an unrestricted leg can fire
                # on any symbol so the disjunction's symbol space is
                # unbounded. The group's only remaining constraint is
                # whatever the AND-required prefix imposed via
                # ``group_syms`` above, plus the denylist applied
                # below.
                pass
            else:
                for t in or_tail:
                    if t.target_symbols:
                        if group_syms is None:
                            group_syms = set(t.target_symbols)
                        else:
                            group_syms.update(t.target_symbols)

        denied = set(g.denied_symbols) if g.denied_symbols else set()

        if group_syms is None:
            # No positive allowlist anywhere in this group. Universal
            # iff the denylist is also empty; otherwise the group's
            # effective scope is ``universe - denied`` and the
            # universal short-circuit doesn't apply.
            if not denied:
                saw_universal = True
                continue
            if universe is None:
                # Caller didn't supply a universe — fall back to the
                # legacy "universal" answer rather than fabricating a
                # narrowed set we can't validate.
                saw_universal = True
                continue
            group_syms = set(universe) - denied
        else:
            group_syms -= denied

        union.update(group_syms)

    if saw_universal:
        # Even one fully-universal group means the predicate could
        # fire on any fetched symbol — warmup must consider all of
        # them.
        return None
    return union if union else None


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
    if args is None:
        return "bar"
    posargs = list(getattr(args, "args", []) or [])
    if len(posargs) >= 3:
        # Method form ``def on_bar(self, ctx, bar):``
        return posargs[2].arg
    if len(posargs) == 2:
        # Free-function form ``def on_bar(ctx, bar):``
        return posargs[1].arg
    return "bar"


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
        if fallback is None and name in fallback_names:
            fallback = node
    return fallback


def _iter_entry_path_assigns(node: ast.AST):
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


def _flatten_test(test: ast.expr) -> List[ast.Compare]:
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
        if inner is None:
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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(node, ast.Compare):
        return None
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    left_val = _static_scalar_value(node.left, name_periods)
    right_val = _static_scalar_value(node.comparators[0], name_periods)
    if left_val is None or right_val is None:
        return None
    op_fn = _STATIC_CMP_OPS.get(type(node.ops[0]))
    if op_fn is None:
        return None
    try:
        return bool(op_fn(left_val, right_val))
    except Exception:  # noqa: BLE001
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
    if args is None:
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
    for param, default in zip(kwonly, kw_defaults):
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
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
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
        return None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
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
    if isinstance(value, ast.Name):
        return name_strings.globals_.get(value.id)
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id in {"self", "cls"}
    ):
        return name_strings.attrs.get(value.attr)
    return None


def _collect_name_strings(
    tree: ast.AST,
    strategy_class: Optional[ast.ClassDef] = None,
) -> _NameStrings:
    """Bind ``NAME = "<string>"`` for string-constant resolution.

    Mirrors :func:`_collect_name_periods` but for string-valued
    assignments. Used by :func:`_symbol_gate` so a target-symbol
    constant like ``TARGET_SYMBOL = "BBB"`` resolves
    ``bar.symbol == TARGET_SYMBOL`` (bare name) and
    ``bar.symbol == self.TARGET`` (attribute) without one overwriting
    the other.

    Returns a :class:`_NameStrings` with two dicts:

    - ``globals_`` (bare-name lookups) — module-level ``Name``
      targets only. Class-body ``Name`` targets do NOT contribute
      because Python's class body is not in lexical scope for
      methods, so a bare ``TARGET`` reference inside ``on_bar``
      resolves through the module/global scope, not the class.
    - ``attrs`` (``self.X`` / ``cls.X`` lookups) — class-body
      ``Name`` targets (which become class attributes accessible
      via ``self.X`` / ``Class.X``) and class ``__init__`` /
      ``__post_init__`` ``self.X = ...`` instance assignments.
      Module-level bare names do NOT contribute because
      ``self.X`` resolution stops at the class chain — it does not
      fall through to module scope.
    """
    bindings = _NameStrings()
    _CONSTRUCTOR_NAMES = {"__init__", "__post_init__"}

    def _resolve_string_value(value: ast.expr, *, in_method: bool) -> Optional[str]:
        """Resolve an assignment RHS to a string at write time.

        Strategies routinely alias module constants into class
        attributes — ``self.TARGET = TARGET`` inside ``__init__`` or
        a class-body ``TARGET = TARGET`` line. Without resolving the
        RHS, the alias was silently dropped (RHS isn't a Constant)
        and ``bar.symbol == self.TARGET`` lost its gate, so a sibling
        indicator condition silently evaluated against every fetched
        DataFrame and the report could falsely flip to
        ``COVERAGE_OK``.

        ``in_method=True`` for assignments inside ``__init__`` (and
        any method body): bare ``Name`` references resolve through
        the module scope only, matching Python's runtime — class-body
        names are not in scope inside methods.
        ``in_method=False`` for assignments lexically in the class
        body: bare ``Name`` references resolve class-local first
        (earlier class-body bindings already recorded into ``attrs``),
        then module/global scope, matching Python's class-namespace
        lookup at class-body execution time.

        ``self.X`` / ``cls.X`` always resolve through ``attrs``.
        """
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            if in_method:
                return bindings.globals_.get(value.id)
            cls_local = bindings.attrs.get(value.id)
            if cls_local is not None:
                return cls_local
            return bindings.globals_.get(value.id)
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id in {"self", "cls"}
        ):
            return bindings.attrs.get(value.attr)
        return None

    def _record_global(target: ast.expr, value: ast.expr, *, overwrite: bool) -> None:
        # Module scope: bare ``Name`` RHS resolution can only consult
        # the module's own dict (``globals_``); ``attrs`` is empty
        # before we enter the class. ``in_method`` is irrelevant here
        # — pass ``True`` so we never fall through to ``attrs`` (which
        # would also be empty, but the explicit choice documents the
        # intent).
        resolved = _resolve_string_value(value, in_method=True)
        if resolved is None:
            return
        if isinstance(target, ast.Name):
            if overwrite:
                bindings.globals_[target.id] = resolved
            else:
                bindings.globals_.setdefault(target.id, resolved)

    def _record_attr(target: ast.expr, value: ast.expr, *, in_method: bool) -> None:
        resolved = _resolve_string_value(value, in_method=in_method)
        if resolved is None:
            return
        if isinstance(target, ast.Name):
            # In a class body, a bare ``Name`` target creates a class
            # attribute (``class S: TARGET = "MSFT"`` → ``S.TARGET``).
            # Inside a method body (``__init__``), a bare ``Name``
            # target is a function local — it does NOT create an
            # instance attribute, so ``self.TARGET`` would still
            # resolve through the class chain at runtime. Skip the
            # local entirely so it doesn't pollute ``attrs``.
            if in_method:
                return
            bindings.attrs[target.id] = resolved
        elif isinstance(target, ast.Attribute):
            bindings.attrs[target.attr] = resolved

    def _walk(node: ast.AST):
        if strategy_class is not None and isinstance(node, ast.ClassDef):
            if node is not strategy_class:
                return
            # Class body: walk all top-level statements through the
            # unconditional-walker so a class-scope ``if True: TARGET =
            # "AAPL"`` (or ``with ...``) lands in ``attrs`` like its
            # plain-top-level counterpart. Without this, the recursive
            # ``_walk(child)`` fallthrough hit the module-scope path and
            # stored the class attribute in ``globals_`` — invisible to
            # ``self.TARGET`` lookups.
            class_param_defaults: Dict[str, ast.Constant] = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in _CONSTRUCTOR_NAMES:
                        # Descend into branches that are statically
                        # guaranteed to execute (``if True: ...``,
                        # ``if <param>:`` where ``<param>`` has a
                        # constant default, ``with ...``) while skipping
                        # unknown-predicate branches (whose dead-branch
                        # values would otherwise overwrite the class
                        # attribute) and loops / try (whose execution
                        # isn't statically guaranteed). See
                        # :func:`_iter_unconditional_constructor_assigns`.
                        param_defaults = _constructor_param_defaults(child)
                        for sub in _iter_unconditional_constructor_assigns(
                            child.body, param_defaults
                        ):
                            if isinstance(sub, ast.Assign):
                                for t in sub.targets:
                                    _record_attr(t, sub.value, in_method=True)
                            elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                                _record_attr(sub.target, sub.value, in_method=True)
                    continue
                # Class body assignment (top-level or nested under a
                # statically-guaranteed ``if True:`` / ``with`` /
                # default-true param guard). Class bodies don't have
                # their own parameters, so we pass an empty defaults
                # table — only literal-Constant predicates resolve.
                for sub in _iter_unconditional_constructor_assigns([child], class_param_defaults):
                    if isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            _record_attr(t, sub.value, in_method=False)
                    elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                        _record_attr(sub.target, sub.value, in_method=False)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _record_global(t, node.value, overwrite=False)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record_global(node.target, node.value, overwrite=False)
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return bindings


def _collect_name_periods(
    tree: ast.AST,
    function_node: Optional[ast.AST] = None,
    strategy_class: Optional[ast.ClassDef] = None,
) -> Dict[str, Union[int, float]]:
    """Bind ``NAME = <int>`` for later ``Name`` / ``self.NAME`` resolution.

    Walks every ``Assign`` / ``AnnAssign`` whose target is either:

    - a bare ``Name`` (module-level ``WINDOW = 80`` or class attribute
      ``WINDOW = 80``), or
    - an ``Attribute`` of the form ``self.WINDOW = 80`` (typically inside
      ``__init__``) — only the attr name is recorded.

    Strategies generated from the standard ideation prompt encourage
    class tuning knobs and ``self.WINDOW`` access; without this both the
    AST walk and downstream lookup would miss the binding entirely.

    When ``strategy_class`` is provided, the walker skips the bodies
    of any sibling ``ClassDef`` so a helper class's same-named
    attribute can't pre-empt the strategy's own constant via the bare-
    attribute keying. ``Helper.PERIOD = 2`` declared before
    ``class Strategy: PERIOD = 20`` would otherwise leave the probe
    resolving ``self.PERIOD`` to 2 instead of 20.

    When ``function_node`` is provided, a second pass walks just that
    function's body and **overwrites** any outer-scope binding that
    shares a name. Python's lexical scope means a local
    ``WINDOW = 5`` inside ``on_bar`` shadows a module/class-level
    ``WINDOW = 200``; without the override the probe would evaluate
    ``sma(close, WINDOW)`` against the outer 200, producing false
    ``INDICATOR_FILTER_TOO_RESTRICTIVE`` / ``COVERAGE_OK`` calls for
    common tuning-variable refactors.
    """
    bindings: Dict[str, Union[int, float]] = {}

    def _record(target: ast.expr, value: ast.expr, *, overwrite: bool) -> None:
        # Reuse the same numeric-literal extractor used downstream so
        # negative ints and unary-minus constants resolve consistently.
        # Allow any numeric value — zero, negatives, and non-integer
        # floats are stored alongside positive ints to support
        # threshold locals like ``ZERO_LINE = 0`` or ``limit = 100.5``
        # that appear as ``Name`` operands in comparisons. Period-use
        # sites validate ``> 0`` and ``is_integer()`` at lookup time
        # so non-positive or float values are never misapplied as
        # indicator windows.
        v = _numeric_literal(value, bindings)
        if v is None:
            return
        ivalue: Union[int, float] = int(v) if float(v).is_integer() else float(v)
        if isinstance(target, ast.Name):
            if overwrite:
                bindings[target.id] = ivalue
            else:
                bindings.setdefault(target.id, ivalue)
        elif isinstance(target, ast.Attribute):
            # ``self.WINDOW`` (or any other instance attribute) — record
            # by attribute name so a later ``self.WINDOW`` reference
            # resolves through _numeric_literal's Attribute branch.
            if overwrite:
                bindings[target.attr] = ivalue
            else:
                bindings.setdefault(target.attr, ivalue)

    def _iter_outer_assigns(node: ast.AST):
        """Yield ``(node, scope)`` tuples for assignments lexically in
        scope for the strategy.

        ``scope`` is ``"module"`` for module-level (or nested
        non-strategy compound) assignments and ``"class"`` for
        assignments inside the strategy ``ClassDef`` (class body and
        constructor body). The caller uses the scope to decide
        ``setdefault`` vs ``overwrite`` semantics: module scope is
        outer-most and only seeds defaults, class scope **overrides**
        module-level bindings because Python's runtime ``self.WINDOW``
        resolves through the class attribute regardless of any
        same-named module constant.

        When ``strategy_class`` is set, descend into module / function
        bodies as usual but only into the strategy's own ``ClassDef``.
        Sibling helper classes are skipped so their same-named
        attributes can't pre-empt the strategy's bare-attr bindings.

        Inside the strategy class, only the constructor's body
        (``__init__`` / ``__post_init__``) is walked for
        ``self.<NAME>`` assignments. Other methods like ``on_bar`` or
        a private ``_helper`` are runtime entry points whose
        ``self.<NAME> = ...`` lines are state mutations, not constants.
        Without this restriction, a helper method ordered before
        ``__init__`` in the class body would seed the binding via
        ``setdefault`` and ``__init__``'s actual value would never bind
        — ``self.THRESHOLD`` would resolve to whatever the helper set
        rather than what the live strategy state holds.

        Module-level helper ``FunctionDef`` / ``AsyncFunctionDef``
        bodies are also skipped (a helper's local ``WINDOW = 999``
        is not a module constant; without this skip a sibling helper
        ordered before the strategy class could pre-empt
        ``class Strategy: WINDOW = 2`` and ``self.WINDOW`` would
        resolve to the helper's value).

        When ``strategy_class`` is None, behaves like ``ast.walk`` over
        Assigns/AnnAssigns but still skips function bodies (the
        previous behaviour incorrectly recorded function locals). All
        yielded entries use ``"module"`` scope since there is no
        identified class boundary.
        """
        _CONSTRUCTOR_NAMES = {"__init__", "__post_init__"}
        if strategy_class is not None and isinstance(node, ast.ClassDef):
            if node is not strategy_class:
                return
            # Walk class-body Assign / AnnAssign directly. For
            # FunctionDef children, only descend into the constructor
            # along the always-taken default-construction path — a
            # blanket ``ast.walk`` records assignments from dead /
            # default-false branches such as
            # ``def __init__(self, enabled=False):
            #       if enabled: self.WINDOW = 200``,
            # whose ``self.WINDOW = 200`` would overwrite the class
            # default ``WINDOW = 2`` and the probe would evaluate
            # indicators with a window the live default-constructed
            # strategy never sees. Mirror the helper used by
            # :func:`_collect_name_strings` to stay consistent.
            # Nested ClassDefs follow the strategy_class skip rule via
            # the recursive call.
            for child in node.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    yield child, "class"
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in _CONSTRUCTOR_NAMES:
                        param_defaults = _constructor_param_defaults(child)
                        for sub in _iter_unconditional_constructor_assigns(
                            child.body, param_defaults
                        ):
                            yield sub, "class"
                else:
                    yield from _iter_outer_assigns(child)
            return
        # Module / nested compound: skip function / async-function
        # bodies entirely — their locals belong to that function's
        # scope, not to the strategy. Descending into them would let a
        # sibling ``def helper(): WINDOW = 999`` pre-empt the
        # strategy's class-level constant via ``setdefault``.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            yield node, "module"
        for child in ast.iter_child_nodes(node):
            yield from _iter_outer_assigns(child)

    # Pass 1: outer-scope assignments. Module scope uses ``setdefault``
    # so cross-scope constants stay isolated; the strategy class's own
    # body and ``__init__`` use overwrite, because a class-level
    # ``WINDOW = 3`` shadows a module-level ``WINDOW = 1`` for any
    # ``self.WINDOW`` reference at runtime. Walking module-first then
    # class-second keeps source order; the per-scope flag controls the
    # write semantics.
    for node, scope in _iter_outer_assigns(tree):
        is_class = scope == "class"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record(target, node.value, overwrite=is_class)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record(node.target, node.value, overwrite=is_class)

    # Pass 2: inner-scope assignments on the entry control-flow path
    # overwrite the outer binding. We skip exit branches of position
    # checks so an exit-only reassignment can't shadow the entry-path
    # binding (matches the entry-only routing used by _visit and the
    # name-evaluator collector).
    if function_node is not None:
        for node in _iter_entry_path_assigns(function_node):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    _record(target, node.value, overwrite=True)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                _record(node.target, node.value, overwrite=True)

    return bindings


def _build_subcond(
    node: ast.Compare,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[_Subcond]:
    # Only support simple a <op> b shape — chained comparisons are rare in
    # generated strategies and ambiguous for hit-rate semantics.
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    op = type(node.ops[0])
    op_fn = _CMP_OPS.get(op)
    if op_fn is None:
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

    return _Subcond(label=label, evaluate=_eval)


def _build_truthy_subcond(
    node: ast.expr,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[_Subcond]:
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
    except Exception:  # noqa: BLE001
        label = inner.id
    if len(label) > _MAX_LABEL_LEN:
        label = label[: _MAX_LABEL_LEN - 1] + "…"

    def _eval(df: pd.DataFrame) -> pd.Series:
        s = evaluator(df)
        return s.fillna(0).astype(bool)

    return _Subcond(label=label, evaluate=_eval)


def _build_compound_and_subcond(
    leg: ast.BoolOp,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
    name_strings: Optional["_NameStrings"] = None,
    bar_name: str = "bar",
) -> Optional[_Subcond]:
    """Build a single subcond for an ``and``-conjunction inside an OR leg.

    For predicates like ``(close > 100 and volume > 0) or (rsi(close)
    < 30 and volume > 0)`` each OR leg is an ``ast.BoolOp(And, ...)``
    rather than an ``ast.Compare``. The disjunction's truthfulness on a
    given bar depends on the bar-wise AND of each leg's inner
    conjuncts, so we synthesise one ``_Subcond`` whose evaluator runs
    the inner subconds and ANDs their masks together.

    Returns ``None`` if no inner term is recognisable (so the OR leg is
    simply skipped, matching how unrecognised top-level legs are
    handled). **Also returns ``None`` when any inner conjunct can't be
    modelled**, even if other conjuncts are recognised — the
    synthesised AND-of-known-conjuncts would be an upper bound on the
    actual mask (the AND fires on a SUBSET of the recognised mask, not
    a superset), so claiming the leg fires whenever the recognised
    half does would be too permissive. Declining lets the parent
    ``_process_or_if`` / ``_build_compound_or_subcond`` mark the
    enclosing OR as having an unknown leg and suppress its
    ``or_group_never_fires`` blocker.
    """
    inner: List[_Subcond] = []
    leg_symbols: Optional[set] = None
    for term in _flatten_top_terms(leg):
        if isinstance(term, ast.Compare):
            # Symbol gates inside a compound OR leg constrain THAT leg
            # to the gated symbols. Capture them here so the synthetic
            # subcond carries a per-leg filter; otherwise the price/
            # indicator mask would evaluate against every DataFrame and
            # an unrelated symbol could appear to satisfy the leg.
            sym = _symbol_gate(term, name_strings, bar_name)
            if sym is not None:
                if leg_symbols is None:
                    leg_symbols = set(sym)
                else:
                    # Same intra-predicate intersection rule as the
                    # AND path: ``bar.symbol == "X" and bar.symbol == "Y"``
                    # is unreachable.
                    leg_symbols &= sym
                continue
            sub = _build_subcond(term, name_periods, name_evaluators)
        else:
            sub = _build_truthy_subcond(term, name_periods, name_evaluators)
        if sub is not None:
            inner.append(sub)
        else:
            # An unmodellable conjunct (e.g. ``self.custom_ok(bar)`` or
            # an unsupported expression) means we can't soundly compute
            # the AND mask — the recognised conjuncts' AND is a
            # superset of the real mask. Decline so the parent OR
            # treats this leg as unknown rather than reporting it as
            # firing whenever the recognised half fires.
            return None
    target_symbols = frozenset(leg_symbols) if leg_symbols is not None else None
    # Empty intersection means the leg's symbol filter is unsatisfiable
    # — drop it like the existing _process_if path does for AND groups.
    if target_symbols is not None and not target_symbols:
        return None
    if not inner:
        return None
    if len(inner) == 1 and target_symbols is None:
        # Only one recognisable conjunct and no per-leg filter — emit
        # it directly so the report row reflects the actual AST node
        # rather than wrapping a single mask in a redundant compound
        # layer.
        return inner[0]

    label = _format_compound_label(leg)
    inner_fns = [s.evaluate for s in inner]

    def _eval_compound(df: pd.DataFrame) -> pd.Series:
        masks: List[pd.Series] = []
        for fn in inner_fns:
            try:
                series = fn(df)
            except Exception:  # noqa: BLE001
                series = pd.Series(False, index=df.index, dtype=bool)
            masks.append(pd.Series(series, index=df.index).fillna(False).astype(bool))
        if not masks:
            return pd.Series(True, index=df.index, dtype=bool)
        result = masks[0]
        for m in masks[1:]:
            result = result & m
        return result

    return _Subcond(label=label, evaluate=_eval_compound, target_symbols=target_symbols)


def _build_compound_or_subcond(
    leg: ast.BoolOp,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
    name_strings: Optional["_NameStrings"] = None,
    bar_name: str = "bar",
) -> Optional[_Subcond]:
    """Build a single subcond for an ``or``-disjunction inside a larger
    AND predicate.

    For predicates like ``if close > 0 and (volume < 0 or close < -1):``
    the inner ``BoolOp(Or, ...)`` is one term of the outer AND, but it
    isn't a Compare or a truthiness expression — without explicit
    handling it gets dropped and the AND classification is based on
    only the surviving Compare conjuncts. Build an outer ``_Subcond``
    whose evaluator is the bar-wise OR of each inner leg's mask. The
    leg can itself be a ``Compare``, a ``BoolOp(And)`` (delegated to
    the existing AND compound builder), or a truthiness term.

    Returns ``None`` when no inner leg is recognisable.
    """
    inner: List[_Subcond] = []
    has_unknown_leg = False
    for term in leg.values:
        if isinstance(term, ast.Compare):
            # ``bar.symbol == "X"`` as a standalone OR leg is a symbol
            # allowlist — the leg is true exactly on bars from "X".
            # Without this branch ``_build_subcond`` rejects the gate
            # (no data-dependent operand), the leg is dropped, and a
            # surrounding AND like
            # ``(bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100``
            # collapses to just ``close > 100`` evaluated against every
            # symbol — an unrelated symbol then satisfies the predicate
            # and the probe falsely reports COVERAGE_OK.
            sym = _symbol_gate(term, name_strings, bar_name)
            if sym is not None:
                inner.append(
                    _Subcond(
                        label=_format_label(term),
                        evaluate=_always_true,
                        target_symbols=frozenset(sym),
                    )
                )
                continue
            sub = _build_subcond(term, name_periods, name_evaluators)
        elif isinstance(term, ast.BoolOp) and isinstance(term.op, ast.And):
            sub = _build_compound_and_subcond(
                term, name_periods, name_evaluators, name_strings, bar_name
            )
        else:
            sub = _build_truthy_subcond(term, name_periods, name_evaluators)
        if sub is not None:
            inner.append(sub)
        else:
            # Track un-modellable legs (e.g. ``self.custom_ok(bar)`` —
            # custom method calls). The synthesised OR-compound subcond
            # carries a ``has_unknown_leg`` flag so the parent AND
            # group's zero-hit blocker rule can suppress false
            # positives when the recognised legs zero out but an
            # unknown alternative may still make the OR fire.
            has_unknown_leg = True
    if not inner:
        return None
    if len(inner) == 1 and not has_unknown_leg:
        return inner[0]

    label = _format_compound_label(leg)
    inner_legs: Tuple[_Subcond, ...] = tuple(inner)

    def _eval_or(df: pd.DataFrame) -> pd.Series:
        # Symbol-blind fallback: used outside the aggregator (e.g. unit
        # tests that call ``sub.evaluate(df)`` directly). The aggregator
        # prefers ``or_legs`` so per-leg ``target_symbols`` are honoured;
        # this path simply ORs every leg's mask.
        masks: List[pd.Series] = []
        for leg_sub in inner_legs:
            try:
                series = leg_sub.evaluate(df)
            except Exception:  # noqa: BLE001
                series = pd.Series(False, index=df.index, dtype=bool)
            masks.append(pd.Series(series, index=df.index).fillna(False).astype(bool))
        if not masks:
            return pd.Series(False, index=df.index, dtype=bool)
        result = masks[0]
        for m in masks[1:]:
            result = result | m
        return result

    # Outer target_symbols: when every leg is symbol-gated, the OR can
    # only fire on bars from the union of those gates. Restricting the
    # outer subcond keeps the per-row hit-rate denominator aligned with
    # the bars that could have contributed. If any leg is unconstrained,
    # the OR can fire on any symbol so the outer gate stays ``None``.
    leg_filters = [leg_sub.target_symbols for leg_sub in inner_legs]
    if leg_filters and all(f is not None for f in leg_filters):
        union: frozenset = frozenset()
        for f in leg_filters:
            union = union | f
        outer_target: Optional[frozenset] = union if union else None
    else:
        outer_target = None

    return _Subcond(
        label=label,
        evaluate=_eval_or,
        target_symbols=outer_target,
        or_legs=inner_legs,
        has_unknown_leg=has_unknown_leg,
    )


def _always_true(df: pd.DataFrame) -> pd.Series:
    """Mask that is True on every bar of ``df``.

    Used as the evaluator for an OR leg whose only content is a
    ``bar.symbol == "X"`` gate: the gate's ``target_symbols`` already
    constrains the leg to firing on the matching symbol's bars, so
    within those bars the leg is unconditionally true.
    """
    return pd.Series(True, index=df.index, dtype=bool)


def _format_compound_label(node: ast.expr) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        text = "<compound>"
    text = text.strip()
    if len(text) > _MAX_LABEL_LEN:
        text = text[: _MAX_LABEL_LEN - 1] + "…"
    return text


def _format_label(node: ast.Compare) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        text = "<expr>"
    text = text.strip()
    if len(text) > _MAX_LABEL_LEN:
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
        if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
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
        if not isinstance(node.value, ast.Call):
            return None
        slc = node.slice
        if not (
            isinstance(slc, ast.Constant)
            and isinstance(slc.value, int)
            and not isinstance(slc.value, bool)
        ):
            return None
        call = node.value
        idx: Optional[int] = slc.value
    elif isinstance(node, ast.Call):
        call = node
        idx = None
    else:
        return None

    func_name = _func_name(call.func)
    if func_name is None:
        return None
    spec = INDICATORS.get(func_name)
    if spec is None:
        return None

    is_tuple_call = idx is not None
    if is_tuple_call != (spec.tuple_arity is not None):
        # ``sma(close, 20)[0]`` (single-Series subscripted) and
        # ``macd(close, 12, 26, 9)`` (tuple bare-called) are both
        # rejected — we'd be guessing the user's intent.
        return None
    if is_tuple_call and not (0 <= idx < spec.tuple_arity):
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
        if i >= len(spec.kwarg_names):
            return False
        slot_name = spec.kwarg_names[i]
        if not _is_valid_scalar(value, slot_name in float_slots):
            return False
    for name, value in extra_kwargs.items():
        if not _is_valid_scalar(value, name in float_slots):
            return False
    return True


def _is_valid_scalar(value: Union[int, float], allow_float: bool) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if v <= 0:
        return False
    if allow_float:
        return True
    return v.is_integer()


def _collect_name_evaluators(
    on_bar: ast.AST, name_periods: Dict[str, int]
) -> Dict[str, Callable[[pd.DataFrame], pd.Series]]:
    """Bind local ``Name = <expr>`` assignments inside ``on_bar`` whose RHS
    resolves to a data-dependent operand.

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
    if not isinstance(value, ast.Call):
        return
    func_name = _func_name(value.func)
    spec = INDICATORS.get(func_name) if func_name else None
    if spec is None or spec.tuple_arity is None:
        return
    if not elements:
        return
    if len(elements) > spec.tuple_arity:
        # Unpacking would TypeError at runtime — don't bind anything.
        return

    extra_pos = _trailing_numeric_args(value, name_periods, start_index=len(spec.data_inputs))
    if extra_pos is None:
        # Unpacked tuple-indicator with an unresolved positional config
        # (e.g. ``upper, _, _ = bollinger_bands(close, PERIOD + 1)``).
        # Don't bind anything — downstream lookups fall through and the
        # comparison gets dropped rather than evaluating against a
        # different indicator from the runtime.
        return
    extra_kwargs = _resolve_known_kwargs(value, name_periods, spec.kwarg_names)
    if extra_kwargs is None:
        # Same guard for unresolvable known kwargs in the unpack form.
        return
    if not _validate_scalar_args(spec, extra_pos, extra_kwargs):
        # ``upper, _, _ = bollinger_bands(close, 0)`` — same decline
        # rule as the indicator-call dispatcher; without it the bound
        # name would later evaluate to all-NaN and the comparison
        # would be misclassified as a zero-hit filter.
        return

    resolved_inputs: List[Callable[[pd.DataFrame], pd.Series]] = []
    for slot_idx, kind in enumerate(spec.data_inputs):
        if kind == "series":
            resolved = _resolve_series_input(value, bindings)
        else:
            # HLC slot for ``stochastic``: honour explicit positional
            # series args the same way ``_indicator_call`` does so
            # ``k, d = stochastic(low, low, close, 3)`` declines rather
            # than silently probing the default high/low/close columns.
            resolved = _positional_series_input(value, slot_idx, kind, bindings)
        if resolved is None:
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
    if (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "bool"
        and len(inner.args) == 1
        and not inner.keywords
    ):
        inner = inner.args[0]

    if isinstance(inner, ast.Compare):
        sub = _build_subcond(inner, name_periods, bindings)
        if sub is not None:
            return sub.evaluate
    return None


def _func_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id.lower()
    if isinstance(func, ast.Attribute):
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
            return pd.Series(float("nan"), index=df.index)

        return _default

    arg = call.args[positional_index]
    column = _column_from(arg)
    if column is not None:

        def _from_column(df: pd.DataFrame, c: str = column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(float("nan"), index=df.index)

        return _from_column

    if isinstance(arg, ast.Name) and name_evaluators is not None:
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

    if arg0 is None:
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
            return pd.Series(float("nan"), index=df.index)

        return _from_column

    if isinstance(arg0, ast.Name) and name_evaluators is not None:
        evaluator = name_evaluators.get(arg0.id)
        if evaluator is not None:

            def _from_binding(df: pd.DataFrame, ev=evaluator) -> pd.Series:
                return ev(df).astype(float)

            return _from_binding

    return None
