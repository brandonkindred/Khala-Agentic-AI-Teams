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
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

import pandas as pd

from investment_team.models import (
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
)
from investment_team.strategy_lab.executor import indicators as _ind

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

# Single-series indicator helpers (take a Series + optional period).
_SERIES_INDICATORS: Dict[str, Callable[..., pd.Series]] = {
    "sma": _ind.sma,
    "ema": _ind.ema,
    "rsi": _ind.rsi,
}

# Indicators that take (high, low, close[, ...]) and return a Series.
_HLC_INDICATORS: Dict[str, Callable[..., pd.Series]] = {
    "atr": _ind.atr,
    "adx": _ind.adx,
}

# Indicators that take (high, low, close, volume) and return a Series.
_OHLCV_INDICATORS: Dict[str, Callable[..., pd.Series]] = {
    "vwap": _ind.vwap,
}

# Tuple-returning helpers (one Series per element). We only recognise
# them inside a Subscript with a constant integer slice — bare calls are
# ambiguous because the user hasn't picked which leg to compare.
# Each entry: (signature_kind, helper, max_idx, kwarg_names).
#   signature_kind: "series" → helper(series, *period_args)
#                   "hlc"    → helper(high, low, close, *period_args)
#   kwarg_names: the kwarg labels the helper accepts after its data
#                inputs, in declared order. Used to forward strategy-
#                provided kwargs (e.g. ``bollinger_bands(close, num_std=0.1)``)
#                so probe results match the strategy's actual thresholds.
_TUPLE_INDICATORS: Dict[str, tuple] = {
    "macd": ("series", _ind.macd, 3, ("fast", "slow", "signal")),
    "bollinger_bands": ("series", _ind.bollinger_bands, 3, ("period", "num_std")),
    "stochastic": ("hlc", _ind.stochastic, 2, ("k_period", "d_period")),
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
    target_symbols = _union_target_symbols(subconds)
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
                # nested OR predicate.
                if group.combinator == "or" and group.ancestor_count > 0:
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
        if any(group.subconds[k].has_unknown_leg for k in range(legs)):
            # Unknown alternative present — can't prove the predicate
            # is unreachable. Suppress the blocker (mirrors the OR
            # zero-hit suppression elsewhere).
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
    # Outer-scope (module / strategy class / __init__) period bindings
    # only. Function-local ``WINDOW = 5`` shadowing (and all
    # ``Name = <indicator>`` bindings inside on_bar) are now applied
    # **flow-sensitively** in :func:`_visit` so a later reassignment
    # can't shadow a predicate that lexically precedes it.
    # ``strategy_class`` confines the outer-scope walk to the strategy's
    # own ``ClassDef`` so a sibling helper class can't pre-empt the
    # strategy's bare-name attribute bindings.
    strategy_class = _find_strategy_class(tree, on_bar)
    name_periods = _collect_name_periods(tree, function_node=None, strategy_class=strategy_class)
    # Local name → indicator evaluator bindings start empty. The
    # walker fills them as it encounters assignments in source order
    # and only the bindings established before a given predicate are
    # visible to that predicate.
    name_evaluators: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}

    groups: List[_Group] = []
    state = {"total": 0}

    def _apply_assign_inplace(stmt: ast.stmt) -> None:
        """Update name_evaluators / name_periods from a single assignment.

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
                _bind_tuple_unpack(target, value, name_periods, name_evaluators)
                continue
            if isinstance(target, ast.Name):
                evaluator = _resolve_assign_evaluator(value, name_periods, name_evaluators)
                if evaluator is not None:
                    name_evaluators[target.id] = evaluator
                else:
                    # RHS is a scalar / unsupported call — drop any
                    # prior indicator binding so downstream lookups
                    # fall through to numeric-literal / OHLCV
                    # resolution.
                    name_evaluators.pop(target.id, None)
                # Numeric-scalar side: record any numeric value
                # (including zero and negatives), preserving int-ness
                # when the value is integer-valued so period-use sites
                # stay clean. Non-integer floats and zero/negative
                # thresholds must also be preserved here:
                # ``_build_operand`` resolves ``Name`` literals through
                # this dict, so without it ``ZERO_LINE = 0; if
                # macd(close)[0] > ZERO_LINE:`` and similar predicates
                # would be dropped and the probe would degenerate to
                # ``UNKNOWN_LOW_COVERAGE``. Period-use sites
                # (:func:`_resolve_period_arg`) re-validate
                # ``> 0`` and ``is_integer()`` so a non-positive or
                # float threshold is never misapplied as an indicator
                # window.
                v = _numeric_literal(value, name_periods)
                if v is not None:
                    name_periods[target.id] = int(v) if float(v).is_integer() else float(v)
            elif isinstance(target, ast.Attribute):
                # ``self.WINDOW = N`` — record by attribute name.
                v = _numeric_literal(value, name_periods)
                if v is not None:
                    name_periods[target.attr] = int(v) if float(v).is_integer() else float(v)

    def _budgeted_extend(group_subs: List[_Subcond], extras: List[_Subcond]) -> bool:
        """Append extras into group within the global subcond budget.

        Returns False when the global cap is hit (caller should stop).
        """
        for sub in extras:
            if state["total"] >= _MAX_SUBCONDITIONS:
                return False
            group_subs.append(sub)
            state["total"] += 1
        return True

    def _process_if(
        test: ast.expr,
        body: List[ast.stmt],
        orelse: List[ast.stmt],
        ancestors: List[_Subcond],
        ancestor_symbols: Optional[set],
    ) -> bool:
        """Process a single if-shape (test + body + orelse) given an
        ancestor stack. Used both for real ``ast.If`` statements and for
        synthesised ifs after stripping a position-gate conjunct.
        """
        # Top-level OR predicate: each leg becomes an independent
        # subcondition row but the group's blocker classification uses
        # disjunction (only too-restrictive when ALL legs are zero).
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return _process_or_if(test, body, orelse, ancestors, ancestor_symbols)

        own_subs: List[_Subcond] = []
        own_symbols: Optional[set] = None
        for term in _flatten_top_terms(test):
            if isinstance(term, ast.Compare):
                sym = _symbol_gate(term)
                if sym is not None:
                    # Multiple ``bar.symbol == X`` gates within a single
                    # ``and`` are conjoined, so a second different literal
                    # *contradicts* the first — they must be intersected,
                    # not unioned. ``bar.symbol == "AAPL" and
                    # bar.symbol == "MSFT"`` collapses to an empty filter,
                    # which downstream drops as unreachable.
                    if own_symbols is None:
                        own_symbols = {sym}
                    else:
                        own_symbols &= {sym}
                    continue
                sub = _build_subcond(term, name_periods, name_evaluators)
                if sub is not None:
                    own_subs.append(sub)
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
                or_compound = _build_compound_or_subcond(term, name_periods, name_evaluators)
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
                continue
            # Truthiness term — ``bool(x)`` or a bare ``Name`` referencing
            # a precomputed indicator. Required for the ideation/codegen
            # shape ``_entry = sma(close, 200) > bar.close`` followed by
            # ``if pos is None and bool(_entry):``. When ``Name`` doesn't
            # resolve to a recognised indicator helper (e.g. compiler-
            # emitted ``self._n_X`` factor methods), we leave the term
            # unhandled rather than silently treating it as always-true.
            truthy = _build_truthy_subcond(term, name_periods, name_evaluators)
            if truthy is not None:
                own_subs.append(truthy)

        effective_symbols = _intersect_symbols(ancestor_symbols, own_symbols)

        group_subs: List[_Subcond] = []
        if not _budgeted_extend(group_subs, ancestors):
            if group_subs:
                groups.append(_Group(subconds=group_subs, target_symbols=effective_symbols))
            return False
        if not _budgeted_extend(group_subs, own_subs):
            if group_subs:
                groups.append(_Group(subconds=group_subs, target_symbols=effective_symbols))
            return False
        if group_subs and not (effective_symbols is not None and not effective_symbols):
            groups.append(_Group(subconds=group_subs, target_symbols=effective_symbols))
        if not _visit(body, ancestors + own_subs, effective_symbols):
            return False
        if not _visit(orelse, ancestors, ancestor_symbols):
            return False
        return True

    def _process_or_if(
        test: ast.BoolOp,
        body: List[ast.stmt],
        orelse: List[ast.stmt],
        ancestors: List[_Subcond],
        ancestor_symbols: Optional[set],
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
                sym = _symbol_gate(leg)
                if sym is not None:
                    own_subs.append(
                        _Subcond(
                            label=_format_label(leg),
                            evaluate=_always_true,
                            target_symbols=frozenset({sym}),
                        )
                    )
                    continue
                sub = _build_subcond(leg, name_periods, name_evaluators)
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
                compound = _build_compound_and_subcond(leg, name_periods, name_evaluators)
                if compound is not None:
                    own_subs.append(compound)
                else:
                    has_unknown_leg = True
                continue
            truthy = _build_truthy_subcond(leg, name_periods, name_evaluators)
            if truthy is not None:
                own_subs.append(truthy)
            else:
                has_unknown_leg = True

        if not own_subs:
            # No recognised legs — fall through to body / orelse without
            # emitting a group, so nested ``if`` analysis still runs.
            if not _visit(body, ancestors, ancestor_symbols):
                return False
            if not _visit(orelse, ancestors, ancestor_symbols):
                return False
            return True

        # Ancestors stay AND-required; OR legs are alternatives. We
        # carry both in one group with combinator="or" plus an
        # ``ancestor_count`` so _aggregate knows where the AND-tail
        # ends and the OR-leg head begins. (See _Group docstring for
        # the per-position rule.)
        group_subs: List[_Subcond] = []
        if not _budgeted_extend(group_subs, ancestors):
            if group_subs:
                groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=ancestor_symbols,
                        combinator="and",
                        ancestor_count=len(group_subs),
                    )
                )
            return False
        ancestor_count = len(group_subs)
        if not _budgeted_extend(group_subs, own_subs):
            if group_subs:
                groups.append(
                    _Group(
                        subconds=group_subs,
                        target_symbols=ancestor_symbols,
                        combinator="or",
                        ancestor_count=ancestor_count,
                        has_unknown_or_leg=has_unknown_leg,
                    )
                )
            return False
        if group_subs:
            groups.append(
                _Group(
                    subconds=group_subs,
                    target_symbols=ancestor_symbols,
                    combinator="or",
                    ancestor_count=ancestor_count,
                    has_unknown_or_leg=has_unknown_leg,
                )
            )
        # Body sees no extra ancestors — see docstring.
        if not _visit(body, ancestors, ancestor_symbols):
            return False
        if not _visit(orelse, ancestors, ancestor_symbols):
            return False
        return True

    def _visit(
        stmts: List[ast.stmt],
        ancestors: List[_Subcond],
        ancestor_symbols: Optional[set],
    ) -> bool:
        # Transactional: snapshot at entry, restore at exit. Each call
        # to _visit (including the recursive descents from _process_if /
        # _process_or_if into body / orelse) leaves the caller's
        # ``name_evaluators`` and ``name_periods`` unchanged, while
        # mutations persist across sibling statements within this
        # for-loop. This gives flow-sensitivity without leaking branch-
        # internal reassignments to siblings or parents.
        saved_evals = dict(name_evaluators)
        saved_periods = dict(name_periods)
        try:
            for stmt in stmts:
                # Apply assignments in source order so each predicate
                # sees only the bindings established by lexically
                # preceding statements. Without this a later
                # reassignment leaks back to earlier predicates via the
                # shared dicts.
                if isinstance(stmt, ast.Assign) or isinstance(stmt, ast.AnnAssign):
                    _apply_assign_inplace(stmt)
                    continue

                if isinstance(stmt, ast.If):
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
                            if not _visit(stmt.body, ancestors, ancestor_symbols):
                                return False
                        else:
                            if not _process_if(
                                gate_residual,
                                stmt.body,
                                [],
                                ancestors,
                                ancestor_symbols,
                            ):
                                return False
                        continue
                    if position_check == "occupied":  # pos is not None — orelse is entry
                        if not _visit(stmt.orelse, ancestors, ancestor_symbols):
                            return False
                        continue

                    if not _process_if(
                        stmt.test, stmt.body, stmt.orelse, ancestors, ancestor_symbols
                    ):
                        return False
                else:
                    # Descend into compound statements (For, While, With,
                    # Try, FunctionDef body) but pass through ancestors so
                    # ``for x in ...: if close > 100: ...`` still inherits
                    # nothing, which is correct.
                    for field in _BLOCK_FIELDS:
                        inner = getattr(stmt, field, None)
                        if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                            if not _visit(inner, ancestors, ancestor_symbols):
                                return False
                    # ast.Try has handlers; each handler.body is a stmt list.
                    handlers = getattr(stmt, "handlers", None)
                    if isinstance(handlers, list):
                        for h in handlers:
                            h_body = getattr(h, "body", None)
                            if isinstance(h_body, list) and h_body:
                                if not _visit(h_body, ancestors, ancestor_symbols):
                                    return False
            return True
        finally:
            name_evaluators.clear()
            name_evaluators.update(saved_evals)
            name_periods.clear()
            name_periods.update(saved_periods)

    body = getattr(on_bar, "body", None)
    if isinstance(body, list):
        _visit(body, [], None)
    return groups


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


def _symbol_gate(node: ast.Compare) -> Optional[str]:
    """Detect ``bar.symbol == "X"`` (or ``"X" == bar.symbol``).

    Returns the literal symbol when matched; ``None`` otherwise. Used to
    constrain a group's evaluation to the matching symbol's DataFrame
    rather than evaluating against every symbol in the universe.
    """
    if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.Is)):
        return None
    left, right = node.left, node.comparators[0]

    def _is_bar_symbol(n: ast.expr) -> bool:
        return (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "bar"
            and n.attr == "symbol"
        )

    def _string_const(n: ast.expr) -> Optional[str]:
        return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None

    if _is_bar_symbol(left):
        sym = _string_const(right)
        return sym
    if _is_bar_symbol(right):
        sym = _string_const(left)
        return sym
    return None


def _union_target_symbols(groups: List[_Group]) -> Optional[set]:
    """Return the union of symbols any group could possibly fire on, or ``None``.

    Used by :func:`run_indicator_probe` to size the warmup check to the
    symbols that can actually satisfy a predicate. Returns ``None`` when
    at least one group is **fully unconstrained** — i.e. neither the
    group-level filter nor any required subcond filter narrows the
    symbol space — so the warmup check stays over every fetched
    DataFrame.

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
    """
    union: set = set()
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
                # ``group_syms`` above.
                if group_syms is None:
                    return None
            else:
                for t in or_tail:
                    if t.target_symbols:
                        if group_syms is None:
                            group_syms = set(t.target_symbols)
                        else:
                            group_syms.update(t.target_symbols)

        if group_syms is None:
            # Fully unconstrained group — predicate could fire on any
            # fetched symbol. Treat the warmup as universal.
            return None
        union.update(group_syms)
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
        """Yield Assign / AnnAssign nodes lexically in scope for the strategy.

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
        previous behaviour incorrectly recorded function locals).
        """
        _CONSTRUCTOR_NAMES = {"__init__", "__post_init__"}
        if strategy_class is not None and isinstance(node, ast.ClassDef):
            if node is not strategy_class:
                return
            # Walk class-body Assign / AnnAssign directly. For
            # FunctionDef children, only descend into the constructor.
            # Nested ClassDefs follow the strategy_class skip rule via
            # the recursive call.
            for child in node.body:
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    yield child
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in _CONSTRUCTOR_NAMES:
                        for sub in ast.walk(child):
                            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                                yield sub
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
            yield node
        for child in ast.iter_child_nodes(node):
            yield from _iter_outer_assigns(child)

    # Pass 1: outer-scope assignments (module / strategy class /
    # __init__) using ``setdefault`` so cross-scope class constants
    # stay isolated. Sibling helper classes are skipped.
    for node in _iter_outer_assigns(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record(target, node.value, overwrite=False)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record(node.target, node.value, overwrite=False)

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
            sym = _symbol_gate(term)
            if sym is not None:
                if leg_symbols is None:
                    leg_symbols = {sym}
                else:
                    # Same intra-predicate intersection rule as the
                    # AND path: ``bar.symbol == "X" and bar.symbol == "Y"``
                    # is unreachable.
                    leg_symbols &= {sym}
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
            sym = _symbol_gate(term)
            if sym is not None:
                inner.append(
                    _Subcond(
                        label=_format_label(term),
                        evaluate=_always_true,
                        target_symbols=frozenset({sym}),
                    )
                )
                continue
            sub = _build_subcond(term, name_periods, name_evaluators)
        elif isinstance(term, ast.BoolOp) and isinstance(term.op, ast.And):
            sub = _build_compound_and_subcond(term, name_periods, name_evaluators)
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
    column = _column_from(node)
    if column is not None:

        def _col(df: pd.DataFrame, c: str = column) -> pd.Series:
            if c in df.columns:
                return df[c].astype(float)
            return pd.Series(float("nan"), index=df.index)

        return _Operand(fn=_col, data_dependent=True)

    # Resolve a Name to a previously-bound indicator-call evaluator
    # (e.g. ``sma_var = sma(close, 200)`` then ``if x > sma_var``).
    # This must be checked BEFORE _numeric_literal so a Name that refers
    # to a computed indicator isn't misinterpreted as a numeric literal.
    if isinstance(node, ast.Name) and name_evaluators is not None:
        evaluator = name_evaluators.get(node.id)
        if evaluator is not None:
            return _Operand(fn=evaluator, data_dependent=True)

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
    """Resolve a node to an OHLCV column name, if possible."""
    if isinstance(node, ast.Name) and node.id in _OHLCV_COLUMNS:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _OHLCV_COLUMNS:
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
    # Tuple-returning helpers are only recognised inside a Subscript with
    # a constant integer index — without one we can't tell which leg the
    # user meant to compare against.
    if isinstance(node, ast.Subscript):
        return _tuple_indicator_subscript(node, name_periods, name_evaluators)

    if not isinstance(node, ast.Call):
        return None
    func_name = _func_name(node.func)
    if func_name is None:
        return None

    if func_name in _SERIES_INDICATORS:
        helper = _SERIES_INDICATORS[func_name]
        series_input = _resolve_series_input(node, name_evaluators)
        if series_input is None:
            # Explicit but unrecognised input (e.g. a custom series).
            # Drop rather than substitute close — see _resolve_series_input.
            return None
        # Series helpers: rsi(series, period), sma(series, period), ...
        period = _resolve_period_arg(node, name_periods, positional_index=1)

        def _eval_series(df: pd.DataFrame) -> pd.Series:
            s = series_input(df)
            if period is not None:
                return helper(s, int(period))
            return helper(s)

        return _eval_series

    if func_name in _HLC_INDICATORS:
        helper = _HLC_INDICATORS[func_name]
        # HLC helpers: atr(high, low, close, period), adx(high, low, close, period).
        # The period is the 4th positional arg (index 3), not the 2nd.
        period = _resolve_period_arg(node, name_periods, positional_index=3)

        def _eval_hlc(df: pd.DataFrame) -> pd.Series:
            for col in ("high", "low", "close"):
                if col not in df.columns:
                    return pd.Series(float("nan"), index=df.index)
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            if period is not None:
                return helper(high, low, close, int(period))
            return helper(high, low, close)

        return _eval_hlc

    if func_name in _OHLCV_INDICATORS:
        helper = _OHLCV_INDICATORS[func_name]

        # vwap(high, low, close, volume) — no scalar period, just OHLCV inputs.
        def _eval_ohlcv(df: pd.DataFrame) -> pd.Series:
            for col in ("high", "low", "close", "volume"):
                if col not in df.columns:
                    return pd.Series(float("nan"), index=df.index)
            return helper(
                df["high"].astype(float),
                df["low"].astype(float),
                df["close"].astype(float),
                df["volume"].astype(float),
            )

        return _eval_ohlcv

    return None


def _tuple_indicator_subscript(
    node: ast.Subscript,
    name_periods: Dict[str, int],
    name_evaluators: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """Resolve ``bollinger_bands(close, 20)[0]`` and similar.

    Recognised only when the inner ``Call`` targets a tuple-returning
    helper and the slice is a constant non-negative integer within the
    helper's tuple arity.
    """
    if not isinstance(node.value, ast.Call):
        return None
    func_name = _func_name(node.value.func)
    if func_name is None or func_name not in _TUPLE_INDICATORS:
        return None
    slc = node.slice
    if not (
        isinstance(slc, ast.Constant)
        and isinstance(slc.value, int)
        and not isinstance(slc.value, bool)
    ):
        return None

    sig_kind, helper, max_idx, kwarg_names = _TUPLE_INDICATORS[func_name]
    idx = slc.value
    if idx < 0 or idx >= max_idx:
        return None

    call = node.value
    # ``positional_start`` is the AST arg index of the first scalar config
    # (period / num_std / fast / etc.) — i.e. one past the data inputs.
    positional_start = 1 if sig_kind == "series" else 3
    extra_pos = _trailing_numeric_args(call, name_periods, start_index=positional_start)
    extra_kwargs = _resolve_known_kwargs(call, name_periods, kwarg_names)

    if sig_kind == "series":
        series_input = _resolve_series_input(call, name_evaluators)
        if series_input is None:
            return None

        def _eval_tuple_series(df: pd.DataFrame) -> pd.Series:
            return helper(series_input(df), *extra_pos, **extra_kwargs)[idx]

        return _eval_tuple_series

    def _eval_tuple_hlc(df: pd.DataFrame) -> pd.Series:
        for col in ("high", "low", "close"):
            if col not in df.columns:
                return pd.Series(float("nan"), index=df.index)
        return helper(
            df["high"].astype(float),
            df["low"].astype(float),
            df["close"].astype(float),
            *extra_pos,
            **extra_kwargs,
        )[idx]

    return _eval_tuple_hlc


def _trailing_numeric_args(
    call: ast.Call,
    name_periods: Dict[str, int],
    *,
    start_index: int,
) -> List[Union[int, float]]:
    """Collect positional numeric args from ``start_index`` onwards.

    Stops at the first non-numeric positional — the user passed a
    Name/expression we can't safely interpret, and silently substituting
    a guess would mis-classify the strategy. Trailing numeric args after
    the data inputs (``num_std`` / ``slow`` / ``signal`` / etc.) are
    preserved in source order and int-ness is preserved so helpers like
    ``rolling(window=N)`` get an int rather than a float.
    """
    out: List[Union[int, float]] = []
    for i in range(start_index, len(call.args)):
        v = _numeric_literal(call.args[i], name_periods)
        if v is None:
            break
        out.append(int(v) if float(v).is_integer() else v)
    return out


def _resolve_known_kwargs(
    call: ast.Call,
    name_periods: Dict[str, int],
    known: tuple,
) -> Dict[str, Union[int, float]]:
    """Pick out keyword arguments the helper actually accepts.

    Unknown kwargs are dropped — passing them through would TypeError
    inside the helper. Numeric values preserve int-ness for the same
    reason as :func:`_trailing_numeric_args`.
    """
    out: Dict[str, Union[int, float]] = {}
    for kw in call.keywords:
        if kw.arg not in known:
            continue
        v = _numeric_literal(kw.value, name_periods)
        if v is None:
            continue
        out[kw.arg] = int(v) if float(v).is_integer() else v
    return out


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
    """
    if not isinstance(value, ast.Call):
        return
    func_name = _func_name(value.func)
    if func_name is None or func_name not in _TUPLE_INDICATORS:
        return
    elements = list(getattr(target, "elts", []))
    if not elements:
        return
    sig_kind, helper, max_idx, kwarg_names = _TUPLE_INDICATORS[func_name]
    if len(elements) > max_idx:
        # Unpacking would TypeError at runtime — don't bind anything.
        return
    positional_start = 1 if sig_kind == "series" else 3
    extra_pos = _trailing_numeric_args(value, name_periods, start_index=positional_start)
    extra_kwargs = _resolve_known_kwargs(value, name_periods, kwarg_names)

    if sig_kind == "series":
        series_input = _resolve_series_input(value, bindings)
        if series_input is None:
            return
        for idx, elem in enumerate(elements):
            if not isinstance(elem, ast.Name):
                continue

            def _make(idx=idx, helper=helper, sin=series_input, ep=extra_pos, ek=extra_kwargs):
                def _eval(df: pd.DataFrame) -> pd.Series:
                    return helper(sin(df), *ep, **ek)[idx]

                return _eval

            bindings[elem.id] = _make()
        return

    # sig_kind == "hlc"
    for idx, elem in enumerate(elements):
        if not isinstance(elem, ast.Name):
            continue

        def _make(idx=idx, helper=helper, ep=extra_pos, ek=extra_kwargs):
            def _eval(df: pd.DataFrame) -> pd.Series:
                for col in ("high", "low", "close"):
                    if col not in df.columns:
                        return pd.Series(float("nan"), index=df.index)
                return helper(
                    df["high"].astype(float),
                    df["low"].astype(float),
                    df["close"].astype(float),
                    *ep,
                    **ek,
                )[idx]

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


def _resolve_period_arg(
    call: ast.Call,
    name_periods: Dict[str, int],
    *,
    positional_index: int = 1,
) -> Optional[int]:
    """Pull the period (integer) from positional or kwarg form.

    ``positional_index`` is the index of the period argument in the
    helper's positional signature: 1 for series helpers like
    ``rsi(series, period)``, 3 for HLC helpers like
    ``atr(high, low, close, period)``.

    Periods must be positive integers — ``name_periods`` may now hold
    non-integer scalar bindings (e.g. ``limit = 100.5``) so threshold
    locals can resolve in operand-side comparisons. Validate
    ``is_integer()`` at lookup time so a float threshold is never
    silently truncated and applied as an indicator window.
    """
    for kw in call.keywords:
        if kw.arg in {"period", "length", "window", "n"}:
            value = _numeric_literal(kw.value, name_periods)
            if value is not None and value > 0 and float(value).is_integer():
                return int(value)
    if len(call.args) > positional_index:
        value = _numeric_literal(call.args[positional_index], name_periods)
        if value is not None and value > 0 and float(value).is_integer():
            return int(value)
    return None
