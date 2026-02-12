"""
Tech Lead orchestrator: runs the full pipeline with feature branches.

Flow:
1. Ensure development branch
2. Read initial_spec.md, parse to requirements
3. Request architecture from Architect when needed
4. Tech Lead generates plan (multi-step: codebase analysis, spec analysis, task generation)
5. For each task: create feature branch, implement, code review, build/test, merge
6. Only one coding agent works at a time (sequential execution)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Path setup when run as module
import sys
_team_dir = Path(__file__).resolve().parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from shared.git_utils import (
    DEVELOPMENT_BRANCH,
    checkout_branch,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    merge_branch,
)
from shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    update_job,
)
from shared.models import TaskUpdate
from shared.repo_writer import write_agent_output

logger = logging.getLogger(__name__)


def _get_agents(llm):
    """Lazy init agents including the code review agent."""
    from architecture_agent import ArchitectureExpertAgent, ArchitectureInput
    from backend_agent import BackendExpertAgent, BackendInput
    from code_review_agent import CodeReviewAgent, CodeReviewInput
    from devops_agent import DevOpsExpertAgent, DevOpsInput
    from frontend_agent import FrontendExpertAgent, FrontendInput
    from qa_agent import QAExpertAgent, QAInput
    from security_agent import CybersecurityExpertAgent, SecurityInput
    from tech_lead_agent import TechLeadAgent, TechLeadInput

    return {
        "architecture": ArchitectureExpertAgent(llm),
        "tech_lead": TechLeadAgent(llm),
        "devops": DevOpsExpertAgent(llm),
        "backend": BackendExpertAgent(llm),
        "frontend": FrontendExpertAgent(llm),
        "security": CybersecurityExpertAgent(llm),
        "qa": QAExpertAgent(llm),
        "code_review": CodeReviewAgent(llm),
    }


def _task_requirements(task) -> str:
    """Build full requirements string including description, user story, requirements, and acceptance criteria."""
    parts = []
    if task.description:
        parts.append(f"Task Description:\n{task.description}")
    if getattr(task, "user_story", None):
        parts.append(f"User Story: {task.user_story}")
    if task.requirements:
        parts.append(f"Technical Requirements:\n{task.requirements}")
    if getattr(task, "acceptance_criteria", None):
        parts.append("Acceptance Criteria:\n- " + "\n- ".join(task.acceptance_criteria))
    return "\n\n".join(parts) if parts else task.description


MAX_REVIEW_ITERATIONS = 10
MAX_CLARIFICATION_REFINEMENTS = 10  # Max times to refine a task based on specialist clarification
MAX_CODE_REVIEW_ITERATIONS = 10    # Max rounds of code review -> fix -> re-review


def _issues_to_dicts(qa_bugs, sec_vulns) -> tuple:
    """Convert QA/Security outputs to dict lists for coding agent input."""
    qa_list = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_bugs or [])]
    sec_list = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_vulns or [])]
    return qa_list, sec_list


def _read_repo_code(repo_path: Path, extensions: List[str] = None) -> str:
    """Read code files from repo, concatenated."""
    if extensions is None:
        extensions = [".py", ".ts", ".tsx", ".java", ".yml", ".yaml"]
    parts = []
    for f in repo_path.rglob("*"):
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(f"### {f.relative_to(repo_path)} ###\n{f.read_text(encoding='utf-8', errors='replace')}")
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


# Max chars to pass to agents for context (avoid token limits)
MAX_EXISTING_CODE_CHARS = 40000
MAX_API_SPEC_CHARS = 20000


def _truncate_for_context(text: str, max_chars: int) -> str:
    """Truncate text for agent context, with truncation notice."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n\n... [truncated, {len(text) - max_chars} more chars]"


def _build_task_update(task_id: str, agent_type: str, result, status: str = "completed") -> TaskUpdate:
    """Construct a TaskUpdate from a specialist agent's output."""
    summary = getattr(result, "summary", "") or ""
    files_changed = list((getattr(result, "files", None) or {}).keys())
    if not files_changed:
        files_changed = list((getattr(result, "artifacts", None) or {}).keys())
    needs_followup = bool(getattr(result, "needs_clarification", False))
    return TaskUpdate(
        task_id=task_id,
        agent_type=agent_type,
        status=status,
        summary=summary,
        files_changed=files_changed,
        needs_followup=needs_followup,
    )


def _run_tech_lead_review(
    tech_lead,
    task_update: TaskUpdate,
    spec_content: str,
    architecture,
    all_tasks: dict,
    completed: set,
    execution_queue: list,
    repo_path: Path,
) -> None:
    """
    Ask the Tech Lead to review progress after a task completes.
    If the Tech Lead identifies gaps, new tasks are added to the execution queue.
    """
    completed_tasks = [t for tid, t in all_tasks.items() if tid in completed]
    remaining_ids = set(execution_queue)
    remaining_tasks = [t for tid, t in all_tasks.items() if tid in remaining_ids]
    codebase_summary = _truncate_for_context(_read_repo_code(repo_path), MAX_EXISTING_CODE_CHARS)

    new_tasks = tech_lead.review_progress(
        task_update=task_update,
        spec_content=spec_content,
        architecture=architecture,
        completed_tasks=completed_tasks,
        remaining_tasks=remaining_tasks,
        codebase_summary=codebase_summary,
    )

    if new_tasks:
        for nt in new_tasks:
            if nt.id not in all_tasks:
                all_tasks[nt.id] = nt
                execution_queue.append(nt.id)
        logger.info(
            "Tech Lead review: added %s new tasks from progress review: %s",
            len(new_tasks),
            [t.id for t in new_tasks],
        )


def _run_code_review(
    agents: dict,
    code_to_review: str,
    spec_content: str,
    task,
    language: str,
    architecture,
    existing_codebase: str | None = None,
):
    """
    Run the code review agent on the given code.
    Returns the CodeReviewOutput.
    """
    from code_review_agent.models import CodeReviewInput
    review_input = CodeReviewInput(
        code=code_to_review,
        spec_content=spec_content,
        task_description=task.description,
        task_requirements=_task_requirements(task),
        acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
        language=language,
        architecture=architecture,
        existing_codebase=existing_codebase,
    )
    return agents["code_review"].run(review_input)


def _code_review_issues_to_dicts(issues) -> list:
    """Convert CodeReviewIssue objects to dicts for coding agent input."""
    return [
        i.model_dump() if hasattr(i, "model_dump") else i.dict()
        for i in (issues or [])
    ]


def _log_code_review_result(review_result, task_id: str) -> None:
    """Log code review result with full issue details for debugging."""
    if review_result.approved:
        logger.info("[%s] Code review APPROVED", task_id)
        if review_result.summary:
            logger.info("[%s]   Summary: %s", task_id, review_result.summary[:300])
        return
    logger.warning(
        "[%s] Code review REJECTED: %s issues (%s critical/major)",
        task_id,
        len(review_result.issues),
        len([i for i in review_result.issues if i.severity in ("critical", "major")]),
    )
    for i, issue in enumerate(review_result.issues, 1):
        logger.warning(
            "[%s]   Issue %s: [%s] %s: %s (file: %s)",
            task_id, i, issue.severity, issue.category,
            issue.description, issue.file_path or "n/a",
        )
        if issue.suggestion:
            logger.warning(
                "[%s]     Suggestion: %s", task_id, issue.suggestion[:300],
            )
    if review_result.summary:
        logger.info("[%s]   Review summary: %s", task_id, review_result.summary[:300])
    if review_result.spec_compliance_notes:
        logger.info("[%s]   Spec compliance: %s", task_id, review_result.spec_compliance_notes[:300])
    if not review_result.issues:
        logger.warning(
            "[%s]   WARNING: Review rejected but returned 0 issues -- coding agent has nothing to fix!",
            task_id,
        )


def _run_build_verification(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> tuple[bool, str]:
    """
    Run build verification for the given agent type.
    Returns (success, error_output).
    For frontend: runs ng build.
    For backend: runs python syntax check (pytest if tests exist).
    """
    from shared.command_runner import run_ng_build, run_python_syntax_check, run_pytest

    if agent_type == "frontend":
        # Look for Angular project (package.json with @angular/core)
        frontend_dir = repo_path / "frontend"
        if not (frontend_dir / "package.json").exists():
            # Try repo root
            if (repo_path / "package.json").exists():
                frontend_dir = repo_path
            else:
                logger.info("Build verification: no Angular project found, skipping ng build")
                return True, ""
        result = run_ng_build(frontend_dir)
        if not result.success:
            logger.warning("Build verification failed for task %s: %s", task_id, result.error_summary[:200])
            return False, result.error_summary
        logger.info("Build verification passed for frontend task %s", task_id)
        return True, ""

    elif agent_type == "backend":
        # Look for Python project
        backend_dir = repo_path / "backend"
        if not any(backend_dir.rglob("*.py")) if backend_dir.exists() else True:
            if any(repo_path.rglob("*.py")):
                backend_dir = repo_path
            else:
                logger.info("Build verification: no Python files found, skipping")
                return True, ""
        result = run_python_syntax_check(backend_dir)
        if not result.success:
            logger.warning("Syntax check failed for task %s: %s", task_id, result.error_summary[:200])
            return False, result.error_summary
        # Also try pytest if tests directory exists
        tests_dir = backend_dir / "tests"
        if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
            test_result = run_pytest(backend_dir)
            if not test_result.success:
                logger.warning("Tests failed for task %s: %s", task_id, test_result.error_summary[:200])
                return False, test_result.error_summary
        logger.info("Build verification passed for backend task %s", task_id)
        return True, ""

    return True, ""


def run_orchestrator(job_id: str, repo_path: str | Path) -> None:
    """
    Main orchestration loop. Runs in background thread.

    IMPORTANT: Only one coding agent works at a time. All tasks are processed
    sequentially to avoid conflicts on the shared repository.
    """
    path = Path(repo_path).resolve()
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)

        from shared.llm import get_llm_client
        llm = get_llm_client()
        agents = _get_agents(llm)

        # 1. Ensure development branch
        ensure_development_branch(path)
        update_job(job_id, current_task="git_setup")

        # 2. Read spec
        from spec_parser import load_spec_from_repo, parse_spec_heuristic, parse_spec_with_llm
        spec_content = load_spec_from_repo(path)
        try:
            requirements = parse_spec_with_llm(spec_content, llm)
        except Exception:
            requirements = parse_spec_heuristic(spec_content)
        update_job(job_id, requirements_title=requirements.title)

        # 3. Architecture (Tech Lead needs it)
        from architecture_agent.models import ArchitectureInput
        arch_agent = agents["architecture"]
        arch_input = ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
        )
        arch_output = arch_agent.run(arch_input)
        architecture = arch_output.architecture
        update_job(job_id, architecture_overview=architecture.overview)

        # 4. Tech Lead generates plan (multi-step: codebase analysis, spec analysis, task generation)
        from tech_lead_agent.models import TechLeadInput
        tech_lead = agents["tech_lead"]
        existing_code = _truncate_for_context(_read_repo_code(path), MAX_EXISTING_CODE_CHARS)
        tech_lead_output = tech_lead.run(TechLeadInput(
            requirements=requirements,
            architecture=architecture,
            repo_path=str(path),
            spec_content=spec_content,
            existing_codebase=existing_code if existing_code != "# No code files found" else None,
        ))
        if tech_lead_output.spec_clarification_needed:
            questions = tech_lead_output.clarification_questions or []
            error_msg = f"Spec is unclear. Tech Lead requests clarification: {'; '.join(questions[:5])}"
            if len(questions) > 5:
                error_msg += f" (+{len(questions) - 5} more)"
            logger.warning(error_msg)
            update_job(job_id, status=JOB_STATUS_FAILED, error=error_msg)
            return

        assignment = tech_lead_output.assignment

        # Store execution order in job state for API polling
        update_job(job_id, execution_order=assignment.execution_order)

        # 5. Execute tasks (use queue to support dynamic fix tasks from Tech Lead)
        completed = set()
        failed: Dict[str, str] = {}  # task_id -> failure reason
        completed_code_task_ids = []  # backend/frontend tasks for security eligibility
        execution_queue = list(assignment.execution_order)
        all_tasks = {t.id: t for t in assignment.tasks}
        total_tasks = len(execution_queue)
        task_counter = 0
        # Safety limit: tasks are never re-queued for dependencies, but QA fix
        # tasks and Tech Lead review tasks can be dynamically added.
        max_passes = total_tasks * 3

        logger.info(
            "=== Starting task execution: %s tasks in queue ===", total_tasks,
        )

        while execution_queue and max_passes > 0:
            max_passes -= 1
            task_id = execution_queue.pop(0)
            task = all_tasks.get(task_id)
            if not task:
                logger.warning("Task %s not found in task registry - skipping", task_id)
                continue

            task_counter += 1
            task_start_time = time.monotonic()
            logger.info(
                "=== [%s/%s] Starting task %s (type=%s, assignee=%s) ===",
                task_counter, total_tasks, task_id, task.type.value, task.assignee,
            )
            update_job(job_id, current_task=task_id)
            branch_name = f"feature/{task_id}"

            try:
                if task.type.value == "git_setup":
                    completed.add(task_id)
                    logger.info("[%s] Git setup task auto-completed", task_id)
                    continue

                # Create feature branch
                ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, task_id)
                if not ok:
                    logger.error("[%s] Feature branch creation FAILED: %s", task_id, msg)
                    failed[task_id] = f"Feature branch creation failed: {msg}"
                    continue

                if task.assignee == "devops":
                    logger.info("[%s] Phase 1: DevOps agent implementing task", task_id)
                    from devops_agent.models import DevOpsInput
                    existing_pipeline = _read_repo_code(path, [".yml", ".yaml"])
                    current_task = task
                    result = None
                    task_completed = False
                    failure_reason = ""
                    for _ in range(MAX_CLARIFICATION_REFINEMENTS + 1):
                        result = agents["devops"].run(DevOpsInput(
                            task_description=current_task.description,
                            requirements=_task_requirements(current_task),
                            architecture=architecture,
                            existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                            tech_stack=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
                        ))
                        if not result.needs_clarification or not result.clarification_requests:
                            break
                        logger.info("DevOps requested clarification for %s: refining task", task_id)
                        current_task = tech_lead.refine_task(
                            current_task, result.clarification_requests, spec_content, architecture,
                        )
                    if result and result.needs_clarification:
                        failure_reason = "Agent still needs clarification after max refinements"
                        logger.warning("Task %s still needs clarification after refinements", task_id)
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    else:
                        subdir = "devops"
                        ok, msg = write_agent_output(path, result, subdir=subdir)
                        if ok:
                            merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                            if merge_ok:
                                delete_branch(path, branch_name)
                                task_completed = True
                            else:
                                failure_reason = f"Merge failed: {merge_msg}"
                                logger.warning("[%s] Merge failed: %s", task_id, merge_msg)
                        else:
                            failure_reason = f"Write failed: {msg}"
                            logger.warning("[%s] DevOps write failed: %s", task_id, msg)
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    elapsed = time.monotonic() - task_start_time
                    if task_completed:
                        completed.add(task_id)
                        logger.info(
                            "[%s] Task COMPLETED in %.1fs (completed: %s/%s)",
                            task_id, elapsed, len(completed), total_tasks,
                        )
                        # Tech Lead reviews progress after devops task completion
                        task_update = _build_task_update(task_id, "devops", result)
                        _run_tech_lead_review(
                            tech_lead, task_update, spec_content, architecture,
                            all_tasks, completed, execution_queue, path,
                        )
                    else:
                        failed[task_id] = failure_reason
                        logger.warning("[%s] Task FAILED after %.1fs: %s", task_id, elapsed, failure_reason)

                elif task.assignee == "backend":
                    # ── SEQUENTIAL CODING: Backend agent has exclusive repo access ──
                    logger.info("[%s] >>> Backend agent acquiring coding slot (other agents wait)", task_id)
                    from backend_agent.models import BackendInput
                    from qa_agent.models import QAInput
                    qa_issues, sec_issues = [], []
                    code_review_issues = []
                    result = None
                    merged = False
                    task_completed = False
                    failure_reason = ""
                    current_task = task

                    # Phase 1: Clarification loop
                    for clarification_round in range(MAX_CLARIFICATION_REFINEMENTS + 1):
                        logger.info(
                            "[%s] Phase 1: Backend agent coding (round %s/%s, review_issues=%s)",
                            task_id, clarification_round + 1, MAX_CLARIFICATION_REFINEMENTS + 1,
                            len(code_review_issues),
                        )
                        existing_code = _truncate_for_context(
                            _read_repo_code(path),
                            MAX_EXISTING_CODE_CHARS,
                        )
                        result = agents["backend"].run(BackendInput(
                            task_description=current_task.description,
                            requirements=_task_requirements(current_task),
                            user_story=getattr(current_task, "user_story", "") or "",
                            spec_content=_truncate_for_context(spec_content, MAX_EXISTING_CODE_CHARS),
                            architecture=architecture,
                            language="python",
                            existing_code=existing_code if existing_code and existing_code != "# No code files found" else None,
                            qa_issues=qa_issues,
                            security_issues=sec_issues,
                            code_review_issues=code_review_issues,
                        ))
                        if result.needs_clarification and result.clarification_requests:
                            if clarification_round < MAX_CLARIFICATION_REFINEMENTS:
                                logger.info("Backend requested clarification for %s: refining task", task_id)
                                current_task = tech_lead.refine_task(
                                    current_task, result.clarification_requests, spec_content, architecture,
                                )
                                continue
                            else:
                                failure_reason = "Agent still needs clarification after max refinements"
                                logger.warning("Task %s still needs clarification - skipping", task_id)
                                checkout_branch(path, DEVELOPMENT_BRANCH)
                                break
                        # Have code - write it
                        ok, msg = write_agent_output(path, result, subdir="backend")
                        if not ok:
                            failure_reason = f"Write failed: {msg}"
                            logger.warning("[%s] Backend write failed: %s", task_id, msg)
                            checkout_branch(path, DEVELOPMENT_BRANCH)
                            break

                        # Phase 2: Build verification
                        logger.info("[%s] Phase 2: Build verification", task_id)
                        build_ok, build_errors = _run_build_verification(path, "backend", task_id)
                        if not build_ok:
                            logger.warning("[%s] Build FAILED - sending errors back to agent", task_id)
                            code_review_issues = [{"severity": "critical", "category": "logic",
                                                   "file_path": "", "description": f"Build/test failed: {build_errors[:2000]}",
                                                   "suggestion": "Fix the compilation/test errors"}]
                            continue  # retry with build errors as code review issues

                        # Phase 3: Code review
                        logger.info(
                            "[%s] Phase 3: Code review (round %s/%s)",
                            task_id, clarification_round + 1, MAX_CLARIFICATION_REFINEMENTS + 1,
                        )
                        code_on_branch = _read_repo_code(path)
                        review_result = _run_code_review(
                            agents, code_on_branch, spec_content, current_task,
                            "python", architecture, existing_code,
                        )
                        _log_code_review_result(review_result, task_id)

                        if not review_result.approved:
                            # Code review found issues - send back to backend agent for fixes
                            code_review_issues = _code_review_issues_to_dicts(review_result.issues)
                            if clarification_round < MAX_CLARIFICATION_REFINEMENTS:
                                logger.info(
                                    "[%s] Sending %s review issues back to backend agent for fixes",
                                    task_id, len(review_result.issues),
                                )
                                continue  # retry with code review issues
                            else:
                                logger.warning(
                                    "[%s] Code review still failing after max iterations - proceeding to QA anyway",
                                    task_id,
                                )

                        # Phase 4: QA review
                        logger.info("[%s] Phase 4: QA review", task_id)
                        code_to_review = _read_repo_code(path)
                        qa_result = agents["qa"].run(QAInput(
                            code=code_to_review, language="python",
                            task_description=current_task.description, architecture=architecture,
                        ))
                        fix_tasks = tech_lead.evaluate_qa_and_create_fix_tasks(
                            current_task, qa_result, spec_content, architecture,
                        )
                        if fix_tasks:
                            for ft in fix_tasks:
                                all_tasks[ft.id] = ft
                                execution_queue.insert(0, ft.id)
                            logger.info("Tech Lead created %s fix tasks from QA feedback", len(fix_tasks))

                        # Phase 5: Merge
                        logger.info("[%s] Phase 5: Merge to development", task_id)
                        merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(path, branch_name)
                            merged = True
                            task_completed = True
                            completed_code_task_ids.append(task_id)
                            logger.info("[%s] Feature branch %s merged to development", task_id, branch_name)
                        else:
                            failure_reason = f"Merge failed: {merge_msg}"
                            logger.warning("[%s] Merge FAILED: %s", task_id, merge_msg)
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                        break  # task done (merged or merge-failed)

                    elapsed = time.monotonic() - task_start_time
                    if not merged:
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    if task_completed:
                        completed.add(task_id)
                        logger.info(
                            "[%s] Task COMPLETED in %.1fs (completed: %s/%s)",
                            task_id, elapsed, len(completed), total_tasks,
                        )
                        task_update = _build_task_update(task_id, "backend", result)
                        _run_tech_lead_review(
                            tech_lead, task_update, spec_content, architecture,
                            all_tasks, completed, execution_queue, path,
                        )
                    else:
                        failed[task_id] = failure_reason or "Backend agent produced no output"
                        logger.warning("[%s] Task FAILED after %.1fs: %s", task_id, elapsed, failed[task_id])
                    logger.info("[%s] <<< Backend agent releasing coding slot", task_id)

                elif task.assignee == "frontend":
                    # ── SEQUENTIAL CODING: Frontend agent has exclusive repo access ──
                    logger.info("[%s] >>> Frontend agent acquiring coding slot (other agents wait)", task_id)
                    from frontend_agent.models import FrontendInput
                    from qa_agent.models import QAInput
                    qa_issues, sec_issues = [], []
                    code_review_issues = []
                    result = None
                    merged = False
                    task_completed = False
                    failure_reason = ""
                    current_task = task

                    # Phase 1: Clarification + code review loop
                    for iteration_round in range(MAX_CODE_REVIEW_ITERATIONS):
                        logger.info(
                            "[%s] Phase 1: Frontend agent coding (round %s/%s, review_issues=%s)",
                            task_id, iteration_round + 1, MAX_CODE_REVIEW_ITERATIONS,
                            len(code_review_issues),
                        )
                        existing_code = _truncate_for_context(
                            _read_repo_code(path, [".ts", ".tsx", ".html", ".scss"]),
                            MAX_EXISTING_CODE_CHARS,
                        )
                        api_endpoints = _truncate_for_context(
                            _read_repo_code(path, [".py"]),
                            MAX_API_SPEC_CHARS,
                        )
                        result = agents["frontend"].run(FrontendInput(
                            task_description=current_task.description,
                            requirements=_task_requirements(current_task),
                            user_story=getattr(current_task, "user_story", "") or "",
                            spec_content=_truncate_for_context(spec_content, MAX_EXISTING_CODE_CHARS),
                            architecture=architecture,
                            existing_code=existing_code if existing_code and existing_code != "# No code files found" else None,
                            api_endpoints=api_endpoints if api_endpoints and api_endpoints != "# No code files found" else None,
                            qa_issues=qa_issues,
                            security_issues=sec_issues,
                            code_review_issues=code_review_issues,
                        ))

                        # Handle clarification requests
                        if result.needs_clarification and result.clarification_requests:
                            if iteration_round < MAX_CLARIFICATION_REFINEMENTS:
                                logger.info("Frontend requested clarification for %s: refining task", task_id)
                                current_task = tech_lead.refine_task(
                                    current_task, result.clarification_requests, spec_content, architecture,
                                )
                                code_review_issues = []  # reset review issues for fresh attempt
                                continue
                            else:
                                failure_reason = "Agent still needs clarification after max refinements"
                                logger.warning("Task %s still needs clarification - skipping", task_id)
                                checkout_branch(path, DEVELOPMENT_BRANCH)
                                break

                        # Have code - write it
                        ok, msg = write_agent_output(path, result, subdir="frontend")
                        if not ok:
                            failure_reason = f"Write failed: {msg}"
                            logger.warning("[%s] Frontend write failed: %s", task_id, msg)
                            checkout_branch(path, DEVELOPMENT_BRANCH)
                            break

                        # Phase 2: Build verification (ng build)
                        logger.info("[%s] Phase 2: Build verification (ng build)", task_id)
                        build_ok, build_errors = _run_build_verification(path, "frontend", task_id)
                        if not build_ok:
                            logger.warning("[%s] Build FAILED - sending errors back to agent", task_id)
                            code_review_issues = [{"severity": "critical", "category": "logic",
                                                   "file_path": "", "description": f"ng build failed: {build_errors[:2000]}",
                                                   "suggestion": "Fix the Angular compilation errors"}]
                            continue  # retry with build errors

                        # Phase 3: Code review
                        logger.info(
                            "[%s] Phase 3: Code review (round %s/%s)",
                            task_id, iteration_round + 1, MAX_CODE_REVIEW_ITERATIONS,
                        )
                        code_on_branch = _read_repo_code(path, [".ts", ".tsx", ".html", ".scss"])
                        review_result = _run_code_review(
                            agents, code_on_branch, spec_content, current_task,
                            "typescript", architecture, existing_code,
                        )
                        _log_code_review_result(review_result, task_id)

                        if not review_result.approved:
                            code_review_issues = _code_review_issues_to_dicts(review_result.issues)
                            if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                                logger.info(
                                    "[%s] Sending %s review issues back to frontend agent for fixes",
                                    task_id, len(review_result.issues),
                                )
                                continue  # retry with code review issues
                            else:
                                logger.warning(
                                    "[%s] Code review still failing after max iterations - proceeding to QA anyway",
                                    task_id,
                                )

                        # Phase 4: QA review
                        logger.info("[%s] Phase 4: QA review", task_id)
                        code_to_review = _read_repo_code(path, [".ts", ".tsx", ".html", ".scss"])
                        qa_result = agents["qa"].run(QAInput(
                            code=code_to_review, language="typescript",
                            task_description=current_task.description, architecture=architecture,
                        ))
                        fix_tasks = tech_lead.evaluate_qa_and_create_fix_tasks(
                            current_task, qa_result, spec_content, architecture,
                        )
                        if fix_tasks:
                            for ft in fix_tasks:
                                all_tasks[ft.id] = ft
                                execution_queue.insert(0, ft.id)
                            logger.info("Tech Lead created %s fix tasks from QA feedback", len(fix_tasks))

                        # Phase 5: Merge to development and cleanup
                        logger.info("[%s] Phase 5: Merge to development", task_id)
                        merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(path, branch_name)
                            merged = True
                            task_completed = True
                            completed_code_task_ids.append(task_id)
                            logger.info("[%s] Feature branch %s merged to development", task_id, branch_name)
                        else:
                            failure_reason = f"Merge failed: {merge_msg}"
                            logger.warning("[%s] Merge FAILED: %s", task_id, merge_msg)
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                        break  # task done (merged or merge-failed)

                    elapsed = time.monotonic() - task_start_time
                    if not merged:
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    if task_completed:
                        completed.add(task_id)
                        logger.info(
                            "[%s] Task COMPLETED in %.1fs (completed: %s/%s)",
                            task_id, elapsed, len(completed), total_tasks,
                        )
                        task_update = _build_task_update(task_id, "frontend", result)
                        _run_tech_lead_review(
                            tech_lead, task_update, spec_content, architecture,
                            all_tasks, completed, execution_queue, path,
                        )
                    else:
                        failed[task_id] = failure_reason or "Frontend agent produced no output"
                        logger.warning("[%s] Task FAILED after %.1fs: %s", task_id, elapsed, failed[task_id])
                    logger.info("[%s] <<< Frontend agent releasing coding slot", task_id)

                else:
                    logger.warning("[%s] Unknown assignee '%s' - auto-completing", task_id, task.assignee)
                    completed.add(task_id)

            except Exception as e:
                elapsed = time.monotonic() - task_start_time
                logger.exception("[%s] Task FAILED with exception after %.1fs", task_id, elapsed)
                failed[task_id] = f"Unhandled exception: {e}"
                checkout_branch(path, DEVELOPMENT_BRANCH)

        # Log final execution summary
        logger.info(
            "=== Task execution finished: %s completed, %s failed, %s remaining in queue (of %s total) ===",
            len(completed), len(failed), len(execution_queue), total_tasks,
        )
        if failed:
            logger.warning("=== Failed task report ===")
            for tid, reason in sorted(failed.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)
        if execution_queue:
            logger.warning(
                "Unprocessed tasks still in queue: %s",
                execution_queue,
            )

        # Persist failed task details and retryable state in job store
        failed_details = [
            {"task_id": tid, "reason": reason, "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid)}
            for tid, reason in failed.items()
        ]
        # Store serializable task data for retry capability
        all_tasks_serialized = {
            tid: t.model_dump() if hasattr(t, "model_dump") else t.dict()
            for tid, t in all_tasks.items()
        }
        update_job(
            job_id,
            failed_tasks=failed_details,
            _all_tasks=all_tasks_serialized,
            _spec_content=spec_content,
            _architecture_overview=architecture.overview if architecture else None,
        )

        # Security: Tech Lead invokes only when code covers 90%+ of spec
        if completed_code_task_ids and tech_lead.should_run_security(
            completed_code_task_ids, spec_content, tech_lead_output.requirement_task_mapping
        ):
            from security_agent.models import SecurityInput
            logger.info("Tech Lead requested security review - running Security agent")
            code_to_review = _read_repo_code(path)
            sec_result = agents["security"].run(SecurityInput(
                code=code_to_review,
                language="python",
                task_description="Full codebase security review",
                architecture=architecture,
            ))
            if sec_result.vulnerabilities:
                logger.warning("Security found %s vulnerabilities: %s", len(sec_result.vulnerabilities), [v.description[:50] for v in sec_result.vulnerabilities[:3]])

        update_job(job_id, status=JOB_STATUS_COMPLETED, progress=100, current_task=None)

    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def run_failed_tasks(job_id: str) -> None:
    """
    Re-run only the failed tasks from a previous job.

    Reads the persisted failed task list and task objects from the job store,
    re-queues them, and executes them through the same pipeline.
    Runs in a background thread (same pattern as run_orchestrator).
    """
    from shared.job_store import get_job
    from shared.models import Task

    job_data = get_job(job_id)
    if not job_data:
        raise ValueError(f"Job {job_id} not found")
    repo_path = job_data.get("repo_path")
    if not repo_path:
        raise ValueError(f"Job {job_id} has no repo_path")
    failed_tasks = job_data.get("failed_tasks") or []
    if not failed_tasks:
        raise ValueError(f"Job {job_id} has no failed tasks to retry")
    all_tasks_data = job_data.get("_all_tasks") or {}
    if not all_tasks_data:
        raise ValueError(f"Job {job_id} has no stored task data for retry")

    failed_ids = [ft["task_id"] for ft in failed_tasks]
    logger.info("=== Retrying %s failed tasks for job %s: %s ===", len(failed_ids), job_id, failed_ids)

    path = Path(repo_path).resolve()
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING, failed_tasks=[], error=None)

        from shared.llm import get_llm_client
        llm = get_llm_client()
        agents = _get_agents(llm)

        ensure_development_branch(path)

        # Reconstruct task objects from stored data
        all_tasks: Dict[str, Task] = {}
        for tid, tdata in all_tasks_data.items():
            try:
                all_tasks[tid] = Task(**tdata)
            except Exception:
                logger.warning("Could not reconstruct task %s from stored data - skipping", tid)

        # Re-read spec for agents that need it
        from spec_parser import load_spec_from_repo
        spec_content = load_spec_from_repo(path)

        # Reconstruct minimal architecture from stored overview
        from shared.models import SystemArchitecture
        arch_overview = job_data.get("_architecture_overview") or job_data.get("architecture_overview") or ""
        architecture = SystemArchitecture(overview=arch_overview)

        tech_lead = agents["tech_lead"]

        # Execute only the failed tasks
        completed = set()
        failed: Dict[str, str] = {}
        completed_code_task_ids = []
        execution_queue = list(failed_ids)
        total_tasks = len(execution_queue)
        task_counter = 0
        max_passes = total_tasks * 3

        while execution_queue and max_passes > 0:
            max_passes -= 1
            task_id = execution_queue.pop(0)
            task = all_tasks.get(task_id)
            if not task:
                logger.warning("Task %s not found in task registry - skipping", task_id)
                continue

            task_counter += 1
            task_start_time = time.monotonic()
            logger.info(
                "=== [RETRY %s/%s] Starting task %s (type=%s, assignee=%s) ===",
                task_counter, total_tasks, task_id, task.type.value, task.assignee,
            )
            update_job(job_id, current_task=task_id)
            branch_name = f"feature/{task_id}"

            try:
                if task.type.value == "git_setup":
                    completed.add(task_id)
                    logger.info("[%s] Git setup task auto-completed", task_id)
                    continue

                ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, task_id)
                if not ok:
                    logger.error("[%s] Feature branch creation FAILED: %s", task_id, msg)
                    failed[task_id] = f"Feature branch creation failed: {msg}"
                    continue

                if task.assignee == "devops":
                    from devops_agent.models import DevOpsInput
                    existing_pipeline = _read_repo_code(path, [".yml", ".yaml"])
                    result = agents["devops"].run(DevOpsInput(
                        task_description=task.description,
                        requirements=_task_requirements(task),
                        architecture=architecture,
                        existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                        tech_stack=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
                    ))
                    ok, msg = write_agent_output(path, result, subdir="devops")
                    if ok:
                        merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(path, branch_name)
                            completed.add(task_id)
                        else:
                            failed[task_id] = f"Merge failed: {merge_msg}"
                    else:
                        failed[task_id] = f"Write failed: {msg}"
                    checkout_branch(path, DEVELOPMENT_BRANCH)

                elif task.assignee == "backend":
                    from backend_agent.models import BackendInput
                    existing_code = _truncate_for_context(_read_repo_code(path), MAX_EXISTING_CODE_CHARS)
                    code_review_issues: list = []
                    task_completed_be = False
                    failure_reason_be = ""
                    for attempt in range(MAX_CLARIFICATION_REFINEMENTS + 1):
                        result = agents["backend"].run(BackendInput(
                            task_description=task.description,
                            requirements=_task_requirements(task),
                            user_story=getattr(task, "user_story", "") or "",
                            spec_content=_truncate_for_context(spec_content, MAX_EXISTING_CODE_CHARS),
                            architecture=architecture,
                            language="python",
                            existing_code=existing_code if existing_code and existing_code != "# No code files found" else None,
                            qa_issues=[],
                            security_issues=[],
                            code_review_issues=code_review_issues,
                        ))
                        ok, msg = write_agent_output(path, result, subdir="backend")
                        if not ok:
                            failure_reason_be = f"Write failed: {msg}"
                            break
                        merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(path, branch_name)
                            task_completed_be = True
                        else:
                            failure_reason_be = f"Merge failed: {merge_msg}"
                        break
                    if task_completed_be:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                    else:
                        failed[task_id] = failure_reason_be or "Backend agent produced no output"
                    checkout_branch(path, DEVELOPMENT_BRANCH)

                elif task.assignee == "frontend":
                    from frontend_agent.models import FrontendInput
                    existing_code = _truncate_for_context(
                        _read_repo_code(path, [".ts", ".tsx", ".html", ".scss"]),
                        MAX_EXISTING_CODE_CHARS,
                    )
                    api_endpoints = _truncate_for_context(
                        _read_repo_code(path, [".py"]),
                        MAX_API_SPEC_CHARS,
                    )
                    code_review_issues_fe: list = []
                    task_completed_fe = False
                    failure_reason_fe = ""
                    for attempt in range(MAX_CODE_REVIEW_ITERATIONS):
                        result = agents["frontend"].run(FrontendInput(
                            task_description=task.description,
                            requirements=_task_requirements(task),
                            user_story=getattr(task, "user_story", "") or "",
                            spec_content=_truncate_for_context(spec_content, MAX_EXISTING_CODE_CHARS),
                            architecture=architecture,
                            existing_code=existing_code if existing_code and existing_code != "# No code files found" else None,
                            api_endpoints=api_endpoints if api_endpoints and api_endpoints != "# No code files found" else None,
                            qa_issues=[],
                            security_issues=[],
                            code_review_issues=code_review_issues_fe,
                        ))
                        ok, msg = write_agent_output(path, result, subdir="frontend")
                        if not ok:
                            failure_reason_fe = f"Write failed: {msg}"
                            break
                        merge_ok, merge_msg = merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(path, branch_name)
                            task_completed_fe = True
                        else:
                            failure_reason_fe = f"Merge failed: {merge_msg}"
                        break
                    if task_completed_fe:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                    else:
                        failed[task_id] = failure_reason_fe or "Frontend agent produced no output"
                    checkout_branch(path, DEVELOPMENT_BRANCH)

                else:
                    completed.add(task_id)

                elapsed = time.monotonic() - task_start_time
                if task_id in completed:
                    logger.info("[%s] Retry COMPLETED in %.1fs", task_id, elapsed)
                else:
                    logger.warning("[%s] Retry FAILED after %.1fs: %s", task_id, elapsed, failed.get(task_id, "unknown"))

            except Exception as e:
                elapsed = time.monotonic() - task_start_time
                logger.exception("[%s] Retry FAILED with exception after %.1fs", task_id, elapsed)
                failed[task_id] = f"Unhandled exception: {e}"
                checkout_branch(path, DEVELOPMENT_BRANCH)

        # Final summary
        logger.info(
            "=== Retry finished: %s completed, %s still failed (of %s retried) ===",
            len(completed), len(failed), total_tasks,
        )
        if failed:
            logger.warning("=== Still-failed task report ===")
            for tid, reason in sorted(failed.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)

        failed_details = [
            {"task_id": tid, "reason": reason, "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid)}
            for tid, reason in failed.items()
        ]
        update_job(job_id, failed_tasks=failed_details, status=JOB_STATUS_COMPLETED, current_task=None)

    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
