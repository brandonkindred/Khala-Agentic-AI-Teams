"""Predicate/symbol-gate resolution helpers for the indicator-coverage probe.

Houses the free functions that resolve position gates, symbol scopes,
static predicates, and name bindings ahead of subcondition construction —
extracted from
:mod:`investment_team.strategy_lab.coverage_probe.indicator_probe`
(#1777, Part 2 of the decomposition started by
:mod:`investment_team.strategy_lab.coverage_probe.subcondition_visitor`
in #1960). Pure: no I/O, no LLM, no subprocess.

This module and ``indicator_probe`` import back from each other:
``indicator_probe`` calls into several of the resolution helpers defined
here (``_extract_subconditions``, ``_union_target_symbols``,
``_flatten_top_terms``, ``_symbol_gate``, ``_NameStrings``,
``_iter_entry_path_assigns``), while this module needs ``_BLOCK_FIELDS``
and ``_numeric_literal`` from ``indicator_probe``'s not-yet-decomposed
"builder cluster". Both cross-imports are placed as the **last top-level
statement** in their respective module — after every same-file definition
that module needs — which keeps the two-way cycle safe regardless of
which of the two modules a caller imports first (whichever loads first
runs to completion, then hands back to the other, which by then finds
everything it needs already bound).

``_extract_subconditions`` additionally needs ``SubconditionVisitor``
from ``subcondition_visitor``, which itself imports back from both this
module and ``indicator_probe``. Resolving that third edge with another
bottom-of-file import would reintroduce a genuine three-module ordering
hazard (it breaks if ``subcondition_visitor`` is ever imported first), so
that one reference is deferred to a function-local import instead — see
the comment inside ``_extract_subconditions``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from dataclasses import field as _field
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
)

import pandas as pd

from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    AndOp,
    BarPredicate,
    Leg,
    MaskLeaf,
    OrOp,
    PredicateGroup,
    Static,
    SymbolGate,
)


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
    # Deferred import: SubconditionVisitor lives in subcondition_visitor.py,
    # which imports back from this module (and from indicator_probe.py) at
    # its own top level. A module-level import here would recreate the
    # partial-init hazard #1960 solved for indicator_probe.py, but one
    # level worse (a genuine 3-module cycle) — importing inside the
    # function body instead means this reference only resolves at call
    # time, by which point both indicator_probe.py and this module have
    # always finished loading. See the module docstring.
    from investment_team.strategy_lab.coverage_probe.subcondition_visitor import (
        SubconditionVisitor,
    )

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


def _is_bar_symbol(n: ast.expr, bar_name: str) -> bool:
    """True iff ``n`` is a ``<bar_name>.symbol`` attribute access.

    Shared by :func:`_early_return_symbol_guard` and :func:`_symbol_gate`,
    which both need to recognise the live-symbol receiver before resolving
    the comparison's other operand.
    """
    return (
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == bar_name
        and n.attr == "symbol"
    )


def _resolve_symbol_string(n: ast.expr, name_strings: Optional["_NameStrings"]) -> Optional[str]:
    """Resolve ``n`` to a string constant for symbol-gate/guard comparison.

    Shared by :func:`_early_return_symbol_guard` and :func:`_symbol_gate`.
    Recognises an inline string literal, a bare ``Name`` bound to a
    module-level string constant (via ``name_strings.globals_``), or a
    ``self.X``/``cls.X`` attribute bound to a class-level string constant
    (via ``name_strings.attrs``).
    """
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    if name_strings is None:  # pragma: no cover — name_strings always provided in live call path
        return None
    # Bare ``Name`` resolves through the module/global scope only —
    # class-body bare names are NOT in lexical scope for methods.
    if isinstance(n, ast.Name):
        return name_strings.globals_.get(n.id)
    # ``self.X`` / ``cls.X`` resolves through the class chain
    # (instance dict via ``__init__`` shadowing class body), never
    # through module scope.
    if (  # pragma: no cover — self.X/cls.X in symbol-gate/early-return-guard position rare in generated strategies
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in {"self", "cls"}
    ):
        return name_strings.attrs.get(n.attr)
    return None


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

    # ``bar.symbol <op> X`` / ``X <op> bar.symbol`` → allow / deny
    # depending on the operator polarity.
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        if isinstance(op, (ast.NotEq, ast.Eq)):
            polarity = "allow" if isinstance(op, ast.NotEq) else "deny"
            left, right = test.left, test.comparators[0]
            if _is_bar_symbol(left, bar_name):
                sym = _resolve_symbol_string(right, name_strings)
                if sym is not None:
                    return polarity, {sym}
            if _is_bar_symbol(
                right, bar_name
            ):  # pragma: no cover — reversed-operand early-return guard rare in generated strategies
                sym = _resolve_symbol_string(left, name_strings)
                if sym is not None:
                    return polarity, {sym}
        # ``bar.symbol not in (X, Y)`` → allow {X, Y}; the matching
        # ``in`` form is the deny variant — both keep the same
        # element-resolution rules and only differ on polarity.
        if isinstance(op, (ast.NotIn, ast.In)):
            polarity = "allow" if isinstance(op, ast.NotIn) else "deny"
            left, right = test.left, test.comparators[0]
            if _is_bar_symbol(left, bar_name) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                syms: set = set()
                for elt in right.elts:
                    s = _resolve_symbol_string(elt, name_strings)
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

    if isinstance(op, (ast.Eq, ast.Is)):
        if _is_bar_symbol(left, bar_name):
            sym = _resolve_symbol_string(right, name_strings)
            return {sym} if sym is not None else None
        if _is_bar_symbol(
            right, bar_name
        ):  # pragma: no cover — reversed operand symbol gate rare in generated strategies
            sym = _resolve_symbol_string(left, name_strings)
            return {sym} if sym is not None else None
        return None

    if isinstance(op, ast.In):
        if not _is_bar_symbol(left, bar_name):
            return None
        if not isinstance(
            right, (ast.Tuple, ast.List, ast.Set)
        ):  # pragma: no cover — non-literal-collection right operand rare in ``in`` symbol gates
            return None
        syms: set = set()
        for elt in right.elts:
            s = _resolve_symbol_string(elt, name_strings)
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


def _flatten_top_terms(test: ast.expr) -> List[ast.expr]:
    """Split a top-level ``and`` chain into individual term expressions.

    Returns the raw expression nodes (not just ``Compare``), so callers
    can recognise truthiness terms such as ``bool(_entry)`` or a bare
    ``Name`` reference to a precomputed indicator series alongside
    ordinary comparisons.
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
    :func:`_collect_unconditional_constructor_assigns` can resolve the
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


def _collect_unconditional_constructor_assigns(
    stmts: List[ast.stmt],
    param_defaults: Optional[Dict[str, ast.Constant]] = None,
) -> List[Union[ast.Assign, ast.AnnAssign]]:
    """Return ``Assign`` / ``AnnAssign`` nodes guaranteed to execute on
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

    - ``Assign`` / ``AnnAssign`` at the current level → collected
    - ``if <Constant>: body else: orelse`` → collect from the branch
      Python's truthiness on ``Constant.value`` selects (the other is
      dead code at runtime)
    - ``if <Name>:`` where ``Name`` is a constructor parameter with a
      ``Constant`` default → resolve via ``param_defaults`` and collect
      from the live branch. Strategies routinely use this shape
      (``def __init__(self, enabled=True): if enabled: ...``); the
      default-construction path is guaranteed.
    - ``if <unknown>: ...`` → skip both branches conservatively
    - ``with <ctx>: body`` / ``async with`` → collect from ``body``
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
                out.extend(_collect_unconditional_constructor_assigns(branch, param_defaults))
            # Unknown predicate — skip both branches; the class-body
            # binding (already recorded) acts as the runtime fallback.
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            out.extend(_collect_unconditional_constructor_assigns(stmt.body, param_defaults))
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
                        for sub in _collect_unconditional_constructor_assigns(
                            child.body, param_defaults
                        ):
                            _dispatch(sub, "record_constructor")
                    continue
                for sub in _collect_unconditional_constructor_assigns([child], class_param_defaults):
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


# _BLOCK_FIELDS and _numeric_literal are part of indicator_probe.py's
# not-yet-decomposed "builder cluster" (out of scope for this move —
# tracked separately). Imported at the bottom, after every name in this
# module is defined, so this module can be imported first or after
# indicator_probe.py without hitting a partial-init ImportError either
# way — see the module docstring.
from investment_team.strategy_lab.coverage_probe.indicator_probe import (  # noqa: E402
    _BLOCK_FIELDS,
    _numeric_literal,
)
