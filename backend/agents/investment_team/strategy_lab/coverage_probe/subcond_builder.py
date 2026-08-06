"""Subcondition/indicator-call construction helpers for the indicator-coverage probe.

Houses the free functions that compile AST comparison/boolean-operator
nodes into executable :class:`~investment_team.strategy_lab.coverage_probe.predicate_ir.Leg`
subconditions and resolve indicator-call expressions into ``df -> Series``
evaluators — extracted from
:mod:`investment_team.strategy_lab.coverage_probe.indicator_probe`
(Part 3 of the decomposition started by
:mod:`investment_team.strategy_lab.coverage_probe.subcondition_visitor`
in #1960 and continued by
:mod:`investment_team.strategy_lab.coverage_probe.predicate_resolution`
in #1973). Pure: no I/O, no LLM, no subprocess.

This module needs ``_AND_OPS``/``_CombinatorOps`` from ``indicator_probe``
and ``_flatten_top_terms``/``_symbol_gate``/``_NameStrings`` from
``predicate_resolution``, while
``predicate_resolution`` needs ``_numeric_literal`` back from here (and
``indicator_probe`` needs nothing from here). Both cross-imports are
placed as the **last top-level statements** in this file, after every
same-file definition they need, mirroring ``indicator_probe`` /
``predicate_resolution``'s own cross-import — this keeps the three-way
cycle safe no matter which of the three modules a caller imports first.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

import pandas as pd

from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    AndOp,
    BarPredicate,
    Leg,
    MaskLeaf,
    OrOp,
    Static,
    SymbolGate,
    leg_gate_symbols,
)
from investment_team.strategy_lab.executor.indicators import INDICATORS

_OHLCV_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})
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
    - ``bool(<Name>)`` and bare ``<Name>`` — resolves the name via the
      ``name_evaluators`` parameter (populated flow-sensitively by
      :meth:`~investment_team.strategy_lab.coverage_probe.subcondition_visitor.SubconditionVisitor._apply_assign_inplace`)
      and treats the resulting series as truthy where it is non-NaN and
      non-zero.

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
        if (
            len(inner) == 1 and and_gate is None
        ):  # pragma: no cover — AND mode is only invoked with a genuine 2+-term BoolOp (see call sites); any symbol-gate term among those terms already sets and_gate non-None, so len(inner)==1 with and_gate None can't occur — kept for defensive symmetry with the OR-mode single-leg collapse below
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
    from investment_team.strategy_lab.ast_utils.names import func_name as _shared_func_name

    return _shared_func_name(func)


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
       ``closes = [b.close for b in history]`` (or any other shape already
       bound in ``name_evaluators`` by
       :meth:`~investment_team.strategy_lab.coverage_probe.subcondition_visitor.SubconditionVisitor._apply_assign_inplace`)
       and then passed the local into the indicator, look up the binding
       and use its callable directly.
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


# _AND_OPS/_CombinatorOps are defined in indicator_probe.py (also
# needed there for its own _OR_OPS construction); _flatten_top_terms/
# _symbol_gate/_NameStrings are defined in predicate_resolution.py.
# _build_compound_subcond above needs these. Imported at the bottom,
# after every name in this module is defined (predicate_resolution.py
# in turn imports _numeric_literal back from here, at ITS bottom), so
# this module can be imported first, or after either of the other two,
# without hitting a partial-init ImportError in any order — see
# predicate_resolution.py's module docstring for the full three-way
# cycle-safety rationale.
from investment_team.strategy_lab.coverage_probe.indicator_probe import (  # noqa: E402
    _AND_OPS,
    _CombinatorOps,
)
from investment_team.strategy_lab.coverage_probe.predicate_resolution import (  # noqa: E402
    _flatten_top_terms,
    _NameStrings,
    _symbol_gate,
)
