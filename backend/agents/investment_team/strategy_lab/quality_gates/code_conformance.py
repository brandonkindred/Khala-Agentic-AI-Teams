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
* Check #7 (time-stop enforcement) is a no-op today because the spec
  DSL has no ``TimeStopRule`` (``spec_dsl.py`` explicitly excludes
  bar-counting time stops). The check is wired up so it activates the
  moment the DSL adds the rule.
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
    StopLossRule,
    TakeProfitRule,
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


def _collect_called_names(tree: ast.AST) -> set[str]:
    """Return the set of function-call names anywhere in ``tree``.

    Uses ``_get_call_name`` so ``indicators.sma(...)`` and bare ``sma(...)``
    both contribute ``"sma"``.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
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
    """True iff the call's ``qty=`` is a literal int (the anti-pattern)."""
    for kw in call.keywords:
        if kw.arg != "qty":
            continue
        return isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int)
    return False


def _iter_strategy_methods(
    cls: ast.ClassDef,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every method directly defined on ``cls`` (excludes nested defs)."""
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _find_if_branches_with_submit_order(
    cls: ast.ClassDef,
) -> List[tuple[ast.If, List[ast.Call]]]:
    """Walk every method on ``cls`` and yield ``(if_node, submit_calls)``
    pairs where ``submit_calls`` is a non-empty list of ``submit_order``
    calls in that ``If``'s body or its ``orelse`` branches.

    Each top-level ``If`` body and each ``elif`` chain branch is treated
    as a separate "branch": ``if A: submit(); elif B: submit()`` yields
    two pairs. Branches nested inside loops or other ``If``s are also
    walked (the engine reaches them too).
    """
    out: List[tuple[ast.If, List[ast.Call]]] = []
    for method in _iter_strategy_methods(cls):
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
    """Yield every node in ``branch.body`` without descending past nested
    ``def`` / ``class`` / ``lambda`` boundaries."""
    stack: List[ast.AST] = list(branch.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
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
            results.extend(self._check_indicator_presence(tree, spec))
            results.extend(self._check_symbol_gate(spec, cls))
            results.extend(self._check_entry_coverage(cls, spec))
            results.extend(self._check_signal_exit_coverage(cls, spec))
            results.extend(self._check_stop_loss_enforcement(cls, spec))
            results.extend(self._check_take_profit_enforcement(cls, spec))
            results.extend(self._check_time_stop_enforcement(spec))
            results.extend(self._check_sizing_math(cls))
            results.extend(self._check_no_extra_side_effects(tree, cls))

            return results or [self._info("Code conforms to spec across all conformance checks.")]

    # ------------------------------------------------------------------
    # Check 1 — indicator presence
    # ------------------------------------------------------------------
    def _check_indicator_presence(self, tree: ast.AST, spec: Any) -> Iterable[QualityGateResult]:
        required = _collect_required_indicators(spec)
        if not required:
            return ()
        called = _collect_called_names(tree)
        missing: List[str] = []
        for name in sorted(required):
            allowed = _INDICATOR_ALLOWED_CALL_NAMES.get(name, frozenset({name}))
            if not (called & allowed):
                missing.append(name)
        if not missing:
            return ()
        return (
            self._critical(
                f"Spec references indicator(s) {missing} but the generated code "
                f"contains no call to any of their named implementations. v1 "
                "requires the named-call form (e.g. ``sma(bars, 50)``); inline "
                "equivalents are not recognised."
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

        branches = _find_if_branches_with_submit_order(cls)
        entry_branches: List[tuple[ast.If, List[ast.Call]]] = []
        for if_node, calls in branches:
            if any(
                _submit_order_has_side_literal(c) and not _submit_order_closes_position(c)
                for c in calls
            ):
                entry_branches.append((if_node, calls))

        if not entry_branches:
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but no "
                    "if/elif branch in the Strategy class contains a "
                    "ctx.submit_order(..., side=...) entry call. Drop the "
                    "entry path was likely removed during refinement."
                ),
            )

        if len(entry_branches) < len(entry_rules):
            return (
                self._critical(
                    f"Spec declares {len(entry_rules)} entry rule(s) but only "
                    f"{len(entry_branches)} entry branch(es) were found in the "
                    "Strategy class. Each EntryRule needs its own if/elif "
                    "branch with a ctx.submit_order(..., side=...) call."
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

        branches = _find_if_branches_with_submit_order(cls)
        exit_branches = [
            (if_node, calls)
            for if_node, calls in branches
            if any(_submit_order_closes_position(c) for c in calls)
        ]
        if not exit_branches:
            return (
                self._critical(
                    f"Spec declares {len(signal_exits)} signal-exit rule(s) "
                    "but no if/elif branch contains a "
                    "ctx.submit_order(..., qty=position.qty) close call."
                ),
            )

        if len(exit_branches) < len(signal_exits):
            return (
                self._critical(
                    f"Spec declares {len(signal_exits)} signal-exit rule(s) "
                    f"but only {len(exit_branches)} exit branch(es) were found "
                    "in the Strategy class."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Check 5 — stop-loss enforcement
    # ------------------------------------------------------------------
    def _check_stop_loss_enforcement(
        self, cls: ast.ClassDef, spec: Any
    ) -> Iterable[QualityGateResult]:
        stop_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, StopLossRule)
        ]
        if not stop_rules:
            return ()
        if self._class_references_position_entry_price(cls):
            return ()
        return (
            self._critical(
                "Spec has a StopLossRule but the Strategy class never "
                "references ``position.entry_price`` (or ``pos.entry_price``) "
                "— the stop-loss threshold cannot be computed against the "
                "entry without it."
            ),
        )

    # ------------------------------------------------------------------
    # Check 6 — take-profit enforcement
    # ------------------------------------------------------------------
    def _check_take_profit_enforcement(
        self, cls: ast.ClassDef, spec: Any
    ) -> Iterable[QualityGateResult]:
        tp_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, TakeProfitRule)
        ]
        if not tp_rules:
            return ()
        if self._class_references_position_entry_price(cls):
            return ()
        return (
            self._critical(
                "Spec has a TakeProfitRule but the Strategy class never "
                "references ``position.entry_price`` (or ``pos.entry_price``) "
                "— the take-profit threshold cannot be computed against the "
                "entry without it."
            ),
        )

    def _class_references_position_entry_price(self, cls: ast.ClassDef) -> bool:
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "entry_price"
                and isinstance(node.value, ast.Name)
                and node.value.id in _POSITION_RECEIVER_NAMES
            ):
                return True
        return False

    # ------------------------------------------------------------------
    # Check 7 — time-stop enforcement (no-op until the DSL grows the rule)
    # ------------------------------------------------------------------
    def _check_time_stop_enforcement(self, spec: Any) -> Iterable[QualityGateResult]:
        # ``TimeStopRule`` is intentionally not a member of the
        # ``ExitRule`` union (see ``spec_dsl.py``); the check stays wired
        # up so it activates the moment the DSL adds it.
        time_stop_cls = globals().get("TimeStopRule")
        if time_stop_cls is None:
            return (
                self._info(
                    "Time-stop check is a no-op: TimeStopRule is not part "
                    "of the current spec DSL (see spec_dsl.py)."
                ),
            )
        time_rules = [
            r for r in (getattr(spec, "exit_rules", []) or []) if isinstance(r, time_stop_cls)
        ]
        if not time_rules:
            return ()
        # Future activation path: look for ctx.bars_held or a class-level
        # held-bars dict updated in on_bar / on_fill. Critical when neither
        # is present.
        return ()  # pragma: no cover — activates with DSL change

    # ------------------------------------------------------------------
    # Check 8 — sizing math present
    # ------------------------------------------------------------------
    def _check_sizing_math(self, cls: ast.ClassDef) -> Iterable[QualityGateResult]:
        all_submit_calls = [
            sub
            for method in _iter_strategy_methods(cls)
            for sub in _iter_method_body_nodes(method)
            if _is_submit_order_call(sub)
        ]
        entry_submit_calls = [c for c in all_submit_calls if not _submit_order_closes_position(c)]
        if not entry_submit_calls:
            # No entry submit_orders to size — Check #3 will already have
            # fired, so silence this one to avoid noisy double-failure.
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
    # Check 9 — no submit_order calls outside hook/helper methods
    # ------------------------------------------------------------------
    def _check_no_extra_side_effects(
        self, tree: ast.AST, cls: ast.ClassDef
    ) -> Iterable[QualityGateResult]:
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
            # closures, so a single leading underscore is allowed. Dunder
            # methods (``__init__`` / ``__call__`` / ``__enter__`` …) are
            # never the right place for a submit_order — exclude them.
            if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
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
