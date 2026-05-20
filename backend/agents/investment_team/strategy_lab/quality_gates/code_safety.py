"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, List

from .code_safety_ast import (
    _BANNED_CALL_PATTERNS,
    _LOOKAHEAD_PATTERNS,
    _calls_form_entry_exit_pair,
    _collect_hook_submit_calls,
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
    """True iff ``spec.exit_rules`` contains at least one rule kind that
    the trading service's ``_EngineExitDispatcher`` will enforce on the
    strategy's behalf via ``evaluate_exit_rules`` — currently
    ``StopLossRule`` (any basis) and ``TakeProfitRule``. ``SignalExitRule``
    is a no-op in the evaluator (see ``executor/rule_compiler.py``), so
    it does NOT relax the order-flow-shape requirement.

    Caller is responsible for checking that the engine-handled exits
    actually cover every entry side the strategy plans to open — see
    :func:`_engine_exits_cover_all_entry_sides` (some StopLossRule
    bases are side-specific in ``_stop_loss_triggers``).
    """
    if spec is None:
        return False
    exit_rules = getattr(spec, "exit_rules", None)
    if not exit_rules:
        return False
    # Import lazily so this module stays importable even when the DSL
    # types aren't available (legacy SQLite-only test contexts).
    from ..spec_dsl import StopLossRule, TakeProfitRule  # local import — see docstring

    return any(isinstance(r, (StopLossRule, TakeProfitRule)) for r in exit_rules)


def _engine_exits_cover_all_entry_sides(spec: Any) -> bool:
    """Spec-level wrapper: every side in ``spec.entry_rules`` must be
    closeable by at least one rule in ``spec.exit_rules``. Used as the
    fallback when the emitted code has dynamic ``side=`` expressions
    the AST can't resolve statically.
    """
    if spec is None:
        return False
    from ..spec_dsl import EntryRule  # local import

    entry_sides: set[str] = set()
    for rule in getattr(spec, "entry_rules", None) or []:
        if isinstance(rule, EntryRule):
            entry_sides.add(rule.side)
    if not entry_sides:
        return False
    return _engine_exits_cover_sides(spec, entry_sides)


def _hook_calls_include_entry(cls: ast.ClassDef, calls: List[ast.Call]) -> bool:
    """True iff at least one of the ``ctx.submit_order(...)`` calls
    reachable from ``on_bar`` is a plausible flat-position entry — has
    a ``side=`` kwarg, has no ``**kwargs`` spread (which could hide a
    close shape in the spread), its ``qty=`` does not reference
    ``position.qty`` / ``pos.qty`` anywhere in the expression, AND its
    enclosing ``if`` chain does not pin ``position`` to non-None (a
    call inside ``if position is not None:`` is an add-on / close,
    not a flat-position entry).

    Used to gate the engine-handled-exit relaxation on actually having
    an entry path. Each tightening below was driven by a codex P1:

    * round-7: reject any call whose qty references ``position.qty`` —
      ``ctx.submit_order(qty=position.qty)`` closes don't count.
    * round-8: walk the qty expression recursively so wrapped close
      shapes (``abs(position.qty)``, ``position.qty * 1``) are caught.
    * round-9 P1a: ``**kwargs`` spreads are NOT treated as entries
      any more — a spread can carry ``qty=position.qty`` just as easily
      as a side literal.
    * round-9 P1b: calls inside ``if position is not None:`` (add-only
      / close-only behaviour) no longer count — only calls reachable
      when ``position`` may still be ``None``.
    """
    for call in calls:
        if not _call_is_plausible_flat_entry(cls, call):
            continue
        return True
    return False


def _call_is_plausible_flat_entry(cls: ast.ClassDef, call: ast.Call) -> bool:
    """Per-call helper used by both :func:`_hook_calls_include_entry`
    and :func:`_entry_sides_emitted_by_calls` so the two stay in sync.

    All four checks must pass for the call to count as a flat-position
    entry: explicit ``side=`` kwarg, no ``**kwargs`` spread, qty does
    not reference ``position.qty``, and the enclosing if-chain does
    not pin position to non-None.
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
        if kw.arg == "qty" and _expr_references_position_qty(kw.value):
            qty_is_close = True
    if not has_side_literal or has_kwargs_spread or qty_is_close:
        return False
    return _call_reachable_when_position_may_be_none(cls, call)


def _expr_references_position_qty(node: ast.AST) -> bool:
    """True iff ``node`` (or any sub-expression) references
    ``position.qty`` / ``pos.qty``. Catches the literal Attribute case
    plus computed shapes like ``abs(position.qty)``, ``position.qty * 1``,
    ``-position.qty``, etc.
    """
    _position_receiver_names = frozenset({"position", "pos"})
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "qty"
            and isinstance(sub.value, ast.Name)
            and sub.value.id in _position_receiver_names
        ):
            return True
    return False


def _call_reachable_when_position_may_be_none(cls: ast.ClassDef, call: ast.Call) -> bool:
    """True iff ``call`` can execute when ``position`` is ``None``.

    Walks ``cls`` looking for the enclosing ``if`` chain around the
    call and refuses any branch whose ``test`` definitively pins
    position to non-None (``position is not None``, bare ``position``
    used as truthy check, ``and``-conjunctions containing either).

    Defaults to ``True`` when the call sits at method scope (no
    enclosing if) or the if-chain doesn't pin position — the
    conformance gate / engine_exit dispatcher can still reject the
    strategy on other grounds; the safety gate's role here is just
    "does a flat entry exist at all".
    """
    chain = _enclosing_if_tests_for(cls, call)
    for test, branch_is_else in chain:
        if branch_is_else:
            # Hit only when the call is in an ``else`` arm. The body
            # of ``else`` of ``if position is None:`` does pin to
            # non-None — but this is a rare shape and the conservative
            # fallback (treat as may-be-None) is safe for the entry
            # detection use case; over-rejecting here would refuse
            # valid strategies with else-arm entries.
            continue
        if _test_pins_position_not_none(test):
            return False
    return True


def _enclosing_if_tests_for(cls: ast.ClassDef, target: ast.AST) -> List[tuple[ast.AST, bool]]:
    """Return ``[(test_expr, branch_is_else), ...]`` for every ``If``
    whose body or orelse contains ``target`` (transitively), in
    outer-to-inner order. Empty when ``target`` is at method scope.
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


def _test_pins_position_not_none(test: ast.AST) -> bool:
    """True iff entering the body of an ``if test:`` guarantees
    ``position`` is not ``None``.

    Matches: ``position is not None`` / ``pos is not None``, bare
    ``position`` (truthy check), and ``and``-conjunctions where any
    operand pins. ``or``-disjunctions don't pin (other operand may
    leave position None).
    """
    _names = frozenset({"position", "pos"})
    if isinstance(test, ast.Compare):
        if (
            isinstance(test.left, ast.Name)
            and test.left.id in _names
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.IsNot)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            return True
    if isinstance(test, ast.Name) and test.id in _names:
        return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for sub in test.values:
            if _test_pins_position_not_none(sub):
                return True
    return False


def _entry_sides_emitted_by_calls(
    cls: ast.ClassDef, calls: List[ast.Call]
) -> tuple[set[str], bool]:
    """Return ``(sides, has_dynamic)`` describing the entry sides the
    strategy code actually submits.

    ``sides`` is the set of ``"long"`` / ``"short"`` literals seen in
    ``side=OrderSide.LONG`` / ``side="LONG"`` kwargs on flat-entry
    calls (per :func:`_call_is_plausible_flat_entry`). ``has_dynamic``
    is True when any plausible entry call uses an expression
    (variable / function return) the AST can't resolve statically —
    those calls relax side-coverage requirements via the spec
    fallback.

    Codex round-8: the engine-handled-exit relaxation previously
    checked ``_engine_exits_cover_all_entry_sides(spec)`` only, which
    misses LLM refinement that emits a side different from what the
    spec declares (e.g. spec says long but code submits SHORT). Using
    the emitted sides catches that drift.
    """
    sides: set[str] = set()
    has_dynamic = False
    for call in calls:
        if not _call_is_plausible_flat_entry(cls, call):
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
    """Pull the ``"long"`` / ``"short"`` literal out of an
    ``OrderSide.LONG`` Attribute, an ``OrderSide("long")`` constructor
    call (named exactly that), or a bare string Constant.

    Returns ``None`` for anything else — variables, function returns,
    computed expressions. Codex round-9 P1c: the previous version
    unwrapped any Call expression via its first arg, so ``pick("LONG")``
    was interpreted as a literal LONG even though ``pick`` could
    return SHORT at runtime. Restrict unwrap to the ``OrderSide``
    constructor name only.
    """
    if isinstance(node, ast.Attribute):
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
    # Only unwrap ``OrderSide(...)`` / ``contract.OrderSide(...)`` — any
    # other call is opaque.
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "OrderSide":
            return _extract_side_literal(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr == "OrderSide":
            return _extract_side_literal(node.args[0])
    return None


def _engine_exits_cover_sides(spec: Any, sides: set[str]) -> bool:
    """True iff every side in ``sides`` is closeable by at least one
    rule in ``spec.exit_rules``, per ``_stop_loss_triggers`` /
    ``_take_profit_triggers`` basis-vs-side compatibility.
    """
    if spec is None or not sides:
        return False
    exit_rules = getattr(spec, "exit_rules", None) or []
    from ..spec_dsl import StopLossRule, TakeProfitRule  # local import

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
        # Run against executable code only — comments and string literals are
        # stripped to avoid false positives.
        out: List[QualityGateResult] = []
        for pattern, reason in _LOOKAHEAD_PATTERNS:
            if pattern.search(ctx.executable):
                out.append(self._critical(f"Look-ahead bias: {reason}"))
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
        # inside ``on_bar`` (directly or via a helper), and the reachable
        # calls must form an entry+exit pair — either two calls with
        # distinct ``side`` values, or a single call with a non-None
        # ``attached_stop_loss`` / ``attached_take_profit`` bracket leg.
        # An entries-only flow is ALSO accepted when ``spec.exit_rules``
        # contains an engine-handled rule (``StopLossRule`` /
        # ``TakeProfitRule``), because the trading service's
        # ``_EngineExitDispatcher`` (``trading_service/service.py``)
        # fires ``engine_exit:<kind>`` orders against those rules every
        # bar — the strategy doesn't need to declare a parallel exit.
        #
        # The trading service only processes ``HarnessResponse`` from
        # ``send_bar``; responses from ``send_start`` / ``send_fill`` /
        # ``send_end`` are discarded, so submissions outside ``on_bar`` are
        # silently dropped — they don't count here.
        if len(ctx.strategy_classes) != 1:
            return ()
        hook_calls = _collect_hook_submit_calls(ctx.strategy_classes[0])
        if not hook_calls:
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
        # Engine-handled-exit relaxation. Four gates must all hold:
        #   (a) ``spec.exit_rules`` declares at least one engine-handled
        #       rule (StopLossRule / TakeProfitRule).
        #   (b) The strategy code itself has at least one plausible
        #       FLAT-position entry (side= literal AND not qty=position.qty
        #       AND not ``**kwargs`` spread AND not inside an enclosing
        #       ``if position is not None:`` guard).
        #   (c) Every EXPLICIT side the code emits in entry calls is
        #       covered by a triggerable engine-handled exit rule
        #       (basis-vs-side compatibility from
        #       ``_stop_loss_triggers``). An explicit uncovered side
        #       cannot be relaxed away by dynamic side= elsewhere
        #       (codex round-9 P1d): a literal ``side=OrderSide.SHORT``
        #       with only long-side trailing-stop coverage is a real
        #       exit-coverage mismatch the gate must surface.
        #   (d) When there's NO explicit emitted side but at least one
        #       call uses a dynamic ``side=`` expression, fall back
        #       to ``spec.entry_rules`` side coverage so the relaxation
        #       can still apply for code we can't analyse statically.
        cls = ctx.strategy_classes[0]
        if _spec_has_engine_handled_exit(ctx.spec) and _hook_calls_include_entry(cls, hook_calls):
            emitted_sides, has_dynamic = _entry_sides_emitted_by_calls(cls, hook_calls)
            if emitted_sides:
                # Every explicit emitted side must be covered. Dynamic
                # side= elsewhere may also need to be checked against
                # the spec — when both explicit AND dynamic appear,
                # require explicit covered AND spec covers all.
                explicit_covered = _engine_exits_cover_sides(ctx.spec, emitted_sides)
                if explicit_covered:
                    if has_dynamic:
                        if _engine_exits_cover_all_entry_sides(ctx.spec):
                            return ()
                    else:
                        return ()
            elif has_dynamic:
                # No explicit sides at all — fall back to spec.entry_rules
                # as a conservative proxy.
                if _engine_exits_cover_all_entry_sides(ctx.spec):
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
        _check_code_length,
        _check_order_flow_shape,
        _check_universe_guard,
    )
