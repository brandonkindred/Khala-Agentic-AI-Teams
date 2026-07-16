"""Map structured code-review findings onto GitHub pull-request review comments.

Pure, side-effect-free helpers used by the ``/review-pr`` flow:

- ``parse_valid_lines`` — turn one file's unified diff into the set of new-file
  line numbers that can carry an inline comment on ``side="RIGHT"``.
- ``map_issues_to_comments`` — route each review finding to a line-anchored
  inline comment (its line is in the diff), a file-level review comment (its file
  changed but the cited line is not in the diff), or a standalone leftover (its
  file is not in the diff at all).
- ``split_review_comments`` — partition mapped comments into the line-anchored
  group (rides the single review) and the file-level group (posted individually
  on the dedicated review-comments endpoint, the only one that takes
  ``subject_type``).
- ``format_comment_body`` — render one finding as a review-comment body.
- ``anchor_to_first_file`` — produce a file-level inline review comment for a
  leftover finding (file not in diff) anchored to the first changed file.
- ``format_issue_comment`` / ``inline_comment_to_timeline_body`` — render one
  finding as its own standalone PR conversation (issue) comment.
- ``build_review_body`` — render the summary-only review body.
- ``choose_event`` — pick the GitHub review event from issue severity.
- ``group_similar_findings`` — cluster pre-existing findings that describe the
  same underlying issue (same category, near-identical description) so they
  become one combined GitHub-issue proposal instead of one each.
- ``proposal_from_findings`` / ``build_issue_from_proposal`` — turn a group of
  similar findings into a persisted issue-proposal dict, then render it as a
  GitHub issue ``(title, body)``.

Every finding gets exactly one comment, all attached to the single review where
possible: a finding on a changed line becomes a line-anchored inline comment, a
finding whose file changed but whose cited line is off-diff becomes a file-level
review comment, and only a finding naming a file absent from the diff is left for
the caller to post as a standalone conversation comment — the review body never
lists findings.

Kept free of any GitHub-client or LLM dependency so it is cheap to unit-test and
reusable. Findings are duck-typed: any object exposing ``severity``, ``category``,
``file_path``, ``description``, ``suggestion`` and ``line`` attributes works.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional, Protocol

from .client import Issue, scrub_token_from_text

# GitHub inline review comments on side=RIGHT must target a line the diff adds
# (`+`) or carries as context (` `). We accept both so the commentable set matches
# exactly what the reviewer is shown by render_annotated_hunks (added + context
# lines) — a finding cited on a context line can still be posted inline rather than
# silently demoted to the body. Set this True to restrict anchoring to added lines.
COMMENT_ON_ADDED_LINES_ONLY = False

# Severities that should block the PR (drive a REQUEST_CHANGES review).
_BLOCKING_SEVERITIES = {"critical", "high"}

# Captures the new-file start line from a hunk header: ``@@ -a,b +c,d @@``.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class ReviewFinding(Protocol):
    """Duck-typed shape consumed by PR review comment mappers."""

    severity: str
    category: str
    file_path: Optional[str]
    description: str
    suggestion: str
    line: Optional[int]


class ExistingCommentRef(Protocol):
    """Duck-typed shape of an existing-comment match a body can reference.

    Kept duck-typed (rather than importing ``existing_comments.ExistingComment``
    directly) so this module stays free of any dependency beyond the GitHub
    finding shape itself — ``existing_comments`` already depends on this
    module (it reuses ``_normalize_path``), so a direct import back would be
    circular.
    """

    html_url: str


def parse_valid_lines(patch: str, *, added_only: bool = COMMENT_ON_ADDED_LINES_ONLY) -> set[int]:
    """Return the new-file line numbers commentable on ``side="RIGHT"``.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub's ``files[].patch``),
          or empty for a binary/oversized/unchanged file.
    Postconditions:
        - Returns the set of 1-based new-file line numbers that appear in the diff
          as added lines (``+``) and, when ``added_only`` is False, context lines
          (`` ``). Removed lines (``-``) are left-side only and never included.
          An empty patch yields an empty set.
    """
    valid: set[int] = set()
    new_line = 0
    in_hunk = False
    for raw in (patch or "").splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        tag = raw[:1]
        if tag == "+":
            valid.add(new_line)
            new_line += 1
        elif tag == " " or raw == "":
            # Context line (a blank context line may arrive as an empty string):
            # it advances the new-file counter regardless of whether we comment on it.
            if not added_only:
                valid.add(new_line)
            new_line += 1
        # '-' removed lines and '\' ("\ No newline at end of file") do not advance
        # the new-file counter and are never commentable on the RIGHT side.
    return valid


def render_annotated_hunks(patch: str) -> str:
    """Render a file's diff hunks as new-file line-numbered text for review.

    Preconditions:
        - ``patch`` is one file's unified-diff text (GitHub's ``files[].patch``),
          or empty for a binary/oversized/unchanged file.
    Postconditions:
        - Returns the added (``+``) and context (`` ``) lines of each hunk, each
          prefixed with its 1-based new-file line number (``123: <code>``), with a
          ``...`` marker between non-contiguous hunks. Removed (``-``) lines are
          omitted (they are not in the new file). The emitted line numbers align
          1:1 with ``parse_valid_lines`` so a cited line maps to a real location.
          Scoping the reviewer to the diff (plus its context) — rather than whole
          files — keeps the review on what the PR actually changed. An empty/binary
          patch yields an empty string.
    """
    out: list[str] = []
    new_line = 0
    in_hunk = False
    first_hunk = True
    for raw in (patch or "").splitlines():
        header = _HUNK_RE.match(raw)
        if header:
            if not first_hunk:
                out.append("...")
            first_hunk = False
            new_line = int(header.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        tag = raw[:1]
        if tag == "+" or tag == " " or raw == "":
            out.append(f"{new_line}: {raw[1:] if raw else ''}")
            new_line += 1
        # '-' removed lines and '\' ("\ No newline...") have no new-file line.
    return "\n".join(out)


def _normalize_path(file_path: str, valid_by_path: dict[str, set[int]]) -> Optional[str]:
    """Resolve a finding's ``file_path`` to a key in ``valid_by_path``.

    Postconditions:
        - Returns the matching diff path, or None when no confident match exists.
          Tries an exact match, then a leading-``./`` strip, then a unique basename
          match (ambiguous basenames yield None rather than a wrong anchor).
    """
    if not file_path:
        return None
    for candidate in (file_path, file_path.lstrip("./"), file_path.lstrip("/")):
        if candidate in valid_by_path:
            return candidate
    base = file_path.rsplit("/", 1)[-1]
    matches = [k for k in valid_by_path if k.rsplit("/", 1)[-1] == base]
    if len(matches) == 1:
        return matches[0]
    return None


def map_issues_to_comments(
    issues: Iterable[Any],
    valid_by_path: dict[str, set[int]],
    existing_by_issue: Optional[dict[int, ExistingCommentRef]] = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Split findings into review comments and standalone leftovers.

    A finding is routed to the single PR review wherever it can be: anchored to a
    changed line when the cited line is in the diff, otherwise attached to the
    file as a whole when the file changed (a fabricated line would be misleading
    and GitHub would 422 it). Only a finding whose file is not in the diff at all
    can't be a review comment, so it is left for the caller to post as a
    standalone conversation comment.

    Preconditions:
        - ``existing_by_issue``, when given, maps ``id(issue)`` (Python object
          identity, valid only among the ``issues`` passed in this same call)
          to the existing PR comment that finding duplicates but which is not
          yet resolved — see ``existing_comments.partition_issues_by_existing_comments``.
          Absent or ``None`` behaves exactly as before this parameter existed.
    Postconditions:
        - Returns ``(review_comments, leftover_issues)``. ``review_comments``
          contains one entry per finding whose file resolves to a path in
          ``valid_by_path``: a line-anchored ``{"path", "line", "side": "RIGHT",
          "body"}`` when ``line`` falls on a commentable line, otherwise a
          file-level ``{"path", "subject_type": "file", "body"}``. Every other
          finding (no resolvable file) is returned as a leftover so the caller
          can post it as its own standalone conversation comment. Nothing is
          dropped, and no comment carries more than one finding. A finding with
          an entry in ``existing_by_issue`` has its body annotated with a
          reference to that existing comment (see ``format_comment_body``).
    """
    review_comments: list[dict[str, Any]] = []
    leftover: list[Any] = []
    existing_by_issue = existing_by_issue or {}
    for issue in issues:
        line = getattr(issue, "line", None)
        path = _normalize_path(getattr(issue, "file_path", "") or "", valid_by_path)
        reference = existing_by_issue.get(id(issue))
        if path is None:
            leftover.append(issue)
        elif line is not None and int(line) in valid_by_path[path]:
            review_comments.append(
                {
                    "path": path,
                    "line": int(line),
                    "side": "RIGHT",
                    "body": format_comment_body(issue, reference),
                }
            )
        else:
            review_comments.append(
                {
                    "path": path,
                    "subject_type": "file",
                    "body": format_comment_body(issue, reference),
                }
            )
    return review_comments, leftover


def is_within_diff(finding: Any, valid_by_path: dict[str, set[int]]) -> bool:
    """True when a finding's file/line is definitely inside this PR's diff.

    Uses the same file/line matching as :func:`map_issues_to_comments`'s
    line-anchored branch, so "within the diff" here means exactly "would be
    posted as a line-anchored inline comment" — a finding whose file matches
    but whose line does not (e.g. unchanged code inside a changed file) is NOT
    considered within the diff, since that code legitimately can be
    pre-existing.

    Postconditions:
        - Returns True iff ``finding.file_path`` resolves to a key in
          ``valid_by_path`` (per :func:`_normalize_path`) and ``finding.line``
          is a commentable line number for that file. False otherwise
          (including when ``finding.line`` is ``None``).
    """
    path = _normalize_path(getattr(finding, "file_path", "") or "", valid_by_path)
    if path is None:
        return False
    line = getattr(finding, "line", None)
    return line is not None and int(line) in valid_by_path[path]


def split_review_comments(
    comments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition mapped comments into line-anchored and file-level groups.

    The two shapes produced by ``map_issues_to_comments``/``anchor_to_first_file``
    travel on different GitHub endpoints: line-anchored comments ride the single
    review (``POST /pulls/{n}/reviews``), while file-level comments
    (``subject_type="file"``) must go on the dedicated comments endpoint
    (``POST /pulls/{n}/comments``) — the reviews array rejects ``subject_type``.

    Preconditions:
        - ``comments`` is a list of entries produced by ``map_issues_to_comments``
          or ``anchor_to_first_file``: each is line-anchored (carries ``line``) or
          file-level (carries ``subject_type == "file"``).
    Postconditions:
        - Returns ``(line_anchored, file_level)`` where ``line_anchored`` is every
          entry carrying a ``line`` key and ``file_level`` is every other entry.
          The split is exhaustive: the two groups' lengths always sum to
          ``len(comments)`` (no entry is dropped or duplicated), so a finding can
          never be silently lost even if a future producer emits an unexpected
          shape — it falls into ``file_level`` and is still posted (file-level).
          Input order is preserved within each group.
    """
    line_anchored = [c for c in comments if "line" in c]
    file_level = [c for c in comments if "line" not in c]
    return line_anchored, file_level


def format_comment_body(
    issue: ReviewFinding, existing_reference: Optional[ExistingCommentRef] = None
) -> str:
    """Render one finding as a review-comment body: what's wrong + the fix (prose).

    Used for both line-anchored and file-level review comments — the body carries
    no location, so the same rendering serves either anchor.

    Preconditions:
        - ``existing_reference``, when given, exposes ``html_url`` (duck-typed —
          see :class:`ExistingCommentRef`); it names an existing, still-open PR
          comment this same finding duplicates.
    Postconditions:
        - Returns ``**[SEVERITY] category** — description`` followed by a
          ``**Suggested fix:**`` paragraph when a suggestion is present, and,
          when ``existing_reference`` is given, a trailing note linking it —
          so a reviewer sees this finding was already raised and is still open,
          rather than reading it as a brand-new duplicate.
    """
    severity = (getattr(issue, "severity", "") or "info").upper()
    category = getattr(issue, "category", "") or "general"
    description = getattr(issue, "description", "") or ""
    suggestion = getattr(issue, "suggestion", "") or ""
    body = f"**[{severity}] {category}** — {description}".rstrip()
    if suggestion:
        body += f"\n\n**Suggested fix:** {suggestion}"
    if existing_reference is not None:
        body += (
            "\n\n_Possibly already tracked by an existing unresolved comment: "
            f"{existing_reference.html_url}_"
        )
    return body


def _location_prefix(path: str, line: Optional[int] = None) -> str:
    """Render the `` `path` — `` / `` `path:line` — `` prefix for a standalone comment.

    Postconditions:
        - Returns ``"`path` — "`` (or ``"`path:line` — "`` when ``line`` is a valid
          1-based line number), or ``""`` when ``path`` is empty, so callers can
          prepend a location to a finding posted away from its diff line. A
          non-positive ``line`` is treated as "no line" rather than emitted as a
          misleading ``path:0``.
    """
    if not path:
        return ""
    anchor = f"{path}:{line}" if line is not None and line > 0 else path
    return f"`{anchor}` — "


def format_issue_comment(
    issue: Any, existing_reference: Optional[ExistingCommentRef] = None
) -> str:
    """Render one finding as its own standalone PR conversation comment.

    Used for findings that could not be anchored to a diff line: each is posted
    as a separate conversation comment rather than batched, so every issue gets
    exactly one comment and no comment ever lists more than one issue.

    Postconditions:
        - Returns ``format_comment_body(issue, existing_reference)`` prefixed
          with a `` `file_path` — `` location when the finding names a file, so
          the standalone comment still points at where the issue lives.
    """
    return (
        f"{_location_prefix(getattr(issue, 'file_path', '') or '')}"
        f"{format_comment_body(issue, existing_reference)}"
    )


def inline_comment_to_timeline_body(comment: dict[str, Any]) -> str:
    """Render an inline-comment dict as a standalone conversation comment body.

    Only used on the rare path where GitHub rejects the review's comments and the
    submission degrades to a body-only review (see ``_submit_review``): the
    dropped findings are re-posted as individual conversation comments so no
    finding is lost.

    Preconditions:
        - ``comment`` is an entry produced by ``map_issues_to_comments`` — it
          carries ``path``, an already-formatted ``body``, and ``line`` only when
          it is a line-anchored comment (file-level comments carry no ``line``).
    Postconditions:
        - Returns the comment ``body`` prefixed with a `` `path:line` — ``
          location, or `` `path` — `` for a file-level comment (no ``line``).
    """
    prefix = _location_prefix(comment.get("path", "") or "", comment.get("line"))
    return f"{prefix}{comment.get('body', '') or ''}"


def build_review_body(summary: str, spec_compliance_notes: str, issue_count: int) -> str:
    """Assemble the summary-only top-level review body.

    The body never lists findings: each finding is posted as its own comment
    (inline when anchorable, otherwise a standalone conversation comment), so the
    body carries only the overall summary and spec-compliance narrative.

    Preconditions:
        - ``issue_count`` (``>= 0``) is passed explicitly by every caller — it has
          NO default. The zero-issue path returns a fixed "all good" message that
          ignores ``summary``/``spec_compliance_notes``, so a caller that silently
          fell back to a ``0`` default could post a false "no issues found" body
          while change-requesting comments sit on the PR. Requiring the argument
          forces the count to reflect the actual findings.
    Postconditions:
        - When ``issue_count`` is 0, always returns a short, fixed affirmational
          message — regardless of ``summary``/``spec_compliance_notes`` — so a
          clean review reads as a terse "all good" signal rather than whatever
          length of narrative the LLM produced.
        - Otherwise returns markdown combining the review summary and
          spec-compliance notes. Never empty — when both are blank it falls back
          to a "N finding(s) reported" line (so an empty summary never omits that
          change-requesting comments sit on the PR).
    """
    if issue_count == 0:
        return "No issues found — the code is of good quality."
    parts: list[str] = []
    if summary and summary.strip():
        parts.append(summary.strip())
    if spec_compliance_notes and spec_compliance_notes.strip():
        parts.append(f"**Spec compliance:** {spec_compliance_notes.strip()}")
    body = "\n\n".join(parts).strip()
    if body:
        return body
    noun = "finding" if issue_count == 1 else "findings"
    return f"Automated code review completed: {issue_count} {noun} reported."


def anchor_to_first_file(
    finding: ReviewFinding,
    valid_by_path: dict[str, set[int]],
    existing_reference: Optional[ExistingCommentRef] = None,
) -> Optional[dict[str, Any]]:
    """Anchor a leftover finding to the first changed file as a file-level review comment.

    Used when a finding's file path cannot be resolved to any path in the PR diff
    (``_normalize_path`` returns ``None``), but at least one changed file exists that
    can carry the comment as a file-level inline review entry.

    Preconditions:
        - ``finding`` is a code-review finding (duck-typed: any object with the
          attributes expected by ``format_comment_body``).
        - ``valid_by_path`` is the dict mapping each changed file's path to the set
          of its commentable line numbers (may be empty).
        - ``existing_reference``, when given, is threaded into
          ``format_comment_body`` unchanged (see :class:`ExistingCommentRef`).
    Postconditions:
        - Returns ``None`` when ``valid_by_path`` is empty (no changed file to anchor
          to; the caller should handle this case — typically the no-files early-exit
          path already prevents this from being reached).
        - Otherwise returns ``{"path": <first key of valid_by_path>,
          "subject_type": "file", "body": format_comment_body(finding, existing_reference)}``.
          The ``subject_type="file"`` field is the GitHub Review Comments API
          parameter that attaches the comment to the file rather than a specific line.
    """
    if not valid_by_path:
        return None
    fallback_path = next(iter(valid_by_path))
    return {
        "path": fallback_path,
        "subject_type": "file",
        "body": format_comment_body(finding, existing_reference),
    }


def choose_event(issues: Iterable[Any], author: str = "", reviewer: str = "") -> str:
    """Pick the GitHub review event from the findings and authorship.

    Postconditions:
        - Returns ``REQUEST_CHANGES`` when any finding is critical/high **and** the
          reviewer did not author the PR (GitHub 422s on requesting changes to your
          own PR); otherwise ``COMMENT``. ``PR_REVIEW_EVENT`` (COMMENT /
          REQUEST_CHANGES / APPROVE) overrides this when set.
    """
    override = (os.environ.get("PR_REVIEW_EVENT") or "").strip().upper()
    if override in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
        return override
    has_blocking = any(
        (getattr(i, "severity", "") or "").lower() in _BLOCKING_SEVERITIES for i in issues
    )
    same_author = bool(author) and bool(reviewer) and author == reviewer
    if has_blocking and not same_author:
        return "REQUEST_CHANGES"
    return "COMMENT"


# Max length of a generated GitHub issue title (GitHub itself allows 256; keep it
# short so the title reads as a headline and the full detail lives in the body).
_ISSUE_TITLE_MAX = 120

# Severities ordered least to most urgent, matching the documented set on
# CodeReviewIssue.severity (software_engineering_team/code_review_agent/models.py).
_SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]

# Minimum Jaccard similarity (on normalized description word-sets) for two
# same-category findings to be treated as the same underlying issue. Word-set
# overlap (rather than raw character similarity) avoids false-merging short,
# genuinely distinct descriptions that happen to share a common prefix (e.g.
# "latent bug A" vs "latent bug B" score low here despite scoring high on
# character-level similarity), while still catching near-duplicates whose
# only difference is a quoted identifier or number (e.g. "bare import `os`"
# vs "bare import `sys`" normalize to the identical word-set).
_SIMILARITY_THRESHOLD = 0.6

_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'")
_DIGITS_RE = re.compile(r"\b\d+\b")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize_for_similarity(text: str) -> frozenset[str]:
    """Reduce a finding description to a comparable bag of words.

    Postconditions:
        - Returns the set of word tokens in ``text``, lowercased, with
          backtick/quoted spans and standalone digit runs dropped first (so
          "bare import `os`" and "bare import `sys`" tokenize identically).
          Empty for blank/punctuation-only input.
    """
    normalized = _QUOTED_RE.sub(" ", text.lower())
    normalized = _DIGITS_RE.sub(" ", normalized)
    return frozenset(_WORD_RE.findall(normalized))


def _jaccard_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Postconditions: returns ``|a & b| / |a | b|``, or 0.0 when both are empty."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def group_similar_findings(
    findings: list[Any], threshold: float = _SIMILARITY_THRESHOLD
) -> list[list[Any]]:
    """Cluster pre-existing findings that describe the same underlying issue.

    Preconditions:
        - ``findings`` are duck-typed review findings exposing ``category`` and
          ``description`` (see module docstring); ``threshold`` is a Jaccard
          similarity in ``[0, 1]``.
    Postconditions:
        - Returns a list of non-empty groups partitioning ``findings``; both
          group order and within-group order match ``findings``' input order,
          so the result is deterministic for a given input.
        - Two findings are only ever grouped together when they share the same
          ``category`` (case-insensitive) AND the Jaccard similarity of their
          tokenized descriptions (see :func:`_tokenize_for_similarity`)
          against the group's first (founding) member is ``>= threshold``.
          Findings in different categories, or whose descriptions diverge
          below the threshold, land in separate single- or multi-member
          groups. Never raises; a finding with a blank category/description
          groups on that blank value like any other.
    """
    groups: list[list[Any]] = []
    representatives: list[tuple[str, frozenset[str]]] = []  # (category, tokens) per group
    for finding in findings:
        category = str(getattr(finding, "category", "") or "general").strip().lower()
        tokens = _tokenize_for_similarity(str(getattr(finding, "description", "") or ""))
        placed = False
        for group, (rep_category, rep_tokens) in zip(groups, representatives):
            if category != rep_category:
                continue
            if _jaccard_similarity(tokens, rep_tokens) >= threshold:
                group.append(finding)
                placed = True
                break
        if not placed:
            groups.append([finding])
            representatives.append((category, tokens))
    return groups


def proposal_from_findings(findings: list[Any], index: int) -> dict[str, Any]:
    """Serialize a group of similar pre-existing findings into one issue-proposal dict.

    A proposal is the persisted, JSON-safe form of one or more ``pre_existing``
    findings (see :func:`group_similar_findings`) that the review flow stores on
    the review summary and later offers to a human as a single GitHub-issue
    candidate. Findings are duck-typed (see :class:`ReviewFinding`).

    Preconditions:
        - ``findings`` is non-empty (one call per group produced by
          ``group_similar_findings``, never an empty group).
        - ``index`` is the group's 0-based position among a review's grouped
          pre-existing findings; it makes the proposal ``id`` (``"p{index}"``)
          stable and unique within one review, which the create-issues endpoint
          uses to select proposals and to mark them created idempotently.
    Postconditions:
        - Returns a dict with the keys ``id``, ``severity``, ``category``,
          ``file_path``, ``line`` (int or None), ``description``, ``suggestion``,
          ``locations``, ``issue_number`` (None) and ``issue_url`` (None).
        - ``locations`` is a list with one entry per finding in ``findings``,
          each ``{"file_path", "line", "description", "suggestion"}`` built the
          same way the top-level fields are; ``file_path``/``line``/
          ``description``/``suggestion`` mirror ``locations[0]`` (the group's
          first/representative finding), so single-finding groups produce the
          same top-level shape as before grouping existed.
        - ``severity`` is the most urgent value across the group (per
          ``_SEVERITY_ORDER``); ``category`` is the group's shared category.
        - The two ``issue_*`` fields start None and are filled in once a GitHub
          issue is created for the proposal. Every string field is coerced with
          ``str()`` so the dict is JSON-serializable regardless of the finding's
          field types, and every ``description``/``suggestion`` is
          token-scrubbed: this proposal is persisted and served through the Code
          Review page before any human opts to file an issue, so it must never
          carry a raw secret any more than a posted PR comment would.
    """

    def _location(finding: Any) -> dict[str, Any]:
        line = getattr(finding, "line", None)
        return {
            "file_path": str(getattr(finding, "file_path", "") or ""),
            "line": line if isinstance(line, int) and line > 0 else None,
            "description": scrub_token_from_text(str(getattr(finding, "description", "") or "")),
            "suggestion": scrub_token_from_text(str(getattr(finding, "suggestion", "") or "")),
        }

    locations = [_location(f) for f in findings]
    severities = {str(getattr(f, "severity", "") or "info").lower() for f in findings}
    severity = max(
        severities, key=lambda s: _SEVERITY_ORDER.index(s) if s in _SEVERITY_ORDER else -1
    )
    primary = locations[0]
    return {
        "id": f"p{index}",
        "severity": severity,
        "category": str(getattr(findings[0], "category", "") or "general"),
        "file_path": primary["file_path"],
        "line": primary["line"],
        "description": primary["description"],
        "suggestion": primary["suggestion"],
        "locations": locations,
        "issue_number": None,
        "issue_url": None,
    }


# Similarity thresholds for duplicate-issue detection (see find_matching_open_issue /
# annotate_duplicate_proposals below). SequenceMatcher.ratio() on casefolded strings;
# 1.0 is an exact match. Two thresholds, not one: the location signal (the finding's
# file_path appearing in the candidate issue's title/body) is itself strong
# corroborating evidence, so a looser text bar is safe once it's present. Without a
# location match, only a near-identical headline should count -- otherwise a generic
# short headline would swallow every open issue that happens to touch a similar theme.
# Both are overridable via env vars (mirroring the PR_REVIEW_EVENT override idiom
# choose_event already uses in this module) for tuning without a code change.
_DUPLICATE_TITLE_THRESHOLD_WITH_LOCATION = 0.5
_DUPLICATE_TITLE_THRESHOLD_NO_LOCATION = 0.8

# Minimum word-set (Jaccard) overlap additionally required for the "no location"
# text-alone signal above. SequenceMatcher's character-level ratio alone is fooled
# by two headlines that share a long templated prefix/suffix around one differing
# keyword -- e.g. "hardcoded secret in config" vs "hardcoded timeout in config"
# scores 0.83 (clears the default 0.8 char-ratio bar) despite describing unrelated
# bugs. Requiring the tokenized descriptions (see _tokenize_for_similarity, already
# used by group_similar_findings for the same reason) to also overlap this much
# catches that case without a location signal to corroborate it. Overridable via
# env var, same convention as the two thresholds above.
_DUPLICATE_TOKEN_OVERLAP_MIN_NO_LOCATION = 0.8

# Minimum word-set (Jaccard) overlap additionally required for the "with location"
# signal above too. The location signal alone is weaker corroboration than it looks
# -- many genuinely distinct bugs share the same file -- so a low with-location ratio
# bar (0.5) plus no token check lets the same "one differing keyword amid shared
# boilerplate" false positive through even with the location signal present (e.g.
# "hardcoded secret in config" vs "hardcoded timeout in config" both mentioning
# config.py: ratio 0.83, but only 0.6 word-set overlap). Set lower than the
# no-location floor (0.7 vs 0.8) since the location match is itself real corroborating
# evidence a same-file paraphrase (reordered words, added filler) can still clear.
# Overridable via env var, same convention as the other thresholds above.
_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION = 0.7


def _duplicate_threshold(env_var: str, default: float) -> float:
    """Read a float similarity-threshold override from the environment, defensively.

    Postconditions:
        - Returns ``float(os.environ[env_var])`` clamped to ``[0.0, 1.0]`` when it
          parses; otherwise returns ``default`` unchanged. A missing var, an
          empty/whitespace string, or unparsable text all degrade to the default
          rather than raising -- matches this repo's "garbage -> documented default"
          convention for numeric env vars (see docs/ENV_VARS.md).
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, min(1.0, value))


# Cap on how many open issues duplicate-detection reads per review. Unbounded
# traversal of a very active repository's open-issue list (GitHubClient.list_open_issues
# already paginates and self-limits at MAX_ISSUES_TRAVERSED=1000, which is still a lot
# of synchronous round-trips) would add real latency to a review's critical path for a
# benefit -- skipping a redundant proposal -- that degrades gracefully anyway. This
# trades recall (a duplicate among issues beyond the cap won't be found) for bounded,
# predictable review latency. Overridable via env var, same convention as the two
# similarity thresholds above.
_DUPLICATE_CHECK_MAX_OPEN_ISSUES = 100


def duplicate_check_max_open_issues() -> int:
    """Return the max number of open issues duplicate-detection reads per review.

    Postconditions:
        - Returns ``int(os.environ["PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES"])`` when it
          parses to a positive integer; otherwise returns
          :data:`_DUPLICATE_CHECK_MAX_OPEN_ISSUES` (100). A missing var, an
          empty/whitespace string, unparsable text, or a non-positive value all
          degrade to the default rather than raising or disabling the cap --
          matches this repo's "garbage -> documented default" convention for
          numeric env vars (see docs/ENV_VARS.md). Never raises.
    """
    raw = (os.environ.get("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES") or "").strip()
    if not raw:
        return _DUPLICATE_CHECK_MAX_OPEN_ISSUES
    try:
        value = int(raw)
    except ValueError:
        return _DUPLICATE_CHECK_MAX_OPEN_ISSUES
    return value if value > 0 else _DUPLICATE_CHECK_MAX_OPEN_ISSUES


# Matches exactly the wrapper `_proposal_title` applies around a proposal's bare
# headline (`"[severity] headline"`, optionally followed by `" (N occurrences)"`
# for a combined proposal) so a candidate issue's title can be un-wrapped back to
# its headline before it is compared to a fresh finding's own bare headline.
_ISSUE_TITLE_WRAPPER_RE = re.compile(
    r"^\[(?:info|low|medium|high|critical)\] (.*?)(?: \(\d+ occurrences\))?$",
    re.IGNORECASE,
)


def _normalized_issue_title(title: str) -> str:
    """Strip Khala's own severity-prefix/occurrences-suffix title wrapper, if present.

    ``_proposal_title`` renders a filed issue's title as ``"[severity] headline"``,
    or ``"[severity] headline (N occurrences)"`` for a combined proposal. Comparing
    a fresh finding's bare headline against that wrapped text via ``SequenceMatcher``
    dilutes the similarity ratio -- the wrapper can be a large fraction of a short
    headline -- which can push a genuine rerun match (against an issue Khala itself
    filed on a previous review) below threshold. Stripping a recognized wrapper
    before computing the ratio undoes exactly what Khala added, so a rerun still
    recognizes its own previously-filed issues.

    Postconditions:
        - Returns the captured headline when ``title`` matches the wrapper pattern
          exactly; otherwise returns ``title`` unchanged (a human-filed issue, or
          one from another tool, never carries this wrapper). Never raises.
    """
    match = _ISSUE_TITLE_WRAPPER_RE.match(title or "")
    return match.group(1) if match else (title or "")


_ISSUE_DESCRIPTION_SECTION_RE = re.compile(
    r"###\s*Description\s*\n+(.*?)(?:\n\n#|\Z)", re.IGNORECASE | re.DOTALL
)


def _issue_description_excerpt(issue: Issue) -> str:
    """Extract the untruncated finding description from a Khala-filed issue's body.

    ``build_issue_from_proposal`` always renders a proposal's full description
    verbatim under a ``### Description`` heading in the issue BODY, even though
    ``_proposal_title`` truncates that same text (to ``_ISSUE_TITLE_MAX``) for the
    issue TITLE. A long headline's truncated title can score a misleadingly low
    similarity ratio against a fresh proposal's full headline even when it is the
    very same finding -- extracting this section lets the caller compare against
    the untruncated text instead, whenever doing so scores higher.

    Postconditions:
        - Returns the text between a ``### Description`` heading and the next
          markdown heading (or the end of the body), stripped -- or ``""`` when no
          such heading is present (a human-filed issue, or one from another tool,
          never carries this exact structure). Never raises.
    """
    match = _ISSUE_DESCRIPTION_SECTION_RE.search(issue.body or "")
    return match.group(1).strip() if match else ""


def _location_appears_in(file_path: str, issue: Issue) -> bool:
    """True when a finding's file_path appears, at a path boundary, in an existing issue's title/body.

    ``build_issue_from_proposal`` renders exactly `` `file_path:line` `` or
    `` `file_path` `` into a Khala-filed issue's "**Location:**" line, so this is a
    cheap, exact structural signal for issues Khala itself opened in a past review --
    and, incidentally, still fires for a human-filed issue that happens to mention the
    same path.

    Preconditions:
        - ``file_path`` is the candidate finding's ``file_path`` (may be empty -- a
          finding with no location can never structurally match anything).
    Postconditions:
        - Returns False when ``file_path`` is empty (an empty string is a substring of
          every string in Python, which would otherwise make every issue "match" a
          location-less finding). Otherwise returns True iff ``file_path`` (casefolded)
          appears in ``issue.title`` or ``issue.body`` (casefolded) with no word
          character (letter/digit/underscore) immediately before or after the match --
          a plain substring check would also match a short/top-level name like
          ``a.py`` inside an unrelated ``data.py``, or ``app.py`` inside ``myapp.py``,
          since both are literal substrings of the other. Requiring a path/word
          boundary on both sides rejects those false positives while still matching
          the exact `` `file_path` ``/`` `file_path:line` `` rendering (bounded by
          backticks, whitespace, or punctuation) and a longer path merely carrying
          ``file_path`` as a `` / ``-separated suffix. Never raises.
    """
    if not file_path:
        return False
    needle = re.escape(file_path.casefold())
    pattern = re.compile(rf"(?<!\w){needle}(?!\w)")
    return bool(
        pattern.search((issue.title or "").casefold())
        or pattern.search((issue.body or "").casefold())
    )


def find_matching_open_issue(
    proposal: dict[str, Any], open_issues: Iterable[Issue]
) -> Optional[Issue]:
    """Return the open issue that most likely already tracks this proposal's bug, if any.

    Two independent signals, either sufficient on its own:
      - Structural + moderate text: the proposal's ``file_path`` appears in the
        candidate issue's title/body (:func:`_location_appears_in`) AND the
        proposal's description headline is at least somewhat similar to the issue's
        title (>= :data:`_DUPLICATE_TITLE_THRESHOLD_WITH_LOCATION`, default 0.5) AND
        their tokenized word-sets overlap at least
        :data:`_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION` (default 0.7) -- the
        location signal alone is weaker corroboration than it looks, since many
        genuinely distinct bugs share the same file; the token-overlap requirement
        guards against the same "one differing keyword amid shared boilerplate"
        false positive described below, now also reachable via the location signal's
        looser ratio bar (e.g. an issue that also happens to mention the same file).
      - Strong text alone: the headline/title character-level similarity clears
        :data:`_DUPLICATE_TITLE_THRESHOLD_NO_LOCATION` (default 0.8) AND their
        tokenized word-sets (:func:`_tokenize_for_similarity`) overlap at least
        :data:`_DUPLICATE_TOKEN_OVERLAP_MIN_NO_LOCATION` (default 0.8) -- covers a
        finding with a blank ``file_path``, or an issue whose body doesn't happen
        to repeat the exact path string. The extra token-overlap requirement
        guards against two headlines that share a long templated prefix/suffix
        around one differing keyword (e.g. "hardcoded secret in config" vs
        "hardcoded timeout in config") scoring a deceptively high character ratio
        despite describing unrelated bugs.

    A candidate issue's title is compared after :func:`_normalized_issue_title`
    strips Khala's own severity-prefix/occurrences-suffix wrapper (if present), so
    a rerun still recognizes an issue Khala itself filed on a previous review --
    without this, the wrapper text dilutes the similarity ratio enough to drop a
    genuine match (especially a short headline) below threshold. The headline is
    also compared against :func:`_issue_description_excerpt` (the issue body's
    ``### Description`` section, when present) and whichever of the two scores
    higher is used -- a Khala-filed issue's TITLE may be truncated to fit
    ``_ISSUE_TITLE_MAX``, but its body always carries the full, untruncated
    description, so a long headline's rerun still recognizes its own previously
    filed issue even though the title alone would score too low.

    Pure and side-effect-free: no GitHub/network access. ``open_issues`` must already
    be a materialized (or safely re-iterable) snapshot -- callers fetch it ONCE per
    review (e.g. via ``GitHubClient.list_open_issues``) and pass the same snapshot to
    every proposal, never re-fetching per finding.

    Preconditions:
        - ``proposal`` is a dict produced by :func:`proposal_from_findings`.
        - ``open_issues`` yields every open issue currently visible to the caller;
          pull requests are already excluded by ``GitHubClient.list_open_issues``.
    Postconditions:
        - Returns the single best-matching ``Issue`` -- the one with the highest
          title-similarity ratio among every issue clearing either signal above -- or
          ``None`` when ``open_issues`` is empty or no issue clears either bar. Ties
          (equal ratio) favor the lowest issue ``number`` (earliest filed), so the
          result is deterministic regardless of ``open_issues``' iteration/pagination
          order. Never raises: a malformed ``Issue`` (blank title/body) is treated as
          clearing neither signal.
    """
    headline = _description_headline(str(proposal.get("description") or "")).casefold()
    file_path = str(proposal.get("file_path") or "")
    with_location = _duplicate_threshold(
        "PR_REVIEW_DUPLICATE_THRESHOLD_WITH_LOCATION",
        _DUPLICATE_TITLE_THRESHOLD_WITH_LOCATION,
    )
    no_location = _duplicate_threshold(
        "PR_REVIEW_DUPLICATE_THRESHOLD_NO_LOCATION", _DUPLICATE_TITLE_THRESHOLD_NO_LOCATION
    )
    no_location_token_overlap = _duplicate_threshold(
        "PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN", _DUPLICATE_TOKEN_OVERLAP_MIN_NO_LOCATION
    )
    with_location_token_overlap = _duplicate_threshold(
        "PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION",
        _DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION,
    )
    headline_tokens = _tokenize_for_similarity(headline)
    best: Optional[Issue] = None
    best_ratio = -1.0
    for issue in open_issues:
        candidate_title = _normalized_issue_title(issue.title or "").casefold()
        description_excerpt = _issue_description_excerpt(issue).casefold()
        candidate_texts = [candidate_title]
        if description_excerpt:
            candidate_texts.append(description_excerpt)
        ratio = -1.0
        best_text = candidate_title
        for text in candidate_texts:
            text_ratio = SequenceMatcher(None, headline, text).ratio()
            if text_ratio > ratio:
                ratio = text_ratio
                best_text = text
        token_overlap = _jaccard_similarity(headline_tokens, _tokenize_for_similarity(best_text))
        text_alone_matches = ratio >= no_location and token_overlap >= no_location_token_overlap
        location_matches = (
            _location_appears_in(file_path, issue)
            and ratio >= with_location
            and token_overlap >= with_location_token_overlap
        )
        matches = location_matches or text_alone_matches
        if not matches:
            continue
        if (
            best is None
            or ratio > best_ratio
            or (ratio == best_ratio and issue.number < best.number)
        ):
            best = issue
            best_ratio = ratio
    return best


def annotate_duplicate_proposals(
    proposals: list[dict[str, Any]], open_issues: Iterable[Issue]
) -> list[dict[str, Any]]:
    """Mark each proposal already tracked by an existing open issue, so it is never
    offered as a fresh "create issue" candidate.

    Reuses the existing ``issue_number``/``issue_url`` fields -- the same ones
    ``create_review_issues`` fills in once a NEW issue is filed -- rather than
    inventing a parallel state: the frontend's ``openProposals()`` filter (``!p.issue_
    url``) already means "don't offer to create/select this one", and
    ``create_review_issues`` already treats any proposal carrying ``issue_url`` as
    filed and skips it (its documented idempotency), so a matched proposal can never
    be filed a second time even if a caller mistakenly still passed its id. The new
    ``matched_existing`` field only distinguishes "Khala already filed this" from
    "this already exists elsewhere" for the frontend's wording -- it does not change
    either consumer's existing filter logic.

    Preconditions:
        - ``proposals`` is a list of proposal dicts fresh from
          :func:`proposal_from_findings` -- every entry's ``issue_url``/
          ``issue_number`` are still ``None`` (this must run before any proposal is
          persisted, shown to a human, or filed for this review).
        - ``open_issues`` is a snapshot of currently-open issues in the target repo,
          fetched ONCE per review by the caller -- never once per proposal.
    Postconditions:
        - Returns a NEW list, same length and order as ``proposals`` (no proposal
          dropped or reordered -- a matched proposal is still shown to the human,
          just not offered as creatable). Each returned dict is a copy carrying every
          field of :func:`proposal_from_findings`'s output plus ``matched_existing:
          bool``. When :func:`find_matching_open_issue` finds a match, ``issue_
          number``/``issue_url`` are overwritten with the matched issue's identity
          (pre-linking the proposal to it) and ``matched_existing`` is True;
          otherwise the proposal is unchanged apart from ``matched_existing: False``.
          Never raises -- pure computation over already-fetched data; never mutates
          an input dict.
    """
    open_issues = list(open_issues)
    out: list[dict[str, Any]] = []
    for p in proposals:
        match = find_matching_open_issue(p, open_issues)
        if match is None:
            out.append({**p, "matched_existing": False})
        else:
            out.append(
                {
                    **p,
                    "matched_existing": True,
                    "issue_number": match.number,
                    "issue_url": match.html_url,
                }
            )
    return out


def _description_headline(description: str) -> str:
    """Extract the single-line headline used for both an issue's title and duplicate matching.

    Postconditions:
        - Returns the first line of ``description``, stripped, or the literal
          "code review finding" when ``description`` is blank -- the fallback
          ``_proposal_title`` has always used, now shared so a proposal's
          generated title and its duplicate-matching text never drift apart.
    """
    description = (description or "").strip()
    return description.splitlines()[0].strip() if description else "code review finding"


def _proposal_title(proposal: dict[str, Any]) -> str:
    """Build a concise, single-line GitHub issue title for a proposal.

    Postconditions:
        - Returns ``"[<severity>] <first line of description>"`` truncated to
          ``_ISSUE_TITLE_MAX`` characters (an ellipsis replaces the tail when it
          would overflow). Falls back to a generic "code review finding" phrase
          when the description is blank, so the title is never empty. When the
          proposal covers more than one location, an ``" ({N} occurrences)"``
          suffix is appended (inside the truncation budget) so a filed issue's
          title signals it's a combined report.
    """
    severity = str(proposal.get("severity") or "info").lower()
    headline = _description_headline(str(proposal.get("description") or ""))
    locations = proposal.get("locations") or []
    suffix = f" ({len(locations)} occurrences)" if len(locations) > 1 else ""
    prefix = f"[{severity}] "
    budget = _ISSUE_TITLE_MAX - len(prefix) - len(suffix)
    if len(headline) > budget:
        headline = headline[: max(0, budget - 1)].rstrip() + "…"
    return f"{prefix}{headline}{suffix}"


def _location_text(file_path: str, line: Any) -> str:
    """Render one ``file_path``/``line`` pair as the markdown location text.

    Postconditions:
        - Returns ``"n/a"`` when ``file_path`` is blank, ``` `path:line` ```
          when ``line`` is a positive int, else ``` `path` ```.
    """
    if not file_path:
        return "n/a"
    if isinstance(line, int) and line > 0:
        return f"`{file_path}:{line}`"
    return f"`{file_path}`"


def build_issue_from_proposal(
    proposal: dict[str, Any], *, pr_number: int, pr_url: str
) -> tuple[str, str]:
    """Render a proposal as a ``(title, body)`` pair for a new GitHub issue.

    Preconditions:
        - ``proposal`` is a dict produced by :func:`proposal_from_findings`.
        - ``pr_number``/``pr_url`` identify the pull request whose review surfaced
          the finding, so the issue records where it came from.
    Postconditions:
        - Returns ``(title, body)``. ``title`` is the concise headline from
          :func:`_proposal_title`; ``body`` is markdown carrying every detail of
          the finding(s) (severity, category, location(s), description,
          suggested fix) plus provenance naming the originating PR — enough for
          a maintainer to act on the issue without the review context.
        - When ``proposal["locations"]`` has more than one entry, the body
          renders a ``### Locations`` section listing every location as a
          bullet (with its own description) instead of the single
          ``**Location:**`` line, and a ``### Suggested fixes`` section listing
          each distinct non-blank suggestion once. A single-location proposal
          renders exactly as before grouping existed. Never raises.
    """
    title = _proposal_title(proposal)
    severity = str(proposal.get("severity") or "info").lower()
    category = str(proposal.get("category") or "general")
    description = str(proposal.get("description") or "").strip()
    suggestion = str(proposal.get("suggestion") or "").strip()
    locations = proposal.get("locations") or []

    lines: list[str] = [
        f"An automated code review of pull request #{pr_number} ({pr_url}) flagged this as a "
        "**pre-existing** bug — a defect in code the pull request did not add or modify. It is "
        "filed here as its own issue so it can be triaged independently of that PR.",
        "",
        f"- **Severity:** {severity}",
        f"- **Category:** {category}",
    ]

    if len(locations) > 1:
        lines.append(f"- **Occurrences:** {len(locations)}")
        lines.extend(["", "### Description", description or "_No description provided._"])
        lines.extend(["", "### Locations"])
        for loc in locations:
            loc_text = _location_text(str(loc.get("file_path") or ""), loc.get("line"))
            loc_description = str(loc.get("description") or "").strip()
            lines.append(f"- {loc_text} — {loc_description or '_No description provided._'}")
        suggestions = list(
            dict.fromkeys(
                str(loc.get("suggestion") or "").strip()
                for loc in locations
                if loc.get("suggestion")
            )
        )
        if suggestions:
            lines.extend(["", "### Suggested fixes"])
            lines.extend(f"- {s}" for s in suggestions)
    else:
        file_path = str(proposal.get("file_path") or "")
        line = proposal.get("line")
        lines.append(f"- **Location:** {_location_text(file_path, line)}")
        lines.extend(["", "### Description", description or "_No description provided._"])
        if suggestion:
            lines.extend(["", "### Suggested fix", suggestion])

    return title, "\n".join(lines)
