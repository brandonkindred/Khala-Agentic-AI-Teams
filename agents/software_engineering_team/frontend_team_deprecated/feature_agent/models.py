"""Models for the Frontend Expert agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class FrontendInput(BaseModel):
    """Input for the Frontend Expert agent."""

    framework_target: str = Field(
        default="",
        description="Target frontend framework for implementation: react | angular | vue. If empty, will be detected from project files.",
    )
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
    architecture: Optional[SystemArchitecture] = None
    existing_code: Optional[str] = None
    api_endpoints: Optional[str] = None
    qa_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="QA issues to fix. Implement fixes and commit to feature branch.",
    )
    security_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Security issues to fix. Implement fixes and commit to feature branch.",
    )
    accessibility_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Accessibility (WCAG 2.2) issues to fix. Implement fixes and commit to feature branch.",
    )
    code_review_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Code review issues to resolve. Fix each issue before re-submitting.",
    )
    suggested_tests_from_qa: Optional[Dict[str, str]] = Field(
        default=None,
        description="Suggested unit_tests and integration_tests from QA/testing sub-agent. "
        "Keys: 'unit_tests', 'integration_tests'. Integrate into appropriate .spec.ts and e2e files.",
    )
    task_plan: Optional[str] = Field(
        default=None,
        description="Implementation plan from _plan_task(). When present, the model must implement "
        "the task according to this plan (realize what_changes and tests_needed).",
    )
    task_contract: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Normalized contract-first frontend task payload (goal/scope/constraints/ACs/NFRs).",
    )
    convergence_hint: Optional[str] = Field(
        default=None,
        description="Optional hint when code review issue count has not decreased over several rounds. "
        "E.g. 'Code review issue count has not decreased; make minimal, targeted fixes and avoid refactoring unrelated code.'",
    )


class FrontendOutput(BaseModel):
    """Output from the Frontend Expert agent."""

    framework_used: str = Field(default="", description="Framework path used for implementation")
    code: str = ""
    summary: str = ""
    files: Dict[str, str] = Field(default_factory=dict)
    components: List[str] = Field(default_factory=list)
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, e.g. feat(ui): add login component",
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
        description="Patterns to add to repo .gitignore (e.g. node_modules/, dist/)",
    )
    npm_packages_to_install: List[str] = Field(
        default_factory=list,
        description="npm package names to install, e.g. ['@ngrx/store', 'ngx-toastr']",
    )


class FrontendWorkflowResult(BaseModel):
    """Result of the frontend agent's autonomous workflow."""

    task_id: str = Field(description="ID of the task that was executed")
    success: bool = Field(default=False, description="True when code was merged to development")
    failure_reason: str = Field(default="", description="Reason for failure if success is False")
    summary: str = Field(default="", description="Final summary of what was implemented")
    llm_unreachable: bool = Field(
        default=False,
        description="True when the agent could not reach the LLM after retries; orchestrator should pause job.",
    )
