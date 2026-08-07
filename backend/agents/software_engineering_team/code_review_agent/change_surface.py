"""Change-surface builder API for diff-first code review.

Public contract for turning PR unified patches or SE old/new file pairs into a
bounded, pre-numbered review input suitable for ``CodeReviewInput`` with
``pre_numbered=True``. Callers consume ``ChangeSurface.code`` (concatenated
``### path ###`` blocks) or ``ChangeSurface.blocks`` (path → body without
headers).

This module locks types, signatures, and empty/no-op builder contracts.
``expand_touched_ranges`` expands touched lines via Python AST when possible,
otherwise a heuristic start or capped context window. ``extract_touched_lines``
wraps GitHub unified-patch helpers for added-only touched lines. Surface
assembly is owned by follow-on work.

Pure helpers: no I/O, no LLM clients, no package-level side effects.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Collection, Mapping, Optional, Sequence

from software_engineering_team.github_source.pr_review_mapping import (
    parse_valid_lines,
)

from .function_boundaries import (
    enclosing_construct,
    enclosing_construct_start_heuristic,
)

__all__ = [
    "ChangeSurface",
    "DEFAULT_EXPANSION_CONTEXT_LINES",
    "LineRange",
    "build_change_surface_from_pairs",
    "build_change_surface_from_patches",
    "expand_touched_ranges",
    "extract_touched_lines",
    "format_change_surface_code",
]

# Max inclusive line span for heuristic / centered fallback ranges. AST hits
# keep full construct bounds; this cap only applies when falling back so a
# missing construct never expands to the whole file.
DEFAULT_EXPANSION_CONTEXT_LINES = 20


@dataclass(frozen=True)
class LineRange:
    """Inclusive 1-based line range in new-file coordinates.

    Invariants:
        - ``1 <= start_line <= end_line``.
    """

    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        """Enforce the inclusive 1-based range invariant.

        Preconditions:
            - ``start_line`` and ``end_line`` are ints (not bools).

        Postconditions:
            - Raises ``ValueError`` when ``start_line < 1`` or
              ``end_line < start_line``; otherwise the instance is valid.
        """
        if not isinstance(self.start_line, int) or isinstance(self.start_line, bool):
            raise ValueError("start_line must be an int")
        if not isinstance(self.end_line, int) or isinstance(self.end_line, bool):
            raise ValueError("end_line must be an int")
        if self.start_line < 1:
            raise ValueError("start_line must be >= 1")
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")


@dataclass(frozen=True)
class ChangeSurface:
    """Chunker-ready change surface for ``CodeReviewInput``.

    Invariants:
        - ``blocks`` maps each path to a pre-numbered body (``N: `` prefixes)
          without ``### path ###`` headers.
        - ``code`` equals ``format_change_surface_code(blocks)``.
        - Non-empty surfaces are always consumed with
          ``CodeReviewInput.pre_numbered=True``.
    """

    blocks: Mapping[str, str] = field(default_factory=dict)

    @property
    def code(self) -> str:
        """Concatenated ``### path ###`` blocks for the legacy ``code`` channel.

        Postconditions:
            - Empty ``blocks`` yields ``""``.
            - Otherwise matches the join style used by PR ``_build_review_code``.
        """
        return format_change_surface_code(self.blocks)

    @property
    def is_empty(self) -> bool:
        """True when no reviewable path bodies are present."""
        return not self.blocks

    @property
    def files_reviewed(self) -> int:
        """Number of paths present in ``blocks``."""
        return len(self.blocks)


def format_change_surface_code(blocks: Mapping[str, str]) -> str:
    """Render path → pre-numbered body mapping as ``### path ###`` blocks.

    Preconditions:
        - ``blocks`` is a mapping (may be empty). Values are treated as opaque
          body text (already pre-numbered when produced by assemblers).

    Postconditions:
        - Empty mapping → ``""``.
        - Otherwise ``"\\n\\n".join(f"### {path} ###\\n{body}" ...)`` in
          insertion order, matching PR ``_build_review_code``.
    """
    if not blocks:
        return ""
    return "\n\n".join(f"### {path} ###\n{body}" for path, body in blocks.items())


def _empty_surface() -> ChangeSurface:
    """Return the canonical empty change surface.

    Postconditions:
        - ``is_empty`` is True, ``code == ""``, ``blocks == {}``.
    """
    return ChangeSurface(blocks={})


def _mapping_has_nonblank_value(mapping: Mapping[str, str]) -> bool:
    """True when any value has non-whitespace content.

    Preconditions:
        - ``mapping`` is a mapping of path → string.

    Postconditions:
        - Returns False for ``{}`` and for mappings whose values are all blank.
    """
    return any((value or "").strip() for value in mapping.values())


def extract_touched_lines(patch: str) -> frozenset[int]:
    """Return added-only new-file line numbers from one file's unified patch.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub ``files[].patch``
          style), or empty / blank for binary / oversized / unchanged files.

    Postconditions:
        - Returns a frozenset of 1-based new-file line numbers that appear as
          added (``+``) lines in the patch.
        - Context (`` ``), removed (``-``), and ``\\ No newline at end of file``
          markers are never included.
        - Empty or blank ``patch`` → empty frozenset.
        - Never raises.
    """
    return frozenset(parse_valid_lines(patch or "", added_only=True))


def build_change_surface_from_patches(
    patches: Mapping[str, str],
    *,
    new_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
    """Build a change surface from per-path unified / PR patch text.

    Preconditions:
        - ``patches`` maps path → one file's unified-diff text (GitHub
          ``files[].patch`` style). May be empty.
        - ``new_contents``, when provided, maps path → full new-file content
          needed later for enclosing-construct expansion. Omitted/`None` is
          allowed; assembly that needs expansion will require it in follow-on
          work.

    Postconditions:
        - ``patches == {}`` or every patch value is blank → empty
          ``ChangeSurface`` (``code == ""``, ``blocks == {}``).
        - Otherwise raises ``NotImplementedError`` until patch assembly
          (#5390) is implemented. Future non-stub behavior: emit pre-numbered
          ``### path ###`` blocks with enclosing-construct expansion; identical
          / empty renders omit that path.
        - ``new_contents`` is reserved for expansion and does not affect the
          empty/no-op decision in this stub.
    """
    if not _mapping_has_nonblank_value(patches):
        return _empty_surface()
    raise NotImplementedError(
        "build_change_surface_from_patches assembly is not implemented yet "
        "(change-surface patch path)"
    )


def build_change_surface_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
    """Build a change surface from SE-style old/new content maps.

    Preconditions:
        - ``new_contents`` maps path → new-file content. May be empty.
        - ``old_contents``, when omitted/`None`, means no base for every path
          (new-file semantics in follow-on assembly). When provided, missing
          keys are treated as absent old content for that path.

    Postconditions:
        - ``new_contents == {}`` → empty ``ChangeSurface`` regardless of
          ``old_contents``.
        - Otherwise raises ``NotImplementedError`` until pair assembly (#5391)
          is implemented. Future non-stub behavior: derive unified diffs
          (difflib or equivalent), then the same pre-numbered expanded surface
          as the patch path; identical old/new yields an empty/no-op for that
          path; new files (no old) are included.
    """
    if not new_contents:
        return _empty_surface()
    raise NotImplementedError(
        "build_change_surface_from_pairs assembly is not implemented yet "
        "(change-surface old/new path)"
    )


_PYTHON_EXTS = frozenset({".py", ".pyi"})


def _path_is_python(path: str) -> bool:
    """True when ``path``'s extension is a Python source/stub suffix."""
    return os.path.splitext(path or "")[1].lower() in _PYTHON_EXTS


def _content_parses_as_python(content: str) -> bool:
    """True when ``content`` parses as a Python module.

    Postconditions:
        - Never raises; returns False on any parse failure.
    """
    try:
        ast.parse(content)
    except Exception:
        return False
    return True


def _should_use_python_ast(content: str, path: str) -> bool:
    """Decide whether the Python AST expansion path applies.

    Postconditions:
        - ``.py`` / ``.pyi`` paths always use the AST path (unparseable content
          then yields an empty result rather than a non-Python fallback).
        - Empty ``path`` uses the AST path only when ``content`` parses.
        - Other extensions return False so the capped heuristic / context-window
          fallback is used instead of AST.
    """
    if _path_is_python(path):
        return True
    if not path:
        return _content_parses_as_python(content)
    return False


def _capped_fallback_range(
    content: str, line: int, *, cap: int = DEFAULT_EXPANSION_CONTEXT_LINES
) -> LineRange:
    """Bounded range for a touched line when AST cannot resolve a construct.

    Preconditions:
        - ``line`` >= 1.
        - ``cap`` >= 1.

    Postconditions:
        - Returned range is inclusive, 1-based, within ``[1, total_lines]``,
          contains ``line`` (clamped into the file), and spans at most
          ``min(cap, total_lines)`` lines — never the whole file solely because
          a construct was missing when ``total_lines > cap``.
        - When ``enclosing_construct_start_heuristic`` finds a start and
          ``line - start + 1 <= cap``, the range begins at that start and
          extends at most ``cap`` lines (through at least ``line``).
        - Otherwise returns a window of at most ``cap`` lines centered on
          ``line`` (re-anchored near EOF as needed).
        - Never raises.
    """
    assert cap >= 1, "cap must be positive"
    total = len(content.splitlines()) or 1
    line = min(max(1, line), total)
    start = enclosing_construct_start_heuristic(content, line)
    if start is not None and line - start + 1 <= cap:
        end = min(total, max(line, start + cap - 1))
        return LineRange(start_line=start, end_line=end)
    # Centered window of at most ``cap`` lines containing ``line``.
    radius = (cap - 1) // 2
    lo = max(1, line - radius)
    hi = min(total, lo + cap - 1)
    lo = max(1, hi - cap + 1)
    return LineRange(start_line=lo, end_line=hi)


def _expand_touched_ranges_python(
    content: str, touched_lines: Collection[int]
) -> Sequence[LineRange]:
    """Map touched lines to unique enclosing construct ranges via AST.

    Preconditions:
        - ``touched_lines`` is non-empty.

    Postconditions:
        - Returns sorted unique inclusive ``LineRange`` values for every touched
          line that has an enclosing function/class (decorators included via
          ``enclosing_construct`` / ``node_start_line``).
        - Module-level lines and unparseable content fall through to the
          capped heuristic / context-window fallback (never the whole file
          solely because a construct is missing when the file exceeds the cap).
        - Never raises.
    """
    found: dict[tuple[int, int], LineRange] = {}
    for line in sorted({int(n) for n in touched_lines}):
        if line < 1:
            continue
        construct = enclosing_construct(content, line)
        if construct is not None:
            key = (construct.start_line, construct.end_line)
            found[key] = LineRange(
                start_line=construct.start_line, end_line=construct.end_line
            )
            continue
        fb = _capped_fallback_range(content, line)
        found[(fb.start_line, fb.end_line)] = fb
    return tuple(found[key] for key in sorted(found))


def _expand_touched_ranges_fallback(
    content: str, touched_lines: Collection[int]
) -> Sequence[LineRange]:
    """Map touched lines via heuristic start or capped centered windows.

    Preconditions:
        - ``touched_lines`` is non-empty.

    Postconditions:
        - Every emitted range spans at most ``DEFAULT_EXPANSION_CONTEXT_LINES``
          lines (or the full file when shorter than the cap).
        - Ranges are unique and sorted by ``(start_line, end_line)``.
        - Never raises.
    """
    found: dict[tuple[int, int], LineRange] = {}
    for line in sorted({int(n) for n in touched_lines}):
        if line < 1:
            continue
        fb = _capped_fallback_range(content, line)
        found[(fb.start_line, fb.end_line)] = fb
    return tuple(found[key] for key in sorted(found))


def expand_touched_ranges(
    content: str,
    touched_lines: Collection[int],
    *,
    path: str = "",
) -> Sequence[LineRange]:
    """Map touched new-file lines to enclosing construct ranges (or fallback).

    Preconditions:
        - ``content`` is the file's new content (plain source, not necessarily
          pre-numbered).
        - ``touched_lines`` is a collection of positive 1-based new-file line
          numbers (may be empty).
        - ``path`` is a path hint for language/heuristic selection (may be "").

    Postconditions:
        - Empty ``touched_lines`` → empty sequence ``()``.
        - For Python paths (``.py`` / ``.pyi``) or empty ``path`` with
          parseable Python content: returns unique inclusive ``LineRange``
          values for enclosing function/class constructs when AST resolves
          them (decorators included consistently with ``function_boundaries``
          / ``code_boundaries``). Lines without a construct (module-level or
          unparsable) use the capped heuristic / context-window fallback.
        - Non-Python paths use the same capped fallback for every touched line.
        - Fallback ranges never span more than
          ``DEFAULT_EXPANSION_CONTEXT_LINES`` lines (or the full file when it
          is shorter) — never the whole file solely because a construct was
          missing when the file is larger than the cap.
        - Never raises.
    """
    if not touched_lines:
        return ()
    if _should_use_python_ast(content, path):
        return _expand_touched_ranges_python(content, touched_lines)
    return _expand_touched_ranges_fallback(content, touched_lines)
