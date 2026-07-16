"""
GitHub-issue-driven runs for the coding team.

Reads open issues from a GitHub repository, picks the first one whose
sub-issues are all closed, converts it to a CodingTeamPlanInput, and lets
the existing orchestrator handle the work.
"""

from .client import (
    MAX_ISSUES_TRAVERSED,
    MAX_REVIEW_COMMENTS_TRAVERSED,
    MAX_REVIEW_THREADS_TRAVERSED,
    GitHubAPIError,
    GitHubClient,
    Issue,
    IssueComment,
    NotAnIssueError,
    PullRequest,
    PullRequestDetail,
    PullRequestFile,
    Repo,
    ReviewComment,
    SubIssue,
    scrub_token_from_text,
)
from .dependency_resolver import ReadyCheckResult, is_ready, pick_ready_issue
from .existing_comments import (
    ExistingComment,
    build_existing_comments,
    match_existing_comment,
    partition_issues_by_existing_comments,
)
from .issue_to_plan import issue_to_plan_input
from .pr_review_mapping import (
    anchor_to_first_file,
    annotate_duplicate_proposals,
    build_issue_from_proposal,
    build_review_body,
    choose_event,
    duplicate_check_max_open_issues,
    find_matching_open_issue,
    format_comment_body,
    format_issue_comment,
    group_similar_findings,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    parse_valid_lines,
    proposal_from_findings,
    render_annotated_hunks,
    split_review_comments,
)
from .repo_reader import GitHubRepoReader

__all__ = [
    "MAX_ISSUES_TRAVERSED",
    "MAX_REVIEW_COMMENTS_TRAVERSED",
    "MAX_REVIEW_THREADS_TRAVERSED",
    "ExistingComment",
    "GitHubAPIError",
    "GitHubClient",
    "GitHubRepoReader",
    "Issue",
    "IssueComment",
    "NotAnIssueError",
    "PullRequest",
    "PullRequestDetail",
    "PullRequestFile",
    "ReadyCheckResult",
    "Repo",
    "ReviewComment",
    "SubIssue",
    "anchor_to_first_file",
    "annotate_duplicate_proposals",
    "build_existing_comments",
    "build_issue_from_proposal",
    "build_review_body",
    "choose_event",
    "duplicate_check_max_open_issues",
    "find_matching_open_issue",
    "format_comment_body",
    "format_issue_comment",
    "group_similar_findings",
    "inline_comment_to_timeline_body",
    "is_ready",
    "is_within_diff",
    "issue_to_plan_input",
    "map_issues_to_comments",
    "match_existing_comment",
    "parse_valid_lines",
    "partition_issues_by_existing_comments",
    "pick_ready_issue",
    "proposal_from_findings",
    "render_annotated_hunks",
    "scrub_token_from_text",
    "split_review_comments",
]
