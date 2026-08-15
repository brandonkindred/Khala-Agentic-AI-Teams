"""Fuzzy + proximity combination of related code-review findings.

Generalizes :mod:`side_effect_consolidation` from the ``side-effects`` category
to the *whole* finding stream. Two same-category findings are combined into one
representative when either signal holds:

    - **Proximity** — both are anchored inside the same enclosing Python
      construct (function/method/class), resolved via the exact AST-based
      ``enclosing_construct`` (reused through
      :class:`side_effect_consolidation._ConstructResolver`). Non-Python files
      contribute no proximity key: the column-0 heuristic cannot distinguish
      indented methods and would false-merge (the same guard the side-effect
      consolidation already documents).
    - **Similarity** — both name the same resolved file *and* their tokenized
      descriptions have Jaccard word-set overlap ``>= threshold`` (default
      ``0.6``, reusing :mod:`github_source.issue_proposals`'s tokenizer and
      Jaccard helper).

Additionally, for ``side-effects`` findings only, the citation signal from the
side-effect consolidation is preserved: a finding whose prose cites a
``path:line`` that falls inside another same-category finding's construct is
combined with it (this is the one signal that can link findings across two
files, exactly as the side-effect consolidation already did — it is the "same
root cause described from both ends" case, not a general cross-file merge).

Grouping is transitive (union-find) and confined to a single **category**: two
findings are never combined across categories, and — apart from the
side-effects citation case above — never across files. Cross-file "same problem
in different places" is intentionally NOT merged here (a merge would collapse
distinct inline-comment anchors, and ``CodeReviewIssue`` has no multi-location
field); that theming is surfaced separately by the systemic-synthesis pass.

This step replaces the exact-match ``coordinator._dedupe_issues`` for the main
finding stream and subsumes ``consolidate_side_effect_issues`` (an exact
duplicate is just a Jaccard-1.0, same-file, same-category similarity match).

Invariants:
    - Deterministic: grouping and merge depend only on ``issues`` order and
      ``shared_index`` content, never on set/dict iteration order for anything
      observable. First-occurrence order is preserved.
    - Fail-open: an unreadable file, unparseable content, or unresolvable
      citation contributes no grouping signal, degrading toward "no
      combination" rather than raising into the review.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from shared.env import parse_float

from .false_positive_filter import CodebaseIndex
from .models import CodeReviewIssue
from .side_effect_consolidation import (
    _SIDE_EFFECT_CATEGORY,
    _canonical_path,
    _ConstructResolver,
    _merge_group,
    _UnionFind,
)

# --- Description similarity (word-set Jaccard) --------------------------------
# Mirrors ``github_source.issue_proposals``'s tokenizer/Jaccard by construction.
# Defined locally rather than imported to keep the generic review engine
# (``code_review_agent``) free of any dependency on the PR-specific
# ``github_source`` layer — the two packages are otherwise fully decoupled, and
# inverting that for a three-line pure function is not worth the coupling. The
# algorithm is a single, well-known metric; both copies must stay in lockstep.

# Default Jaccard floor for treating two same-category, same-file descriptions
# as the same underlying issue (0.6). Overridable per :func:`combine_findings`.
_SIMILARITY_THRESHOLD = 0.6

_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'")
_DIGITS_RE = re.compile(r"\b\d+\b")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize_for_similarity(text: str) -> "frozenset[str]":
    """Reduce a description to a comparable bag of words.

    Postconditions:
        - Returns the lowercased word tokens of ``text`` with backtick/quoted
          spans and standalone digit runs dropped first (so "bare import `os`"
          and "bare import `sys`" tokenize identically). Empty for
          blank/punctuation-only input. Pure; never raises.
    """
    normalized = _QUOTED_RE.sub(" ", text.lower())
    normalized = _DIGITS_RE.sub(" ", normalized)
    return frozenset(_WORD_RE.findall(normalized))


def _jaccard_similarity(a: "frozenset[str]", b: "frozenset[str]") -> float:
    """Postconditions: returns ``|a & b| / |a | b|``, or 0.0 when both are empty."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# Similarity floor for the fuzzy signal, overridable via this env var. Mirrors
# ``issue_proposals``' default (0.6) so a description that differs from another
# only by a quoted identifier/number still combines, while genuinely distinct
# descriptions in the same file do not. Clamped to ``[0.0, 1.0]``.
_COMBINE_SIMILARITY_THRESHOLD_ENV = "CODE_REVIEW_COMBINE_SIMILARITY_THRESHOLD"

_ConstructKey = Tuple[str, int]


def _resolve_similarity_threshold(similarity_threshold: Optional[float]) -> float:
    """Resolve the effective Jaccard threshold.

    Preconditions:
        - ``similarity_threshold`` is ``None`` (consult the env var) or a float.

    Postconditions:
        - Returns ``similarity_threshold`` clamped to ``[0.0, 1.0]`` when given;
          otherwise ``parse_float(_COMBINE_SIMILARITY_THRESHOLD_ENV, 0.6)``
          clamped to ``[0.0, 1.0]``. Never raises.
    """
    if similarity_threshold is not None:
        return max(0.0, min(1.0, similarity_threshold))
    return parse_float(
        _COMBINE_SIMILARITY_THRESHOLD_ENV,
        _SIMILARITY_THRESHOLD,
        minimum=0.0,
        maximum=1.0,
    )


def _normalized_category(issue: CodeReviewIssue) -> str:
    """Return ``issue.category`` lowercased/stripped (``""`` when blank). Pure."""
    return (issue.category or "").strip().lower()


def combine_findings(
    issues: List[CodeReviewIssue],
    shared_index: CodebaseIndex,
    *,
    similarity_threshold: Optional[float] = None,
) -> List[CodeReviewIssue]:
    """Combine related findings by proximity and same-file similarity.

    Preconditions:
        - ``issues`` is the merged finding list (map-phase findings plus any
          additive tail-pass findings), before the false-positive filter.
        - ``shared_index`` was built from the same submission that produced
          ``issues`` (its file contents match what the findings cite/anchor).
        - ``similarity_threshold`` is ``None`` (use the env default) or a
          Jaccard threshold; values outside ``[0, 1]`` are clamped.

    Postconditions:
        - Findings are grouped only within one normalized ``category`` (blank
          categories group among themselves). Two findings combine when they
          share the same enclosing Python construct (proximity), or the same
          resolved file with tokenized-description Jaccard ``>= threshold``
          (similarity); for ``side-effects`` findings only, a finding whose
          prose cites a ``path:line`` inside another same-category finding's
          construct also combines (transitively, via union-find).
        - Each group of ``>= 2`` findings becomes exactly one merged
          ``CodeReviewIssue`` (see ``side_effect_consolidation._merge_group``,
          called with the group's shared category): ``severity`` is the group
          max, ``line``/``start_line`` span the majority file's cited lines,
          descriptions/suggestions are exact-deduped, and ``pre_existing`` is
          the AND across the group. The merged issue is placed at the position
          of the group's earliest-occurring member; the group's other members
          are dropped.
        - A finding that groups with no other passes through unchanged, in its
          original relative position; so does the whole list when
          ``len(issues) < 2``.
        - Merges never cross a category boundary, and never cross a file
          boundary except through the ``side-effects`` citation signal above.
        - Deterministic and never raises (see module Invariants).
    """
    if len(issues) < 2:
        return list(issues)

    threshold = _resolve_similarity_threshold(similarity_threshold)
    resolver = _ConstructResolver(shared_index)
    uf = _UnionFind(len(issues))

    categories = [_normalized_category(issue) for issue in issues]
    files = [_canonical_path(shared_index, issue.file_path or "") for issue in issues]
    tokens = [_tokenize_for_similarity(issue.description or "") for issue in issues]
    construct_keys = [resolver.construct_key(issue.file_path, issue.line) for issue in issues]

    # Proximity: findings sharing (category, enclosing construct) merge. Keyed
    # on the normalized category so a naming finding and a logic finding in the
    # same function are never combined.
    key_to_positions: Dict[Tuple[str, _ConstructKey], List[int]] = {}
    for pos, key in enumerate(construct_keys):
        if key is not None:
            key_to_positions.setdefault((categories[pos], key), []).append(pos)
    for positions in key_to_positions.values():
        for other in positions[1:]:
            uf.union(positions[0], other)

    # Citation signal (side-effects only, preserving the side-effect
    # consolidation's cross-end linking): a finding whose prose cites a
    # path:line inside another same-category finding's construct merges with it.
    # Confined to side-effects so the generalized combine introduces no new
    # cross-file merges for other categories.
    for pos, issue in enumerate(issues):
        if categories[pos] != _SIDE_EFFECT_CATEGORY:
            continue
        for cited_key in resolver.citation_keys(f"{issue.description} {issue.suggestion}"):
            for target_pos in key_to_positions.get((categories[pos], cited_key), []):
                if target_pos != pos:
                    uf.union(pos, target_pos)

    # Similarity: within each (category, resolved-file) bucket, findings whose
    # tokenized descriptions overlap at/above the threshold merge. Unanchored
    # findings (no resolvable file) cannot same-file group. O(k^2) per bucket,
    # where k is the bucket size (small in practice; the whole list is capped
    # downstream).
    similarity_buckets: Dict[Tuple[str, str], List[int]] = {}
    for pos in range(len(issues)):
        if not files[pos]:
            continue
        similarity_buckets.setdefault((categories[pos], files[pos]), []).append(pos)
    for positions in similarity_buckets.values():
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                a, b = positions[i], positions[j]
                if _jaccard_similarity(tokens[a], tokens[b]) >= threshold:
                    uf.union(a, b)

    groups: Dict[int, List[int]] = {}
    for pos in range(len(issues)):
        groups.setdefault(uf.find(pos), []).append(pos)

    merged_at: Dict[int, CodeReviewIssue] = {}
    dropped: set[int] = set()
    for positions in groups.values():
        if len(positions) < 2:
            continue
        member_indices = sorted(positions)
        # Every member shares one normalized category by construction; carry the
        # first member's raw category spelling into the merge.
        group_category = issues[member_indices[0]].category
        merged_at[member_indices[0]] = _merge_group(
            [issues[i] for i in member_indices],
            shared_index,
            category=group_category,
        )
        dropped.update(member_indices[1:])

    return [merged_at.get(idx, issue) for idx, issue in enumerate(issues) if idx not in dropped]
