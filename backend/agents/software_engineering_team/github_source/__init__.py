"""
GitHub source integrations for the coding team.

Includes issue selection for issue-driven runs, PR review comment mapping,
existing-comment handling, issue-proposal generation, and repository reading.
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
from .issue_grooming_runner import IssueGroomingRunner
from .issue_grooming_scoring import (
    ScoreBreakdown,
    complexity_label,
    merge_complexity_label,
    nearest_fibonacci,
    score_issue,
)
from .issue_grooming_split import (
    build_sub_issue,
    extract_checklist_items,
    should_split,
)
from .issue_proposals import (
    annotate_duplicate_proposals,
    build_issue_from_proposal,
    duplicate_check_max_open_issues,
    find_matching_open_issue,
    group_similar_findings,
    proposal_from_findings,
)
from .issue_to_plan import issue_to_plan_input
from .pr_review_mapping import (
    build_review_body,
    choose_event,
    format_comment_body,
    format_issue_comment,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    parse_valid_lines,
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
    "IssueGroomingRunner",
    "NotAnIssueError",
    "PullRequest",
    "PullRequestDetail",
    "PullRequestFile",
    "ReadyCheckResult",
    "Repo",
    "ReviewComment",
    "ScoreBreakdown",
    "SubIssue",
    "annotate_duplicate_proposals",
    "build_existing_comments",
    "build_issue_from_proposal",
    "build_review_body",
    "build_sub_issue",
    "choose_event",
    "complexity_label",
    "duplicate_check_max_open_issues",
    "extract_checklist_items",
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
    "merge_complexity_label",
    "nearest_fibonacci",
    "parse_valid_lines",
    "partition_issues_by_existing_comments",
    "pick_ready_issue",
    "proposal_from_findings",
    "render_annotated_hunks",
    "score_issue",
    "scrub_token_from_text",
    "should_split",
    "split_review_comments",
]
