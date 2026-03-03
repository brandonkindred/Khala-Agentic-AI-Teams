"""
Frontend Orchestrator Agent: sub-orchestration for frontend tasks.

Runs the full pipeline: Design (UX -> UI) -> Design System -> Architecture ->
Implementation (FrontendExpertAgent) -> UX Engineer polish -> Quality gates ->
Merge + Build/Release. Enforces the "ready to ship" gate checklist.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskUpdate
from software_engineering_team.shared.repo_utils import (
    int_env as _int_env,
    read_repo_code,
    truncate_for_context,
    FRONTEND_EXTENSIONS,
)
from software_engineering_team.shared.task_utils import (
    task_requirements,
    task_requirements_with_expectations,
)

from frontend_team_deprecated.feature_agent import FrontendExpertAgent, FrontendInput
from frontend_team_deprecated.feature_agent.agent import (
    _extract_affected_file_paths_from_frontend_build_errors,
    _read_frontend_affected_files_code,
    _apply_frontend_build_fix_edits,
)
from frontend_team_deprecated.feature_agent.models import FrontendOutput, FrontendWorkflowResult

from .models import (
    DesignSystemOutput,
    FrontendArchitectOutput,
    UIDesignerOutput,
    UXDesignerOutput,
    build_feature_implementation_context,
)
from .ux_designer import UXDesignerAgent, UXDesignerInput
from .ui_designer import UIDesignerAgent, UIDesignerInput
from .design_system import DesignSystemAgent, DesignSystemInput
from .frontend_architect import FrontendArchitectAgent, FrontendArchitectInput
from .ux_engineer import UXEngineerAgent, UXEngineerInput
from .performance_engineer import PerformanceEngineerAgent, PerformanceEngineerInput
from .build_release import BuildReleaseAgent, BuildReleaseInput

logger = logging.getLogger(__name__)


MAX_CODE_REVIEW_ITERATIONS = _int_env("SW_MAX_CODE_REVIEW_ITERATIONS", 100)
MAX_CLARIFICATION_REFINEMENTS = _int_env("SW_MAX_CLARIFICATION_REFINEMENTS", 100)
MAX_SAME_BUILD_FAILURES = _int_env("SW_MAX_SAME_BUILD_FAILURES", 3)
# Frontend-specific checklists for shared agents
FRONTEND_SECURITY_CHECKLIST = (
    "Frontend Security checklist: CSP posture, safe token storage (httpOnly cookies, secure storage), "
    "PKCE for OAuth, sanitization of user input, dependency vulnerability scan."
)
FRONTEND_A11Y_CHECKLIST = (
    "Per-component accessibility acceptance criteria: keyboard navigation, focus order, "
    "screen reader behavior (ARIA usage validation), WCAG 2.2 alignment."
)
FRONTEND_CODE_REVIEW_CHECKLIST = (
    "Frontend anti-patterns to check: bad hooks usage, rerender storms, prop drilling, "
    "missing trackBy in *ngFor, unnecessary change detection triggers."
)

# Lightweight path: skip design for implementation-only or fix tasks
LIGHTWEIGHT_KEYWORDS = ("fix", "resolve", "update", "patch", "correct", "remediate", "refactor", "adjust", "tweak")
LIGHTWEIGHT_MAX_DESC_LEN = 400


_task_requirements = task_requirements


def _task_requirements_with_route_expectations(task: Task, repo_path: Path) -> str:
    """Build requirements string including route/component expectations from repo."""
    return task_requirements_with_expectations(task, repo_path, "frontend")


_truncate_for_context = truncate_for_context


def _read_repo_code(repo_path: Path, extensions: List[str] | None = None) -> str:
    """Read code files from repo. Delegates to shared.repo_utils."""
    if extensions is None:
        extensions = FRONTEND_EXTENSIONS
    return read_repo_code(repo_path, extensions)


def _is_lightweight_task(task: Task) -> bool:
    """Determine if task should skip design phase (implementation-only or fix)."""
    desc = (task.description or "").lower()
    if len(desc) < LIGHTWEIGHT_MAX_DESC_LEN and any(kw in desc for kw in LIGHTWEIGHT_KEYWORDS):
        return True
    return False


class FrontendOrchestratorAgent:
    """
    Frontend Orchestrator: resolves conflicts, sequences work, enforces budgets,
    and runs the full frontend pipeline with all 12 agents.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

        self.feature_agent = FrontendExpertAgent(llm_client)
        self.ux_designer = UXDesignerAgent(llm_client)
        self.ui_designer = UIDesignerAgent(llm_client)
        self.design_system = DesignSystemAgent(llm_client)
        self.frontend_architect = FrontendArchitectAgent(llm_client)
        self.ux_engineer = UXEngineerAgent(llm_client)
        self.performance_engineer = PerformanceEngineerAgent(llm_client)
        self.build_release = BuildReleaseAgent(llm_client)

    def run_workflow(
        self,
        *,
        repo_path: Path,
        backend_dir: Path,
        task: Task,
        spec_content: str,
        architecture: Optional[SystemArchitecture],
        qa_agent: Any,
        accessibility_agent: Any,
        security_agent: Any,
        code_review_agent: Any,
        acceptance_verifier_agent: Any | None = None,
        dbc_agent: Any = None,
        tech_lead: Any = None,
        build_verifier: Callable[..., Tuple[bool, str]],
        doc_agent: Any | None = None,
        completed_tasks: List[Task] | None = None,
        remaining_tasks: List[Task] | None = None,
        all_tasks: Dict[str, Task] | None = None,
        append_backend_task_fn: Optional[Callable[[Task], None]] = None,
        append_frontend_task_fn: Optional[Callable[[str], None]] = None,
        linting_tool_agent: Any | None = None,
        build_fix_specialist: Any | None = None,
    ) -> FrontendWorkflowResult:
        """
        Execute the full frontend pipeline: design -> architecture -> implementation ->
        polish -> quality gates -> merge -> build/release.
        """
        from software_engineering_team.shared.command_runner import run_command_with_nvm
        from software_engineering_team.shared.git_utils import (
            DEVELOPMENT_BRANCH,
            checkout_branch,
            create_feature_branch,
            delete_branch,
            merge_branch,
        )
        from software_engineering_team.shared.repo_writer import write_agent_output, NO_FILES_TO_WRITE_MSG

        task_id = task.id
        branch_name = f"feature/{task_id}"

        try:
            ok, msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, task_id)
            if not ok:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=f"Feature branch failed: {msg}",
                )
        except Exception as e:
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason=f"Feature branch failed: {e}",
            )

        from software_engineering_team.shared.command_runner import ensure_frontend_dependencies_installed
        install_result = ensure_frontend_dependencies_installed(repo_path)
        if not install_result.success:
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return FrontendWorkflowResult(
                task_id=task_id,
                success=False,
                failure_reason="Frontend dependency install failed: " + (
                    install_result.error_summary or install_result.stderr or "unknown"
                ),
            )

        # --- Design phase (skip for lightweight tasks) ---
        ux_output: Optional[UXDesignerOutput] = None
        ui_output: Optional[UIDesignerOutput] = None
        design_system_output: Optional[DesignSystemOutput] = None
        architect_output: Optional[FrontendArchitectOutput] = None

        from software_engineering_team.shared.context_sizing import compute_spec_content_chars

        use_lightweight = _is_lightweight_task(task)
        max_spec = compute_spec_content_chars(self.feature_agent.llm)
        spec_truncated = _truncate_for_context(spec_content, max_spec)

        if not use_lightweight:
            logger.info("[%s] Frontend team: running design phase (UX -> UI -> Design System)", task_id)
            ux_output = self.ux_designer.run(UXDesignerInput(
                task_description=task.description,
                task_id=task_id,
                spec_content=spec_truncated,
                architecture=architecture,
                user_story=getattr(task, "user_story", "") or "",
            ))
            ui_output = self.ui_designer.run(UIDesignerInput(
                task_description=task.description,
                task_id=task_id,
                spec_content=spec_truncated,
                architecture=architecture,
                user_story=getattr(task, "user_story", "") or "",
                ux_output=ux_output,
            ))
            design_system_output = self.design_system.run(DesignSystemInput(
                task_description=task.description,
                task_id=task_id,
                spec_content=spec_truncated,
                architecture=architecture,
                user_story=getattr(task, "user_story", "") or "",
                ui_output=ui_output,
            ))

        # --- Architecture phase (always run) ---
        logger.info("[%s] Frontend team: running architecture phase", task_id)
        architect_output = self.frontend_architect.run(FrontendArchitectInput(
            task_description=task.description,
            task_id=task_id,
            spec_content=spec_truncated,
            architecture=architecture,
            user_story=getattr(task, "user_story", "") or "",
            ux_output=ux_output,
            ui_output=ui_output,
            design_system_output=design_system_output,
        ))

        artifact_context = build_feature_implementation_context(
            ux=ux_output,
            ui=ui_output,
            design_system=design_system_output,
            architect=architect_output,
        )

        # --- Implementation loop (mirrors FrontendExpertAgent.run_workflow) ---
        qa_issues: List[Dict[str, Any]] = []
        sec_issues: List[Dict[str, Any]] = []
        a11y_issues: List[Dict[str, Any]] = []
        code_review_issues: List[Dict[str, Any]] = []
        suggested_tests_from_qa: Optional[Dict[str, str]] = None
        result: Optional[FrontendOutput] = None
        current_task = task
        last_build_error_sig = ""
        consecutive_same_build_failures = 0
        write_tests_requested = False

        base_requirements = _task_requirements_with_route_expectations(current_task, repo_path)
        if artifact_context:
            enriched_requirements = base_requirements + "\n\n" + artifact_context
        else:
            enriched_requirements = base_requirements

        for iteration_round in range(MAX_CODE_REVIEW_ITERATIONS):
            existing_code = _truncate_for_context(
                _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"]),
                MAX_EXISTING_CODE_CHARS,
            )
            api_endpoints = _truncate_for_context(
                _read_repo_code(backend_dir, [".py"]),
                MAX_API_SPEC_CHARS,
            )

            plan_text = ""
            if not qa_issues and not sec_issues and not a11y_issues and not code_review_issues:
                plan_text = self.feature_agent._plan_task(
                    task=current_task,
                    existing_code=existing_code,
                    spec_content=spec_content,
                    architecture=architecture,
                    api_endpoints=api_endpoints if api_endpoints != "# No code files found" else None,
                )
                if plan_text:
                    logger.info("[%s] Orchestrator: planning complete, plan length=%d chars", task_id, len(plan_text))

            result = self.feature_agent.run(FrontendInput(
                task_description=current_task.description,
                requirements=enriched_requirements,
                user_story=getattr(current_task, "user_story", "") or "",
                spec_content=spec_truncated,
                architecture=architecture,
                existing_code=existing_code if existing_code != "# No code files found" else None,
                api_endpoints=api_endpoints if api_endpoints != "# No code files found" else None,
                qa_issues=qa_issues,
                security_issues=sec_issues,
                accessibility_issues=a11y_issues,
                code_review_issues=code_review_issues,
                suggested_tests_from_qa=suggested_tests_from_qa,
                task_plan=plan_text if plan_text else None,
            ))

            if result.needs_clarification and result.clarification_requests:
                if tech_lead and iteration_round < MAX_CLARIFICATION_REFINEMENTS:
                    current_task = tech_lead.refine_task(
                        current_task, result.clarification_requests, spec_content, architecture,
                    )
                    code_review_issues = []
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="Agent needs clarification after max refinements",
                )

            ok, write_msg = write_agent_output(repo_path, result, subdir="")
            if not ok:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                failure_reason = (
                    "Frontend agent did not propose any file changes for this task"
                    if write_msg == NO_FILES_TO_WRITE_MSG
                    else f"Write failed: {write_msg}"
                )
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=failure_reason,
                )

            if result.npm_packages_to_install:
                install_cmd = ["npm", "install", "--save"] + result.npm_packages_to_install
                install_res = run_command_with_nvm(install_cmd, cwd=repo_path)
                if not install_res.success:
                    logger.warning(
                        "[%s] npm install for packages %s failed: %s",
                        task_id, result.npm_packages_to_install, install_res.stderr[:500],
                    )

            # ─── Lint verification (before build) ──────────────────────
            if linting_tool_agent is not None:
                try:
                    from linting_tool_agent.models import LintToolInput as _LintInput
                    lint_result = linting_tool_agent.run(_LintInput(
                        repo_path=str(repo_path),
                        agent_type="frontend",
                        task_id=task_id,
                        task_description=current_task.description,
                    ))
                    if not lint_result.execution_result.success:
                        logger.info(
                            "[%s] Orchestrator lint: %d issue(s), %d edit(s)",
                            task_id,
                            lint_result.execution_result.issue_count,
                            len(lint_result.edits),
                        )
                        if lint_result.edits:
                            lint_files: Dict[str, str] = {}
                            repo_root = repo_path.resolve()
                            for e in lint_result.edits:
                                file_abs = (repo_path / e.file_path).resolve()
                                try:
                                    rel_path = str(file_abs.relative_to(repo_root))
                                except ValueError:
                                    continue
                                if not file_abs.is_file():
                                    continue
                                current_content = lint_files.get(rel_path)
                                if current_content is None:
                                    current_content = file_abs.read_text(
                                        encoding="utf-8", errors="replace"
                                    )
                                if e.old_text not in current_content:
                                    continue
                                lint_files[rel_path] = current_content.replace(
                                    e.old_text, e.new_text, 1
                                )
                            if lint_files:
                                write_agent_output(
                                    repo_path,
                                    type("_LR", (), {"files": lint_files, "summary": lint_result.summary})(),
                                    subdir="",
                                )
                        elif lint_result.linter_issues:
                            code_review_issues = [
                                {
                                    "severity": li.severity,
                                    "description": f"[{li.rule}] {li.message}",
                                    "file_path": li.file_path,
                                    "suggestion": f"Fix lint violation {li.rule} at line {li.line}",
                                }
                                for li in lint_result.linter_issues[:20]
                            ]
                            continue
                except Exception as lint_err:
                    logger.warning(
                        "[%s] Orchestrator lint step failed (non-blocking): %s",
                        task_id, lint_err,
                    )

            build_ok, build_errors = build_verifier(repo_path, "frontend", task_id)
            if not build_ok:
                if build_errors.startswith("ENV:"):
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return FrontendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        failure_reason="Unsupported environment: " + build_errors[4:].strip()[:500],
                    )
                build_error_sig = (build_errors[:800] or build_errors).strip()
                if build_error_sig == last_build_error_sig:
                    consecutive_same_build_failures += 1
                else:
                    last_build_error_sig = build_error_sig
                    consecutive_same_build_failures = 1
                if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                    return FrontendWorkflowResult(
                        task_id=task_id,
                        success=False,
                        failure_reason=(
                            f"Build failed {MAX_SAME_BUILD_FAILURES} times with the same error. "
                            f"Last error: {build_errors[:500]}"
                        ),
                    )

                # Try BuildFixSpecialist for minimal targeted fix before QA fallback
                if consecutive_same_build_failures >= 2 and build_fix_specialist is not None:
                    try:
                        from build_fix_specialist.models import BuildFixInput
                        affected_paths = _extract_affected_file_paths_from_frontend_build_errors(
                            build_errors, repo_path,
                        )
                        affected_code = _read_frontend_affected_files_code(repo_path, affected_paths)
                        bf_result = build_fix_specialist.run(BuildFixInput(
                            build_errors=build_errors[:4000],
                            affected_files_code=affected_code,
                            task_description=current_task.description,
                        ))
                        if bf_result.edits:
                            ok_apply, msg_apply, files_dict = _apply_frontend_build_fix_edits(
                                repo_path, bf_result.edits,
                            )
                            if ok_apply and files_dict:
                                ok_write, _ = write_agent_output(
                                    repo_path,
                                    type("_BF", (), {"files": files_dict, "summary": bf_result.summary})(),
                                    subdir="",
                                )
                                if ok_write:
                                    logger.info(
                                        "[%s] WORKFLOW   BuildFixSpecialist applied %d edit(s), re-running build",
                                        task_id, len(files_dict),
                                    )
                                    continue
                    except Exception as bf_err:
                        logger.warning(
                            "[%s] WORKFLOW   BuildFixSpecialist failed (non-blocking): %s",
                            task_id, bf_err,
                        )

                # Invoke testing sub-agent to analyze build errors and produce fix recommendations
                code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                from qa_agent.models import QAInput as QAI
                qa_fix_result = qa_agent.run(QAI(
                    code=code_on_branch,
                    language="typescript",
                    task_description=current_task.description,
                    architecture=architecture,
                    build_errors=build_errors[:4000],
                    request_mode="fix_build",
                ))
                qa_issues = [
                    b.model_dump() if hasattr(b, "model_dump") else b.dict()
                    for b in (qa_fix_result.bugs_found or [])
                ]
                if not qa_issues:
                    qa_issues = [{
                        "severity": "critical",
                        "description": f"Build failed: {build_errors[:2000]}",
                        "recommendation": "Fix the compilation errors",
                    }]
                if consecutive_same_build_failures >= 2:
                    qa_issues.insert(0, {
                        "severity": "critical",
                        "description": (
                            f"ESCALATION: This build error has occurred {consecutive_same_build_failures} times. "
                            "Focus ONLY on fixing this specific error. Make minimal, targeted changes."
                        ),
                        "recommendation": "Apply the minimal fix indicated by the error message.",
                    })
                code_review_issues = []
                continue

            consecutive_same_build_failures = 0
            last_build_error_sig = ""

            # After first successful build: have testing sub-agent write unit and integration tests
            if not write_tests_requested:
                write_tests_requested = True
                code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                from qa_agent.models import QAInput as QAI
                qa_tests_result = qa_agent.run(QAI(
                    code=code_on_branch,
                    language="typescript",
                    task_description=current_task.description,
                    architecture=architecture,
                    request_mode="write_tests",
                ))
                tests_dict = {}
                if qa_tests_result.unit_tests:
                    tests_dict["unit_tests"] = qa_tests_result.unit_tests
                if qa_tests_result.integration_tests:
                    tests_dict["integration_tests"] = qa_tests_result.integration_tests
                if tests_dict:
                    suggested_tests_from_qa = tests_dict
                    continue

            suggested_tests_from_qa = None
            code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
            from software_engineering_team.shared.context_sizing import compute_code_review_total_chars, compute_existing_code_chars
            max_code = compute_existing_code_chars(self.feature_agent.llm)
            max_review = compute_code_review_total_chars(self.feature_agent.llm)
            existing_code_ctx = _truncate_for_context(code_on_branch, max_code)
            from code_review_agent.models import CodeReviewInput
            code_for_review = _truncate_for_context(code_on_branch, max_review)

            task_reqs_with_checklist = _task_requirements(current_task)
            if FRONTEND_CODE_REVIEW_CHECKLIST:
                task_reqs_with_checklist += "\n\n**Frontend checklist:** " + FRONTEND_CODE_REVIEW_CHECKLIST

            review_result = code_review_agent.run(CodeReviewInput(
                code=code_for_review,
                spec_content=spec_content,
                task_description=current_task.description,
                task_requirements=task_reqs_with_checklist,
                acceptance_criteria=getattr(current_task, "acceptance_criteria", []) or [],
                language="typescript",
                architecture=architecture,
                existing_codebase=existing_code_ctx,
            ))
            if not review_result.approved:
                code_review_issues = [
                    i.model_dump() if hasattr(i, "model_dump") else i.dict()
                    for i in (review_result.issues or [])
                ]
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="Code review did not approve after max iterations",
                )

            if acceptance_verifier_agent and getattr(current_task, "acceptance_criteria", None):
                code_for_verify = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                if code_for_verify and code_for_verify != "# No code files found":
                    from acceptance_verifier_agent.models import AcceptanceVerifierInput
                    av_result = acceptance_verifier_agent.run(AcceptanceVerifierInput(
                        code=code_for_verify,
                        task_description=current_task.description,
                        acceptance_criteria=current_task.acceptance_criteria,
                        spec_content=spec_content,
                        architecture=architecture,
                        language="typescript",
                    ))
                    if not av_result.all_satisfied:
                        unsatisfied = [c for c in av_result.per_criterion if not c.satisfied]
                        code_review_issues = [
                            {
                                "severity": "major",
                                "category": "acceptance_criteria",
                                "file_path": "",
                                "description": f"Criterion not satisfied: {c.criterion}. Evidence: {c.evidence}",
                                "suggestion": f"Implement or fix code to satisfy: {c.criterion}",
                            }
                            for c in unsatisfied
                        ]
                        if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                            continue
                        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                        return FrontendWorkflowResult(
                            task_id=task_id,
                            success=False,
                            failure_reason="Acceptance criteria not satisfied after max iterations",
                        )

            code_review_issues = []
            code_to_review = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])

            # UX Engineer polish pass
            ux_eng_result = self.ux_engineer.run(UXEngineerInput(
                code=code_to_review[:30000],
                task_description=current_task.description,
                task_id=task_id,
                architecture=architecture,
            ))
            if not ux_eng_result.approved and ux_eng_result.issues:
                code_review_issues = ux_eng_result.issues
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue

            from qa_agent.models import QAInput
            qa_result = qa_agent.run(QAInput(
                code=code_to_review,
                language="typescript",
                task_description=current_task.description,
                architecture=architecture,
            ))
            from accessibility_agent.models import AccessibilityInput
            a11y_task_desc = current_task.description
            if FRONTEND_A11Y_CHECKLIST:
                a11y_task_desc += "\n\n" + FRONTEND_A11Y_CHECKLIST
            a11y_result = accessibility_agent.run(AccessibilityInput(
                code=code_to_review,
                language="typescript",
                task_description=a11y_task_desc,
                architecture=architecture,
            ))
            from security_agent.models import SecurityInput
            sec_result = security_agent.run(SecurityInput(
                code=code_to_review,
                language="typescript",
                task_description=current_task.description,
                architecture=architecture,
                context=FRONTEND_SECURITY_CHECKLIST,
            ))

            qa_issues = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_result.bugs_found or [])]
            a11y_issues = [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in (a11y_result.issues or [])]
            sec_issues = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_result.vulnerabilities or [])]

            perf_result = self.performance_engineer.run(PerformanceEngineerInput(
                code=code_to_review[:25000],
                task_description=current_task.description,
                task_id=task_id,
                build_output="",
                architecture=architecture,
            ))
            if not perf_result.approved and perf_result.issues:
                code_review_issues = perf_result.issues
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue

            all_approved = qa_result.approved and a11y_result.approved and sec_result.approved and perf_result.approved
            if not all_approved:
                if not qa_result.approved and qa_result.bugs_found:
                    qa_issues = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in qa_result.bugs_found]
                if not a11y_result.approved and a11y_result.issues:
                    a11y_issues = [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in a11y_result.issues]
                if not sec_result.approved and sec_result.vulnerabilities:
                    sec_issues = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in sec_result.vulnerabilities]
                if not perf_result.approved and perf_result.issues:
                    code_review_issues = list(perf_result.issues)
                if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                    continue
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason="QA, accessibility, security, or performance did not approve after max iterations",
                )

            if all_tasks and append_backend_task_fn and tech_lead:
                fix_tasks = tech_lead.evaluate_qa_and_create_fix_tasks(
                    current_task, qa_result, spec_content, architecture,
                )
                if fix_tasks:
                    for ft in fix_tasks:
                        if getattr(ft, "assignee", None) == "backend":
                            all_tasks[ft.id] = ft
                            append_backend_task_fn(ft)

            if dbc_agent:
                self._run_dbc_review(
                    dbc_agent=dbc_agent,
                    repo_path=repo_path,
                    task_id=task_id,
                    task_description=current_task.description,
                    architecture=architecture,
                )

            merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
            if merge_ok:
                delete_branch(repo_path, branch_name)
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)

                if doc_agent and completed_tasks is not None and remaining_tasks is not None and tech_lead:
                    task_update = TaskUpdate(
                        task_id=task_id,
                        agent_type="frontend",
                        status="completed",
                        summary=result.summary if result else "",
                        files_changed=list((result.files or {}).keys()) if result else [],
                        needs_followup=False,
                    )
                    codebase_summary = _truncate_for_context(
                        _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"]),
                        compute_existing_code_chars(self.feature_agent.llm),
                    )
                    new_tasks = tech_lead.review_progress(
                        task_update=task_update,
                        spec_content=spec_content,
                        architecture=architecture,
                        completed_tasks=completed_tasks,
                        remaining_tasks=remaining_tasks,
                        codebase_summary=codebase_summary,
                    )
                    if new_tasks and append_frontend_task_fn:
                        for nt in new_tasks:
                            if all_tasks and nt.id not in all_tasks:
                                all_tasks[nt.id] = nt
                            append_frontend_task_fn(nt.id)
                    if doc_agent:
                        tech_lead.trigger_documentation_update(
                            doc_agent=doc_agent,
                            repo_path=repo_path,
                            task_update=task_update,
                            spec_content=spec_content,
                            architecture=architecture,
                            codebase_summary=codebase_summary,
                        )

                code_for_br = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
                build_release_result = self.build_release.run(BuildReleaseInput(
                    task_description=task.description,
                    task_id=task_id,
                    spec_content=spec_truncated,
                    architecture=architecture,
                    existing_pipeline=_read_repo_code(repo_path, [".yml", ".yaml"]),
                    repo_code_summary=f"Frontend application, {len(code_for_br)} chars of code",
                ))
                try:
                    plan_dir = repo_path / "plan"
                    if not plan_dir.exists():
                        plan_dir = repo_path.parent / "plan"
                    if plan_dir.exists():
                        br_doc = (
                            f"# Build and Release Plan (Frontend)\n\n"
                            f"## CI Plan\n{build_release_result.ci_plan}\n\n"
                            f"## Preview Environment\n{build_release_result.preview_env_plan}\n\n"
                            f"## Release and Rollback\n{build_release_result.release_rollback_plan}\n\n"
                            f"## Source Maps and Error Reporting\n{build_release_result.source_maps_error_reporting}\n"
                        )
                        (plan_dir / "frontend_build_release.md").write_text(br_doc, encoding="utf-8")
                except Exception as e:
                    logger.debug("Could not persist build/release plan: %s", e)

                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=True,
                    summary=result.summary if result else "",
                )
            else:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return FrontendWorkflowResult(
                    task_id=task_id,
                    success=False,
                    failure_reason=f"Merge failed: {merge_msg}",
                )

        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return FrontendWorkflowResult(
            task_id=task_id,
            success=False,
            failure_reason="Review loop exhausted without merge",
        )

    @staticmethod
    def _run_dbc_review(
        *,
        dbc_agent: Any,
        repo_path: Path,
        task_id: str,
        task_description: str,
        architecture: Optional[SystemArchitecture],
    ) -> None:
        """Run DBC comments agent on frontend code."""
        from technical_writers.dbc_comments_agent.models import DbcCommentsInput
        from software_engineering_team.shared.git_utils import write_files_and_commit

        try:
            dbc_code = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])
            if not dbc_code or dbc_code == "# No code files found":
                return
            dbc_result = dbc_agent.run(DbcCommentsInput(
                code=dbc_code,
                language="typescript",
                task_description=task_description,
                architecture=architecture,
            ))
            if not dbc_result.already_compliant and dbc_result.files:
                write_files_and_commit(
                    repo_path,
                    dbc_result.files,
                    dbc_result.suggested_commit_message,
                )
        except Exception as e:
            logger.warning("[%s] DBC review failed (non-blocking): %s", task_id, e)
