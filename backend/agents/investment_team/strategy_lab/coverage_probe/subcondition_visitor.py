"""Statement-list-driven AST walker for indicator-probe subcondition extraction.

Houses :class:`SubconditionVisitor`, the walker that turns an ``on_bar``
(or equivalent entry-path) function body into a list of
:class:`~investment_team.strategy_lab.coverage_probe.predicate_ir.PredicateGroup`
objects: it recurses statement-by-statement through ``if``/``elif``/``else``
shapes (and the compound statements around them), tracks flow-sensitive
local name bindings (indicator evaluators, numeric periods, string
constants), and recognises the symbol-gate and position-gate idioms
generated strategies use for entry/exit routing and per-symbol filtering.

Depends on module-level operand-building, name-binding, and symbol/label
helpers defined in
:mod:`investment_team.strategy_lab.coverage_probe.indicator_probe`, which
also imports :class:`SubconditionVisitor` back for its own
``_extract_subconditions`` driver — see the import at the bottom of that
module for why the two modules must only be entered via
``indicator_probe``.

Pure: no I/O, no LLM, no subprocess.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional

import pandas as pd

from investment_team.strategy_lab.coverage_probe.indicator_probe import (
    _AND_OPS,
    _BLOCK_FIELDS,
    _MAX_SUBCONDITIONS,
    _OR_OPS,
    _bar_param_name,
    _bind_tuple_unpack,
    _build_compound_subcond,
    _build_subcond,
    _build_truthy_subcond,
    _collect_name_periods,
    _collect_name_strings,
    _early_return_symbol_guard,
    _evaluate_static_predicate,
    _find_strategy_class,
    _flatten_top_terms,
    _format_label,
    _intersect_symbols,
    _is_return_only_body,
    _numeric_literal,
    _resolve_assign_evaluator,
    _resolve_string_in_method,
    _strip_position_gate,
    _symbol_gate,
)
from investment_team.strategy_lab.coverage_probe.predicate_ir import (
    Leg,
    PredicateGroup,
    Static,
    SymbolGate,
    build_and_group,
    build_or_group,
    leg_gate_symbols,
    tree_or_unknown,
)


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
                    or_compound_gate = leg_gate_symbols(or_compound)
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
                    build_and_group(
                        group_legs, effective_symbols, effective_unknown, effective_denied
                    )
                )
            return False
        if not self._budgeted_extend(
            group_legs, own_subs
        ):  # pragma: no cover — budget exhaustion (>16 legs) unreachable in production
            if group_legs:
                self._groups.append(
                    build_and_group(
                        group_legs, effective_symbols, effective_unknown, effective_denied
                    )
                )
            return False
        if group_legs and not (effective_symbols is not None and not effective_symbols):
            self._groups.append(
                build_and_group(group_legs, effective_symbols, effective_unknown, effective_denied)
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
                    build_and_group(
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
                    build_or_group(
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
                build_or_group(
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
            or_compound_gate = leg_gate_symbols(or_compound)
            if or_compound_gate is not None:
                body_symbols = _intersect_symbols(ancestor_symbols, set(or_compound_gate))
            if tree_or_unknown(or_compound.inner):
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
