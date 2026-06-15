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
import os

# Extensions parsed with Python's ``ast`` for exact, decorator-aware ranges.
_PYTHON_EXTS = frozenset({".py", ".pyi"})

# A heuristic break line must not *start* with one of these tokens: a bare
# closing bracket, a block-comment close, or a line comment would otherwise be
# cut away from the block it belongs to.
_HEURISTIC_SKIP_PREFIXES = ("}", ")", "]", "*/", "//", "#")


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
        - Never raises: on a parse error, minified/single-line input, unknown
          language, or empty content it returns ``frozenset()``, which the
          caller treats as "no preferred breaks → split on line boundaries".
        - Pure: no I/O, no mutation of the arguments or module state.
    """
    if not content or not content.strip():
        return frozenset()
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _PYTHON_EXTS:
        breaks = _python_break_lines(content)
        if breaks:
            return breaks
        # An empty Python result means the source did not parse (e.g. a partial
        # snippet under review); fall through to the language-agnostic heuristic
        # rather than giving up, since column-0 ``def``/``class`` lines are still
        # good cut points.
    return _heuristic_break_lines(content)


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
    except (SyntaxError, ValueError):
        return frozenset()
    breaks: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            for dec in node.decorator_list:
                start = min(start, dec.lineno)
            breaks.add(start)
    return frozenset(breaks)


def _heuristic_break_lines(content: str) -> frozenset[int]:
    """Column-0 declaration lines, for non-Python (or unparseable) source.

    A line is a break point when it is non-blank, begins at column 0 (no
    leading whitespace), and does not start with a closing/continuation/comment
    token. Across brace and indentation languages, top-level declarations
    (``function``/``class``/``const x = () =>``/``export``/``interface``/
    ``@Component``/``def``) sit at column 0, while their bodies are indented —
    so this finds construct boundaries without parsing the language.

    Postconditions:
        - Returns 1-based line numbers in ``[1, len(content.splitlines())]``.
        - Returns ``frozenset()`` for minified/single-construct input with no
          interior column-0 lines, so the caller falls back to line boundaries.
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
