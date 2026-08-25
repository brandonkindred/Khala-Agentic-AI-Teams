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

from .false_positive_filter import CodebaseIndex
from .function_boundaries import (
    enclosing_construct,
    strip_numbered_prefixes,
)
from .models import CodeReviewInput, CodeReviewIssue

_SIDE_EFFECT_CATEGORY = "side-effects"

# Default-on toggle: an explicit ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION=false``/
# ``0``/``no`` disables merging of related "side-effects" findings (see
# docs/ENV_VARS.md). Any other value (or unset) leaves consolidation enabled.
# Single source of truth for the toggle's name — every caller (coordinator.py,
# temporal/activities.py, mapping.py's cache fingerprint) imports this constant
# rather than re-spelling the env var name, so a rename can't silently drift.
SIDE_EFFECT_CONSOLIDATION_ENV = "CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION"

# Default-on toggle for the mutation-vs-replaced-code contract sub-check inside
# the side-effect / blast-radius pass (see side_effect_impact_pass.py): an
# explicit ``CODE_REVIEW_MUTATION_ANALYSIS=false``/``0``/``no`` disables it (see
# docs/ENV_VARS.md). Any other value (or unset) leaves it enabled. Defined here
# rather than in a tail-pass module, mirroring ``SIDE_EFFECT_CONSOLIDATION_ENV``
# immediately above: this is a neutral, side-effect-free location so the
# low-level cache/fingerprint layer (mapping.py) never has to import a tail-pass
# module just to read a toggle name. Every caller (side_effect_impact_pass.py,
# merged_architecture_side_effect_pass.py, coordinator.py, mapping.py's cache
# fingerprint) imports this constant rather than re-spelling the env var name.
MUTATION_ANALYSIS_ENV = "CODE_REVIEW_MUTATION_ANALYSIS"


def effective_replaced_content(
    input_data: CodeReviewInput, mutation_on: bool
) -> Optional[Dict[str, str]]:
    """The before-image to show the model, gated by the mutation-analysis toggle.

    Single source of truth for the "hide replaced_content entirely when the
    toggle is off" rule, so ``side_effect_impact_pass._run_pass`` and
    ``merged_architecture_side_effect_pass._run_pass`` share one implementation
    instead of duplicating ``input_data.replaced_content if mutation_on else
    None`` at each call site.

    Preconditions: none.

    Postconditions:
        - Returns ``input_data.replaced_content`` unchanged when ``mutation_on``
          is True.
        - Returns ``None`` when ``mutation_on`` is False, regardless of whether
          ``input_data.replaced_content`` is set -- the before-image must be
          hidden from the model entirely in that case, not merely passed
          through with an instruction to ignore it. Pure; never raises.
    """
    return input_data.replaced_content if mutation_on else None


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Extensions resolved via the AST-based enclosing_construct (exact ranges).
# Non-Python extensions intentionally contribute no same-construct key: the
# column-0 heuristic cannot distinguish indented methods and would false-merge.
_PYTHON_EXTS = frozenset({".py", ".pyi"})

# Conventional extensionless filenames a citation may reference (build/config
# files have no ``.ext`` for the first alternative below to anchor on).
_EXTENSIONLESS_NAMES = (
    "Dockerfile",
    "Makefile",
    "Rakefile",
    "Gemfile",
    "Procfile",
    "Jenkinsfile",
    "Vagrantfile",
    "LICENSE",
)

# Matches a "path:line" citation embedded in a finding's prose, e.g. the
# side-effect prompt's expected "caller at foo/bar.py:42 assumes ..." phrasing,
# or a conventional extensionless file like "Dockerfile:12". Best-effort: a
# citation phrased without a literal ``path:line`` substring (e.g. "line 42 of
# foo/bar.py") is not recognized.
_CITATION_RE = re.compile(
    r"([\w./\\-]+\.\w+|\b(?:" + "|".join(_EXTENSIONLESS_NAMES) + r")\b):(\d+)"
)

_ConstructKey = Tuple[str, int]


class _UnionFind:
    """Minimal disjoint-set over ``range(n)``, path-compressed on find.

    Invariants:
        - Every element starts in its own singleton set.
        - ``find(x) == find(y)`` iff ``x`` and ``y`` have been unioned
          (directly or transitively).
    """

    def __init__(self, n: int) -> None:
        """Preconditions: ``n >= 0``. Postconditions: ``find(x) == x`` for every ``x in range(n)``."""
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        """Preconditions: ``x in range(n)`` (from ``__init__``). Postconditions: returns ``x``'s set
        representative, path-compressing along the way; never raises for an in-range ``x``."""
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        """Preconditions: ``a`` and ``b`` are both in ``range(n)``. Postconditions:
        ``find(a) == find(b)`` afterwards; a no-op when they were already in the same set."""
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
        """Return ``(canonical_path, construct_start_line)``, or None if unresolvable.

        Postconditions:
            - Returns None when ``file_path``/``line`` is blank/None, the path
              cannot be resolved via ``CodebaseIndex.resolve_path``, the file
              cannot be read from ``shared_index``, or no enclosing construct
              is found. Never raises.
            - The returned path is the canonical key from ``resolve_path`` (so
              a basename citation like ``foo.py`` and a finding keyed as
              ``app/foo.py`` share a group key when they resolve to the same
              file).
            - Only Python files (``.py``/``.pyi``) contribute a grouping key,
              via the exact AST-based ``enclosing_construct``. Non-Python
              files return None: the column-0 start-line heuristic cannot
              distinguish indented methods inside a class (Java, C#,
              class-based TypeScript), and consolidating on it would merge
              independent findings. Prefer no consolidation over false merges.
            - Content carrying the PR-review path's ``"N: "``-prefixed hunk
              annotations is normalized (prefixes stripped, ``line`` remapped
              to its physical index) before resolution. When the source was
              annotated-hunk content, bare column-0 ``...`` gap markers are
              preserved and ``enclosing_construct(..., annotated_hunks=True)``
              resolves each hunk independently so a later indented continuation
              is not attached to the preceding open construct. Ordinary
              full-file Ellipsis stubs are left alone.
        """
        if not file_path or not line or line < 1:
            return None
        canonical = self._shared_index.resolve_path(file_path)
        if not canonical:
            return None
        _, ext = os.path.splitext(canonical)
        if ext.lower() not in _PYTHON_EXTS:
            # Column-0 heuristics cannot tell indented methods apart; skip
            # same-construct grouping for non-Python rather than false-merge.
            # Checked before reading/stripping content so non-Python files
            # never pay for a scan whose result is discarded.
            return None
        content = self._content(canonical)
        if not content:
            return None
        stripped, physical, mapper = strip_numbered_prefixes(content, line)
        construct = enclosing_construct(stripped, physical, annotated_hunks=mapper is not None)
        if construct is None:
            return None
        return (canonical, construct.start_line)

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
    """Preconditions: ``severity`` is a string (may be blank/None-like via ``or ""``).

    Postconditions: returns a lower rank for a more severe value (``critical`` < ``high`` <
    ``medium`` < ``low`` < ``info``); an unrecognized or blank value ranks lowest (last).
    Never raises.
    """
    return _SEVERITY_RANK.get((severity or "").strip().lower(), len(_SEVERITY_RANK))


def _dedupe_exact(items: List[str]) -> List[str]:
    """Drop exact duplicate strings while preserving first-seen order.

    Unlike the fuzzy ``dedupe_strings`` helper (0.85 SequenceMatcher threshold),
    this keeps descriptions that differ only in a cited ``path:line`` — those
    caller-specific details are the blast-radius evidence the consolidated
    finding must surface.

    Preconditions:
        - ``items`` is a list of strings.

    Postconditions:
        - Returns a new list containing each distinct string from ``items``
          exactly once, in first-occurrence order. Never raises.
    """
    seen: set[str] = set()
    unique: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _canonical_path(shared_index: CodebaseIndex, file_path: str) -> str:
    """Resolve ``file_path`` to a vote/range key, or ``""`` when unanchored.

    Postconditions:
        - Blank/`None` paths yield ``""`` (they do not vote).
        - Otherwise returns ``shared_index.resolve_path(...)`` when that
          succeeds, else the stripped literal — so an unresolvable path still
          participates under its own spelling rather than being dropped.
    """
    stripped = (file_path or "").strip()
    if not stripped:
        return ""
    return shared_index.resolve_path(stripped) or stripped


def _merge_group(
    group: List[CodeReviewIssue],
    shared_index: CodebaseIndex,
    category: Optional[str] = None,
) -> CodeReviewIssue:
    """Merge two or more related findings of one category into one issue.

    Starts from the highest-severity member's full ``model_dump()`` so any
    ``CodeReviewIssue`` fields this pass does not explicitly recompute
    (currently ``title``, and any future fields) are preserved rather than
    silently dropped. Only the fields listed in the postconditions below are
    recomputed from the group.

    Preconditions:
        - ``len(group) >= 2``; every issue in the group shares one category.
        - ``category`` is that shared category, or ``None`` to default to
          ``"side-effects"`` (back-compat for the side-effect consolidation
          caller, which only ever merges ``"side-effects"`` groups). When
          given, it becomes the merged issue's category and drives the
          consolidated-description label.
        - ``shared_index`` is the same index used for construct grouping, so
          path aliases (``foo.py`` vs ``app/foo.py``) resolve consistently.

    Postconditions:
        - ``file_path`` is the group's majority **non-blank** file after
          ``CodebaseIndex.resolve_path`` (ties broken by earliest occurrence
          among non-blank canonical keys). Blank/`None` paths do not vote, so
          an unanchored finding that citation-groups with an anchored one
          cannot wipe the known anchor. Path aliases that resolve to the same
          file vote as one key, so a basename map finding cannot win a tie
          over a canonically spelled additive finding and exclude its lines
          from the published range. Falls back to ``""`` only when every
          member is unanchored.
        - ``line``/``start_line`` span that canonical file's cited lines within
          the group (``start_line`` is None when only one distinct line is
          cited, matching a single-line issue's shape).
        - ``severity`` is the highest-ranked severity in the group
          (``critical`` > ``high`` > ``medium`` > ``low`` > ``info``).
        - ``category`` is ``category`` (the group's shared category), or
          ``"side-effects"`` when ``category`` is ``None``.
        - ``description`` is the group's non-blank values with exact duplicates
          removed (order-preserving). A single surviving value is used verbatim;
          multiple values are prefixed with "Consolidated N related
          <category> findings:" (the ``side-effects`` category renders as
          "side-effect") and joined as a bulleted list. Exact (not
          fuzzy) dedupe is intentional: near-identical wording that cites
          different callers must all survive.
        - ``suggestion`` is the group's non-blank values with exact duplicates
          removed. A single surviving value is used verbatim;
          multiple values are joined as a plain bulleted list (no preamble).
        - ``pre_existing`` is True only when every issue in the group is.
        - ``omission`` is True when any issue in the group is (OR across the
          group, the inverse quantifier from ``pre_existing``'s AND): a
          combined finding stays a required-add/modify signal if any member
          flagged one. This composes correctly with ``pre_existing``'s AND
          without extra reconciliation -- ``CodeReviewIssue`` rejects
          ``omission=True and pre_existing=True`` on every group member (see
          ``_omission_implies_in_scope``), so a member with ``omission=True``
          necessarily contributes ``pre_existing=False`` to the AND,
          guaranteeing the merged pair can never itself be invalid.
        - Every other ``CodeReviewIssue`` field (including ``title``) is
          copied from the highest-severity member.
    """
    assert shared_index is not None, "_merge_group requires a CodebaseIndex (see preconditions)"
    file_counts: OrderedDict[str, int] = OrderedDict()
    for issue in group:
        fp = _canonical_path(shared_index, issue.file_path or "")
        if not fp:
            # Unanchored members still merge via citations, but must not win
            # the location vote over a known path:file anchor.
            continue
        file_counts[fp] = file_counts.get(fp, 0) + 1
    majority_file = max(file_counts, key=lambda fp: file_counts[fp]) if file_counts else ""

    # A member issue may already carry its own multi-line range
    # (start_line..line); use each member's effective start (its own
    # start_line, falling back to its line for a single-line issue) so a
    # merge doesn't collapse an already-multi-line member down to just its
    # end line. Compare on canonical paths so basename aliases contribute
    # their lines to the same span as the resolved key.
    majority_lines = [
        issue
        for issue in group
        if _canonical_path(shared_index, issue.file_path or "") == majority_file
        and issue.line is not None
    ]
    starts = [
        issue.start_line if issue.start_line is not None else issue.line for issue in majority_lines
    ]
    ends = [issue.line for issue in majority_lines]
    line = max(ends) if ends else None
    earliest_start = min(starts) if starts else None
    start_line = earliest_start if earliest_start is not None and earliest_start != line else None

    best = min(group, key=lambda i: _severity_rank(i.severity))

    effective_category = category if category else _SIDE_EFFECT_CATEGORY
    # "side-effects" reads better as "side-effect findings"; every other
    # category is used verbatim (e.g. "logic findings", "naming findings").
    category_label = (
        "side-effect" if effective_category == _SIDE_EFFECT_CATEGORY else effective_category
    )

    descriptions = _dedupe_exact([i.description for i in group if i.description])
    if len(descriptions) <= 1:
        description = descriptions[0] if descriptions else ""
    else:
        description = (
            f"Consolidated {len(descriptions)} related {category_label} findings:\n"
            + "\n".join(f"- {d}" for d in descriptions)
        )

    suggestions = _dedupe_exact([i.suggestion for i in group if i.suggestion])
    if len(suggestions) <= 1:
        suggestion = suggestions[0] if suggestions else ""
    else:
        suggestion = "\n".join(f"- {s}" for s in suggestions)

    # Preserve non-merged fields (e.g. title) from the highest-severity member.
    payload = best.model_dump()
    payload.update(
        {
            "severity": best.severity,
            "category": effective_category,
            "file_path": majority_file,
            "line": line,
            "start_line": start_line,
            "description": description,
            "suggestion": suggestion,
            "pre_existing": all(i.pre_existing for i in group),
            "omission": any(i.omission for i in group),
        }
    )
    return CodeReviewIssue.model_validate(payload)


def consolidate_side_effect_issues(
    issues: List[CodeReviewIssue],
    shared_index: CodebaseIndex,
) -> List[CodeReviewIssue]:
    """Merge related ``"side-effects"`` findings into single, consolidated issues.

    Two findings are grouped together when either holds:
        - They are anchored inside the same enclosing Python function/method/
          class (exact AST range). Non-Python files do not contribute a
          same-construct key — the column-0 heuristic cannot distinguish
          indented methods and would false-merge independent findings.
        - One finding's description/suggestion cites a ``path:line`` that
          falls inside the other finding's own enclosing construct
          (citations are resolved through the same Python-only key).
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
        merged_at[member_indices[0]] = _merge_group(
            [issues[i] for i in member_indices],
            shared_index,
        )
        dropped.update(member_indices[1:])

    return [merged_at.get(idx, issue) for idx, issue in enumerate(issues) if idx not in dropped]
