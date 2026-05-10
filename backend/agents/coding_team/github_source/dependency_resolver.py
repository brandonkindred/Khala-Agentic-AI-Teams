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
    for issue in client.list_open_issues(owner, repo, label=label):
        result = is_ready(client, owner, repo, issue)
        if result.ready:
            return issue, result
    return None
