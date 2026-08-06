"""Change-surface builder API for diff-first code review.

Public contract for turning PR unified patches or SE old/new file pairs into a
bounded, pre-numbered review input suitable for ``CodeReviewInput`` with
``pre_numbered=True``. Callers consume ``ChangeSurface.code`` (concatenated
``### path ###`` blocks) or ``ChangeSurface.blocks`` (path → body without
headers).

This module currently locks types, signatures, and empty/no-op postconditions.
Expansion and assembly logic are owned by follow-on work; non-empty builder /
expand inputs raise ``NotImplementedError`` until those land.

Pure helpers: no I/O, no LLM clients, no package-level side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Mapping, Optional, Sequence

__all__ = [
    "ChangeSurface",
    "LineRange",
    "build_change_surface_from_pairs",
    "build_change_surface_from_patches",
    "expand_touched_ranges",
    "format_change_surface_code",
]


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
        - Otherwise raises ``NotImplementedError`` until expansion (#5389) is
          implemented. Future non-stub behavior: return one or more inclusive
          ``LineRange`` values for enclosing function/class constructs; when no
          construct is found, return a capped context window around the touched
          lines — never the whole file solely because a construct was missing.
          Multi-hunk collapse across shared constructs is owned by later work.
    """
    if not touched_lines:
        return ()
    raise NotImplementedError(
        "expand_touched_ranges is not implemented yet (change-surface expansion)"
    )
