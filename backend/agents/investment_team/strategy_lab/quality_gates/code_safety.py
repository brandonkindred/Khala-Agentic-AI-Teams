"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional

from .models import QualityGateResult

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


class CodeSafetyChecker:
    """Scan generated strategy code for unsafe patterns before subprocess execution."""

    def check(self, code: str) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        # 1. Parse the code
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"Code has a syntax error: {e}",
                )
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
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=(
                        "Code must define exactly one subclass of contract.Strategy; "
                        "none found. Use `from contract import Strategy` and `class "
                        "MyStrategy(Strategy): ...`."
                    ),
                )
            )
        elif len(strategy_classes) > 1:
            names = ", ".join(sorted(c.name for c in strategy_classes))
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=(
                        f"Code defines multiple Strategy subclasses ({names}); the "
                        "harness accepts exactly one."
                    ),
                )
            )
        else:
            strategy_cls = strategy_classes[0]
            on_bar_issue = _validate_on_bar(strategy_cls)
            if on_bar_issue is not None:
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=on_bar_issue,
                    )
                )

        # 3. Walk AST for banned imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(
                            QualityGateResult(
                                gate_name=GATE,
                                passed=False,
                                severity="critical",
                                details=f"Banned import: '{alias.name}' — network/filesystem/system access not allowed.",
                            )
                        )
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(
                            QualityGateResult(
                                gate_name=GATE,
                                passed=False,
                                severity="warning",
                                details=f"Non-allowlisted import: '{alias.name}' — may not be available in sandbox.",
                            )
                        )

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_module = node.module.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(
                            QualityGateResult(
                                gate_name=GATE,
                                passed=False,
                                severity="critical",
                                details=f"Banned import: 'from {node.module}' — network/filesystem/system access not allowed.",
                            )
                        )
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(
                            QualityGateResult(
                                gate_name=GATE,
                                passed=False,
                                severity="warning",
                                details=f"Non-allowlisted import: 'from {node.module}' — may not be available in sandbox.",
                            )
                        )

        # 4. Walk AST for banned function calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = _get_call_name(node)
                if func_name in ("exec", "eval", "compile", "__import__", "globals", "breakpoint"):
                    results.append(
                        QualityGateResult(
                            gate_name=GATE,
                            passed=False,
                            severity="critical",
                            details=f"Banned function call: '{func_name}()' — dynamic code execution not allowed.",
                        )
                    )
                if func_name == "open":
                    results.append(
                        QualityGateResult(
                            gate_name=GATE,
                            passed=False,
                            severity="critical",
                            details="Banned function call: 'open()' — file I/O not allowed in strategy code.",
                        )
                    )
                if func_name in ("setattr", "delattr"):
                    results.append(
                        QualityGateResult(
                            gate_name=GATE,
                            passed=False,
                            severity="critical",
                            details=f"Banned function call: '{func_name}()' — attribute manipulation not allowed.",
                        )
                    )

        # 5. Regex fallback for patterns AST might miss
        for pattern in _BANNED_CALL_PATTERNS:
            if pattern.search(code):
                match_text = pattern.pattern.replace(r"\b", "").replace(r"\s*\(", "(")
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=f"Regex detected banned pattern: '{match_text}'.",
                    )
                )

        # 6. Look-ahead bias detection (run against executable code only,
        #    excluding comments and string literals to avoid false positives)
        executable = _strip_comments_and_strings(code)
        for pattern, reason in _LOOKAHEAD_PATTERNS:
            if pattern.search(executable):
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=f"Look-ahead bias: {reason}",
                    )
                )

        # 7. Code length
        line_count = len(code.splitlines())
        if line_count > 1000:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=f"Code is {line_count} lines — consider simplifying (limit: 1000).",
                )
            )

        # 8. Order-flow shape (#547): every viable strategy must call
        #    ``ctx.submit_order(...)`` from inside one of the engine-callable
        #    hook methods (``on_bar``, ``on_start``, ``on_fill``, ``on_end``),
        #    and the reachable calls must form a real entry+exit pair —
        #    either two calls with distinct ``side`` values, or a single
        #    call with a non-None ``attached_stop_loss`` /
        #    ``attached_take_profit`` (bracket / OCO support, #389).
        #
        #    * Engine hooks accept only their second positional parameter
        #      as the context receiver (``def on_bar(self, ctx, bar):``
        #      looks for ``ctx.submit_order``; a swapped
        #      ``def on_bar(self, bar, ctx):`` accepts only ``bar`` and
        #      its ``submit_order`` would crash at runtime — flagged).
        #    * Helpers reached via ``self.<method>(...)`` from a hook
        #      relax the receiver check to any non-``self`` positional
        #      parameter, since the call site we can't statically resolve
        #      may pass the context through.
        #    * Two ``side="LONG"`` calls do not form an entry+exit pair;
        #      a real exit requires the opposite side or a bracket leg.
        if len(strategy_classes) == 1:
            hook_calls, has_entry_capable_call = _collect_hook_submit_calls(strategy_classes[0])
            if len(hook_calls) == 0:
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=(
                            "No ctx.submit_order call found inside on_bar / on_start / "
                            "on_fill / on_end — strategy has no entry path reachable "
                            "from the engine and would emit zero trades."
                        ),
                    )
                )
            elif not has_entry_capable_call:
                # All reachable submit_order calls live in on_fill / on_end,
                # which the runtime only invokes AFTER a prior fill / at
                # stream end. With no order originating from on_bar /
                # on_start, the strategy never seeds a position and emits
                # zero trades.
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=(
                            "ctx.submit_order calls were found only in on_fill / on_end. "
                            "Those hooks run after a prior fill / after the data stream "
                            "and cannot bootstrap a position; the strategy needs at "
                            "least one submit_order in on_bar or on_start."
                        ),
                    )
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
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=detail,
                    )
                )

        if not results:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details="Code passed all safety checks.",
                )
            )

        return results


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node (handles simple names and attribute access)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


# Engine-callable hook method names (``contract.Strategy``). The runtime
# invokes each positionally, so the parameter NAME the strategy chooses
# for the context object is the strategy's choice — we read it off the
# signature rather than hard-coding ``ctx``.
_ENGINE_HOOK_METHODS = frozenset({"on_bar", "on_start", "on_fill", "on_end"})

# Hooks that can BOOTSTRAP a position. ``on_bar`` runs on every finalised
# bar; ``on_start`` runs once before the first bar. ``on_fill`` only runs
# AFTER a prior order fill (so it can't seed the first order) and
# ``on_end`` runs after the data stream — neither can initiate trading
# on its own. The order-flow gate requires at least one ``submit_order``
# reachable from an entry-capable hook to avoid the false-positive of a
# strategy whose only orders sit in ``on_fill`` / ``on_end``.
_ENTRY_CAPABLE_HOOK_METHODS = frozenset({"on_bar", "on_start"})


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


def _collect_hook_submit_calls(cls: ast.ClassDef) -> tuple[List[ast.Call], bool]:
    """Return reachable ``submit_order(...)`` calls and an entry-reachability flag.

    Returns a tuple ``(calls, has_entry_capable_call)``:

    * ``calls`` is every ``submit_order`` call statically reachable from
      any engine hook (or from a helper invoked transitively via
      ``self.<method>(...)``).
    * ``has_entry_capable_call`` is True iff at least one call originates
      from a hook that can initiate trading — ``on_bar`` or ``on_start``
      directly, or a helper reachable from one of those hooks.
      ``on_fill`` and ``on_end`` cannot bootstrap a position on their
      own; a strategy whose only orders sit in those hooks will never
      submit anything in practice.

    Walks the AST but stops at nested function/class boundaries — an
    uninvoked local ``def`` inside ``on_bar`` containing
    ``ctx.submit_order(...)`` does NOT satisfy the gate, because Python
    only creates the function object and never executes its body.

    Engine hooks are dispatched positionally; only the second positional
    parameter is the StrategyContext. Helpers reached via
    ``self.<method>(...)`` relax to any non-``self`` positional parameter
    since the call site can pass the context through any of them.
    """
    methods_by_name: Dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.iter_child_nodes(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods_by_name[node.name] = node

    calls: List[ast.Call] = []
    visited: set[str] = set()
    # Worklist entries: (method, is_hook, from_entry_capable_root). is_hook
    # restricts the accepted receiver to the second positional parameter
    # only; from_entry_capable_root tracks whether this method is reached
    # transitively from on_bar / on_start so we can flag the entry path.
    has_entry_capable_call = False
    worklist: List[tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool, bool]] = []
    for name, m in methods_by_name.items():
        if name in _ENGINE_HOOK_METHODS:
            worklist.append((m, True, name in _ENTRY_CAPABLE_HOOK_METHODS))

    while worklist:
        method, is_hook, from_entry = worklist.pop()
        # Multiple roots may reach the same helper. Visit each
        # (method, from_entry) combination at most once so a helper
        # reached from both on_bar and on_fill still propagates the
        # entry-capable flag rather than getting masked by the first
        # non-entry visit.
        visit_key = f"{method.name}#{1 if from_entry else 0}"
        if visit_key in visited:
            continue
        visited.add(visit_key)

        if is_hook:
            # Engine hook: only the second positional is the context.
            # ``bar``/``fill`` siblings do not have ``submit_order``.
            if len(method.args.args) >= 2:
                receiver_names = {method.args.args[1].arg}
            else:
                receiver_names = set()
        else:
            # Helper reached from a hook: accept any positional parameter
            # except ``self`` — we can't statically track which one was
            # bound to the context, but any of them could be.
            receiver_names = {arg.arg for arg in method.args.args if arg.arg != "self"}

        for sub in _iter_method_body_nodes(method):
            if not isinstance(sub, ast.Call):
                continue
            if (
                isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "submit_order"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id in receiver_names
            ):
                calls.append(sub)
                if from_entry:
                    has_entry_capable_call = True
                continue
            # Queue ``self.<helper>(...)`` for traversal as a helper.
            if (
                isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "self"
            ):
                helper = methods_by_name.get(sub.func.attr)
                if helper is not None:
                    next_key = f"{helper.name}#{1 if from_entry else 0}"
                    if next_key not in visited:
                        worklist.append((helper, False, from_entry))

    return calls, has_entry_capable_call


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


def _calls_form_entry_exit_pair(calls: List[ast.Call]) -> bool:
    """True iff the collected submit_order calls plausibly include both an
    entry and an exit leg.

    The runtime contract closes a position by submitting an opposite-side
    order (``LONG`` closes a ``SHORT``, ``SHORT`` closes a ``LONG``) with
    ``qty == position.qty``. We approximate statically:

    * If any call carries ``attached_stop_loss`` / ``attached_take_profit``
      (a non-None bracket leg), it brings its own exit.
    * Otherwise, require both ``LONG`` and ``SHORT`` to appear across the
      collected calls. Same-side multiplicity (``LONG`` + ``LONG``) is one-
      sided and fails the gate.
    * Calls whose side cannot be determined statically (computed expression
      that picks the direction from position / signal state) are treated
      optimistically — even a single such call passes, since legitimate
      strategies route both entry and exit through one call site whose
      side is a runtime branch.
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
    # Distinct LONG + SHORT means at least one is opposite-side closing the other.
    if "LONG" in sides_seen and "SHORT" in sides_seen:
        return True
    # Dynamic side: accept any number of calls (including a single one) that
    # route entry/exit through a runtime branch — false-failing these is
    # worse than letting the backtest surface a genuinely one-sided runtime.
    if has_unknown:
        return True
    return False


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
