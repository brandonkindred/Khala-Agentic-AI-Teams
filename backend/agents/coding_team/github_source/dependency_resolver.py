"""
Sub-issue-based dependency resolver. An issue is "ready" iff none of its
sub-issues are open. Returned `ReadyCheckResult` includes the fetched
sub-issues so callers don't have to re-query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .client import GitHubClient, Issue, SubIssue


@dataclass(frozen=True)
class ReadyCheckResult:
    ready: bool
    blocking: tuple[int, ...]
    sub_issues: tuple[SubIssue, ...]


def is_ready(client: GitHubClient, owner: str, repo: str, issue: Issue) -> ReadyCheckResult:
    """Determine whether ``issue`` is ready to work: none of its sub-issues are still open.

    Preconditions:
        - ``issue`` is an issue on ``owner/repo``.
    Postconditions:
        - Fetches ``issue``'s sub-issues via ``client.list_sub_issues`` (one call) and
          returns a ``ReadyCheckResult`` whose ``ready`` is True iff none are open,
          ``blocking`` carries the numbers of the still-open ones (empty when ready),
          and ``sub_issues`` carries every fetched sub-issue (open and closed) so the
          caller does not have to re-query them.
    """
    subs = tuple(client.list_sub_issues(owner, repo, issue.number))
    open_subs = [s for s in subs if s.state == "open"]
    return ReadyCheckResult(
        ready=not open_subs,
        blocking=tuple(s.number for s in open_subs),
        sub_issues=subs,
    )


def pick_ready_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    label: Optional[str] = None,
) -> Optional[tuple[Issue, ReadyCheckResult]]:
    """Return the first open issue (optionally filtered by ``label``) with no open sub-issues.

    Preconditions:
        - ``label``, when given, is a valid label name on ``owner/repo``.
    Postconditions:
        - Iterates open issues in ``client.list_open_issues``'s order, calling
          ``is_ready`` on each, and returns the ``(issue, ReadyCheckResult)`` pair for
          the first one found ready. Returns ``None`` when no open issue is ready
          (including when there are no open issues at all).
    """
    for issue in client.list_open_issues(owner, repo, label=label):
        result = is_ready(client, owner, repo, issue)
        if result.ready:
            return issue, result
    return None
