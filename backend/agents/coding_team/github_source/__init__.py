"""
GitHub-issue-driven runs for the coding team.

Reads open issues from a GitHub repository, picks the first one whose
sub-issues are all closed, converts it to a CodingTeamPlanInput, and lets
the existing orchestrator handle the work.
"""

from .client import (
    MAX_ISSUES_TRAVERSED,
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    PullRequest,
    PullRequestDetail,
    PullRequestFile,
    Repo,
    SubIssue,
    scrub_token_from_text,
)
from .dependency_resolver import ReadyCheckResult, is_ready, pick_ready_issue
from .issue_to_plan import issue_to_plan_input
from .pr_review_mapping import (
    build_review_body,
    choose_event,
    format_comment_body,
    map_issues_to_comments,
    parse_valid_lines,
    render_annotated_hunks,
)

__all__ = [
    "MAX_ISSUES_TRAVERSED",
    "GitHubAPIError",
    "GitHubClient",
    "Issue",
    "NotAnIssueError",
    "PullRequest",
    "PullRequestDetail",
    "PullRequestFile",
    "ReadyCheckResult",
    "Repo",
    "SubIssue",
    "build_review_body",
    "choose_event",
    "format_comment_body",
    "is_ready",
    "issue_to_plan_input",
    "map_issues_to_comments",
    "parse_valid_lines",
    "pick_ready_issue",
    "render_annotated_hunks",
    "scrub_token_from_text",
]
