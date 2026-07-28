"""Consolidation of related side-effect findings, by proximity or reference.

The side-effect / blast-radius pass (``side_effect_impact_pass.py``) emits one
``CodeReviewIssue`` per caller-breaking behavior change it tool-verifies. Two
(or more) of those findings can describe the same underlying root cause:

    - Two findings anchored inside the same enclosing function/method — the
      chunk-blind pass can surface more than one blast-radius symptom of a
      single change to that construct.
    - One finding that narrates a broken caller by citing its file/line (the
      finding's own ``description`` is prose, e.g. "... caller at
      foo/bar.py:42 assumes the old shape"), and another finding whose own
      ``file_path``/``line`` sits inside that cited construct — these are the
      same underlying issue described from two ends.

This module merges each such group into a single ``CodeReviewIssue`` before
the coordinator's exact-match ``_dedupe_issues`` runs, so a submission with
five side-effect symptoms of one root cause surfaces as one consolidated
finding instead of five near-duplicates.

Only ``category == "side-effects"`` issues are grouped; every other issue
(including the pass's own ``"documentation"`` findings) passes through
unchanged and in its original relative position.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from software_engineering_team.shared.deduplication import dedupe_strings

from .false_positive_filter import CodebaseIndex
from .function_boundaries import (
    enclosing_construct,
    enclosing_construct_start_heuristic,
    strip_numbered_prefixes,
)
from .models import CodeReviewIssue

_SIDE_EFFECT_CATEGORY = "side-effects"

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Extensions resolved via the AST-based enclosing_construct (exact ranges);
# everything else falls back to the column-0 start-line heuristic.
_PYTHON_EXTS = frozenset({".py", ".pyi"})

# Matches a "path:line" citation embedded in a finding's prose, e.g. the
# side-effect prompt's expected "caller at foo/bar.py:42 assumes ..." phrasing.
# Best-effort: a citation phrased without a literal ``path:line`` substring
# (e.g. "line 42 of foo/bar.py") is not recognized.
_CITATION_RE = re.compile(r"([\w./\\-]+\.\w+):(\d+)")

_ConstructKey = Tuple[str, int]


class _UnionFind:
    """Minimal disjoint-set over ``range(n)``, path-compressed on find.

    Invariants:
        - Every element starts in its own singleton set.
        - ``find(x) == find(y)`` iff ``x`` and ``y`` have been unioned
          (directly or transitively).
    """

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


class _ConstructResolver:
    """Resolves ``(file_path, line)`` to an enclosing-construct group key.

    Memoizes file content per path (read once from ``shared_index`` no matter
    how many findings/citations reference the same file).
    """

    def __init__(self, shared_index: CodebaseIndex) -> None:
        self._shared_index = shared_index
        self._content_cache: Dict[str, Optional[str]] = {}

    def _content(self, file_path: str) -> Optional[str]:
        if file_path not in self._content_cache:
            self._content_cache[file_path] = self._shared_index.read_file_or_none(file_path)
        return self._content_cache[file_path]

    def construct_key(self, file_path: str, line: Optional[int]) -> Optional[_ConstructKey]:
        """Return ``(file_path, construct_start_line)``, or None if unresolvable.

        Postconditions:
            - Returns None when ``file_path``/``line`` is blank/None, the file
              cannot be read from ``shared_index``, or no enclosing construct
              (or heuristic start line) is found. Never raises.
            - Python files (``.py``/``.pyi``) resolve via the exact AST-based
              ``enclosing_construct``; every other extension resolves via the
              coarser ``enclosing_construct_start_heuristic`` (start line only).
            - Content carrying the PR-review path's ``"N: "``-prefixed hunk
              annotations is normalized (prefixes stripped, ``line`` remapped
              to its physical index) before resolution, exactly as
              ``find_function_at_line`` does, so a pre-numbered excerpt
              parses correctly instead of failing (Python) or spuriously
              treating every prefixed line as column-0 (heuristic).
        """
        if not file_path or not line or line < 1:
            return None
        content = self._content(file_path)
        if not content:
            return None
        stripped, physical, _mapper = strip_numbered_prefixes(content, line)
        _, ext = os.path.splitext(file_path)
        if ext.lower() in _PYTHON_EXTS:
            construct = enclosing_construct(stripped, physical)
            start = construct.start_line if construct is not None else None
        else:
            start = enclosing_construct_start_heuristic(stripped, physical)
        if start is None:
            return None
        return (file_path, start)

    def citation_keys(self, text: str) -> List[_ConstructKey]:
        """Resolve every ``path:line`` citation in ``text`` to a construct key.

        Postconditions:
            - Returns a construct key for each citation that resolves; a
              citation naming an unreadable path, or one with no resolvable
              construct, contributes nothing (never raises).
        """
        keys: List[_ConstructKey] = []
        for match in _CITATION_RE.finditer(text or ""):
            path, line_str = match.group(1), match.group(2)
            try:
                line = int(line_str)
            except ValueError:
                continue
            key = self.construct_key(path, line)
            if key is not None:
                keys.append(key)
        return keys


def _severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").strip().lower(), len(_SEVERITY_RANK))


def _merge_group(group: List[CodeReviewIssue]) -> CodeReviewIssue:
    """Merge two or more side-effect findings into one consolidated issue.

    Preconditions:
        - ``len(group) >= 2``; every issue's ``category`` is ``"side-effects"``.

    Postconditions:
        - ``file_path`` is the group's majority file (ties broken by earliest
          occurrence); ``line``/``start_line`` span that file's cited lines
          within the group (``start_line`` is None when only one distinct
          line is cited, matching a single-line issue's shape).
        - ``severity`` is the highest-ranked severity in the group
          (``critical`` > ``high`` > ``medium`` > ``low`` > ``info``).
        - ``description`` is the group's non-blank values deduped via
          ``dedupe_strings``. A single surviving value is used verbatim;
          multiple values are prefixed with "Consolidated N related
          side-effect findings:" and joined as a bulleted list.
        - ``suggestion`` is the group's non-blank values deduped via
          ``dedupe_strings``. A single surviving value is used verbatim;
          multiple values are joined as a plain bulleted list (no preamble).
        - ``pre_existing`` is True only when every issue in the group is.
    """
    file_counts: "OrderedDict[str, int]" = OrderedDict()
    for issue in group:
        fp = issue.file_path or ""
        file_counts[fp] = file_counts.get(fp, 0) + 1
    majority_file = max(file_counts, key=lambda fp: file_counts[fp])

    # A member issue may already carry its own multi-line range
    # (start_line..line); use each member's effective start (its own
    # start_line, falling back to its line for a single-line issue) so a
    # merge doesn't collapse an already-multi-line member down to just its
    # end line.
    starts = [
        issue.start_line if issue.start_line is not None else issue.line
        for issue in group
        if (issue.file_path or "") == majority_file and issue.line is not None
    ]
    ends = [
        issue.line
        for issue in group
        if (issue.file_path or "") == majority_file and issue.line is not None
    ]
    line = max(ends) if ends else None
    earliest_start = min(starts) if starts else None
    start_line = earliest_start if earliest_start is not None and earliest_start != line else None

    best = min(group, key=lambda i: _severity_rank(i.severity))

    descriptions = dedupe_strings([i.description for i in group if i.description])
    if len(descriptions) <= 1:
        description = descriptions[0] if descriptions else ""
    else:
        description = (
            f"Consolidated {len(descriptions)} related side-effect findings:\n"
            + "\n".join(f"- {d}" for d in descriptions)
        )

    suggestions = dedupe_strings([i.suggestion for i in group if i.suggestion])
    if len(suggestions) <= 1:
        suggestion = suggestions[0] if suggestions else ""
    else:
        suggestion = "\n".join(f"- {s}" for s in suggestions)

    return CodeReviewIssue(
        severity=best.severity,
        category=_SIDE_EFFECT_CATEGORY,
        file_path=majority_file,
        line=line,
        start_line=start_line,
        description=description,
        suggestion=suggestion,
        pre_existing=all(i.pre_existing for i in group),
    )


def consolidate_side_effect_issues(
    issues: List[CodeReviewIssue],
    shared_index: CodebaseIndex,
) -> List[CodeReviewIssue]:
    """Merge related ``"side-effects"`` findings into single, consolidated issues.

    Two findings are grouped together when either holds:
        - They are anchored inside the same enclosing function/method/class
          (Python: exact AST range; other languages: best-guess start line).
        - One finding's description/suggestion cites a ``path:line`` that
          falls inside the other finding's own enclosing construct.
    Grouping is transitive (union-find), so a chain of findings that each
    reference or share a construct with the next all merge into one issue.

    Preconditions:
        - ``issues`` is the tail-pass output, before ``_dedupe_issues`` runs.
        - ``shared_index`` was built from the same submission that produced
          ``issues`` (so its file contents match what the findings cite).

    Postconditions:
        - Every issue with ``category != "side-effects"`` passes through
          unchanged, in its original relative position.
        - ``category == "side-effects"`` issues that group with no other
          issue pass through unchanged.
        - Each group of >= 2 grouped issues becomes exactly one merged
          ``CodeReviewIssue`` (see :func:`_merge_group`), placed at the
          position of the group's earliest-occurring member; the group's
          other members are dropped.
        - Never raises: an unreadable file, unparseable content, or citation
          that cannot be resolved to a construct simply contributes no
          grouping signal, degrading toward "no consolidation" rather than
          failing the review.
    """
    side_effect_indices = [
        i for i, issue in enumerate(issues) if issue.category == _SIDE_EFFECT_CATEGORY
    ]
    if len(side_effect_indices) < 2:
        return list(issues)

    resolver = _ConstructResolver(shared_index)
    uf = _UnionFind(len(side_effect_indices))
    key_to_positions: Dict[_ConstructKey, List[int]] = {}

    for pos, idx in enumerate(side_effect_indices):
        issue = issues[idx]
        key = resolver.construct_key(issue.file_path, issue.line)
        if key is not None:
            key_to_positions.setdefault(key, []).append(pos)

    # Same-construct grouping: every finding anchored in the same function/
    # method/class merges together.
    for positions in key_to_positions.values():
        for other in positions[1:]:
            uf.union(positions[0], other)

    # Shared-reference grouping: a finding's own citations pointing at
    # another finding's enclosing construct merges the two (transitively,
    # via union-find, with anything already grouped with either side).
    for pos, idx in enumerate(side_effect_indices):
        issue = issues[idx]
        for cited_key in resolver.citation_keys(f"{issue.description} {issue.suggestion}"):
            for target_pos in key_to_positions.get(cited_key, []):
                if target_pos != pos:
                    uf.union(pos, target_pos)

    groups: Dict[int, List[int]] = {}
    for pos in range(len(side_effect_indices)):
        groups.setdefault(uf.find(pos), []).append(pos)

    merged_at: Dict[int, CodeReviewIssue] = {}
    dropped: set[int] = set()
    for positions in groups.values():
        if len(positions) < 2:
            continue
        member_indices = sorted(side_effect_indices[p] for p in positions)
        merged_at[member_indices[0]] = _merge_group([issues[i] for i in member_indices])
        dropped.update(member_indices[1:])

    return [merged_at.get(idx, issue) for idx, issue in enumerate(issues) if idx not in dropped]
