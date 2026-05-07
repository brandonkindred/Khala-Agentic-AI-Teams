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
from typing import Callable, Dict, List, Optional, Union

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
    """

    subconds: List[_Subcond]
    target_symbols: Optional[set]


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

    if warmup_bars_required > 0 and bars_checked < warmup_bars_required:
        return CoverageReport(
            coverage_category=CoverageCategory.INSUFFICIENT_BARS,
            summary=(
                f"insufficient bars: {bars_checked} available, {warmup_bars_required} required"
            ),
            likely_blockers=[
                LikelyBlocker(
                    reason="insufficient_bars",
                    evidence=f"bars_checked={bars_checked} < warmup={warmup_bars_required}",
                )
            ],
            **base_kwargs,
        )

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

    # Deduplicate the SubconditionCoverage list by (label, target_symbols)
    # so symbol-gated duplicates stay distinct — same predicate text
    # under two different ``bar.symbol == "X"`` branches must surface as
    # two coverage rows so a per-symbol zero-hit blocker is visible.
    subcoverages: List[SubconditionCoverage] = []
    seen_keys: set = set()
    for sub, syms, hits, last in zip(
        flat_subconds, flat_subcond_symbols, sub_hit_counts, sub_last_true
    ):
        key = (sub.label, syms)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # Per-row denominator: a symbol-gated row's hits only come from
        # bars in its target_symbols, so dividing by the global total
        # would understate the per-symbol coverage rate. Restrict the
        # denominator to the bars that could have contributed.
        if syms is not None:
            denom = sum(per_symbol_bars.get(s, 0) for s in syms)
        else:
            denom = total_eval_bars
        rate = (hits / denom) if denom > 0 else 0.0
        # Augment the rendered label with the symbol filter so the
        # report distinguishes symbol-gated duplicates without growing
        # the model schema.
        label = sub.label
        if syms is not None and syms:
            label = f"{label} [{','.join(sorted(syms))}]"
        subcoverages.append(
            SubconditionCoverage(
                label=label,
                hit_count=hits,
                hit_rate=min(max(rate, 0.0), 1.0),
                last_true_bar=last,
            )
        )

    zero_hits = [sc for sc in subcoverages if sc.hit_count == 0]
    blockers: List[LikelyBlocker] = []
    if zero_hits:
        category = CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
        summary = f"{len(zero_hits)} of {len(subcoverages)} indicator subconditions never fired"
        for sc in zero_hits:
            blockers.append(
                LikelyBlocker(
                    reason="indicator_filter_zero_hits",
                    evidence=sc.label,
                    hit_rate=0.0,
                )
            )
        return CoverageReport(
            coverage_category=category,
            summary=summary,
            subconditions=subcoverages,
            likely_blockers=blockers[:_MAX_LIKELY_BLOCKERS],
            **base_kwargs,
        )

    # Find any single ``if`` predicate whose legs all fire individually
    # but whose bar-wise AND is empty. We only flag CONJUNCTION_NEVER_TRUE
    # for a real per-predicate contradiction — never across unrelated
    # ``if`` branches.
    empty_conj_group: Optional[_Group] = None
    base = 0
    for group_idx, group in enumerate(groups):
        legs = len(group.subconds)
        if (
            legs >= 2
            and group_evaluated[group_idx]
            and group_conjunction_hits[group_idx] == 0
            and all(sub_hit_counts[base + k] > 0 for k in range(legs))
        ):
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
    name_periods = _collect_name_periods(tree)
    on_bar = _find_on_bar(tree)
    if on_bar is None:
        return []

    # Pre-pass: bind any local Name to its computed indicator. Strategies
    # following the standard template (see prompts/ideation_system.md)
    # write ``sma_var = sma(close, 200)`` then ``if bar.close > sma_var``
    # — the comparison's RHS is a Name, not a Call, so without this pass
    # the subcondition is dropped.
    name_evaluators = _collect_name_evaluators(on_bar, name_periods)

    groups: List[_Group] = []
    state = {"total": 0}

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

    def _visit(
        stmts: List[ast.stmt],
        ancestors: List[_Subcond],
        ancestor_symbols: Optional[set],
    ) -> bool:
        for stmt in stmts:
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

                if not _process_if(stmt.test, stmt.body, stmt.orelse, ancestors, ancestor_symbols):
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


def _collect_name_periods(tree: ast.AST) -> Dict[str, int]:
    """Bind ``NAME = <int>`` for later ``Name`` / ``self.NAME`` resolution.

    Walks every ``Assign`` / ``AnnAssign`` whose target is either:

    - a bare ``Name`` (module-level ``WINDOW = 80`` or class attribute
      ``WINDOW = 80``), or
    - an ``Attribute`` of the form ``self.WINDOW = 80`` (typically inside
      ``__init__``) — only the attr name is recorded.

    Strategies generated from the standard ideation prompt encourage
    class tuning knobs and ``self.WINDOW`` access; without this both the
    AST walk and downstream lookup would miss the binding entirely.
    """
    bindings: Dict[str, int] = {}

    def _record(target: ast.expr, value: ast.expr) -> None:
        # Reuse the same numeric-literal extractor used downstream so
        # negative ints and unary-minus constants resolve consistently.
        v = _numeric_literal(value, bindings)
        if v is None or float(v) <= 0 or not float(v).is_integer():
            return
        ivalue = int(v)
        if isinstance(target, ast.Name):
            bindings.setdefault(target.id, ivalue)
        elif isinstance(target, ast.Attribute):
            # ``self.WINDOW`` (or any other instance attribute) — record
            # by attribute name so a later ``self.WINDOW`` reference
            # resolves through _numeric_literal's Attribute branch.
            bindings.setdefault(target.attr, ivalue)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _record(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _record(node.target, node.value)
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

    indicator_fn = _indicator_call(node, name_periods)
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
    node: ast.expr, name_periods: Dict[str, int]
) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    # Tuple-returning helpers are only recognised inside a Subscript with
    # a constant integer index — without one we can't tell which leg the
    # user meant to compare against.
    if isinstance(node, ast.Subscript):
        return _tuple_indicator_subscript(node, name_periods)

    if not isinstance(node, ast.Call):
        return None
    func_name = _func_name(node.func)
    if func_name is None:
        return None

    if func_name in _SERIES_INDICATORS:
        helper = _SERIES_INDICATORS[func_name]
        column = _series_arg_column(node)
        if column is None:
            # Explicit but unrecognised input (e.g. a custom series).
            # Drop rather than substitute close — see _series_arg_column.
            return None
        # Series helpers: rsi(series, period), sma(series, period), ...
        period = _resolve_period_arg(node, name_periods, positional_index=1)

        def _eval_series(df: pd.DataFrame) -> pd.Series:
            if column not in df.columns:
                return pd.Series(float("nan"), index=df.index)
            if period is not None:
                return helper(df[column].astype(float), int(period))
            return helper(df[column].astype(float))

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
    node: ast.Subscript, name_periods: Dict[str, int]
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
        column = _series_arg_column(call)
        if column is None:
            return None

        def _eval_tuple_series(df: pd.DataFrame) -> pd.Series:
            if column not in df.columns:
                return pd.Series(float("nan"), index=df.index)
            return helper(df[column].astype(float), *extra_pos, **extra_kwargs)[idx]

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
    for node in ast.walk(on_bar):
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
        operand = _build_operand(value, name_periods, bindings)
        if operand is None or not operand.data_dependent:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, operand.fn)
    return bindings


def _func_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id.lower()
    if isinstance(func, ast.Attribute):
        return func.attr.lower()
    return None


def _series_arg_column(call: ast.Call) -> Optional[str]:
    """Pick the source column for a single-series indicator call.

    A bare call like ``sma()`` falls back to ``close`` (rare, but
    harmless: no other column is implied). When the strategy passes an
    *explicit* argument that we can't pin to an OHLCV column —
    ``rsi(self.history)``, ``sma(custom_series)``, etc. — return
    ``None`` so the caller drops the indicator entirely. Silently
    substituting ``close`` would mis-evaluate volume/OHLC filters and
    produce false COVERAGE_OK reports.
    """
    if not call.args:
        return "close"
    return _column_from(call.args[0])


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
    """
    for kw in call.keywords:
        if kw.arg in {"period", "length", "window", "n"}:
            value = _numeric_literal(kw.value, name_periods)
            if value is not None and value > 0:
                return int(value)
    if len(call.args) > positional_index:
        value = _numeric_literal(call.args[positional_index], name_periods)
        if value is not None and value > 0:
            return int(value)
    return None
