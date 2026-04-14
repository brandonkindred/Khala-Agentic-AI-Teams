"""AST + regex code safety scanner for generated strategy Python code."""

from __future__ import annotations

import ast
import re
from typing import List

from .models import QualityGateResult

GATE = "code_safety"

BANNED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "socket", "http", "urllib", "requests",
    "shutil", "pathlib", "importlib", "ctypes", "pickle", "shelve",
    "sqlite3", "multiprocessing", "threading", "signal", "io",
    "tempfile", "glob", "webbrowser", "ftplib", "smtplib", "telnetlib",
    "xmlrpc", "asyncio",
})

ALLOWED_IMPORTS = frozenset({
    "pandas", "numpy", "indicators", "math", "datetime", "collections",
    "itertools", "functools", "typing", "dataclasses", "enum", "abc",
    "re", "copy", "statistics", "decimal", "fractions", "operator",
    "json",
})

# Regex patterns for dangerous calls that AST analysis might miss in edge cases.
_BANNED_CALL_PATTERNS = [
    re.compile(r"\bexec\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bcompile\s*\("),
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\bglobals\s*\("),
    re.compile(r"\bbreakpoint\s*\("),
]


class CodeSafetyChecker:
    """Scan generated strategy code for unsafe patterns before subprocess execution."""

    def check(self, code: str) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        # 1. Parse the code
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details=f"Code has a syntax error: {e}",
            ))
            return results

        # 2. Check run_strategy function exists with correct signature
        run_strategy_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_strategy":
                run_strategy_found = True
                param_count = len(node.args.args)
                if param_count != 2:
                    results.append(QualityGateResult(
                        gate_name=GATE, passed=False, severity="critical",
                        details=f"run_strategy() must accept exactly 2 parameters (data, config), found {param_count}.",
                    ))
                break

        if not run_strategy_found:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details="Code does not define a run_strategy() function.",
            ))

        # 3. Walk AST for banned imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(QualityGateResult(
                            gate_name=GATE, passed=False, severity="critical",
                            details=f"Banned import: '{alias.name}' — network/filesystem/system access not allowed.",
                        ))
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(QualityGateResult(
                            gate_name=GATE, passed=False, severity="warning",
                            details=f"Non-allowlisted import: '{alias.name}' — may not be available in sandbox.",
                        ))

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_module = node.module.split(".")[0]
                    if top_module in BANNED_IMPORTS:
                        results.append(QualityGateResult(
                            gate_name=GATE, passed=False, severity="critical",
                            details=f"Banned import: 'from {node.module}' — network/filesystem/system access not allowed.",
                        ))
                    elif top_module not in ALLOWED_IMPORTS:
                        results.append(QualityGateResult(
                            gate_name=GATE, passed=False, severity="warning",
                            details=f"Non-allowlisted import: 'from {node.module}' — may not be available in sandbox.",
                        ))

        # 4. Walk AST for banned function calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = _get_call_name(node)
                if func_name in ("exec", "eval", "compile", "__import__", "globals", "breakpoint"):
                    results.append(QualityGateResult(
                        gate_name=GATE, passed=False, severity="critical",
                        details=f"Banned function call: '{func_name}()' — dynamic code execution not allowed.",
                    ))
                if func_name == "open":
                    results.append(QualityGateResult(
                        gate_name=GATE, passed=False, severity="critical",
                        details="Banned function call: 'open()' — file I/O not allowed in strategy code.",
                    ))
                if func_name in ("setattr", "delattr"):
                    results.append(QualityGateResult(
                        gate_name=GATE, passed=False, severity="critical",
                        details=f"Banned function call: '{func_name}()' — attribute manipulation not allowed.",
                    ))

        # 5. Regex fallback for patterns AST might miss
        for pattern in _BANNED_CALL_PATTERNS:
            if pattern.search(code):
                match_text = pattern.pattern.replace(r"\b", "").replace(r"\s*\(", "(")
                results.append(QualityGateResult(
                    gate_name=GATE, passed=False, severity="critical",
                    details=f"Regex detected banned pattern: '{match_text}'.",
                ))

        # 6. Code length
        line_count = len(code.splitlines())
        if line_count > 1000:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="warning",
                details=f"Code is {line_count} lines — consider simplifying (limit: 1000).",
            ))

        if not results:
            results.append(QualityGateResult(
                gate_name=GATE, passed=True, severity="info",
                details="Code passed all safety checks.",
            ))

        return results


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node (handles simple names and attribute access)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""
