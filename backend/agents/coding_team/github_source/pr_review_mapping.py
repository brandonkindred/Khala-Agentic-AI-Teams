"""Map structured code-review findings onto GitHub pull-request review comments.

Pure, side-effect-free helpers used by the ``/review-pr`` flow:

- ``parse_valid_lines`` — turn one file's unified diff into the set of new-file
  line numbers that can carry an inline comment on ``side="RIGHT"``.
- ``map_issues_to_comments`` — route each review finding to either an inline
  comment (its line is in the diff) or a standalone leftover (it is not).
- ``format_comment_body`` — render one finding as an inline-comment body.
- ``format_issue_comment`` / ``inline_comment_to_timeline_body`` — render one
  finding as its own standalone PR conversation (issue) comment.
- ``build_review_body`` — render the summary-only review body.
- ``choose_event`` — pick the GitHub review event from issue severity.

Every finding gets exactly one comment: anchorable findings become inline review
comments (one each), and the rest are posted as individual conversation comments
by the caller — the review body never lists findings.

Kept free of any GitHub-client or LLM dependency so it is cheap to unit-test and
reusable. Findings are duck-typed: any object exposing ``severity``, ``category``,
``file_path``, ``description``, ``suggestion`` and ``line`` attributes works.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

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
    issues: Iterable[Any], valid_by_path: dict[str, set[int]]
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Split findings into inline comments and standalone leftovers.

    Postconditions:
        - Returns ``(inline_comments, leftover_issues)``. A finding becomes an
          inline comment iff it carries a ``line`` that resolves to a path in
          ``valid_by_path`` and falls on a commentable line; every other finding
          is returned as a leftover so the caller can post it as its own
          standalone conversation comment (nothing is dropped). Each inline
          comment is ``{"path", "line", "side": "RIGHT", "body"}``.
    """
    inline: list[dict[str, Any]] = []
    leftover: list[Any] = []
    for issue in issues:
        line = getattr(issue, "line", None)
        path = _normalize_path(getattr(issue, "file_path", "") or "", valid_by_path)
        if line is not None and path is not None and int(line) in valid_by_path[path]:
            inline.append(
                {
                    "path": path,
                    "line": int(line),
                    "side": "RIGHT",
                    "body": format_comment_body(issue),
                }
            )
        else:
            leftover.append(issue)
    return inline, leftover


def format_comment_body(issue: Any) -> str:
    """Render one finding as an inline-comment body: what's wrong + the fix (prose).

    Postconditions:
        - Returns ``**[SEVERITY] category** — description`` followed by a
          ``**Suggested fix:**`` paragraph when a suggestion is present.
    """
    severity = (getattr(issue, "severity", "") or "info").upper()
    category = getattr(issue, "category", "") or "general"
    description = getattr(issue, "description", "") or ""
    suggestion = getattr(issue, "suggestion", "") or ""
    body = f"**[{severity}] {category}** — {description}".rstrip()
    if suggestion:
        body += f"\n\n**Suggested fix:** {suggestion}"
    return body


def _location_prefix(path: str, line: Optional[int] = None) -> str:
    """Render the `` `path` — `` / `` `path:line` — `` prefix for a standalone comment.

    Postconditions:
        - Returns ``"`path` — "`` (or ``"`path:line` — "`` when ``line`` is given),
          or ``""`` when ``path`` is empty, so callers can prepend a location to a
          finding posted away from its diff line.
    """
    if not path:
        return ""
    anchor = f"{path}:{line}" if line is not None else path
    return f"`{anchor}` — "


def format_issue_comment(issue: Any) -> str:
    """Render one finding as its own standalone PR conversation comment.

    Used for findings that could not be anchored to a diff line: each is posted
    as a separate conversation comment rather than batched, so every issue gets
    exactly one comment and no comment ever lists more than one issue.

    Postconditions:
        - Returns ``format_comment_body(issue)`` prefixed with a `` `file_path` — ``
          location when the finding names a file, so the standalone comment still
          points at where the issue lives.
    """
    return f"{_location_prefix(getattr(issue, 'file_path', '') or '')}{format_comment_body(issue)}"


def inline_comment_to_timeline_body(comment: dict[str, Any]) -> str:
    """Render an inline-comment dict as a standalone conversation comment body.

    Only used on the rare path where GitHub rejects the review's inline comments
    and the submission degrades to a body-only review (see ``_submit_review``):
    the dropped inline findings are re-posted as individual conversation comments
    so no finding is lost.

    Preconditions:
        - ``comment`` is an entry produced by ``map_issues_to_comments`` — it
          carries ``path``, ``line`` and an already-formatted ``body``.
    Postconditions:
        - Returns the comment ``body`` prefixed with a `` `path:line` — `` location.
    """
    prefix = _location_prefix(comment.get("path", "") or "", comment.get("line"))
    return f"{prefix}{comment.get('body', '') or ''}"


def build_review_body(summary: str, spec_compliance_notes: str, issue_count: int = 0) -> str:
    """Assemble the summary-only top-level review body.

    The body never lists findings: each finding is posted as its own comment
    (inline when anchorable, otherwise a standalone conversation comment), so the
    body carries only the overall summary and spec-compliance narrative.

    Postconditions:
        - Returns markdown combining the review summary and spec-compliance notes.
          Never empty — when both are blank it falls back to a line that reflects
          ``issue_count``: a "N finding(s) posted as comment(s)" line when findings
          exist (so an empty summary never claims "no blocking issues" while
          change-requesting comments sit on the PR), otherwise a "no issues" line.
    """
    parts: list[str] = []
    if summary and summary.strip():
        parts.append(summary.strip())
    if spec_compliance_notes and spec_compliance_notes.strip():
        parts.append(f"**Spec compliance:** {spec_compliance_notes.strip()}")
    body = "\n\n".join(parts).strip()
    if body:
        return body
    if issue_count > 0:
        return f"Automated code review completed: {issue_count} finding(s) posted as comment(s)."
    return "Automated code review completed. No blocking issues found."


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
