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
- ``format_issue_comment`` / ``inline_comment_to_timeline_body`` — render one
  finding as its own standalone PR conversation (issue) comment.
- ``build_review_body`` — render the summary-only review body.
- ``choose_event`` — pick the GitHub review event from issue severity.

Every finding gets exactly one comment: a finding on a changed line becomes a
line-anchored inline comment (rides the single review), a finding whose file
changed but whose cited line is off-diff becomes a file-level review comment,
and a finding naming a file absent from the diff at all is left as a leftover
for the caller (``_partition_review_issues`` in ``api/pr_review.py``) to
render via ``format_issue_comment`` and post as its own standalone
conversation comment — never silently dropped, and never misattributed to an
unrelated changed file. The review body itself never lists findings.

Kept free of any GitHub-client or LLM dependency so it is cheap to unit-test and
reusable. Findings are duck-typed: any object exposing ``severity``, ``category``,
``file_path``, ``title``, ``description``, ``suggestion`` and ``line`` attributes
works.

The duplicate-issue-detection/proposal subsystem that used to live here (cluster
pre-existing findings, match them against open issues, render a GitHub issue) is
in :mod:`github_source.issue_proposals`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional, Protocol

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

# Gutter between a right-aligned line number and the source: ``  9|     foo(``.
# A pipe (not ``: ``) so the prefix cannot be mistaken for Python ``def``/dict
# syntax, and so width-padding cannot collide with an indented ``    1: value``
# dict key. Source text always starts at column ``width + 2``.
_NUMBERED_LINE_SEP = "| "

# Change-surface gutter markers occupying a single fixed leading column so the
# reviewer has direct evidence of what the diff touched. Added/modified (new-side)
# lines carry ``+``; enclosing context lines carry a space so the line numbers stay
# column-aligned and every gutter in a marked block is the same width. The marker
# is a separate column BEFORE the number, so it never changes the rendered line
# number the posting/mapping layer relies on.
TOUCHED_LINE_MARKER = "+"
CONTEXT_LINE_MARKER = " "  # a single space, not an empty string


class ReviewFinding(Protocol):
    """Duck-typed shape consumed by PR review comment mappers."""

    severity: str
    category: str
    file_path: Optional[str]
    title: str
    description: str
    suggestion: str
    line: Optional[int]


# Mirrors code_review_agent.models.derive_issue_title, kept as a local, pure
# duplicate rather than an import: this module is deliberately dependency-free
# (see module docstring) so it stays cheap to unit-test, and code_review_agent
# transitively imports strands/httpx/boto3 at package-import time.
_TITLE_MAX_LEN = 80


def _fallback_title(description: str) -> str:
    """Derive a short title from a finding's description when it has none.

    Postconditions:
        - Returns the description's first line, trimmed to at most
          ``_TITLE_MAX_LEN`` characters TOTAL (including a trailing "…" when
          truncated); prefers breaking at a word boundary, but falls back to a
          hard character boundary when the first word is longer than the limit.
          Returns "" only when ``description`` is blank.
    """
    stripped = (description or "").strip()
    if not stripped:
        return ""
    text = stripped.splitlines()[0].strip()
    if not text or len(text) <= _TITLE_MAX_LEN:
        return text
    limit = _TITLE_MAX_LEN - 1  # reserve one character for the trailing ellipsis
    prefix = text[:limit]
    truncated = prefix.rsplit(" ", 1)[0].rstrip(",.;:—-") or prefix.rstrip()
    return f"{truncated}…"


class ExistingCommentRef(Protocol):
    """Duck-typed shape of an existing-comment match a body can reference.

    Kept duck-typed (rather than importing ``existing_comments.ExistingComment``
    directly) so this module stays free of any dependency beyond the GitHub
    finding shape itself — ``existing_comments`` already depends on this
    module (it reuses ``_normalize_path``), so a direct import back would be
    circular.
    """

    html_url: str


def numbered_line_width(line_numbers: Iterable[int]) -> int:
    """Width of the widest 1-based line number in ``line_numbers``.

    Postconditions:
        - Returns ``len(str(max(line_numbers)))`` when the iterable yields at
          least one int; ``1`` when it is empty so a caller can still format
          a blank excerpt without a zero/negative width.
    """
    widest = 0
    for n in line_numbers:
        widest = max(widest, n)
    return len(str(widest)) if widest else 1


def format_numbered_source_line(n: int, text: str, *, width: int, marker: str = "") -> str:
    """Render one source line with a column-aligned ``N| `` gutter.

    Preconditions:
        - ``n`` is a positive 1-based line number; ``width >= len(str(n))``.
        - ``marker`` is either ``""`` (no marker column) or a single character
          (a change-surface gutter marker such as ``"+"`` on a touched line or
          ``" "`` on a context line).
    Postconditions:
        - When ``marker`` is ``""`` returns ``f"{n:>width}| {text}"`` exactly —
          byte-identical to the un-marked gutter every existing caller relies on.
        - When ``marker`` is a single character it is prepended as a fixed
          leading column: ``f"{marker}{n:>width}| {text}"``. The rendered line
          NUMBER is unchanged (only a marker column is added), so a marked and
          an un-marked line cite the same number; keeping ``width`` equal across
          an excerpt keeps hanging indents visually 4 columns, not 5, at
          9→10 / 99→100.
    """
    assert marker == "" or len(marker) == 1, "marker must be empty or a single character"
    return f"{marker}{n:>{width}}{_NUMBERED_LINE_SEP}{text}"


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
            # Context line. A legitimate blank line in the source carries a leading
            # space in the diff (tag == " "); raw == "" is a defensive fallback for a
            # malformed/empty line in the patch text itself. Either way it advances
            # the new-file counter regardless of whether we comment on it.
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
          prefixed with a change-surface marker column (``+`` on an
          added/modified line, a space on a context line), a right-aligned
          1-based new-file line number, and a ``| `` gutter (``+ 9| <code>`` /
          ``  9| <code>``), with a ``...`` marker between non-contiguous hunks.
          The marker column is BEFORE the number, so it never alters the
          rendered line number: the emitted numbers still align 1:1 with
          ``parse_valid_lines`` so a cited line maps to a real location.
          Removed (``-``) lines are omitted (they are not in the new file).
          Gutter width is the widest line number in the excerpt so source
          columns (and therefore hanging indents) stay aligned across
          9→10 / 99→100. Scoping the reviewer to the diff (plus its context) —
          rather than whole files — keeps the review on what the PR actually
          changed. An empty/binary patch yields an empty string.
    """
    rows: list[tuple[Optional[int], str, str]] = []
    new_line = 0
    in_hunk = False
    first_hunk = True
    for raw in (patch or "").splitlines():
        header = _HUNK_RE.match(raw)
        if header:
            if not first_hunk:
                rows.append((None, "...", ""))
            first_hunk = False
            new_line = int(header.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        tag = raw[:1]
        if tag == "+":
            rows.append((new_line, raw[1:], TOUCHED_LINE_MARKER))
            new_line += 1
        elif tag == " " or raw == "":
            rows.append((new_line, raw[1:] if raw else "", CONTEXT_LINE_MARKER))
            new_line += 1
        # '-' removed lines and '\' ("\ No newline...") have no new-file line.
    width = numbered_line_width(n for n, _, _ in rows if n is not None)
    return "\n".join(
        text if n is None else format_numbered_source_line(n, text, width=width, marker=marker)
        for n, text, marker in rows
    )


def _normalize_path(file_path: str, valid_by_path: dict[str, set[int]]) -> Optional[str]:
    """Resolve a finding's ``file_path`` to a key in ``valid_by_path``.

    Postconditions:
        - Returns the matching diff path, or None when no confident match exists.
          Tries an exact match, then strips a literal leading ``./`` (not any
          combination of ``.``/``/`` characters — a dotfile like ``.gitignore`` is
          left untouched), then strips a literal leading ``/``, then a unique
          basename match (ambiguous basenames yield None rather than a wrong
          anchor).
    """
    if not file_path:
        return None
    dot_slash_stripped = file_path[2:] if file_path.startswith("./") else file_path
    slash_stripped = file_path[1:] if file_path.startswith("/") else file_path
    for candidate in (file_path, dot_slash_stripped, slash_stripped):
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
          dropped, and no comment carries more than one finding. A finding that
          becomes a ``review_comments`` entry and has an entry in
          ``existing_by_issue`` has its body annotated with a reference to that
          existing comment (see ``format_comment_body``); leftover issues are
          returned unchanged, without that annotation, for the caller to render
          via ``format_issue_comment``.
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
        else:
            try:
                line_num = int(line) if line is not None else None
            except (TypeError, ValueError):
                line_num = None
            if line_num is not None and line_num in valid_by_path[path]:
                review_comments.append(
                    {
                        "path": path,
                        "line": line_num,
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
    if line is None:
        return False
    try:
        line_num = int(line)
    except (TypeError, ValueError):
        return False
    return line_num in valid_by_path[path]


def split_review_comments(
    comments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition mapped comments into line-anchored and file-level groups.

    The two shapes produced by ``map_issues_to_comments`` travel on different
    GitHub endpoints: line-anchored comments ride the single review
    (``POST /pulls/{n}/reviews``), while file-level comments
    (``subject_type="file"``) must go on the dedicated comments endpoint
    (``POST /pulls/{n}/comments``) — the reviews array rejects ``subject_type``.

    Preconditions:
        - ``comments`` is a list of entries produced by ``map_issues_to_comments``:
          each is line-anchored (carries ``line``) or file-level (carries
          ``subject_type == "file"``).
    Postconditions:
        - Returns ``(line_anchored, file_level)`` where ``line_anchored`` is every
          entry carrying a ``line`` key and ``file_level`` is every other entry.
          The split is exhaustive: the two groups' lengths always sum to
          ``len(comments)`` (no entry is dropped or duplicated), so a finding can
          never be silently lost. Entries carrying a ``line`` key are classified as
          line-anchored; all other entries — including any future producer's
          unexpected shape — fall into ``file_level`` and are still posted.
          Input order is preserved within each group.
    """
    line_anchored = [c for c in comments if "line" in c]
    file_level = [c for c in comments if "line" not in c]
    return line_anchored, file_level


def format_comment_body(
    issue: ReviewFinding, existing_reference: Optional[ExistingCommentRef] = None
) -> str:
    """Render one finding as a review-comment body: a title, the problem, and the fix.

    Used for both line-anchored and file-level review comments — the body carries
    no location, so the same rendering serves either anchor.

    Preconditions:
        - ``existing_reference``, when given, exposes ``html_url`` (duck-typed —
          see :class:`ExistingCommentRef`); it names an existing, still-open PR
          comment this same finding duplicates.
    Postconditions:
        - Returns a bolded title heading (the finding's own ``title``, or one
          derived from ``description`` when blank), followed by a
          ``_[SEVERITY] category_`` tag line, the problem description, and,
          when a suggestion is present, a ``**Suggested fix:**`` paragraph.
          When the description is short enough that its derived title
          reproduces it verbatim, the description paragraph is omitted rather
          than repeating the same text twice. When both ``title`` and
          ``description`` are blank, the heading itself falls back to
          ``**[SEVERITY] category**`` and the separate tag line is omitted, so
          severity/category is never shown twice. When ``existing_reference``
          is given, a trailing note links it — so a reviewer sees this finding
          was already raised and is still open, rather than reading it as a
          brand-new duplicate.
    """
    severity = (getattr(issue, "severity", "") or "info").upper()
    category = getattr(issue, "category", "") or "general"
    description = getattr(issue, "description", "") or ""
    suggestion = getattr(issue, "suggestion", "") or ""
    title = (getattr(issue, "title", "") or "").strip() or _fallback_title(description)
    if title:
        body = f"**{title}**\n_[{severity}] {category}_"
    else:
        body = f"**[{severity}] {category}**"
    if description and description.strip() != title:
        body += f"\n\n{description}"
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
    assert issue_count >= 0, "issue_count must be non-negative"
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


def choose_event(issues: Iterable[Any], author: str = "", reviewer: str = "") -> str:
    """Pick the GitHub review event from the findings and authorship.

    Postconditions:
        - Returns ``REQUEST_CHANGES`` when any finding is critical/high **and**
          ``author``/``reviewer`` are both known (non-empty) and different (GitHub
          422s on requesting changes to your own PR); otherwise ``COMMENT``. When
          authorship is unknown (either name empty), this defaults to ``COMMENT``
          rather than assuming the reviewer isn't the author. ``PR_REVIEW_EVENT``
          (COMMENT / REQUEST_CHANGES / APPROVE) overrides this when set.
    """
    override = (os.environ.get("PR_REVIEW_EVENT") or "").strip().upper()
    if override in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
        return override
    has_blocking = any(
        (getattr(i, "severity", "") or "").lower() in _BLOCKING_SEVERITIES for i in issues
    )
    if has_blocking and author and reviewer and author != reviewer:
        return "REQUEST_CHANGES"
    return "COMMENT"
