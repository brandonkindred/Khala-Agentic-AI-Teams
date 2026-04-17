"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
import re
from typing import List, Optional

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
    # No on_bar at all — not strictly fatal (base class no-op), but no
    # orders will be emitted so flag it.
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
