"""Models for the Backend Expert agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class BackendInput(BaseModel):
    """Input for the Backend Expert agent."""

    task_description: str
    requirements: str = ""
    user_story: str = Field(
        default="",
        description="User story describing intended usage: As a [role], I want [goal] so that [benefit]",
    )
    spec_content: str = Field(
        default="",
        description="Full project specification for context on the overall application being built.",
    )
    language: str = "python"  # python or java
    architecture: Optional[SystemArchitecture] = None
    existing_code: Optional[str] = None
    api_spec: Optional[str] = None
    qa_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="QA issues to fix. Implement fixes and commit to feature branch.",
    )
    security_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Security issues to fix. Implement fixes and commit to feature branch.",
    )
    code_review_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Code review issues to resolve. Fix each issue before re-submitting.",
    )
    suggested_tests_from_qa: Optional[Dict[str, str]] = Field(
        default=None,
        description="Suggested unit_tests and integration_tests from QA/testing sub-agent. "
        "Keys: 'unit_tests', 'integration_tests'. Integrate into appropriate tests/test_*.py files.",
    )
    task_plan: Optional[str] = Field(
        default=None,
        description="Implementation plan from _plan_task(). When present, the model must implement "
        "the task according to this plan (realize what_changes and tests_needed).",
    )
    specialist_tooling_plan: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional Backend Agent V2 specialist orchestration plan prepared by Tech Lead/Planning. "
            "Expected keys may include devops, api, quality_review, qa, data_engineering, auth_security, "
            "and general_problem_solver with directives for how each specialist should contribute to implementation "
            "across planning, execution, review, and testing in its specialty."
        ),
    )
    specialist_findings: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional findings returned by specialist agents. Use these results as additional "
            "constraints and implementation guidance when generating backend code and tests."
        ),
    )
    problem_solver_max_cycles: int = Field(
        default=20,
        ge=1,
        le=20,
        description="Maximum collaboration cycles with the general problem-solving specialist when bugs are identified.",
    )


class BackendOutput(BaseModel):
    """Output from the Backend Expert agent."""

    code: str = ""
    language: str = "python"
    summary: str = ""
    files: Dict[str, str] = Field(default_factory=dict)
    tests: str = ""
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. feat(api): add user authentication",
    )
    needs_clarification: bool = Field(
        default=False,
        description="When True, task is ambiguous; do not implement until clarification_requests are answered",
    )
    clarification_requests: List[str] = Field(
        default_factory=list,
        description="Specific questions for Tech Lead when task is poorly defined",
    )
    gitignore_entries: List[str] = Field(
        default_factory=list,
        description="Patterns to add to repo .gitignore (e.g. __pycache__/, .env)",
    )
    used_stub_fallback: bool = Field(
        default=False,
        description="True when LLM produced no files and stub was injected; workflow should notify Tech Lead.",
    )


class ReviewIterationRecord(BaseModel):
    """Record of a single review iteration within the workflow loop."""

    iteration: int = Field(description="1-based iteration number")
    build_passed: bool = True
    build_errors: str = ""
    code_review_approved: bool = True
    code_review_issue_count: int = 0
    security_approved: bool = True
    security_issue_count: int = 0
    qa_approved: bool = True
    qa_issue_count: int = 0
    dbc_already_compliant: bool = True
    dbc_comments_added: int = 0
    dbc_comments_updated: int = 0
    action_taken: str = Field(
        default="",
        description="What the agent did in response: 'fixed_build', 'fixed_review_issues', 'fixed_security_issues', 'fixed_qa_issues', 'no_issues'",
    )


class BackendWorkflowResult(BaseModel):
    """
    Full result of the backend agent's autonomous workflow.

    Captures the outcome of the 9-step lifecycle:
    1. Create feature branch
    2. Generate code
    3. Commit code
    4. Trigger QA + DBC reviews
    5. Wait for review responses
    6. Fix issues and commit
    7. Merge to development
    8. Delete feature branch
    9. Notify tech lead

    Postconditions:
        - If success is True, the feature branch has been merged and deleted.
        - If success is False, failure_reason describes why.
        - review_history contains a record for each review iteration attempted.
    """

    task_id: str = Field(description="ID of the task that was executed")
    success: bool = Field(default=False, description="True when code was merged to development")
    branch_name: str = Field(
        default="", description="Feature branch name used (e.g. feature/task-id)"
    )
    iterations_used: int = Field(default=0, description="Number of review iterations completed")
    final_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Final set of files written to the repository",
    )
    review_history: List[ReviewIterationRecord] = Field(
        default_factory=list,
        description="Record of each review iteration for auditability",
    )
    summary: str = Field(default="", description="Final summary of what was implemented")
    failure_reason: str = Field(default="", description="Reason for failure if success is False")
    needs_followup: bool = Field(
        default=False,
        description="When True, Tech Lead should consider creating a follow-up fix task",
    )
