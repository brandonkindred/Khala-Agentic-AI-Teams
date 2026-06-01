"""Deterministic spec→code conformance gate (issue #541).

Runs immediately after :class:`CodeSafetyChecker` and before the first
sandbox execution. Fails ``severity="critical"`` when generated strategy
code does not implement what the spec declares — e.g. a spec rule
references an indicator that the code never calls, or an entry rule has
no corresponding code branch.

The post-hoc, LLM-driven :class:`TradeAlignmentAgent` can only reason
about rules that actually fired in the test window. This gate catches the
silent failure mode where a rule is never implemented and therefore
never fires at all.

Critical failures route back to synthesis via the orchestrator's
``_refine_or_exhaust(failure_phase="validation")`` path (same routing as
``CodeSafetyChecker``).

Scope choices (issue #541, v1):

* Check #1 (indicator presence) requires **named calls only** — inline
  equivalents (e.g. ``sum(x)/len(x)`` as a hand-rolled SMA) are not
  recognised and will fail. Recognising inline patterns is a future
  enhancement; the deterministic compiler track (#538) makes it
  unnecessary on the compiled path.
* Check #5 (bar-counting exit rejection) scans generated code for
  bar-counting exit patterns (variables like ``bars_held``,
  ``hold_count``, ``days_held`` and ``if counter >= N: close``
  idioms). These implement the forbidden "time stop" concept and are
  rejected with a critical finding.
* Check #2 (symbol gate) reuses the same AST helpers as
  ``CodeSafetyChecker._check_universe_guard``. The duplication is
  intentional defense-in-depth so this gate remains self-sufficient.
"""

from __future__ import annotations

import ast
from typing import Any, ClassVar, Iterable, List, Optional

from ..spec_dsl import (
    EntryRule,
    IndicatorName,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from .code_safety_ast import (
    _find_strategy_subclasses,
    _get_call_name,
    _has_universe_constant,
    _has_universe_guard_in_on_bar,
    _iter_method_body_nodes,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "code_conformance"

# Hook methods on the Strategy class within which ``ctx.submit_order`` is
# allowed. Helper methods whose names start with "_" are also allowed
# because the existing :class:`CodeSafetyChecker` order-flow gate already
# requires reachable order flow to originate in ``on_bar``; the conformance
# gate's role here is to forbid stray submissions in unrelated methods
# (e.g. ``__init__`` or a public ``run`` wrapper).
_ALLOWED_HOOK_NAMES: frozenset[str] = frozenset({"on_bar", "on_fill", "on_end"})

# DSL → set of acceptable AST call-name(s) for the indicator's named
# implementation. Most map 1:1 with the indicator name; ``bollinger``
# accepts the ``bollinger_bands`` helper name from
# ``strategy_lab/executor/indicators.py``.
_INDICATOR_ALLOWED_CALL_NAMES: dict[str, frozenset[str]] = {
    "sma": frozenset({"sma"}),
    "ema": frozenset({"ema"}),
    "rsi": frozenset({"rsi"}),
    "macd": frozenset({"macd"}),
    "bollinger": frozenset({"bollinger_bands", "bollinger"}),
    "atr": frozenset({"atr"}),
    "adx": frozenset({"adx"}),
    "stochastic": frozenset({"stochastic"}),
    "vwap": frozenset({"vwap"}),
}

assert set(_INDICATOR_ALLOWED_CALL_NAMES) == set(IndicatorName.__args__), (
    "indicator allow-list must cover every DSL IndicatorName literal"
)

# Names recognised as the position-snapshot receiver in exit branches.
_POSITION_RECEIVER_NAMES: frozenset[str] = frozenset({"position", "pos"})


def _indicators_in_predicate(p: Predicate) -> set[str]:
    """Return the set of DSL indicator names referenced on either side of ``p``."""
    out: set[str] = set()
    for side in (p.lhs, p.rhs):
        if isinstance(side, IndicatorRef):
            out.add(side.name)
    return out


def _collect_required_indicators(spec: Any) -> set[str]:
    """Return the union of indicator names referenced by every rule in ``spec``."""
    refs: set[str] = set()
    for rule in getattr(spec, "entry_rules", []) or []:
        if isinstance(rule, EntryRule):
            refs |= _indicators_in_predicate(rule.when)
    for rule in getattr(spec, "exit_rules", []) or []:
        if isinstance(rule, SignalExitRule):
            refs |= _indicators_in_predicate(rule.when)
    return refs


def _collect_called_names_in_methods(cls: ast.ClassDef, method_names: frozenset[str]) -> set[str]:
    """Return the set of function-call names found inside the listed
    methods of ``cls`` (descent stops at nested defs/classes/lambdas).

    Used by check #1 (indicator presence) so that ``sma(...)`` called
    only from a dead helper does not satisfy the requirement that
    ``on_bar`` actually computes the indicator at runtime.

    ``_get_call_name`` normalises ``indicators.sma(...)`` and bare
    ``sma(...)`` to the same ``"sma"`` key.
    """
    out: set[str] = set()
    for method in _iter_strategy_methods(cls):
        if method.name not in method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if isinstance(node, ast.Call):
                name = _get_call_name(node)
                if name:
                    out.add(name)
    return out


def _is_submit_order_call(node: ast.AST) -> bool:
    """True iff ``node`` is a ``Call`` whose function attribute is ``submit_order``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit_order"
    )


def _submit_order_has_side_literal(call: ast.Call) -> bool:
    """True iff the call has a ``side=`` kwarg with any value.

    The CodeSafetyChecker shapes the recognised literal forms more
    strictly (``OrderSide.LONG`` / ``"LONG"``); for conformance coverage
    the presence of a ``side=`` kwarg at all is enough to mark the
    branch as an entry path. Exit paths use ``qty=position.qty`` and are
    detected by ``_submit_order_closes_position`` instead.
    """
    return any(kw.arg == "side" for kw in call.keywords)


def _submit_order_is_kwargs_spread(call: ast.Call) -> bool:
    """True iff the call uses a ``**kwargs`` expansion.

    ``ctx.submit_order(**order_kwargs)`` cannot be statically resolved —
    the spread may carry ``side`` (entry) or ``qty=position.qty`` (exit)
    dynamically. Coverage checks credit a branch with such a call as
    BOTH a plausible entry and a plausible exit so conformant code that
    builds kwargs dynamically does not trip false-positive criticals.
    """
    return any(kw.arg is None for kw in call.keywords)


def _submit_order_closes_position(call: ast.Call) -> bool:
    """True iff ``call`` passes ``qty=<pos|position>.qty``.

    Matches the exit shape from the issue:
    ``ctx.submit_order(..., qty=position.qty)`` or the ``pos`` alias.
    """
    for kw in call.keywords:
        if kw.arg != "qty":
            continue
        v = kw.value
        if (
            isinstance(v, ast.Attribute)
            and v.attr == "qty"
            and isinstance(v.value, ast.Name)
            and v.value.id in _POSITION_RECEIVER_NAMES
        ):
            return True
    return False


def _node_references_ctx_equity(node: ast.AST) -> bool:
    """True iff ``node`` (or any sub-expression) references ``ctx.equity``
    or ``ctx.capital``. Used by the sizing check to confirm the account
    value flows into the qty calculation somewhere in the same scope.
    """
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr in ("equity", "capital")
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "ctx"
        ):
            return True
    return False


def _qty_is_constant_int(call: ast.Call) -> bool:
    """True iff the call's ``qty=`` is a literal int (the anti-pattern).

    Excludes ``bool`` even though ``True`` / ``False`` are ``int``
    subclasses — a boolean qty is a different anti-pattern that the
    sizing check would misreport as "literal integer" otherwise.
    """
    for kw in call.keywords:
        if kw.arg != "qty":
            continue
        return (
            isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, int)
            and not isinstance(kw.value.value, bool)
        )
    return False


def _iter_strategy_methods(
    cls: ast.ClassDef,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every method directly defined on ``cls`` (excludes nested defs)."""
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _methods_reachable_from(cls: ast.ClassDef, roots: Iterable[str]) -> frozenset[str]:
    """Return method names reachable from ``roots`` via
    ``self.<method>(...)`` calls (transitively).

    Used by every check that needs "is this code actually executed at
    runtime?" — entry/exit coverage walks from ``on_bar`` only, while
    check #9 (side-effects) walks from every allowed hook so a private
    helper reachable from ``on_fill`` is not flagged as a dead helper.
    """
    methods = {m.name: m for m in _iter_strategy_methods(cls)}
    reachable: set[str] = {r for r in roots if r in methods}
    worklist: List[str] = list(reachable)
    while worklist:
        name = worklist.pop()
        method = methods.get(name)
        if method is None:
            continue
        for node in _iter_method_body_nodes(method):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                continue
            callee = node.func.attr
            if callee in methods and callee not in reachable:
                reachable.add(callee)
                worklist.append(callee)
    return frozenset(reachable)


def _methods_reachable_from_on_bar(cls: ast.ClassDef) -> frozenset[str]:
    """BFS from ``on_bar`` — used by entry/exit coverage checks."""
    return _methods_reachable_from(cls, ("on_bar",))


def _methods_reachable_from_hooks(cls: ast.ClassDef) -> frozenset[str]:
    """BFS from every allowed hook (``on_bar`` / ``on_fill`` / ``on_end``)
    — used by check #9 (side-effects) so a private helper reachable
    from ``on_fill`` is not treated as dead code.
    """
    return _methods_reachable_from(cls, _ALLOWED_HOOK_NAMES)


def _find_if_branches_with_submit_order(
    cls: ast.ClassDef,
    *,
    reachable_method_names: Optional[frozenset[str]] = None,
) -> List[tuple[ast.If, List[ast.Call]]]:
    """Walk methods on ``cls`` and yield ``(if_node, submit_calls)``
    pairs where ``submit_calls`` is a non-empty list of ``submit_order``
    calls in that ``If``'s body or its ``orelse`` branches.

    When ``reachable_method_names`` is supplied, methods not in that set
    are skipped — used by entry/exit coverage so dead code in unused
    helpers cannot satisfy the gate.

    Each top-level ``If`` body and each ``elif`` chain branch is treated
    as a separate "branch": ``if A: submit(); elif B: submit()`` yields
    two pairs. Branches nested inside loops or other ``If``s are also
    walked (the engine reaches them too).
    """
    out: List[tuple[ast.If, List[ast.Call]]] = []
    for method in _iter_strategy_methods(cls):
        if reachable_method_names is not None and method.name not in reachable_method_names:
            continue
        for node in _iter_method_body_nodes(method):
            if not isinstance(node, ast.If):
                continue
            for branch in _iter_if_branches(node):
                calls = [
                    sub for sub in _iter_branch_body_nodes(branch) if _is_submit_order_call(sub)
                ]
                if calls:
                    out.append((branch, calls))
    return out


def _iter_if_branches(node: ast.If) -> Iterable[ast.If]:
    """Yield every ``If`` node in an ``if/elif/.../else`` chain rooted at
    ``node``.

    Python represents ``elif`` as a single-element ``orelse`` containing
    another ``If`` node. ``else: ...`` becomes a non-empty ``orelse``
    whose elements are statements (not an ``If``); the gate ignores
    plain ``else`` branches because they have no test referencing
    indicators.
    """
    cur: Optional[ast.If] = node
    while isinstance(cur, ast.If):
        yield cur
        if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]
        else:
            cur = None


def _iter_branch_body_nodes(branch: ast.If) -> Iterable[ast.AST]:
    """Yield every node directly in ``branch.body`` without descending
    past nested ``def`` / ``class`` / ``lambda`` / ``If`` boundaries.

    Stopping at nested ``If`` boundaries is what makes branch-coverage
    counts honest: ``if A: if B: submit()`` is one logical entry, not
    two. The inner ``If`` is enumerated separately as its own branch
    by ``_find_if_branches_with_submit_order``, so its ``submit_order``
    is credited once — to the innermost branch — not double-counted
    against both the outer and inner.
    """
    stack: List[ast.AST] = list(branch.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.If)
        ):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _names_referenced(expr: ast.AST) -> set[str]:
    """Return the set of all ``Name.id`` and ``Call`` func-names within ``expr``."""
    out: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            out.add(node.id)
        if isinstance(node, ast.Call):
            name = _get_call_name(node)
            if name:
                out.add(name)
    return out


def _find_enclosing_funcdef(
    tree: ast.AST, target: ast.AST
) -> Optional[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the innermost ``FunctionDef`` whose body contains ``target``,
    or ``None`` when ``target`` is at module scope.

    Walks every function definition and uses ``_iter_method_body_nodes``
    (which stops at nested ``def`` / ``class`` / ``lambda`` boundaries)
    so the innermost direct enclosing scope is the one that matches.
    """
    enclosing: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in _iter_method_body_nodes(func):
            if sub is target:
                enclosing = func
                break
    return enclosing


def _is_engine_managed(spec: Any) -> bool:
    """True when the engine dispatchers own entry and exit decisions for this spec.

    Pre:  ``spec`` is a ``StrategySpec`` or ``None``.
    Post: True when entry rules exist AND ``requires_custom_code`` is False —
    the compiled output is a thin indicator shim with zero ``submit_order``
    calls, and all orders come from ``_EngineEntryDispatcher`` /
    ``_EngineExitDispatcher``.
    """
    if spec is None:
        return False
    if getattr(spec, "requires_custom_code", False):
        return False
    entry_rules = getattr(spec, "entry_rules", None)
    return bool(entry_rules)


class CodeConformanceGate(GateResultsMixin):
    """Deterministic gate that checks generated code implements ``spec``.

    Runs synchronously in the synthesis phase, after
    :class:`CodeSafetyChecker` and before the sandbox executes. Yields one
    or more :class:`QualityGateResult` entries per check; every critical
    failure routes the orchestrator back into refinement.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        code: str,
        spec: Any,
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        """Run every conformance check.

        Pre: ``code`` is a string; ``phase`` is a valid phase literal.
        Post: returned list is non-empty; every entry carries the
        caller's ``phase`` and ``gate_name == GATE``.
        """
        with self._using_phase(phase):
            try:
                tree = ast.parse(code)
            except SyntaxError as e:
                return [self._critical(f"Code has a syntax error: {e}")]

            strategy_classes = _find_strategy_subclasses(tree)
            if len(strategy_classes) != 1:
                # CodeSafetyChecker owns the "exactly one Strategy class"
                # rule and emits its own critical. Conformance needs a
                # single class to walk; emit info and let the safety gate
                # drive the failure.
                return [
                    self._info(
                        "Skipped: code has 0 or multiple Strategy subclasses "
                        "(handled by code_safety)."
                    )
                ]
            cls = strategy_classes[0]

            results: List[QualityGateResult] = []
            results.extend(self._check_indicator_presence(cls, spec))
            results.extend(self._check_symbol_gate(spec, cls))
            results.extend(self._check_entry_coverage(cls, spec))
            results.extend(self._check_signal_exit_coverage(cls, spec))
            results.extend(self._check_bar_counting_exit(cls))
            results.extend(self._check_sizing_math(cls, spec))
            results.extend(self._check_no_extra_side_effects(tree, cls))

            return results or [self._info("Code conforms to spec across all conformance checks.")]

    # ------------------------------------------------------------------
    # Check 1 — indicator presence
    # ------------------------------------------------------------------
    def _check_indicator_presence(
        self, cls: ast.ClassDef, spec: Any
    ) -> Iterable[QualityGateResult]:
        required = _collect_required_indicators(spec)
        if not required:
            return ()
        # Indicator calls only count when they are actually executed at
        # runtime: walk methods reachable from on_bar (Codex PR #588 P1).
        reachable = _methods_reachable_from_on_bar(cls)
        called = _collect_called_names_in_methods(cls, reachable)
        missing: List[str] = []
        for name in sorted(required):
            allowed = _INDICATOR_ALLOWED_CALL_NAMES.get(name, frozenset({name}))
            if not (called & allowed):
                missing.append(name)
        if not missing:
            return ()
        return (
            self._critical(
                f"Spec references indicator(s) {missing} but no method "
                "reachable from on_bar calls any of their named "
                "implementations. v1 requires the named-call form "
                "(e.g. ``sma(bars, 50)``); inline equivalents and calls "
                "in unreachable helpers are not recognised."
            ),
        )

    # ------------------------------------------------------------------
    # Check 2 — symbol/universe gate (defense-in-depth with code_safety)
    # ------------------------------------------------------------------
    def _check_symbol_gate(self, spec: Any, cls: ast.ClassDef) -> Iterable[QualityGateResult]:
        if not getattr(spec, "target_symbols", None):
            return ()
        if not _has_universe_constant(cls):
            return (
                self._critical(
                    "Spec has non-empty target_symbols but the Strategy class "
                    "does not declare a UNIVERSE = frozenset({...}) (or "
                    "set/tuple) class-level constant. Without UNIVERSE plus "
                    "an ``if bar.symbol not in self.UNIVERSE: return`` guard "
                    "in on_bar, bars for non-target symbols will be processed."
                ),
            )
        if not _has_universe_guard_in_on_bar(cls):
            return (
                self._critical(
                    "Strategy defines UNIVERSE but on_bar lacks the runtime "
                    "``if bar.symbol not in self.UNIVERSE: return`` guard "
                    "rejecting bars whose symbol is not in the universe."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 3 — entry predicate coverage
    # ------------------------------------------------------------------
    def _check_entry_coverage(self, cls: ast.ClassDef, spec: Any) -> Iterable[QualityGateResult]:
        entry_rules = [
            r for r in (getattr(spec, "entry_rules", []) or []) if isinstance(r, EntryRule)
        ]
        if not entry_rules:
            return ()

        if _is_engine_managed(spec):
            return (
                self._info(
                    f"Spec declares {len(entry_rules)} entry rule(s); entries are "
                    "engine-managed via _EngineEntryDispatcher — no inline "
                    "submit_order entry branches required."
                ),
            )

        reachable = _methods_reachable_from_on_bar(cls)
        branches = _find_if_branches_with_submit_order(cls, reachable_method_names=reachable)
        entry_branches: List[tuple[ast.If, List[ast.Call]]] = []
        for if_node, calls in branches:
            if any(
                _submit_order_is_kwargs_spread(c)
                or (_submit_order_has_side_literal(c) and not _submit_order_closes_position(c))
                for c in calls
            ):
                entry_branches.append((if_node, calls))

        if not entry_branches:
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but no "
                    "if/elif branch reachable from on_bar contains a "
                    "ctx.submit_order(..., side=...) entry call. The entry "
                    "path was likely removed during refinement or lives in "
                    "an unreachable helper."
                ),
            )

        if len(entry_branches) < len(entry_rules):
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but only "
                    f"{len(entry_branches)} entry branch(es) were found "
                    "reachable from on_bar. Each EntryRule needs its own "
                    "if/elif branch with a ctx.submit_order(..., side=...) "
                    "call."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 4 — signal-exit predicate coverage
    # ------------------------------------------------------------------
    def _check_signal_exit_coverage(
        self, cls: ast.ClassDef, spec: Any
    ) -> Iterable[QualityGateResult]:
        signal_exits = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, SignalExitRule)
        ]
        if not signal_exits:
            return ()

        if _is_engine_managed(spec):
            return (
                self._info(
                    f"Spec declares {len(signal_exits)} signal-exit rule(s); "
                    "signal exits are engine-managed via _EngineExitDispatcher "
                    "— no inline submit_order exit branches required."
                ),
            )

        reachable = _methods_reachable_from_on_bar(cls)
        branches = _find_if_branches_with_submit_order(cls, reachable_method_names=reachable)
        exit_branches = [
            (if_node, calls)
            for if_node, calls in branches
            if any(
                _submit_order_closes_position(c) or _submit_order_is_kwargs_spread(c) for c in calls
            )
        ]
        if not exit_branches:
            return (
                self._critical(
                    f"Spec declares {len(signal_exits)} signal-exit rule(s) "
                    "but no if/elif branch reachable from on_bar contains a "
                    "ctx.submit_order(..., qty=position.qty) close call."
                ),
            )

        if len(exit_branches) < len(signal_exits):
            return (
                self._critical(
                    f"Spec declares {len(signal_exits)} signal-exit rule(s) "
                    f"but only {len(exit_branches)} exit branch(es) were found "
                    "reachable from on_bar."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 5 — bar-counting exit rejection
    # ------------------------------------------------------------------

    _BAR_COUNTER_NAMES: ClassVar[frozenset] = frozenset({
        "bars_held", "hold_count", "days_held", "bars_in_trade",
        "held_bars", "bar_count", "hold_period", "bars_since_entry",
        "hold_bars", "n_bars_held", "num_bars_held", "time_in_trade",
        "holding_period", "exit_countdown", "bar_counter",
    })

    def _check_bar_counting_exit(self, cls: ast.ClassDef) -> Iterable[QualityGateResult]:
        violations: list[str] = []
        for method in _iter_strategy_methods(cls):
            for node in ast.walk(method):
                # Only flag self.<counter> instance attributes — these persist
                # across bars and are the pattern LLMs use for holding-period
                # exits.  Bare locals (e.g. a diagnostic `bar_count`) are left
                # alone to avoid false positives on non-exit bookkeeping.
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in self._BAR_COUNTER_NAMES
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                ):
                    violations.append(f"self.{node.attr}")
        unique = sorted(set(violations))
        if not unique:
            return ()
        names = ", ".join(f"`{v}`" for v in unique)
        return (
            self._critical(
                f"Bar-counting exit detected ({names}). "
                f"Exits must close on price, P&L, or signal reversal — "
                f"not on an arbitrary bar counter."
            ),
        )

    # ------------------------------------------------------------------
    # Check 6 — sizing math present
    # ------------------------------------------------------------------
    def _check_sizing_math(self, cls: ast.ClassDef, spec: Any = None) -> Iterable[QualityGateResult]:
        all_submit_calls = [
            sub
            for method in _iter_strategy_methods(cls)
            for sub in _iter_method_body_nodes(method)
            if _is_submit_order_call(sub)
        ]
        entry_submit_calls = [c for c in all_submit_calls if not _submit_order_closes_position(c)]
        if not entry_submit_calls:
            return ()

        # Hardcoded-int qty on every entry is an immediate fail regardless
        # of whether ctx.equity is referenced elsewhere in the class.
        if all(_qty_is_constant_int(c) for c in entry_submit_calls):
            return (
                self._critical(
                    "Every entry ctx.submit_order call passes a literal "
                    "integer ``qty=``. Sizing must be derived from "
                    "``ctx.equity`` or ``ctx.capital`` so the position "
                    "scales with the account, not a hardcoded number."
                ),
            )

        # Otherwise: any reference to ctx.equity / ctx.capital anywhere in
        # the Strategy class is enough — qty is usually computed into a
        # local variable (``qty = max(1, int(ctx.equity * 0.02 / bar.close))``)
        # which then flows into the submit_order call as ``qty=qty``. We
        # do not attempt to trace the local var across statements; the
        # presence of the account-value reference and the absence of a
        # hardcoded int kwarg is the v1 contract.
        if _node_references_ctx_equity(cls):
            return ()
        return (
            self._critical(
                "Strategy class never references ``ctx.equity`` or "
                "``ctx.capital``. Sizing must derive ``qty=`` from the "
                "account value so the strategy scales correctly."
            ),
        )

    # ------------------------------------------------------------------
    # Check 7 — no submit_order calls outside hook/helper methods
    # ------------------------------------------------------------------
    def _check_no_extra_side_effects(
        self, tree: ast.AST, cls: ast.ClassDef
    ) -> Iterable[QualityGateResult]:
        # ``_helper`` methods are only allowed when actually reachable
        # from an allowed hook — a dead ``_helper`` containing
        # ctx.submit_order would otherwise pass silently (Codex PR #588).
        reachable_helpers = _methods_reachable_from_hooks(cls)
        offenders: List[str] = []
        for node in ast.walk(tree):
            if not _is_submit_order_call(node):
                continue
            enclosing = _find_enclosing_funcdef(tree, node)
            if enclosing is None:
                offenders.append("<module scope>")
                continue
            name = enclosing.name
            if name in _ALLOWED_HOOK_NAMES:
                continue
            # ``_helper`` is the conventional name for hook-reachable
            # closures; allow it only when actually reachable. Dunder
            # methods (``__init__`` / ``__call__`` / ``__enter__`` …) are
            # never the right place for a submit_order — exclude them
            # outright.
            is_helper_name = name.startswith("_") and not (
                name.startswith("__") and name.endswith("__")
            )
            if is_helper_name and name in reachable_helpers:
                continue
            offenders.append(name)
        if not offenders:
            return ()
        return (
            self._critical(
                f"ctx.submit_order called from disallowed scope(s) "
                f"{sorted(set(offenders))}. Order submissions must live in "
                "on_bar / on_fill / on_end (or in a private ``_helper`` "
                "method reachable from one of those hooks)."
            ),
        )
