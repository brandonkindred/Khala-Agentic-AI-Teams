"""Class/method unit extraction for the code-review engine's cohesion pass.

Pure, LLM-free source analysis that splits a Python file into its top-level
classes, each with a compact, body-free summary of its methods (signature +
docstring). The class-level cohesion review is built on these units: it judges
whether a class's methods collectively serve the class's stated purpose (its
name + docstring), catching single-responsibility violations, methods that do
not belong, and purpose/behavior mismatches that a per-function review reading
one method at a time cannot see.

This module mirrors ``code_boundaries.py``'s contract: it does no I/O, holds no
state, and never raises. Only Python (``.py``/``.pyi``) is parsed; every other
language — and any Python source that fails to parse — yields ``[]`` so the
cohesion pass simply does not run for it (the per-function map review still
covers that code). That guarantees the extractor can never make a review worse
than before.
"""

from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass
from typing import List, Tuple, Union

logger = logging.getLogger(__name__)

# Extensions parsed with Python's ``ast``; cohesion extraction is Python-only.
_PYTHON_EXTS = frozenset({".py", ".pyi"})

_FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


@dataclass(frozen=True)
class MethodSummary:
    """A compact, body-free view of one method for the cohesion prompt.

    Invariants:
        - ``signature`` is the method's ``def``/``async def`` line without its
          body (e.g. ``def add(self, x: int) -> int:``).
        - ``start_line``/``end_line`` are 1-based, inclusive, and fall within the
          enclosing class's own line range.
    """

    name: str
    signature: str
    docstring: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ClassUnit:
    """One class plus the method summaries the cohesion pass evaluates.

    Invariants:
        - ``start_line``/``end_line`` are 1-based, inclusive, and bracket the
          whole class (the earliest decorator line through the class body's last
          line).
        - ``methods`` lists the class's directly-defined methods in source order;
          nested/inner classes and their methods are not recursed into.
        - ``docstring`` is the class docstring ('' when absent) — the stated
          purpose the pass judges its methods against.
    """

    name: str
    docstring: str
    start_line: int
    end_line: int
    methods: Tuple[MethodSummary, ...] = ()


def extract_classes(path: str, content: str) -> List[ClassUnit]:
    """Extract top-level classes (with method summaries) from a Python file.

    Preconditions:
        - ``path`` names the file (used only to detect the language by its
          extension; '' is treated as non-Python) and ``content`` is its full
          text. Neither is None.

    Postconditions:
        - Returns one ``ClassUnit`` per top-level class defined in ``content``,
          in source order, each carrying a ``MethodSummary`` for every method
          defined directly in the class body.
        - Returns ``[]`` for non-Python files, empty/blank content, content that
          fails to parse, and Python files with no top-level class. Never raises.
        - Pure: no I/O, no mutation of the arguments or module state.
    """
    if not content or not content.strip():
        return []
    ext = os.path.splitext(path or "")[1].lower()
    if ext not in _PYTHON_EXTS:
        return []
    try:
        tree = ast.parse(content)
    except Exception as exc:
        # Honor "never raises": SyntaxError/ValueError on malformed source, but
        # also RecursionError/MemoryError on pathological input. Any failure
        # degrades to "no classes", exactly like code_boundaries.
        logger.debug("code_units: ast parse failed for %s: %s", path or "<unknown>", exc)
        return []
    return [_class_unit(node) for node in tree.body if isinstance(node, ast.ClassDef)]


def _node_start_line(node: ast.AST) -> int:
    """1-based start line of ``node``, accounting for decorators.

    Postconditions:
        - Returns ``node.lineno`` lowered to the earliest decorator line when the
          node is decorated, so a range starts above its decorators.
    """
    start = node.lineno  # type: ignore[attr-defined]
    for dec in getattr(node, "decorator_list", None) or []:
        start = min(start, dec.lineno)
    return start


def _node_end_line(node: ast.AST) -> int:
    """1-based inclusive end line of ``node`` (falls back to its start line).

    Postconditions:
        - Returns ``node.end_lineno`` when present (Python 3.8+), else the
          node's own ``lineno`` so the range is always well-formed.
    """
    return getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]


def _class_unit(node: ast.ClassDef) -> ClassUnit:
    """Build a ``ClassUnit`` (with direct-method summaries) for a class node.

    Postconditions:
        - ``methods`` covers only functions defined directly in the class body,
          in source order; the class docstring is '' when absent.
    """
    methods = tuple(
        _method_summary(child)
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return ClassUnit(
        name=node.name,
        docstring=ast.get_docstring(node) or "",
        start_line=_node_start_line(node),
        end_line=_node_end_line(node),
        methods=methods,
    )


def _method_summary(node: _FunctionNode) -> MethodSummary:
    """Build a body-free ``MethodSummary`` for a function/method node.

    Postconditions:
        - ``signature`` is the single-line ``def``/``async def`` header;
          ``docstring`` is '' when the method has none.
    """
    return MethodSummary(
        name=node.name,
        signature=_signature(node),
        docstring=ast.get_docstring(node) or "",
        start_line=_node_start_line(node),
        end_line=_node_end_line(node),
    )


def _signature(node: _FunctionNode) -> str:
    """Render the body-free ``def`` line for a function/method node.

    Postconditions:
        - Returns ``def name(args) -> ret:`` (or ``async def ...``) as one line;
          falls back to ``def name(...):`` if arguments cannot be unparsed. Never
          raises.
    """
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 - a best-effort signature must never break extraction
        return f"{prefix} {node.name}(...):"
    ret = ""
    if node.returns is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:  # noqa: BLE001 - drop only the return annotation on failure
            ret = ""
    return f"{prefix} {node.name}({args}){ret}:"
