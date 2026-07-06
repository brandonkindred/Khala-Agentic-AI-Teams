"""Function/method boundary detection for code-review chunking.

Pure source-analysis helpers that name the lines where a new top-level code
construct begins, so the coordinator can cut oversized files *between* whole
functions/methods/classes instead of mid-body. The detector degrades to "no
boundaries" on anything it cannot parse (partial code, minified code, unknown
language), which the caller reads as "fall back to line-boundary splitting" —
so it never makes splitting worse than the previous behavior.

This module does no I/O and holds no state; every function is a pure mapping
from ``(path, content)`` (or ``content``) to a set of 1-based line numbers.
"""

from __future__ import annotations

import ast
import logging
import os

logger = logging.getLogger(__name__)

# Extensions parsed with Python's ``ast`` for exact, decorator-aware ranges.
_PYTHON_EXTS = frozenset({".py", ".pyi"})

# A heuristic break line must not *start* with one of these tokens: a bare
# closing bracket, a block-comment open/close, or a line comment would
# otherwise be cut away from the block it belongs to.
_HEURISTIC_SKIP_PREFIXES = ("}", ")", "]", "*/", "/*", "//", "#")


def preferred_break_lines(path: str, content: str) -> frozenset[int]:
    """Name the 1-based lines of ``content`` that start a top-level construct.

    A returned line number ``b`` means "line ``b`` begins a new function,
    method, class, or top-level declaration, so cutting *before* it keeps the
    preceding construct whole." Numbers are relative to ``content`` (line 1 is
    its first line), matching how the coordinator's splitter counts lines.

    Preconditions:
        - ``content`` is the full text of one file block (``path`` may be '' for
          headerless code); neither argument is None.

    Postconditions:
        - Every returned number is in ``[1, len(content.splitlines())]``.
        - Never raises. On a parse error, unknown language, or empty/blank
          content it returns ``frozenset()``; for minified/single-line input it
          may return ``frozenset({1})``. Either way the caller falls back to
          line-boundary splitting, because the splitter never cuts before line 1
          and treats the absence of *interior* breaks as "no preferred breaks".
        - Pure: no I/O, no mutation of the arguments or module state.
    """
    if not content or not content.strip():
        return frozenset()
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _PYTHON_EXTS:
        breaks = _python_break_lines(content)
        if breaks:
            return breaks
        # An empty Python result means either the source did not parse (e.g. a
        # partial snippet under review) or it has no top-level functions/classes
        # (a script of bare statements). In both cases fall through to the
        # language-agnostic heuristic rather than giving up, since column-0
        # ``def``/``class`` lines are still good cut points.
    return _heuristic_break_lines(content)


def node_start_line(node: ast.AST) -> int:
    """1-based start line of a def/class node, lowered to its earliest decorator.

    The single source of the "a construct starts above its decorators" rule,
    shared by the boundary detector here, the class/method extractor
    (``code_units``), and the enclosing-construct finder
    (``false_positive_filter``), so all three agree on where a construct begins.

    Preconditions:
        - ``node`` has a ``lineno`` (a def/class/statement node).

    Postconditions:
        - Returns ``node.lineno`` lowered to the earliest decorator line when the
          node is decorated, else ``node.lineno``. Never raises.
    """
    start = node.lineno  # type: ignore[attr-defined]
    for dec in getattr(node, "decorator_list", None) or []:
        start = min(start, dec.lineno)
    return start


def node_end_line(node: ast.AST) -> int:
    """1-based inclusive end line of ``node`` (``end_lineno``, else ``lineno``).

    Postconditions:
        - Returns ``node.end_lineno`` when present (Python 3.8+), else the node's
          own ``lineno`` so the range is always well-formed. Never raises.
    """
    return getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]


def _python_break_lines(content: str) -> frozenset[int]:
    """Top-level def/async-def/class start lines via ``ast``.

    Postconditions:
        - Returns the start line of every top-level function/class in
          ``content``; for a decorated construct, the earliest decorator line
          (so a cut lands above the decorators, never between a decorator and
          its def).
        - Returns ``frozenset()`` on ``SyntaxError`` or any other parse failure,
          so the caller can fall back instead of crashing on partial code.
    """
    try:
        tree = ast.parse(content)
    except Exception as exc:
        # Honor the "never raises" postcondition: SyntaxError/ValueError on
        # malformed source, but also RecursionError on deeply nested input,
        # MemoryError, etc. Any parse failure degrades to "no boundaries".
        # Log at debug so unexpected failures are still diagnosable.
        logger.debug("ast parse failed during break-line detection: %s", exc)
        return frozenset()
    breaks: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            breaks.add(node_start_line(node))
    return frozenset(breaks)


def _heuristic_break_lines(content: str) -> frozenset[int]:
    """Column-0 declaration lines, for non-Python (or unparseable) source.

    A line is a break point when it is non-blank, begins at column 0 (no
    leading whitespace), and does not start with a closing/continuation/comment
    token. Across brace and indentation languages, top-level declarations
    (``function``/``class``/``const x = () =>``/``export``/``interface``/
    ``@Component``/``def``) sit at column 0, while their bodies are indented —
    so this finds construct boundaries without parsing the language.

    Known limitation: a column-0 *continuation* keyword (``else``/``elif``/
    ``catch``/``finally`` followed by ``{`` or ``:``) is treated as a boundary,
    so a top-level if/else or try/catch chain can be split between its arms.
    This only yields a *suboptimal* split (a slightly different chunk edge); it
    never severs a function/method/class, which is the guarantee callers rely
    on. Keyword matching is intentionally avoided here because ``startswith``
    cannot distinguish a continuation keyword from an identifier with the same
    prefix (e.g. a top-level ``catchAll = ...``), and a missed continuation is
    cheaper than a wrongly-skipped real declaration.

    Postconditions:
        - Returns 1-based line numbers in ``[1, len(content.splitlines())]``.
        - Returns ``frozenset()`` only for truly empty/blank content. Single-line
          input returns ``frozenset({1})`` (line 1 is column-0 and not skipped);
          since the splitter never cuts before line 1, this still degrades to
          line-boundary splitting — there are no *interior* breaks to use.
    """
    breaks: set[int] = set()
    for i, line in enumerate(content.splitlines(), start=1):
        if not line or not line.strip():
            continue
        if line[0].isspace():
            continue
        # Leading whitespace is already ruled out above, so the line starts at
        # column 0 — no lstrip needed before checking the skip prefixes.
        if line.startswith(_HEURISTIC_SKIP_PREFIXES):
            continue
        breaks.add(i)
    return frozenset(breaks)
