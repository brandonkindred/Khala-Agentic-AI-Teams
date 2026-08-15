"""Change-surface builder API for diff-first code review.

Public contract for turning PR unified patches or SE old/new file pairs into a
bounded, pre-numbered review input suitable for ``CodeReviewInput`` with
``pre_numbered=True``. Callers consume ``ChangeSurface.blocks`` (path → body
without headers) via ``CodeReviewInput.files=``.

This module locks types, signatures, and empty/no-op builder contracts.
``expand_touched_ranges`` expands touched lines via Python AST when possible,
otherwise a heuristic start or capped context window. ``extract_touched_lines``
wraps GitHub unified-patch helpers for added-only touched lines. The rendered
body marks each added/modified (touched) line with a leading ``+`` gutter
column and each enclosing context line with a space, so the reviewer has direct
evidence of the change surface; the marker sits before the line number and never
shifts the 1-based numbers the posting/mapping layer depends on.
``render_patch_hunks`` wraps annotated hunk rendering for the same patch text.
Surface assembly from unified patches is implemented; ``unified_diffs_from_pairs``
derives per-path diffs from SE old/new maps. Full pairs surface assembly
remains follow-on work.

Pure helpers: no I/O, no LLM clients, no package-level side effects.
"""

from __future__ import annotations

import ast
import difflib
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Collection, Mapping, Optional, Sequence

from software_engineering_team.github_source.pr_review_mapping import (
    CONTEXT_LINE_MARKER,
    TOUCHED_LINE_MARKER,
    format_numbered_source_line,
    numbered_line_width,
    parse_valid_lines,
    render_annotated_hunks,
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
    "render_patch_hunks",
    "unified_diffs_from_pairs",
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
        - ``blocks`` maps each path to a pre-numbered body (``N| `` prefixes,
          each line carrying a leading ``+``/space change-surface marker column)
          without ``### path ###`` headers.
        - Non-empty surfaces are always consumed with
          ``CodeReviewInput.pre_numbered=True``.
    """

    blocks: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when no reviewable path bodies are present."""
        return not self.blocks

    @property
    def files_reviewed(self) -> int:
        """Number of paths present in ``blocks``."""
        return len(self.blocks)


def _empty_surface() -> ChangeSurface:
    """Return the canonical empty change surface.

    Postconditions:
        - ``is_empty`` is True, ``blocks == {}``.
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


def render_patch_hunks(patch: str) -> str:
    """Render annotated hunk text for one file's unified / PR patch.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub ``files[].patch``
          style), or empty / blank for binary / oversized / unchanged files.

    Postconditions:
        - Return value is identical to ``render_annotated_hunks(patch)`` for
          every input (string equality).
        - Empty or blank ``patch`` → ``""``.
        - Never raises.
    """
    return render_annotated_hunks(patch or "")


def unified_diffs_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build per-path unified diffs from SE-style old/new content maps.

    Preconditions:
        - ``new_contents`` maps path → new-file text (may be empty).
        - ``old_contents``, when omitted/`None`, means empty old for every
          path. When provided, missing keys are treated as empty old for
          that path.

    Postconditions:
        - ``new_contents == {}`` → ``{}``.
        - Result contains exactly the keys of ``new_contents`` (insertion
          order preserved).
        - For each path: if resolved old text equals new text → ``""``;
          otherwise a non-empty ``difflib.unified_diff`` string with
          ``fromfile=f"a/{path}"``, ``tofile=f"b/{path}"``, using
          ``splitlines(keepends=True)``.
        - Paths present only in ``old_contents`` are ignored.
        - Never raises for well-typed string mappings.
    """
    if not new_contents:
        return {}
    old_map = old_contents  # None means empty old for every path
    out: dict[str, str] = {}
    for path, new_text in new_contents.items():
        if old_map is None:
            old_text = ""
        else:
            old_text = old_map[path] if path in old_map else ""
        if old_text == new_text:
            out[path] = ""
            continue
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        out[path] = "".join(diff)
    return out


def _merge_line_ranges(ranges: Sequence[LineRange]) -> tuple[LineRange, ...]:
    """Merge overlapping or adjacent inclusive line ranges.

    Preconditions:
        - ``ranges`` is a sequence of valid ``LineRange`` values (may be empty).

    Postconditions:
        - Returns sorted unique merged ranges where each next range starts at
          ``prev.end_line + 2`` or later (overlap or ``start <= end + 1`` merges).
        - Empty input → ``()``.
        - Never raises for valid ``LineRange`` inputs.
    """
    if not ranges:
        return ()
    ordered = sorted(ranges, key=lambda r: (r.start_line, r.end_line))
    merged: list[LineRange] = [ordered[0]]
    for r in ordered[1:]:
        cur = merged[-1]
        if r.start_line <= cur.end_line + 1:
            merged[-1] = LineRange(
                start_line=cur.start_line,
                end_line=max(cur.end_line, r.end_line),
            )
        else:
            merged.append(r)
    return tuple(merged)


def _pre_number_ranges(
    content: str,
    ranges: Sequence[LineRange],
    touched: Collection[int] = (),
) -> str:
    """Render merged-or-raw ranges as pre-numbered body text with gap markers.

    Preconditions:
        - ``content`` is the full new-file text (may be empty).
        - ``ranges`` is a sequence of inclusive 1-based ``LineRange`` values
          (caller should merge first when desired).
        - ``touched`` is the set of 1-based new-file line numbers that were
          added/modified (e.g. ``extract_touched_lines(patch)``). May be empty.

    Postconditions:
        - Emits a column-aligned ``N| <line>`` gutter for each line in each
          range, clamped to the file's last line when ``end_line`` exceeds
          length. Gutter width is the widest emitted line number so hanging
          indents stay visually 4 columns across 9→10 / 99→100.
        - When ``touched`` is non-empty, every emitted source line additionally
          carries a single leading marker column — ``+`` when its number is in
          ``touched`` (added/modified), a space otherwise (enclosing context) —
          so the reviewer can tell the change surface from the context it was
          given. The marker sits BEFORE the number, so the rendered line NUMBER
          is unchanged (the number a citation maps against is identical with or
          without the marker). When ``touched`` is empty, no marker column is
          emitted (the body is byte-identical to the un-marked rendering).
        - Between successive ranges, inserts a bare ``...`` line (never marked).
        - Empty ``ranges`` or empty file with no emitable lines → ``\"\"``.
        - Never raises.
    """
    lines = content.splitlines()
    if not ranges or not lines:
        return ""
    total = len(lines)
    touched_set = {int(n) for n in touched}
    mark = bool(touched_set)
    rows: list[tuple[Optional[int], str]] = []
    for idx, r in enumerate(ranges):
        if idx > 0:
            rows.append((None, "..."))
        start = min(max(1, r.start_line), total)
        end = min(max(start, r.end_line), total)
        for n in range(start, end + 1):
            rows.append((n, lines[n - 1]))
    width = numbered_line_width(n for n, _ in rows if n is not None)

    def _marker(n: int) -> str:
        if not mark:
            return ""
        return TOUCHED_LINE_MARKER if n in touched_set else CONTEXT_LINE_MARKER

    return "\n".join(
        text if n is None else format_numbered_source_line(n, text, width=width, marker=_marker(n))
        for n, text in rows
    )


def _assemble_path_block(path: str, patch: str, content: str) -> Optional[str]:
    """Build one path's pre-numbered body, or ``None`` to omit the path.

    Preconditions:
        - ``path`` is the review path key (may be empty string).
        - ``patch`` is one file's unified-diff text.
        - ``content`` is the full new-file text for expansion (caller must not
          pass blank content; blank is treated as omit).

    Postconditions:
        - Blank ``content`` → ``None``.
        - Empty ``extract_touched_lines(patch)`` → ``None``.
        - Otherwise expands, merges, and pre-numbers, marking the added/modified
          (touched) lines distinctly from enclosing context (see
          ``_pre_number_ranges``); empty body → ``None``.
        - Never raises.
    """
    if not (content or "").strip():
        return None
    touched = extract_touched_lines(patch)
    if not touched:
        return None
    ranges = expand_touched_ranges(content, touched, path=path)
    merged = _merge_line_ranges(ranges)
    body = _pre_number_ranges(content, merged, touched)
    if not body.strip():
        return None
    return body


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
          used for enclosing-construct expansion. Omitted/`None` means no
          content for any path (all non-blank patches are omitted).

    Postconditions:
        - ``patches == {}`` or every patch value is blank → empty
          ``ChangeSurface`` (``blocks == {}``).
        - For each path with a non-blank patch, in iteration order: omit when
          ``new_contents`` is missing/blank for that path, when there are no
          added touched lines, or when the assembled body is empty; otherwise
          include a pre-numbered expanded body.
        - Never raises for well-typed string mappings.
    """
    if not _mapping_has_nonblank_value(patches):
        return _empty_surface()
    contents = new_contents or {}
    blocks: OrderedDict[str, str] = OrderedDict()
    for path, patch in patches.items():
        if not (patch or "").strip():
            continue
        body = _assemble_path_block(path, patch, contents.get(path, ""))
        if body is not None:
            blocks[path] = body
    if not blocks:
        return _empty_surface()
    return ChangeSurface(blocks=blocks)


def build_change_surface_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
    """Build a change surface from SE-style old/new content maps.

    Preconditions:
        - ``new_contents`` maps path → new-file content. May be empty.
        - ``old_contents``, when omitted/`None`, means empty old for every
          path. When provided, missing keys are treated as empty old for
          that path (same as ``unified_diffs_from_pairs``).

    Postconditions:
        - ``new_contents == {}`` → empty ``ChangeSurface`` regardless of
          ``old_contents``.
        - Otherwise equivalent to
          ``build_change_surface_from_patches(
              unified_diffs_from_pairs(new_contents, old_contents),
              new_contents=new_contents,
          )``: identical old/new → blank patch → path omitted; all-identical
          / no assemblable bodies → empty surface; new and modified files
          with assemblable bodies match the patch-path surface for those
          diffs.
        - Never raises for well-typed string mappings.
    """
    if not new_contents:
        return _empty_surface()
    patches = unified_diffs_from_pairs(new_contents, old_contents)
    return build_change_surface_from_patches(patches, new_contents=new_contents)


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
            found[key] = LineRange(start_line=construct.start_line, end_line=construct.end_line)
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
