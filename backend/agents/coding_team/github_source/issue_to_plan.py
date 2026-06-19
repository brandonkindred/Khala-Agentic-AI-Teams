"""
Map a GitHub Issue (plus its sub-issues) into the CodingTeamPlanInput model
that `run_coding_team_orchestrator` expects.

GitHub-specific metadata is stashed under `project_overview["github_issue"]`
so downstream code can reach it without a model change.
"""

from __future__ import annotations

from typing import Sequence

from coding_team.models import CodingTeamPlanInput

from .client import Issue, SubIssue


def issue_to_plan_input(
    issue: Issue,
    repo_path: str,
    sub_issues: Sequence[SubIssue],
    owner: str,
    repo: str,
) -> CodingTeamPlanInput:
    closed_summary = "\n".join(
        f"- #{s.number} {s.title}" for s in sub_issues if s.state == "closed"
    )
    return CodingTeamPlanInput(
        requirements_title=issue.title,
        # ``issue.body`` is already coerced to "" in the client when GitHub
        # returns null, so no fallback needed here.
        requirements_description=issue.body,
        project_overview={
            "github_issue": {
                "owner": owner,
                "repo": repo,
                "number": issue.number,
                "html_url": issue.html_url,
                "labels": list(issue.labels),
            }
        },
        # Closed sub-issues are genuinely-finished work, so they go in completed_work_summary (the
        # basis for the already_complete short-circuit) — NOT existing_code_summary, which is for
        # ordinary repo context that may still need changes.
        completed_work_summary=(
            f"Already-completed sub-issues:\n{closed_summary}" if closed_summary else None
        ),
        repo_path=repo_path,
    )
