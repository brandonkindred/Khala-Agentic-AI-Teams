"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, List

from ..spec_dsl import SignalExitRule, StopLossRule, TakeProfitRule
from .code_safety_ast import (
    _BANNED_CALL_PATTERNS,
    _LOOKAHEAD_PATTERNS,
    _LOOKAHEAD_WARNING_PATTERNS,
    _calls_form_entry_exit_pair,
    _collect_hook_submit_calls,
    _find_forward_access_warnings,
    _find_strategy_subclasses,
    _get_call_name,
    _has_universe_constant,
    _has_universe_guard_in_on_bar,
    _strip_comments_and_strings,
    _validate_on_bar,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "code_safety"

BANNED_IMPORTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "http",
        "urllib",
        "requests",
        "shutil",
        "pathlib",
        "importlib",
        "ctypes",
        "pickle",
        "shelve",
        "sqlite3",
        "multiprocessing",
        "threading",
        "signal",
        "io",
        "tempfile",
        "glob",
        "webbrowser",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio",
    }
)

ALLOWED_IMPORTS = frozenset(
    {
        # The event-driven Strategy contract types — injected into the
        # subprocess by :class:`StreamingHarness`.
        "contract",
        # Pre-built technical indicators still copied into the sandbox.
        "indicators",
        # Stdlib-only helpers. pandas / numpy are deliberately excluded:
        # the event-driven contract delivers bars one at a time via
        # ``on_bar(ctx, bar)`` and strategies never need a DataFrame.
        "math",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "typing",
        "dataclasses",
        "enum",
        "abc",
        "re",
        "copy",
        "statistics",
        "decimal",
        "fractions",
        "operator",
        "json",
    }
)


def _spec_has_engine_handled_exit(spec: Any) -> bool:
    """True iff ``spec.exit_rules`` contains a rule the engine enforces.

    Pre:  ``spec`` is a ``StrategySpec`` or ``None``.
    Post: True when ``spec.exit_rules`` contains at least one
          ``StopLossRule``, ``TakeProfitRule``, or ``SignalExitRule``
          (all enforced engine-side via ``evaluate_exit_rules`` /
          ``_EngineExitDispatcher``).
    Invariant: stop-loss/take-profit basis-vs-side coverage is NOT
          checked here — callers must additionally invoke
          :func:`_engine_exits_cover_sides` against the relevant
          entry sides.
    """
    if spec is None:
        return False
    exit_rules = getattr(spec, "exit_rules", None)
    if not exit_rules:
        return False
    return any(isinstance(r, (StopLossRule, TakeProfitRule, SignalExitRule)) for r in exit_rules)


def _spec_is_fully_engine_managed(spec: Any) -> bool:
    """True when both entries and exits are engine-managed for this spec.

    Pre:  ``spec`` is a ``StrategySpec`` or ``None``.
    Post: True when entry rules exist, at least one exit rule is
          engine-handled, and the spec is NOT custom-code. The compiled
          strategy is a pure indicator shim with zero ``submit_order``
          calls — all orders come from the engine dispatchers.
    """
    if spec is None:
        return False
    if getattr(spec, "requires_custom_code", False):
        return False
    entry_rules = getattr(spec, "entry_rules", None)
    if not entry_rules:
        return False
    return _spec_has_engine_handled_exit(spec)


def _hook_calls_include_entry(cls: ast.ClassDef, calls: List[ast.Call]) -> bool:
    """True iff any ``ctx.submit_order(...)`` reachable from ``on_bar`` looks like a flat entry.

    Pre:  ``cls`` is the single ``Strategy`` subclass; ``calls`` is the
          submit-order calls reachable from the engine hooks on that class.
    Post: True iff at least one call in ``calls`` satisfies
          :func:`_call_is_plausible_flat_entry`.
    """
    position_names = _collect_position_aliases(cls)
    for call in calls:
        if not _call_is_plausible_flat_entry(cls, call, position_names):
            continue
        return True
    return False


def _call_is_plausible_flat_entry(
    cls: ast.ClassDef, call: ast.Call, position_names: frozenset[str]
) -> bool:
    """True iff ``call`` is a flat-position entry.

    Pre:  ``call`` is a ``ctx.submit_order(...)`` call inside ``cls``;
          ``position_names`` is the set of identifiers bound to
          ``ctx.position(...)`` results in ``cls``.
    Post: True iff ALL of: (a) ``call`` has a ``side=`` kwarg, (b) no
          ``**kwargs`` spread (which could hide a close shape), (c)
          ``qty=`` does not reference ``<name>.qty`` for any
          ``<name>`` in ``position_names``, (d) the enclosing ``if``
          chain does not pin ``position`` to non-None.
    Invariant: shared by :func:`_hook_calls_include_entry` and
          :func:`_entry_sides_emitted_by_calls` so both views agree.
    """
    has_side_literal = False
    has_kwargs_spread = False
    qty_is_close = False
    for kw in call.keywords:
        if kw.arg is None:
            has_kwargs_spread = True
            continue
        if kw.arg == "side":
            has_side_literal = True
        if kw.arg == "qty" and _expr_references_position_qty(kw.value, position_names):
            qty_is_close = True
    if not has_side_literal or has_kwargs_spread or qty_is_close:
        return False
    return _call_reachable_when_position_may_be_none(cls, call, position_names)


def _collect_position_aliases(cls: ast.ClassDef) -> frozenset[str]:
    """Return the identifiers bound to ``ctx.position(...)`` calls in ``cls``.

    Pre:  ``cls`` is a parsed ``ClassDef``.
    Post: result always contains ``{"position", "pos"}`` plus the LHS
          name of every ``<name> = ctx.position(...)`` assignment found
          anywhere in ``cls`` (any method, any nested scope).
    """
    names: set[str] = {"position", "pos"}
    for node in ast.walk(cls):
        if not isinstance(node, ast.Assign):
            continue
        if not _is_ctx_position_call(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return frozenset(names)


def _is_ctx_position_call(node: ast.AST) -> bool:
    """True iff ``node`` is a ``ctx.position(...)`` call expression."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "position"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    )


def _expr_references_position_qty(
    node: ast.AST, position_names: frozenset[str] = frozenset({"position", "pos"})
) -> bool:
    """True iff any sub-expression of ``node`` is ``<name>.qty`` for ``<name>`` in ``position_names``.

    Pre:  ``node`` is an AST expression; ``position_names`` is the
          alias set from :func:`_collect_position_aliases`.
    Post: True for the literal ``Attribute`` case and any wrapping
          expression that contains it — ``abs(p.qty)``, ``p.qty * 1``,
          ``-p.qty``, ``p.qty if cond else q.qty``, etc.
    """
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "qty"
            and isinstance(sub.value, ast.Name)
            and sub.value.id in position_names
        ):
            return True
    return False


def _call_reachable_when_position_may_be_none(
    cls: ast.ClassDef, call: ast.Call, position_names: frozenset[str]
) -> bool:
    """True iff ``call`` can execute when ``position`` may be ``None``.

    Pre:  ``call`` is inside ``cls``; ``position_names`` is the alias
          set from :func:`_collect_position_aliases`.
    Post: False iff some enclosing ``if`` branch around ``call`` pins
          one of ``position_names`` to non-None: either the body of
          a test matched by :func:`_test_pins_position_not_none`, or
          the ``else``-arm of a test matched by
          :func:`_test_pins_position_is_none`.
    Invariant: defaults to True when there is no pinning if-chain.
          False positives are corrected by the conformance gate /
          engine-exit dispatcher; the safety gate's role here is only
          "does a flat entry exist at all".
    """
    chain = _enclosing_if_tests_for(cls, call)
    for test, branch_is_else in chain:
        if branch_is_else:
            if _test_pins_position_is_none(test, position_names):
                return False
        else:
            if _test_pins_position_not_none(test, position_names):
                return False
    return True


def _enclosing_if_tests_for(cls: ast.ClassDef, target: ast.AST) -> List[tuple[ast.AST, bool]]:
    """Return ``[(test_expr, branch_is_else), ...]`` for every ``If`` whose body or orelse transitively contains ``target``.

    Pre:  ``cls`` is the class containing ``target``.
    Post: list is outer-to-inner; ``branch_is_else`` is True iff
          ``target`` reached the ``If`` via its ``orelse`` branch.
          Empty when ``target`` is at method scope.
    """
    found: List[tuple[ast.AST, bool]] = []

    def _walk(node: ast.AST, stack: List[tuple[ast.AST, bool]]) -> bool:
        if node is target:
            found.extend(stack)
            return True
        if isinstance(node, ast.If):
            for child in node.body:
                if _walk(child, stack + [(node.test, False)]):
                    return True
            for child in node.orelse:
                if _walk(child, stack + [(node.test, True)]):
                    return True
            return False
        for child in ast.iter_child_nodes(node):
            if _walk(child, stack):
                return True
        return False

    for child in ast.iter_child_nodes(cls):
        if _walk(child, []):
            break
    return found


def _test_pins_position_not_none(test: ast.AST, position_names: frozenset[str]) -> bool:
    """True iff entering the body of ``if test:`` guarantees a position name is not None.

    Pre:  ``test`` is an AST expression; ``position_names`` is the
          set of names treated as position bindings.
    Post: True for any of the following shapes against a name in
          ``position_names``: ``<name> is not None``, ``<name> != None``,
          bare ``<name>`` (truthy check), ``not (<name> is None)``,
          and ``and``-conjunctions where any operand pins. ``or``
          never pins.
    """
    if isinstance(test, ast.Compare):
        if (
            isinstance(test.left, ast.Name)
            and test.left.id in position_names
            and len(test.ops) == 1
            and isinstance(test.ops[0], (ast.IsNot, ast.NotEq))
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            return True
    if isinstance(test, ast.Name) and test.id in position_names:
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(
        test.op, ast.Not
    ):  # pragma: no cover — ``not (pos is None)`` shape rare in generated strategies
        if _test_pins_position_is_none(test.operand, position_names):
            return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for sub in test.values:
            if _test_pins_position_not_none(sub, position_names):
                return True
    return False


def _test_pins_position_is_none(test: ast.AST, position_names: frozenset[str]) -> bool:
    """True iff entering the body of ``if test:`` guarantees a position name IS None.

    Pre:  ``test`` is an AST expression; ``position_names`` is the
          set of names treated as position bindings.
    Post: True for any of the following shapes against a name in
          ``position_names``: ``<name> is None``, ``<name> == None``,
          ``not <name>`` (None is falsy), ``not (<name> is not None)``,
          and ``and``-conjunctions where any operand pins. ``or``
          never pins.
    """
    if isinstance(test, ast.Compare):
        if (
            isinstance(test.left, ast.Name)
            and test.left.id in position_names
            and len(test.ops) == 1
            and isinstance(test.ops[0], (ast.Is, ast.Eq))
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            return True
    if (
        isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    ):  # pragma: no cover — ``not pos`` / ``not (pos is not None)`` shapes rare in generated strategies
        if isinstance(test.operand, ast.Name) and test.operand.id in position_names:
            return True
        if _test_pins_position_not_none(test.operand, position_names):
            return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for sub in test.values:
            if _test_pins_position_is_none(sub, position_names):
                return True
    return False


def _entry_sides_emitted_by_calls(
    cls: ast.ClassDef, calls: List[ast.Call]
) -> tuple[set[str], bool]:
    """Return ``(sides, has_dynamic)`` for the entry sides actually submitted in ``calls``.

    Pre:  ``cls`` is the strategy class; ``calls`` are
          ``ctx.submit_order(...)`` calls reachable from engine hooks.
    Post: ``sides`` ⊆ ``{"long", "short"}``, drawn from the literal
          ``side=`` kwargs on calls satisfying
          :func:`_call_is_plausible_flat_entry` and resolvable by
          :func:`_extract_side_literal`. ``has_dynamic`` is True iff
          at least one plausible entry call carries a non-literal
          ``side=`` expression (variable, opaque call, etc.).
    """
    sides: set[str] = set()
    has_dynamic = False
    position_names = _collect_position_aliases(cls)
    for call in calls:
        if not _call_is_plausible_flat_entry(cls, call, position_names):
            continue
        for kw in call.keywords:
            if kw.arg != "side":
                continue
            literal = _extract_side_literal(kw.value)
            if literal is not None:
                sides.add(literal)
            else:
                has_dynamic = True
            break
    return sides, has_dynamic


def _extract_side_literal(node: ast.AST) -> str | None:
    """Return ``"long"`` / ``"short"`` if ``node`` is a recognisable side literal, else ``None``.

    Pre:  ``node`` is an AST expression used as the value of a ``side=`` kwarg.
    Post: returns ``"long"`` / ``"short"`` for: an ``OrderSide.LONG`` /
          ``contract.OrderSide.SHORT`` attribute whose root is
          ``OrderSide``; an ``OrderSide(...)`` call whose first arg is
          itself a recognisable literal; or a bare ``"LONG"`` /
          ``"SHORT"`` string constant. Returns ``None`` for variables,
          opaque calls, computed expressions, and attributes whose
          root is not the bound ``OrderSide`` (so a user-defined
          ``FakeSide.LONG`` with arbitrary value never satisfies
          side-coverage statically).
    """
    if isinstance(node, ast.Attribute):
        if not _attr_value_is_order_side(node.value):
            return None
        if node.attr.upper() == "LONG":
            return "long"
        if node.attr.upper() == "SHORT":
            return "short"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value.strip().upper()
        if v == "LONG":
            return "long"
        if v == "SHORT":
            return "short"
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "OrderSide":
            return _extract_side_literal(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr == "OrderSide":
            return _extract_side_literal(node.args[0])
    return None


def _attr_value_is_order_side(node: ast.AST) -> bool:
    """True iff ``node`` is ``Name("OrderSide")`` or an ``Attribute`` ending in ``.OrderSide``."""
    return (isinstance(node, ast.Name) and node.id == "OrderSide") or (
        isinstance(node, ast.Attribute) and node.attr == "OrderSide"
    )


def _engine_exits_cover_sides(spec: Any, sides: set[str]) -> bool:
    """True iff every side in ``sides`` is closeable by at least one ``spec.exit_rules`` entry.

    Pre:  ``spec`` is a ``StrategySpec`` or ``None``; ``sides`` ⊆
          ``{"long", "short"}``.
    Post: False if ``spec`` is None or ``sides`` is empty. Otherwise
          True iff for every side ∈ ``sides`` there exists a rule
          in ``spec.exit_rules`` that triggers on that side per the
          basis-vs-side compatibility map: ``TakeProfitRule``,
          ``SignalExitRule``, and ``StopLossRule(basis="entry_price")``
          cover both sides; ``StopLossRule(basis="trailing_high")``
          covers only long; ``StopLossRule(basis="trailing_low")``
          covers only short.
    """
    if spec is None or not sides:
        return False
    exit_rules = getattr(spec, "exit_rules", None) or []

    def _rule_covers_side(rule: Any, side: str) -> bool:
        if isinstance(rule, TakeProfitRule):
            return True
        if isinstance(rule, StopLossRule):
            basis = rule.basis
            if basis == "entry_price":
                return True
            if basis == "trailing_high":
                return side == "long"
            if basis == "trailing_low":
                return side == "short"
        if isinstance(rule, SignalExitRule):
            return True
        return False

    for side in sides:
        if not any(_rule_covers_side(r, side) for r in exit_rules):
            return False
    return True


@dataclass(frozen=True)
class CodeSafetyCtx:
    """Per-``check`` context handed to every rule in ``CodeSafetyChecker._RULES``.

    Built once at the top of ``check`` after the syntax-error short-circuit.
    Threading the ctx explicitly through each rule replaces the previous
    ``self._<attr>`` pattern that risked bleed-over across concurrent
    ``check`` invocations.
    """

    code: str
    tree: ast.Module
    spec: Any
    strategy_classes: List[ast.ClassDef]
    executable: str


class CodeSafetyChecker(GateResultsMixin):
    """Scan generated strategy code for unsafe patterns before subprocess execution.

    Contract: every call to :meth:`check` returns a non-empty
    ``List[QualityGateResult]``. Every entry carries the caller's ``phase``
    and ``gate_name == GATE``. Rules are listed in ``_RULES`` and iterated in
    order; a syntax-error short-circuit fires before any other rule because
    the AST-based rules cannot run without a parse tree.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        code: str,
        spec: Any = None,
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        """Run the safety checks and tag every result with ``phase``.

        Pre: ``code`` is a string; ``phase`` is a valid phase literal.
        Post: every returned result carries the caller's ``phase`` and
        ``gate_name == GATE``. The default matches the primary refinement-
        loop call site; callers re-using the checker in a different phase
        (e.g. the trade-alignment fix path, which lives in verification)
        must pass ``phase`` explicitly.

        ``spec`` is the active ``StrategySpec`` when available; it's used by
        the symbol-universe rule to verify that the generated module
        contains a ``UNIVERSE`` constant and a ``bar.symbol not in
        self.UNIVERSE: return`` guard whenever ``spec.target_symbols`` is
        non-empty. Other rules ignore ``spec``; passing ``None`` (the
        default) keeps the legacy call sites and tests behaving as before.
        """
        with self._using_phase(phase):
            # Parse first — a syntax error is a hard short-circuit because
            # every AST rule below requires a tree.
            try:
                tree = ast.parse(code)
            except SyntaxError as e:
                return [self._critical(f"Code has a syntax error: {e}")]

            ctx = CodeSafetyCtx(
                code=code,
                tree=tree,
                spec=spec,
                strategy_classes=_find_strategy_subclasses(tree),
                executable=_strip_comments_and_strings(code),
            )
            results = [r for rule in self._RULES for r in rule(self, ctx)]
            return results or [self._info("Code passed all safety checks.")]

    # ------------------------------------------------------------------
    # Rules — each reads call-scoped state and yields zero or more results.
    # ------------------------------------------------------------------
    def _check_strategy_class_shape(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        # The streaming harness requires exactly one Strategy subclass with a
        # correctly-shaped ``on_bar``. Flagging here turns a runtime
        # classification error into an actionable refinement hint.
        n = len(ctx.strategy_classes)
        if n == 0:
            return (
                self._critical(
                    "Code must define exactly one subclass of contract.Strategy; "
                    "none found. Use `from contract import Strategy` and `class "
                    "MyStrategy(Strategy): ...`."
                ),
            )
        if n > 1:
            names = ", ".join(sorted(c.name for c in ctx.strategy_classes))
            return (
                self._critical(
                    f"Code defines multiple Strategy subclasses ({names}); the "
                    "harness accepts exactly one."
                ),
            )
        on_bar_issue = _validate_on_bar(ctx.strategy_classes[0])
        if on_bar_issue is not None:
            return (self._critical(on_bar_issue),)
        return ()

    def _check_banned_imports(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        out: List[QualityGateResult] = []
        for node in ast.walk(ctx.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        out.append(
                            self._critical(
                                f"Banned import: '{alias.name}' — "
                                "network/filesystem/system access not allowed."
                            )
                        )
                    elif top_module not in ALLOWED_IMPORTS:
                        out.append(
                            self._warning(
                                f"Non-allowlisted import: '{alias.name}' — "
                                "may not be available in sandbox."
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_module = node.module.split(".")[0]
                if top_module in BANNED_IMPORTS:
                    out.append(
                        self._critical(
                            f"Banned import: 'from {node.module}' — "
                            "network/filesystem/system access not allowed."
                        )
                    )
                elif top_module not in ALLOWED_IMPORTS:
                    out.append(
                        self._warning(
                            f"Non-allowlisted import: 'from {node.module}' — "
                            "may not be available in sandbox."
                        )
                    )
        return out

    def _check_banned_calls(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        out: List[QualityGateResult] = []
        for node in ast.walk(ctx.tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = _get_call_name(node)
            if func_name in ("exec", "eval", "compile", "__import__", "globals", "breakpoint"):
                out.append(
                    self._critical(
                        f"Banned function call: '{func_name}()' — "
                        "dynamic code execution not allowed."
                    )
                )
            if func_name == "open":
                out.append(
                    self._critical(
                        "Banned function call: 'open()' — file I/O not allowed in strategy code."
                    )
                )
            if func_name in ("setattr", "delattr"):
                out.append(
                    self._critical(
                        f"Banned function call: '{func_name}()' — "
                        "attribute manipulation not allowed."
                    )
                )
        return out

    def _check_banned_call_regex(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        # AST sometimes misses patterns hidden behind getattr / dynamic
        # attribute access; the regex pass catches those.
        out: List[QualityGateResult] = []
        for pattern in _BANNED_CALL_PATTERNS:
            if pattern.search(ctx.code):
                match_text = pattern.pattern.replace(r"\b", "").replace(r"\s*\(", "(")
                out.append(self._critical(f"Regex detected banned pattern: '{match_text}'."))
        return out

    def _check_lookahead_bias(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        """Scan executable code for syntactic look-ahead tripwires.

        Preconditions:
          - ``ctx.executable`` is ``ctx.code`` with comments and string
            literals blanked, so docstring examples can't false-flag.
        Postconditions:
          - Returns one critical result per matched pattern in
            :data:`_LOOKAHEAD_PATTERNS` (forward-attribute access whose
            only correct response is to refuse the run).
          - Returns one warning per matched pattern in
            :data:`_LOOKAHEAD_WARNING_PATTERNS` (dynamic-attribute idioms
            that dodge the runtime AttributeError trap).
          - Returns the empty list when nothing matches.
        """
        out: List[QualityGateResult] = []
        for pattern, reason in _LOOKAHEAD_PATTERNS:
            if pattern.search(ctx.executable):
                out.append(self._critical(f"Look-ahead bias: {reason}"))
        for pattern, reason in _LOOKAHEAD_WARNING_PATTERNS:
            if pattern.search(ctx.executable):
                out.append(self._warning(f"Look-ahead bias: {reason}"))
        return out

    def _check_forward_access_patterns(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        """Flag AST-level forward-access idioms the regex pass cannot see.

        Preconditions:
          - ``ctx.tree`` is a parsed module; ``ctx.strategy_classes`` is the
            list of ``Strategy`` subclasses returned by
            :func:`_find_strategy_subclasses`.
        Postconditions:
          - Returns one warning result per distinct findings produced by
            :func:`_find_forward_access_warnings`:
              * ``getattr(bar, ...)`` / ``getattr(ctx, ...)`` calls (AST
                companion to the regex check, catches multi-line forms).
              * ``try: <bar.* / ctx.*> except AttributeError:`` blocks
                that swallow the lookahead_violation trap.
              * ``Subscript`` on a class-bound preloaded series whose index
                is a positive offset from an iteration variable, e.g.
                ``self._closes[i + 1]``.
          - Returns the empty list when no strategy class is present or
            no pattern matches — paired with :func:`_check_lookahead_bias`
            (critical-path) so a single piece of source surfaces all
            forward-access concerns in one pass.
        """
        out: List[QualityGateResult] = []
        for cls in ctx.strategy_classes:
            for reason in _find_forward_access_warnings(cls):
                out.append(self._warning(f"Look-ahead bias: {reason}"))
        return out

    def _check_code_length(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        line_count = len(ctx.code.splitlines())
        if line_count > 1000:
            return (
                self._warning(f"Code is {line_count} lines — consider simplifying (limit: 1000)."),
            )
        return ()

    def _check_order_flow_shape(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        # Every viable strategy must call ``ctx.submit_order(...)`` from
        # inside ``on_bar`` (directly or via a helper). The trading
        # service only consumes ``HarnessResponse`` from ``send_bar`` —
        # submissions reached from ``send_start`` / ``send_fill`` /
        # ``send_end`` are silently dropped — so only ``on_bar``-reachable
        # calls count. The reachable calls must form one of:
        #   - an entry+exit pair (two calls with distinct ``side``);
        #   - a single entry with an ``attached_stop_loss`` /
        #     ``attached_take_profit`` bracket leg;
        #   - an entries-only flow whose missing exit is supplied by
        #     ``spec.exit_rules`` (engine-handled relaxation below);
        #   - zero calls when the spec is fully engine-managed.
        if len(ctx.strategy_classes) != 1:
            return ()
        hook_calls = _collect_hook_submit_calls(ctx.strategy_classes[0])
        if not hook_calls:
            if _spec_is_fully_engine_managed(ctx.spec):
                return ()
            return (
                self._critical(
                    "No ctx.submit_order call reachable from on_bar — strategy "
                    "has no entry path that the engine will process. The "
                    "trading service only consumes orders submitted from "
                    "on_bar (responses from on_start / on_fill / on_end are "
                    "currently dropped), so any submission outside on_bar is "
                    "silently ignored."
                ),
            )
        if _calls_form_entry_exit_pair(hook_calls):
            return ()
        # Engine-handled-exit relaxation. All of the following must hold
        # to accept an entries-only flow:
        #   (1) spec has an engine-handled exit rule
        #       (:func:`_spec_has_engine_handled_exit`);
        #   (2) the strategy has at least one plausible flat entry
        #       (:func:`_hook_calls_include_entry`);
        #   (3) every explicit ``side=`` literal in entry calls is
        #       covered by a triggerable rule in ``spec.exit_rules``.
        # Dynamic ``side=`` expressions cannot reach this branch in
        # practice — :func:`_calls_form_entry_exit_pair` treats them
        # optimistically and short-circuits above.
        cls = ctx.strategy_classes[0]
        if _spec_has_engine_handled_exit(ctx.spec) and _hook_calls_include_entry(cls, hook_calls):
            emitted_sides, _ = _entry_sides_emitted_by_calls(cls, hook_calls)
            if emitted_sides and _engine_exits_cover_sides(ctx.spec, emitted_sides):
                return ()
        if len(hook_calls) == 1:
            detail = (
                "Only one ctx.submit_order call found in the engine hooks "
                "and no non-None attached bracket exit (attached_stop_loss "
                "/ attached_take_profit) — strategy has no exit path. Either "
                "submit an opposite-side close, attach a bracket leg, or "
                "declare a StopLossRule / TakeProfitRule in spec.exit_rules "
                "(the engine's evaluate_exit_rules will fire those)."
            )
        else:
            detail = (
                "Multiple ctx.submit_order calls found but all use the same "
                "OrderSide and no bracket exit is attached — strategy has no "
                "real exit leg. Closing a position requires submitting the "
                "opposite OrderSide (LONG closes SHORT, SHORT closes LONG), "
                "attaching an attached_stop_loss / attached_take_profit "
                "bracket leg, or declaring a StopLossRule / TakeProfitRule "
                "in spec.exit_rules for engine-side enforcement."
            )
        return (self._critical(detail),)

    def _check_universe_guard(self, ctx: CodeSafetyCtx) -> Iterable[QualityGateResult]:
        # When ``spec.target_symbols`` is non-empty the generated module MUST
        # declare a class-level ``UNIVERSE`` set/frozenset and guard ``on_bar``
        # with ``if bar.symbol not in self.UNIVERSE: return``. The historical
        # replay stream interleaves bars across every fetched symbol; without
        # this guard a permissive predicate trades whichever ticker fires
        # first, not the one named in the hypothesis.
        if ctx.spec is None or not getattr(ctx.spec, "target_symbols", None):
            return ()
        if len(ctx.strategy_classes) != 1:
            return ()
        strategy_cls = ctx.strategy_classes[0]
        if not _has_universe_constant(strategy_cls):
            return (
                self._critical(
                    "Spec has non-empty target_symbols but the strategy "
                    "class is missing a UNIVERSE = frozenset({...}) (or "
                    "set/tuple) class-level constant. Without UNIVERSE + "
                    "an `if bar.symbol not in self.UNIVERSE: return` guard "
                    "at the top of on_bar, the historical replay stream "
                    "will feed bars for every fetched symbol to the "
                    "signal logic and trades will land on the wrong asset."
                ),
            )
        if not _has_universe_guard_in_on_bar(strategy_cls):
            return (
                self._critical(
                    "Strategy defines UNIVERSE but on_bar is missing the "
                    "`if bar.symbol not in self.UNIVERSE: return` guard. "
                    "Without the early-exit, the historical replay stream "
                    "will deliver bars for every fetched symbol and the "
                    "signal logic will trade tickers outside target_symbols."
                ),
            )
        return ()

    # Rules iterated in order by ``check``. Order is preserved so error
    # messages remain stable across runs.
    _RULES: ClassVar[tuple] = (
        _check_strategy_class_shape,
        _check_banned_imports,
        _check_banned_calls,
        _check_banned_call_regex,
        _check_lookahead_bias,
        _check_forward_access_patterns,
        _check_code_length,
        _check_order_flow_shape,
        _check_universe_guard,
    )
