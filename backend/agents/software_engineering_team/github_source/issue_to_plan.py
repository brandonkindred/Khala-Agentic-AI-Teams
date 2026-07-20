"""
Map a GitHub Issue (plus its sub-issues) into the CodingTeamPlanInput model
that `run_coding_team_orchestrator` expects.

GitHub-specific metadata is stashed under `project_overview["github_issue"]`
so downstream code can reach it without a model change.
"""

from __future__ import annotations

from typing import Sequence

from software_engineering_team.models import CodingTeamPlanInput

from .client import Issue, SubIssue


def issue_to_plan_input(
    issue: Issue,
    repo_path: str,
    sub_issues: Sequence[SubIssue],
    owner: str,
    repo: str,
) -> CodingTeamPlanInput:
    """Map a GitHub ``issue`` (plus its ``sub_issues``) into a ``CodingTeamPlanInput``.

    Preconditions:
        - ``issue`` belongs to ``owner/repo``; ``sub_issues`` is the issue's fetched
          sub-issue list (possibly empty).
    Postconditions:
        - Returns a ``CodingTeamPlanInput`` whose ``requirements_title``/
          ``requirements_description`` come from ``issue.title``/``issue.body``, whose
          ``project_overview["github_issue"]`` carries ``owner``/``repo``/``number``/
          ``html_url``/``labels`` for downstream GitHub-aware code, and whose
          ``repo_path`` is the given ``repo_path``. ``completed_work_summary`` is set
          from the closed sub-issues (one bullet per closed sub-issue) when any exist,
          else ``None`` — closed sub-issues are genuinely-finished work and feed the
          Tech Lead's already_complete short-circuit, unlike ordinary repo context.
    """
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
