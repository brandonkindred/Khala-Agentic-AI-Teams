"""
GitHub-issue-driven runs for the coding team.

Reads open issues from a GitHub repository, picks the first one whose
sub-issues are all closed, converts it to a CodingTeamPlanInput, and lets
the existing orchestrator handle the work.
"""

from .client import (
    GitHubAPIError,
    GitHubClient,
    Issue,
    PullRequest,
    Repo,
    SubIssue,
)
from .dependency_resolver import ReadyCheckResult, is_ready, pick_ready_issue
from .issue_to_plan import issue_to_plan_input

__all__ = [
    "GitHubAPIError",
    "GitHubClient",
    "Issue",
    "PullRequest",
    "ReadyCheckResult",
    "Repo",
    "SubIssue",
    "is_ready",
    "issue_to_plan_input",
    "pick_ready_issue",
]
