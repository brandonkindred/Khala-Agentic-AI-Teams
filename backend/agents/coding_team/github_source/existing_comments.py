"""Match code-review findings against comments already on a pull request.

Pure, side-effect-free helpers — no GitHub-client or LLM dependency, mirroring
the philosophy of ``pr_review_mapping`` — that let the ``/review-pr`` flow
recognize when a new finding duplicates something already on the PR:

- ``build_existing_comments`` — combine raw REST/GraphQL fetches (review
  comments, their resolved-thread ids, and Khala's own past standalone
  comments) into one uniform ``ExistingComment`` list.
- ``match_existing_comment`` — find the existing comment (if any) a finding
  duplicates: same file/line (or both file-level) and similar-enough text.
- ``partition_issues_by_existing_comments`` — split findings into those to
  keep (annotated with a match when unresolved) and those to drop (matched an
  already-resolved comment).

Findings are duck-typed exactly as in ``pr_review_mapping``: any object
exposing ``file_path``, ``line`` and ``description`` works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from .client import KHALA_COMMENT_MARKER, IssueComment, ReviewComment

# Reused for the same fuzzy path resolution pr_review_mapping already applies
# when matching a finding's file_path against a diff path (exact, leading-./
# strip, unique-basename fallback) — findings and existing comments should
# resolve file paths identically.
from .pr_review_mapping import _normalize_path

# Below this ratio (difflib.SequenceMatcher over normalized description/body
# text), a same-location existing comment is treated as a DIFFERENT issue, not
# a duplicate — two distinct findings can legitimately land on the same line.
_SIMILARITY_THRESHOLD = 0.6

# Inverse of pr_review_mapping._location_prefix: a standalone comment Khala
# posted itself always starts with this markdown-code location anchor.
_LOCATION_PREFIX_RE = re.compile(r"^`([^`]+)`\s+—\s+")


@dataclass(frozen=True)
class ExistingComment:
    """One comment already on the PR, normalized for matching against a new finding.

    ``path`` is ``None`` when no location could be recovered (only possible for
    a standalone comment whose body did not start with a parseable location
    anchor — e.g. one of Khala's own status notices). ``resolved`` is always
    False for a standalone comment — GitHub has no resolution concept for
    conversation comments.
    """

    path: Optional[str]
    line: Optional[int]
    body: str
    html_url: str
    resolved: bool


def _parse_standalone_location(body: str) -> tuple[Optional[str], Optional[int]]:
    """Recover ``(path, line)`` from a standalone comment's leading location anchor.

    Preconditions:
        - ``body`` is the raw comment text (may or may not carry the anchor
          ``format_issue_comment``/``inline_comment_to_timeline_body`` produce).
    Postconditions:
        - Returns ``(path, line)`` when ``body`` starts with a `` `anchor` — ``
          prefix, splitting ``anchor`` on its last ``:`` when the suffix is a
          positive integer (a line number) — otherwise ``line`` is None and
          ``path`` is the whole anchor. Returns ``(None, None)`` when no such
          prefix is present (a human's freeform comment, or one of Khala's own
          comments that carries no location, e.g. a status/error notice).
    """
    m = _LOCATION_PREFIX_RE.match(body or "")
    if not m:
        return None, None
    anchor = m.group(1)
    if ":" in anchor:
        head, _, tail = anchor.rpartition(":")
        if head and tail.isdigit() and int(tail) > 0:
            return head, int(tail)
    return anchor, None


def build_existing_comments(
    review_comments: Iterable[ReviewComment],
    resolved_ids: Iterable[int],
    issue_comments: Iterable[IssueComment],
) -> list[ExistingComment]:
    """Combine raw GitHub fetches into a uniform list for matching.

    Preconditions:
        - ``review_comments`` and ``issue_comments`` are the full, unfiltered
          results of ``GitHubClient.list_review_comments``/``list_issue_comments``
          for one pull request; ``resolved_ids`` is
          ``GitHubClient.get_resolved_review_thread_comment_ids``'s result for
          the same pull request.
    Postconditions:
        - Returns one ``ExistingComment`` per review comment (``resolved`` set
          from membership in ``resolved_ids``) plus one per Khala-authored
          standalone comment (identified by :data:`KHALA_COMMENT_MARKER`; a
          human's conversation comment is never treated as an existing
          finding-comment, since it carries no reliable structured location) —
          ``resolved`` is always False for these. Order is review comments
          first, then standalone comments, each group in its input order.
    """
    resolved_set = set(resolved_ids)
    out: list[ExistingComment] = []
    for rc in review_comments:
        out.append(
            ExistingComment(
                path=rc.path or None,
                line=rc.line,
                body=rc.body,
                html_url=rc.html_url,
                resolved=rc.id in resolved_set,
            )
        )
    for ic in issue_comments:
        if KHALA_COMMENT_MARKER not in ic.body:
            continue
        path, line = _parse_standalone_location(ic.body)
        out.append(
            ExistingComment(
                path=path, line=line, body=ic.body, html_url=ic.html_url, resolved=False
            )
        )
    return out


def _similar_enough(a: str, b: str) -> bool:
    """True when two finding/comment texts are alike enough to be the same issue.

    Postconditions:
        - Returns whether ``SequenceMatcher.ratio()`` (case/whitespace-insensitive)
          over ``a``/``b`` is at least :data:`_SIMILARITY_THRESHOLD`.
    """
    return (
        SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()
        >= _SIMILARITY_THRESHOLD
    )


def match_existing_comment(
    issue: Any, existing: Iterable[ExistingComment]
) -> Optional[ExistingComment]:
    """Find the existing comment (if any) that ``issue`` duplicates.

    A finding duplicates an existing comment at exactly the granularity a
    review comment can express: the same resolved file path and the same line
    (or, for a file-level finding/comment, both with no line at all).
    Location alone is not sufficient — two distinct findings can legitimately
    land on the same line — so a location match is confirmed only when the
    finding's ``description`` is textually similar enough to the existing
    comment's body (see :data:`_SIMILARITY_THRESHOLD`).

    Preconditions:
        - ``issue`` exposes ``file_path``, ``line`` and ``description`` (the
          same duck-typed shape ``pr_review_mapping`` consumes).
    Postconditions:
        - Returns the first location-and-content match in ``existing``'s
          order, or ``None`` when no existing comment matches.
    """
    file_path = getattr(issue, "file_path", "") or ""
    raw_line = getattr(issue, "line", None)
    issue_line = int(raw_line) if raw_line is not None else None
    description = getattr(issue, "description", "") or ""
    for candidate in existing:
        if candidate.path is None:
            continue
        if _normalize_path(file_path, {candidate.path: set()}) is None:
            continue
        if candidate.line != issue_line:
            continue
        if _similar_enough(description, candidate.body):
            return candidate
    return None


def partition_issues_by_existing_comments(
    issues: Iterable[Any], existing: Iterable[ExistingComment]
) -> tuple[list[Any], list[Any], dict[int, ExistingComment]]:
    """Split findings by whether they duplicate an existing PR comment.

    Preconditions:
        - ``issues`` is the reviewer's findings for this PR (already filtered
          to those that belong on this PR — e.g. with ``pre_existing``
          findings already routed elsewhere); ``existing`` is
          :func:`build_existing_comments`'s output for the same PR.
    Postconditions:
        - Returns ``(kept, dropped, references)``. ``dropped`` holds every
          issue matching an already-RESOLVED existing comment (already
          addressed — never posted again). ``kept`` holds every other issue,
          in its original order. ``references`` maps ``id(issue)`` (for each
          kept issue that matched an UNRESOLVED existing comment) to that
          comment, so the caller can annotate the posted comment with a
          reference to it; an issue with no match, or whose match was dropped,
          has no entry. ``len(kept) + len(dropped) == len(list(issues))``.
    """
    existing_list = list(existing)
    kept: list[Any] = []
    dropped: list[Any] = []
    references: dict[int, ExistingComment] = {}
    for issue in issues:
        match = match_existing_comment(issue, existing_list)
        if match is None:
            kept.append(issue)
        elif match.resolved:
            dropped.append(issue)
        else:
            kept.append(issue)
            references[id(issue)] = match
    return kept, dropped, references
