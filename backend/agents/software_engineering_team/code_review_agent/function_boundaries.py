"""Structured enclosing-construct lookup for code-review source analysis.

Two callers in this package need to know "what function/method/class/best-guess
construct encloses line N of this file": the false-positive verifier's
``find_function_at_line`` tool (``false_positive_filter.py``), which formats the
answer into a string for an LLM to read, and the side-effect consolidation pass
(``side_effect_consolidation.py``), which needs the same answer as structured
data to group findings. This module is the single place that computation lives,
so both agree on construct ranges and neither re-implements AST walking.

Pure source-analysis: no I/O, no state, never raises.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .code_boundaries import node_end_line, node_start_line

# Column-0 token prefixes that must NOT be counted as construct start lines by
# the heuristic fallback used for non-Python files.
_HEURISTIC_SKIP = ("}", ")", "]", "*/", "/*", "//", "#", "*", "...")

# The ``render_annotated_hunks`` path (coding-team PR review) prefixes each hunk
# line with its original file line number: ``4242: const x = 1;``. This pattern
# detects and strips those prefixes so the boundary helpers below receive plain
# code and a physical (1-based) line index.
_LINE_NUMBER_PREFIX_RE = re.compile(r"^(\d+): ")

# Bare inter-hunk gap marker emitted by ``render_annotated_hunks`` between
# non-contiguous hunks. Joining across it would attach a later hunk's indented
# lines to the preceding hunk's open construct — prefer per-hunk resolution.
_HUNK_SEPARATOR = "..."


@dataclass(frozen=True)
class EnclosingConstruct:
    """The innermost Python function/method/class enclosing a target line.

    Invariants:
        - ``start_line <= end_line``; both are 1-based and inclusive.
        - ``start_line`` is lowered to the earliest decorator line when the
          construct is decorated (matches ``node_start_line``).
        - ``kind`` is ``"function"`` or ``"class"``.
        - ``name`` is qualified as ``"ClassName.function_name"`` when ``kind``
          is ``"function"`` and the function's AST span is nested inside a
          class body (a direct method, or a helper function defined inside a
          method); otherwise it is the bare name.
    """

    start_line: int
    end_line: int
    name: str
    kind: str


def strip_numbered_prefixes(
    content: str, line_number: int
) -> Tuple[str, int, Optional[Callable[[int], int]]]:
    """Strip ``N: `` line-number prefixes from pre-numbered hunk content.

    The coding-team PR-review path calls ``render_annotated_hunks`` which
    prepends each line with its new-file line number: ``4242: const x = 1;``.
    This content reaches the verifier's ``CodebaseIndex`` verbatim, so the
    boundary-lookup functions below must strip those prefixes before scanning.

    Preconditions:
        - ``content`` is a string (may be empty).
        - ``line_number`` is a positive int (not a bool).

    Postconditions:
        - If the first non-blank line does NOT match ``r'^\\d+: '``, the
          content is not pre-numbered; returns ``(content, line_number, None)``
          unchanged — no remap is needed.
        - Otherwise returns ``(stripped_content, physical_index, line_mapper)``
          where:
          - ``stripped_content`` is the content with all ``N: `` prefixes
            removed. Bare ``...`` hunk-gap markers from
            ``render_annotated_hunks`` are kept as-is so
            :func:`enclosing_construct` can resolve each hunk independently
            without joining them into one AST (joining would attach a later
            hunk's indented lines to the preceding open construct).
          - ``physical_index`` is the 1-based line index in
            ``stripped_content`` whose original prefix equals ``line_number``.
            When no line matches exactly (the target line was a removed ``-``
            line absent from the hunk), the last line with prefix <
            ``line_number`` is used; falls back to 1 when nothing precedes.
          - ``line_mapper(physical)`` maps a physical line index back to its
            original file line number (or to ``physical`` if the line had no
            numbered prefix, e.g. a separator).
        - Raises ``TypeError`` / ``ValueError`` when preconditions are violated;
          otherwise never raises.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
        raise ValueError("line_number must be a positive integer")
    lines = content.splitlines()
    if not lines:
        return content, line_number, None

    first_nonblank = next((ln for ln in lines if ln.strip()), "")
    if not _LINE_NUMBER_PREFIX_RE.match(first_nonblank):
        return content, line_number, None

    stripped: List[str] = []
    phys_to_orig: Dict[int, int] = {}
    physical_index = 1
    exact_match = False
    last_before: Optional[int] = None

    for i, line in enumerate(lines, start=1):
        m = _LINE_NUMBER_PREFIX_RE.match(line)
        if m:
            orig = int(m.group(1))
            phys_to_orig[i] = orig
            stripped.append(line[m.end() :])
            if orig == line_number and not exact_match:
                physical_index = i
                exact_match = True
            elif orig < line_number:
                last_before = i
        else:
            stripped.append(line)

    if not exact_match and last_before is not None:
        physical_index = last_before

    def _lookup(phys: int) -> int:
        return phys_to_orig.get(phys, phys)

    return "\n".join(stripped), physical_index, _lookup


def _hunk_segments(lines: List[str]) -> List[Tuple[int, int, List[str]]]:
    """Split ``lines`` on bare ``...`` separators into contiguous hunks.

    Postconditions:
        - Returns ``(global_start, global_end, segment_lines)`` triples with
          1-based inclusive endpoints in the parent ``lines`` list.
        - Separator lines themselves are not included in any segment.
        - Empty when ``lines`` is empty or contains only separators.
    """
    segments: List[Tuple[int, int, List[str]]] = []
    current: List[str] = []
    start = 1
    for i, line in enumerate(lines, start=1):
        if line.strip() == _HUNK_SEPARATOR:
            if current:
                segments.append((start, i - 1, current))
                current = []
            continue
        if not current:
            start = i
        current.append(line)
    if current:
        segments.append((start, start + len(current) - 1, current))
    return segments


def _enclosing_construct_ast(content: str, line_number: int) -> Optional[EnclosingConstruct]:
    """AST-based enclosing-construct lookup over a single contiguous snippet."""
    try:
        tree = ast.parse(content)
    except Exception:
        return None

    candidates: List[Tuple[int, int, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start_line = node_start_line(node)
        end_line = node_end_line(node)
        if start_line <= line_number <= end_line:
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            candidates.append((end_line - start_line, start_line, end_line, node.name, kind))

    if not candidates:
        return None

    # Smallest span → innermost enclosing construct.
    _, func_start, func_end, name, kind = min(candidates)

    qualified_name = name
    if kind == "function":
        enclosing_classes = [
            (span, cname)
            for span, cstart, cend, cname, ckind in candidates
            if ckind == "class" and cstart <= func_start and cend >= func_end
        ]
        if enclosing_classes:
            _, class_name = min(enclosing_classes)
            qualified_name = f"{class_name}.{name}"

    return EnclosingConstruct(
        start_line=func_start, end_line=func_end, name=qualified_name, kind=kind
    )


def enclosing_construct(content: str, line_number: int) -> Optional[EnclosingConstruct]:
    """Find the innermost Python function/method/class enclosing ``line_number``.

    When ``content`` contains bare ``...`` hunk-gap markers (from
    ``render_annotated_hunks``), each gap-bounded hunk is resolved
    independently. Joining across the gap would attach a later hunk's
    indented lines to the preceding open construct and invent a false
    enclosing function; an unparseable continuation hunk (declaration
    outside the shown context) returns ``None`` rather than guessing.

    Preconditions:
        - ``content`` is a string (may be empty).
        - ``line_number`` >= 1.

    Postconditions:
        - Returns ``None`` when ``content`` fails to parse as Python, the
          target line falls in an unparseable hunk, or no
          ``FunctionDef``/``AsyncFunctionDef``/``ClassDef`` node brackets
          ``line_number`` (module level). Never raises.
        - Otherwise returns the smallest-span (innermost) enclosing node,
          with ``start_line``/``end_line`` expressed in the parent
          ``content``'s 1-based coordinates.
        - Start/end lines come from the shared ``node_start_line``/
          ``node_end_line`` helpers so AST consumers agree on ranges.
    """
    lines = content.splitlines()
    if any(line.strip() == _HUNK_SEPARATOR for line in lines):
        for seg_start, seg_end, seg_lines in _hunk_segments(lines):
            if not (seg_start <= line_number <= seg_end):
                continue
            local_line = line_number - seg_start + 1
            local = _enclosing_construct_ast("\n".join(seg_lines), local_line)
            if local is None:
                return None
            return EnclosingConstruct(
                start_line=local.start_line + seg_start - 1,
                end_line=local.end_line + seg_start - 1,
                name=local.name,
                kind=local.kind,
            )
        return None

    return _enclosing_construct_ast(content, line_number)


def enclosing_construct_start_heuristic(content: str, line_number: int) -> Optional[int]:
    """Best-guess construct start line for non-Python content.

    Scans from the first line up to ``line_number`` and returns the start line
    of the last column-0 non-comment, non-closing-delimiter line found — the
    same heuristic used by ``code_boundaries._heuristic_break_lines`` for
    chunk splitting. That includes imports, exports, assignments, and other
    top-level statements as well as function/class declarations; only blank
    lines, indented lines, and ``_HEURISTIC_SKIP`` prefixes (comments /
    closers) are ignored. Useful for TypeScript, JavaScript, Go, and other
    non-Python languages, where no name or end line is available (unlike
    :func:`enclosing_construct`).

    Preconditions:
        - ``content`` is a string (may be empty).
        - ``line_number`` >= 1.

    Postconditions:
        - Returns the best-guess start line, or ``None`` when no column-0
          non-comment, non-closing-delimiter line precedes ``line_number``.
          Never raises.
    """
    best_start: Optional[int] = None
    for i, line in enumerate(content.splitlines(), start=1):
        if i > line_number:
            break
        if not line or not line.strip():
            continue
        if line[0].isspace():
            continue
        if line.startswith(_HEURISTIC_SKIP):
            continue
        best_start = i
    return best_start
