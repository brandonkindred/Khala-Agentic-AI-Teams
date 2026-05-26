"""
GitHub-issue-driven runs for the coding team.

Reads open issues from a GitHub repository, picks the first one whose
sub-issues are all closed, converts it to a CodingTeamPlanInput, and lets
the existing orchestrator handle the work.
"""

from .client import (
    MAX_ISSUES_TRAVERSED,
    CheckRun,
    CIStatusSummary,
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    PullRequest,
    Repo,
    SubIssue,
    scrub_token_from_text,
)
from .dependency_resolver import ReadyCheckResult, is_ready, pick_ready_issue
from .issue_to_plan import issue_to_plan_input

__all__ = [
    "CIStatusSummary",
    "CheckRun",
    "MAX_ISSUES_TRAVERSED",
    "GitHubAPIError",
    "GitHubClient",
    "Issue",
    "NotAnIssueError",
    "PullRequest",
    "ReadyCheckResult",
    "Repo",
    "SubIssue",
    "is_ready",
    "issue_to_plan_input",
    "pick_ready_issue",
    "scrub_token_from_text",
]
