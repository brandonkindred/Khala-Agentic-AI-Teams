"""
GitHub source integrations for the coding team.

Includes issue selection for issue-driven runs, PR review comment mapping,
existing-comment handling, issue-proposal generation, LLM Fibonacci-scoring
prompt/schema contract and scorer, and repository reading.
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
    ReviewThread,
    ReviewThreadsUnavailableError,
    SubIssue,
    scrub_token_from_text,
)
from .dependency_resolver import ReadyCheckResult, is_ready, pick_ready_issue
from .enhanced_issue_builder import (
    build_enhanced_issue_from_proposal,
    compute_complexity_score,
)
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
    from_unified_score,
    merge_complexity_label,
    nearest_fibonacci,
    score_issue,
)
from .issue_grooming_split import (
    build_sub_issue,
    extract_checklist_items,
    should_split,
)
from .issue_heuristic_scorer import score_issue_heuristically
from .issue_llm_scorer import score_issue_via_llm
from .issue_proposals import (
    annotate_duplicate_proposals,
    build_issue_from_proposal,
    duplicate_check_max_open_issues,
    find_matching_open_issue,
    group_similar_findings,
    proposal_from_findings,
)

# Aliased: issue_scorer.score_issue (the LLM/heuristic mode-facade actually
# wired into IssueGroomingRunner, via from_unified_score's adaptation back to
# the legacy shape) would otherwise collide with issue_grooming_scoring.score_issue
# (the standalone legacy heuristic scorer, no longer Phase A's live path but
# still exported as a tested pure function, already re-exported above) at this
# package's top level.
from .issue_scorer import DEFAULT_SCORING_MODE, SCORING_MODES, resolve_scoring_mode
from .issue_scorer import score_issue as score_issue_by_mode

# Aliased: issue_scoring.ScoreBreakdown (the LLM-response schema, unrelated
# fields) would otherwise collide with issue_grooming_scoring.ScoreBreakdown
# (the heuristic scorer's own, already re-exported above) at this package's
# top level.
from .issue_scoring import FIBONACCI_COMPLEXITY_VALUES, build_scoring_prompt
from .issue_scoring import ScoreBreakdown as LLMScoreBreakdown
from .issue_to_plan import issue_to_plan_input
from .pr_review_mapping import (
    build_review_body,
    choose_event,
    format_comment_body,
    format_issue_comment,
    format_systemic_findings_comment,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    parse_removed_lines,
    parse_valid_lines,
    render_annotated_hunks,
    render_removed_hunks,
    split_review_comments,
)
from .repo_reader import GitHubRepoReader

__all__ = [
    "DEFAULT_SCORING_MODE",
    "FIBONACCI_COMPLEXITY_VALUES",
    "MAX_ISSUES_TRAVERSED",
    "MAX_REVIEW_COMMENTS_TRAVERSED",
    "MAX_REVIEW_THREADS_TRAVERSED",
    "SCORING_MODES",
    "ExistingComment",
    "GitHubAPIError",
    "GitHubClient",
    "GitHubRepoReader",
    "Issue",
    "IssueComment",
    "IssueGroomingRunner",
    "LLMScoreBreakdown",
    "NotAnIssueError",
    "PullRequest",
    "PullRequestDetail",
    "PullRequestFile",
    "ReadyCheckResult",
    "Repo",
    "ReviewComment",
    "ReviewThread",
    "ReviewThreadsUnavailableError",
    "ScoreBreakdown",
    "SubIssue",
    "annotate_duplicate_proposals",
    "build_enhanced_issue_from_proposal",
    "build_existing_comments",
    "build_issue_from_proposal",
    "build_review_body",
    "build_sub_issue",
    "build_scoring_prompt",
    "choose_event",
    "complexity_label",
    "compute_complexity_score",
    "duplicate_check_max_open_issues",
    "extract_checklist_items",
    "find_matching_open_issue",
    "format_comment_body",
    "format_issue_comment",
    "format_systemic_findings_comment",
    "from_unified_score",
    "group_similar_findings",
    "inline_comment_to_timeline_body",
    "is_ready",
    "is_within_diff",
    "issue_to_plan_input",
    "map_issues_to_comments",
    "match_existing_comment",
    "merge_complexity_label",
    "nearest_fibonacci",
    "parse_removed_lines",
    "parse_valid_lines",
    "partition_issues_by_existing_comments",
    "pick_ready_issue",
    "proposal_from_findings",
    "render_annotated_hunks",
    "render_removed_hunks",
    "resolve_scoring_mode",
    "score_issue",
    "score_issue_by_mode",
    "score_issue_heuristically",
    "score_issue_via_llm",
    "scrub_token_from_text",
    "should_split",
    "split_review_comments",
]
