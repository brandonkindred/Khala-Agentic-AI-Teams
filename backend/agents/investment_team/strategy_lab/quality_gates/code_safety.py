"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
import re
from typing import Any, ClassVar, Dict, List, Optional

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

# Regex patterns for dangerous calls that AST analysis might miss in edge cases.
_BANNED_CALL_PATTERNS = [
    re.compile(r"\bexec\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bcompile\s*\("),
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\bglobals\s*\("),
    re.compile(r"\bbreakpoint\s*\("),
]

# Look-ahead bias patterns — accessing future data from within the
# ``Strategy`` subclass. Most look-ahead is structurally impossible in the
# event-driven contract (``ctx`` has no accessor for future data, and
# ``AttributeError`` on a forward field is trapped as ``lookahead_violation``
# at runtime), but these regexes catch obvious tripwires before the code
# even runs.
_LOOKAHEAD_PATTERNS = [
    (
        re.compile(r"\bctx\s*\.\s*future_\w+"),
        "ctx.future_* does not exist — use only ctx.history(symbol, n)",
    ),
    (
        re.compile(r"\bbar\s*\.\s*(?:next|future)_\w+"),
        "bar.next_* / bar.future_* does not exist — only current-bar fields are delivered",
    ),
    (
        re.compile(r"\bctx\s*\.\s*peek\b"),
        "ctx.peek(...) does not exist — the engine does not expose forward bars",
    ),
]


class CodeSafetyChecker(GateResultsMixin):
    """Scan generated strategy code for unsafe patterns before subprocess execution."""

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
        self._set_phase(phase)
        results: List[QualityGateResult] = []

        # 1. Parse the code
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            results.append(
                self._critical(f"Code has a syntax error: {e}")
            )
            return results

        # 2. Check the module defines exactly one contract.Strategy subclass
        #    with a correctly-shaped ``on_bar`` method. The PR-3 streaming
        #    harness requires this shape and raises at runtime otherwise;
        #    flagging here turns a runtime classification error into an
        #    actionable refinement hint.
        strategy_classes = _find_strategy_subclasses(tree)
        if len(strategy_classes) == 0:
            results.append(
                self._critical("Code must define exactly one subclass of contract.Strategy; "
                        "none found. Use `from contract import Strategy` and `class "
                        "MyStrategy(Strategy): ...`.")
            )
        elif len(strategy_classes) > 1:
            names = ", ".join(sorted(c.name for c in strategy_classes))
            results.append(
                self._critical(f"Code defines multiple Strategy subclasses ({names}); the "
                        "harness accepts exactly one.")
            )
        else:
            strategy_cls = strategy_classes[0]
            on_bar_issue = _validate_on_bar(strategy_cls)
            if on_bar_issue is not None:
                results.append(
                    self._critical(on_bar_issue)
                )

        # 3. Walk AST for banned imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(
                            self._critical(f"Banned import: '{alias.name}' — network/filesystem/system access not allowed.")
                        )
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(
                            self._warning(f"Non-allowlisted import: '{alias.name}' — may not be available in sandbox.")
                        )

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_module = node.module.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(
                            self._critical(f"Banned import: 'from {node.module}' — network/filesystem/system access not allowed.")
                        )
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(
                            self._warning(f"Non-allowlisted import: 'from {node.module}' — may not be available in sandbox.")
                        )

        # 4. Walk AST for banned function calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = _get_call_name(node)
                if func_name in ("exec", "eval", "compile", "__import__", "globals", "breakpoint"):
                    results.append(
                        self._critical(f"Banned function call: '{func_name}()' — dynamic code execution not allowed.")
                    )
                if func_name == "open":
                    results.append(
                        self._critical("Banned function call: 'open()' — file I/O not allowed in strategy code.")
                    )
                if func_name in ("setattr", "delattr"):
                    results.append(
                        self._critical(f"Banned function call: '{func_name}()' — attribute manipulation not allowed.")
                    )

        # 5. Regex fallback for patterns AST might miss
        for pattern in _BANNED_CALL_PATTERNS:
            if pattern.search(code):
                match_text = pattern.pattern.replace(r"\b", "").replace(r"\s*\(", "(")
                results.append(
                    self._critical(f"Regex detected banned pattern: '{match_text}'.")
                )

        # 6. Look-ahead bias detection (run against executable code only,
        #    excluding comments and string literals to avoid false positives)
        executable = _strip_comments_and_strings(code)
        for pattern, reason in _LOOKAHEAD_PATTERNS:
            if pattern.search(executable):
                results.append(
                    self._critical(f"Look-ahead bias: {reason}")
                )

        # 7. Code length
        line_count = len(code.splitlines())
        if line_count > 1000:
            results.append(
                self._warning(f"Code is {line_count} lines — consider simplifying (limit: 1000).")
            )

        # 8. Order-flow shape (#547): every viable strategy must call
        #    ``ctx.submit_order(...)`` from inside ``on_bar`` (directly or
        #    via a helper invoked from it), and the reachable calls must
        #    form a real entry+exit pair — either two calls with distinct
        #    ``side`` values, or a single call with a non-None
        #    ``attached_stop_loss`` / ``attached_take_profit`` (bracket /
        #    OCO support, #389).
        #
        #    * The trading service only processes the ``HarnessResponse``
        #      from ``send_bar``; ``send_start`` / ``send_fill`` /
        #      ``send_end`` responses are discarded today, so any
        #      ``submit_order`` made from those hooks is dropped before
        #      backtesting. The gate counts only ``on_bar``-reachable
        #      calls to match the engine's real behaviour.
        #    * ``on_bar`` accepts only its second positional parameter as
        #      the context receiver (``def on_bar(self, ctx, bar):``
        #      looks for ``ctx.submit_order``; a swapped
        #      ``def on_bar(self, bar, ctx):`` accepts only ``bar`` and
        #      ``ctx.submit_order`` would crash at runtime — flagged).
        #    * Helpers reached via ``self.<method>(...)`` from ``on_bar``
        #      relax the receiver check to any non-``self`` positional
        #      parameter, since the call site we can't statically resolve
        #      may pass the context through.
        #    * Two ``side="LONG"`` calls do not form an entry+exit pair;
        #      a real exit requires the opposite side or a bracket leg.
        if len(strategy_classes) == 1:
            hook_calls = _collect_hook_submit_calls(strategy_classes[0])
            if len(hook_calls) == 0:
                results.append(
                    self._critical("No ctx.submit_order call reachable from on_bar — strategy "
                            "has no entry path that the engine will process. The "
                            "trading service only consumes orders submitted from "
                            "on_bar (responses from on_start / on_fill / on_end are "
                            "currently dropped), so any submission outside on_bar is "
                            "silently ignored.")
                )
            elif not _calls_form_entry_exit_pair(hook_calls):
                if len(hook_calls) == 1:
                    detail = (
                        "Only one ctx.submit_order call found in the engine hooks "
                        "and no non-None attached bracket exit (attached_stop_loss "
                        "/ attached_take_profit) — strategy has no exit path."
                    )
                else:
                    detail = (
                        "Multiple ctx.submit_order calls found but all use the same "
                        "OrderSide and no bracket exit is attached — strategy has no "
                        "real exit leg. Closing a position requires submitting the "
                        "opposite OrderSide (LONG closes SHORT, SHORT closes LONG) or "
                        "attaching an attached_stop_loss / attached_take_profit "
                        "bracket leg."
                    )
                results.append(
                    self._critical(detail)
                )

        # 9. Symbol-universe guard (#524). When ``spec.target_symbols`` is
        #    non-empty, the generated module MUST declare a class-level
        #    ``UNIVERSE`` set/frozenset and guard ``on_bar`` with
        #    ``if bar.symbol not in self.UNIVERSE: return``. The historical
        #    replay stream interleaves bars across every fetched symbol;
        #    without this guard a permissive predicate trades whichever
        #    ticker fires first, not the one named in the hypothesis.
        if spec is not None and getattr(spec, "target_symbols", None):
            if len(strategy_classes) == 1:
                strategy_cls = strategy_classes[0]
                if not _has_universe_constant(strategy_cls):
                    results.append(
                        self._critical("Spec has non-empty target_symbols but the strategy "
                                "class is missing a UNIVERSE = frozenset({...}) (or "
                                "set/tuple) class-level constant. Without UNIVERSE + "
                                "an `if bar.symbol not in self.UNIVERSE: return` guard "
                                "at the top of on_bar, the historical replay stream "
                                "will feed bars for every fetched symbol to the "
                                "signal logic and trades will land on the wrong asset.")
                    )
                elif not _has_universe_guard_in_on_bar(strategy_cls):
                    results.append(
                        self._critical("Strategy defines UNIVERSE but on_bar is missing the "
                                "`if bar.symbol not in self.UNIVERSE: return` guard. "
                                "Without the early-exit, the historical replay stream "
                                "will deliver bars for every fetched symbol and the "
                                "signal logic will trade tickers outside target_symbols.")
                    )

        if not results:
            results.append(
                self._info("Code passed all safety checks.")
            )

        return results


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node (handles simple names and attribute access)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


# Hook methods whose ``submit_order`` calls actually reach the engine.
#
# The streaming harness sends every hook (``on_start`` / ``on_bar`` /
# ``on_fill`` / ``on_end``) to the strategy subprocess, but the trading
# service ONLY processes the ``HarnessResponse`` returned from ``send_bar``
# into pending orders (see ``trading_service/service.py``). The responses
# from ``send_start`` (line 483-489), ``send_fill`` (line 637-641), and
# ``send_end`` (line 1114) are discarded, so any ``submit_order`` made
# from those hooks is dropped before backtesting. The order-flow gate
# only credits ``on_bar`` calls — matching the engine's real behaviour
# avoids passing strategies that would silently emit zero trades.
_PROCESSED_HOOK_METHODS = frozenset({"on_bar"})


def _iter_method_body_nodes(method: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield every AST node in ``method``'s body without descending into
    nested ``def`` / ``async def`` / ``lambda`` / ``class`` bodies.

    Python only creates the function/class object for a nested
    declaration; its body never runs unless something explicitly invokes
    it. Naïvely using ``ast.walk(method)`` would treat ``submit_order``
    calls inside an uninvoked local helper inside the hook as reachable,
    which is wrong — those calls never reach the runtime engine.
    """
    stack: List[ast.AST] = list(method.body)
    while stack:
        node = stack.pop()
        yield node
        # Stop descent at any nested function / class / lambda boundary —
        # its body is a new scope that only runs if explicitly invoked.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _iter_method_body_in_source_order(scope: ast.AST):
    """Yield every node in ``scope``'s body in source order, without
    descending into nested ``def`` / ``class`` / ``lambda`` bodies.

    Unlike :func:`_iter_method_body_nodes` (stack-based, arbitrary
    pop order), this generator visits parent statements before their
    children and processes sibling statements in declaration order.
    The flow-sensitive alias tracker depends on this ordering so a
    ``trade_ctx = ctx`` assignment updates state BEFORE later
    ``trade_ctx.submit_order(...)`` calls are visited.
    """
    body = getattr(scope, "body", None)
    if not isinstance(body, list):
        return

    def visit(node: ast.AST):
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    for stmt in body:
        yield from visit(stmt)


def _find_nested_def_defs(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return a name → def map for every nested function defined directly
    inside ``method``'s body (any depth, but not inside another nested
    def or class).

    These are local closures: ``def enter(): ctx.submit_order(...)`` style
    helpers. When the outer scope explicitly calls them by name, the
    engine reaches their body — so the order-flow walker needs to descend.
    """
    local_defs: Dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    # Walk method's body but stop at nested def/class boundaries so we
    # collect only the immediate closures of this scope (not closures of
    # closures).
    for node in _iter_method_body_nodes(method):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_defs.setdefault(node.name, node)
    return local_defs


def _resolve_helper_receivers(
    call: ast.Call,
    helper: ast.FunctionDef | ast.AsyncFunctionDef,
    outer_receivers: frozenset[str],
) -> frozenset[str]:
    """Return the helper parameter names that are bound to the outer
    scope's context at the call site.

    For ``self._trade(ctx, bar)`` where the outer scope's
    ``receiver_names`` is ``{"ctx"}``: the first positional after self
    in ``_trade``'s signature receives ``ctx``, so its name (e.g.
    ``ctx`` or whatever the helper named that parameter) goes into the
    returned set. Calls that don't pass any outer-receiver name as an
    argument end up with an empty set — a ``submit_order`` inside such
    a helper does NOT satisfy the gate.

    Both positional and keyword arguments are tracked. Star-args /
    keyword-spreads are ignored (rare in strategy code).
    """
    helper_params = [arg.arg for arg in helper.args.args if arg.arg != "self"]
    bound: set[str] = set()

    # Positional arguments — match against helper params by index.
    for idx, arg in enumerate(call.args):
        if idx >= len(helper_params):
            break
        if isinstance(arg, ast.Name) and arg.id in outer_receivers:
            bound.add(helper_params[idx])

    # Keyword arguments — match the parameter the keyword names.
    for kw in call.keywords:
        if kw.arg is None:
            # **kwargs spread — can't statically resolve.
            continue
        if (
            isinstance(kw.value, ast.Name)
            and kw.value.id in outer_receivers
            and kw.arg in helper_params
        ):
            bound.add(kw.arg)

    return frozenset(bound)


def _collect_hook_submit_calls(cls: ast.ClassDef) -> List[ast.Call]:
    """Return every ``submit_order(...)`` call reachable from ``on_bar``.

    ``on_bar`` is the only engine hook whose ``HarnessResponse`` is
    actually processed into pending orders by the trading service
    (``send_start`` / ``send_fill`` / ``send_end`` are called without
    consuming their responses, so any ``submit_order`` made from those
    hooks is silently dropped). Limiting the gate's roots to ``on_bar``
    matches the engine's real behaviour.

    The walker follows three kinds of edges from each reachable scope:

    * ``self.<method>(...)`` — descends into class methods. The helper
      has its own parameter list; we relax the receiver check to any
      non-``self`` positional parameter since the call site can pass
      the context through any of them.
    * ``<local_name>(...)`` — descends into local closures defined in
      the SAME scope (e.g. ``def enter(): ctx.submit_order(...)`` then
      ``enter()`` inside ``on_bar``). Local closures inherit the outer
      scope's bindings, so they keep the OUTER scope's receiver names
      rather than introducing their own (the closure captures ``ctx``
      from the enclosing frame).

    Walks each scope's body but stops at nested ``def`` / ``class`` /
    ``lambda`` boundaries — bodies of locally-defined-but-uninvoked
    helpers are not reachable because Python only creates the function
    object and never executes the body without a call.

    ``on_bar`` is dispatched positionally, so only its second positional
    parameter is the StrategyContext.
    """
    methods_by_name: Dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods_by_name[node.name] = node

    calls: List[ast.Call] = []
    # Visit key is (id(scope), receiver_names) so a helper called twice
    # with different bindings (e.g. ``self._trade(bar)`` then
    # ``self._trade(ctx)``) is analyzed once per distinct binding rather
    # than getting masked by the first visit.
    visited_keys: set[tuple[int, frozenset[str]]] = set()
    # Worklist entries: (scope_node, receiver_names). receiver_names is
    # the set of identifiers we accept as the context receiver inside
    # this scope's body — for hooks, the second positional; for class
    # helpers, parameters bound at the call site; for local closures,
    # call-site-bound parameters PLUS the outer scope's receivers (the
    # closure also captures them from the enclosing frame).
    worklist: List[tuple[ast.AST, frozenset[str]]] = []
    for name, m in methods_by_name.items():
        if name in _PROCESSED_HOOK_METHODS:
            if len(m.args.args) >= 2:
                worklist.append((m, frozenset({m.args.args[1].arg})))

    while worklist:
        scope, initial_receivers = worklist.pop()
        visit_key = (id(scope), initial_receivers)
        if visit_key in visited_keys:
            continue
        visited_keys.add(visit_key)

        # Local closures defined directly in this scope — followed only
        # when their name is invoked below.
        local_defs = (
            _find_nested_def_defs(scope)
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            else {}
        )

        # Flow-sensitive walk. ``state`` tracks the currently-live alias
        # set as we proceed through the scope in source order. The walker
        # updates it as it encounters ``Assign`` / ``AnnAssign`` events,
        # so a ``submit_order`` call is credited only when the alias was
        # bound to a known receiver at or before the call's source
        # position — calls before a ``trade_ctx = ctx`` assignment, and
        # calls after a later ``trade_ctx = something_else`` rebind, no
        # longer pick up the alias.
        state: set[str] = set(initial_receivers)

        for sub in _iter_method_body_in_source_order(scope):
            # Update alias state on simple name-to-name assignments.
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                target = sub.targets[0].id
                if isinstance(sub.value, ast.Name) and sub.value.id in state:
                    state.add(target)
                elif target in state:
                    # Rebound to a non-receiver expression — drop the alias.
                    state.discard(target)
                continue
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                target = sub.target.id
                if isinstance(sub.value, ast.Name) and sub.value.id in state:
                    state.add(target)
                elif target in state:
                    state.discard(target)
                continue

            if not isinstance(sub, ast.Call):
                continue

            current_receivers = frozenset(state)

            # 1. <ctx>.submit_order(...) — the order we count.
            if (
                isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "submit_order"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id in current_receivers
            ):
                calls.append(sub)
                continue
            # 2. self.<helper>(...) — class method. The helper's accepted
            #    receivers are bound to the call-site arguments: only
            #    parameters that received a name from the OUTER scope's
            #    live receivers are treated as ctx-bound.
            if (
                isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "self"
            ):
                helper = methods_by_name.get(sub.func.attr)
                if helper is not None:
                    helper_receivers = _resolve_helper_receivers(sub, helper, current_receivers)
                    if (id(helper), helper_receivers) not in visited_keys:
                        worklist.append((helper, helper_receivers))
                continue
            # 3. <local_name>(...) — closure defined in the same scope.
            #    Closures see two sources of context bindings:
            #    - Parameters bound at the call site (``def enter(c): ...;
            #      enter(ctx)`` binds ``c`` to ``ctx``).
            #    - Outer-scope captures (``def enter(): ctx.submit_order(
            #      ...)`` references the enclosing frame's live receivers).
            #    Captured names are visible only when the closure does
            #    NOT shadow them with its own parameter — ``def
            #    enter(ctx): ...; enter(bar)`` rebinds ``ctx`` to ``bar``,
            #    so the outer ``ctx`` is no longer reachable inside.
            if isinstance(sub.func, ast.Name):
                local = local_defs.get(sub.func.id)
                if local is not None:
                    call_bound = _resolve_helper_receivers(sub, local, current_receivers)
                    closure_param_names = frozenset(
                        arg.arg for arg in local.args.args if arg.arg != "self"
                    )
                    captured = current_receivers - closure_param_names
                    closure_receivers = captured | call_bound
                    if (id(local), closure_receivers) not in visited_keys:
                        worklist.append((local, closure_receivers))

    return calls


# Recognised ``OrderSide`` literal values. The runtime contract
# (``trading_service.strategy.contract.OrderSide``) defines exactly two
# enum members — ``LONG`` and ``SHORT`` — and ``StrategyContext.submit_order``
# coerces with ``OrderSide(side)``. ``FLAT`` / ``CLOSE`` / ``BUY`` / ``SELL``
# literals would crash at runtime, so they are NOT recognised here; the
# gate treats them as "unknown" and lets the downstream backtest surface
# the real validation error.
_RECOGNISED_SIDES = frozenset({"LONG", "SHORT"})


def _submit_order_side(node: ast.Call) -> Optional[str]:
    """Best-effort extraction of the ``side`` value from a submit_order call.

    Returns an upper-cased string when the call uses a recognised
    ``OrderSide`` literal form, else None when the side is dynamic / a
    non-OrderSide literal / can't be determined statically.
    """
    for kw in node.keywords:
        if kw.arg != "side":
            continue
        val = kw.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            upper = val.value.upper()
            return upper if upper in _RECOGNISED_SIDES else None
        if isinstance(val, ast.Attribute):
            upper = val.attr.upper()
            return upper if upper in _RECOGNISED_SIDES else None
    return None


def _submit_order_symbol(node: ast.Call) -> Optional[str]:
    """Best-effort extraction of the ``symbol`` value from a submit_order
    call. Returns the literal string when the call uses a string Constant,
    else None when the symbol is computed/dynamic or missing.

    Dynamic-symbol calls (``symbol=bar.symbol`` etc.) all share the same
    runtime symbol per ``on_bar`` invocation, so the entry/exit grouping
    treats them as one logical group (key=None).
    """
    for kw in node.keywords:
        if kw.arg != "symbol":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _group_forms_entry_exit_pair(calls: List[ast.Call]) -> bool:
    """True iff a single-symbol group of submit_order calls plausibly
    contains both an entry and an opposite-side exit leg.

    * Any call carrying a non-None ``attached_stop_loss`` /
      ``attached_take_profit`` bracket leg satisfies the group on its own.
    * Otherwise require both ``LONG`` and ``SHORT`` to appear across the
      group's calls. Same-side multiplicity (``LONG`` + ``LONG``) fails.
    * Unknown / dynamic side values are treated optimistically — the
      runtime branch may pick either direction.
    """
    if any(_has_attached_exit_kwarg(c) for c in calls):
        return True
    sides_seen: set[str] = set()
    has_unknown = False
    for c in calls:
        side = _submit_order_side(c)
        if side is None:
            has_unknown = True
        else:
            sides_seen.add(side)
    if "LONG" in sides_seen and "SHORT" in sides_seen:
        return True
    if has_unknown:
        return True
    return False


def _calls_form_entry_exit_pair(calls: List[ast.Call]) -> bool:
    """True iff the collected submit_order calls plausibly form an
    entry+exit pair on EVERY symbol they target.

    The engine closes positions per-symbol — ``portfolio.positions[bar.symbol]``
    is looked up against the order's own symbol — so a strategy that
    opens ``LONG`` on ``"SPY"`` and ``SHORT`` on ``"TLT"`` has two
    open entries and zero exits, not a balanced pair.

    Grouping rules:

    * Calls with literal-string ``symbol`` arguments form per-symbol
      groups (one per distinct literal).
    * Calls with dynamic / computed / missing ``symbol`` arguments are
      pooled in a single "unknown" group, since within one ``on_bar``
      invocation they all resolve to the same runtime symbol.
    * Dynamic-symbol calls AUGMENT each literal-symbol group's pair
      check: on a single-symbol run, a generated strategy may enter
      with ``symbol=bar.symbol`` and exit with ``symbol="SPY"`` (or
      vice versa), so the dynamic group's calls plausibly target each
      literal symbol at runtime. Each literal group passes iff
      ``literal_calls + dynamic_calls`` forms a valid pair.
    * The dynamic group also passes on its own when no literal groups
      exist (all calls dynamic).
    """
    literal_groups: Dict[str, List[ast.Call]] = {}
    dynamic_calls: List[ast.Call] = []
    for c in calls:
        sym = _submit_order_symbol(c)
        if sym is None:
            dynamic_calls.append(c)
        else:
            literal_groups.setdefault(sym, []).append(c)

    if not literal_groups:
        return _group_forms_entry_exit_pair(dynamic_calls)

    for group in literal_groups.values():
        if not _group_forms_entry_exit_pair(group + dynamic_calls):
            return False
    return True


def _has_attached_exit_kwarg(node: ast.Call) -> bool:
    """True iff the call passes a non-None ``attached_stop_loss`` or
    ``attached_take_profit``.

    Bracket / OCO orders (issue #389) bundle the exit logic onto the entry
    submission, so a single ``ctx.submit_order(..., attached_stop_loss=...)``
    is a complete entry+exit pair. Explicit ``=None`` literals are
    excluded — at the AST level ``kw.value`` is always an ``ast.AST``
    node (e.g. ``ast.Constant(value=None)``), never a Python ``None``,
    so the older ``kw.value is not None`` check would falsely accept
    an explicit ``attached_stop_loss=None`` as a real bracket leg.
    """
    for kw in node.keywords:
        if kw.arg not in ("attached_stop_loss", "attached_take_profit"):
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            continue
        return True
    return False


def _find_strategy_subclasses(tree: ast.AST) -> List[ast.ClassDef]:
    """Return every top-level class whose bases include a reference to
    ``Strategy`` or ``contract.Strategy``.

    We can't resolve inheritance across modules statically, so this is a
    syntactic check — but the harness uses the same shape (``issubclass``
    against the imported ``contract.Strategy``) and will agree with our
    classification for any direct subclass defined in the module.
    """
    out: List[ast.ClassDef] = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Strategy":
                out.append(node)
                break
            if (
                isinstance(base, ast.Attribute)
                and base.attr == "Strategy"
                and isinstance(base.value, ast.Name)
                and base.value.id == "contract"
            ):
                out.append(node)
                break
    return out


def _validate_on_bar(cls: ast.ClassDef) -> Optional[str]:
    """Return a human-readable error string if ``cls`` lacks a usable
    ``on_bar`` override, else ``None``.

    The harness requires ``on_bar(self, ctx, bar)``. Missing the method is
    allowed (the base class no-op runs and produces no trades — caught by
    anomaly gates), but a wrong signature would crash at the first call
    and deserves a clearer up-front error.
    """
    for node in ast.iter_child_nodes(cls):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "on_bar":
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            return (
                "on_bar must be a regular (non-async) method — the harness calls "
                "it synchronously once per finalised bar."
            )
        param_count = len(node.args.args)
        if param_count != 3:
            return (
                f"{cls.name}.on_bar must accept exactly 3 parameters (self, ctx, bar); "
                f"found {param_count}."
            )
        return None
    # No on_bar override — the base class no-op would emit zero trades, so
    # this is a critical failure (#547). CodeSafetyChecker.check wraps any
    # non-None return here as severity="critical".
    return (
        f"{cls.name} does not override on_bar(self, ctx, bar); the base class "
        "no-op will run and the strategy will emit zero trades."
    )


# Regex that matches Python comments and string literals (single/double,
# triple-quoted, and raw strings).  Used to produce a "code-only" view
# for look-ahead bias scanning so that examples in comments or docstrings
# don't trigger false-positive critical failures.
_COMMENTS_AND_STRINGS = re.compile(
    r"#[^\n]*"  # line comments
    r'|"""[\s\S]*?"""'  # triple-double-quoted strings
    r"|'''[\s\S]*?'''"  # triple-single-quoted strings
    r'|"(?:\\.|[^"\\])*"'  # double-quoted strings
    r"|'(?:\\.|[^'\\])*'",  # single-quoted strings
)


def _strip_comments_and_strings(code: str) -> str:
    """Replace comments and string literals with whitespace-equivalent placeholders."""
    return _COMMENTS_AND_STRINGS.sub(lambda m: " " * len(m.group()), code)


# ──────────────────────────────────────────────────────────────────────────
# Issue #524 — symbol-universe guard detection
# ──────────────────────────────────────────────────────────────────────────


def _has_universe_constant(cls: ast.ClassDef) -> bool:
    """True iff ``cls`` declares a class-level ``UNIVERSE`` bound to a
    ``frozenset``/``set``/``tuple`` literal (or a set/list/tuple displayed
    literal).

    The boilerplate uses ``UNIVERSE = frozenset({"QQQ"})`` but LLMs vary —
    ``set(...)``, ``{"QQQ"}`` (set literal), ``("QQQ",)``, and ``["QQQ"]``
    are all accepted because the runtime guard only requires ``in``-able
    membership. An empty literal is fine — when ``target_symbols`` is
    non-empty the generation contract should produce a non-empty set, but
    the gate's job is structural presence, not content equality (#526
    will gate the runtime trade-vs-target match).
    """
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "UNIVERSE":
                    return _is_collection_literal_expr(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "UNIVERSE":
                return node.value is not None and _is_collection_literal_expr(node.value)
    return False


_COLLECTION_BUILDERS = frozenset({"frozenset", "set", "tuple", "list"})


def _is_collection_literal_expr(value: ast.AST) -> bool:
    """True iff ``value`` is a set/list/tuple display or a call to one of
    the recognised collection builders (``frozenset(...)``, ``set(...)``,
    ``tuple(...)``, ``list(...)``).
    """
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return True
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id in _COLLECTION_BUILDERS
    return False


def _has_universe_guard_in_on_bar(cls: ast.ClassDef) -> bool:
    """True iff ``on_bar`` contains an early-exit of the shape
    ``if <name>.symbol not in self.UNIVERSE: return`` somewhere in its
    top-level body.

    Only ``on_bar`` is checked — the engine guards bar dispatch, so the
    other hooks don't need the same predicate. The receiver-name on the
    left is intentionally not pinned to ``bar``: the engine dispatches
    positionally and ``on_bar(self, ctx, my_bar)`` is also valid. We
    accept ``self.UNIVERSE`` and bare ``UNIVERSE`` on the right.
    """
    for node in ast.iter_child_nodes(cls):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "on_bar":
            continue
        for stmt in node.body:
            if _is_universe_guard_stmt(stmt):
                return True
        return False
    return False


def _is_universe_guard_stmt(stmt: ast.AST) -> bool:
    """True iff ``stmt`` matches ``if <Name>.symbol not in {self.,}UNIVERSE: return``."""
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return False
    if not isinstance(test.ops[0], ast.NotIn):
        return False
    # Left side: <Name>.symbol
    left = test.left
    if not (
        isinstance(left, ast.Attribute)
        and left.attr == "symbol"
        and isinstance(left.value, ast.Name)
    ):
        return False
    # Right side: self.UNIVERSE or UNIVERSE
    right = test.comparators[0]
    if isinstance(right, ast.Attribute):
        if not (
            right.attr == "UNIVERSE"
            and isinstance(right.value, ast.Name)
            and right.value.id == "self"
        ):
            return False
    elif isinstance(right, ast.Name):
        if right.id != "UNIVERSE":
            return False
    else:
        return False
    # Body must contain a ``return`` (bare or ``return None``) as the first
    # statement. ``return None`` is semantically identical to a bare
    # ``return`` and some lint styles prefer it; both are accepted.
    if not stmt.body:
        return False
    first = stmt.body[0]
    if not isinstance(first, ast.Return):
        return False
    if first.value is None:
        return True
    return isinstance(first.value, ast.Constant) and first.value.value is None
