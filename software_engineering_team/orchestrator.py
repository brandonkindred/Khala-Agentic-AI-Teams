"""
Tech Lead orchestrator: runs the full pipeline with feature branches.

Flow:
1. Read initial_spec.md from work path (no git required at root)
2. Request architecture, Tech Lead generates plan
3. Prefix tasks (devops, git_setup) run sequentially on work path
4. Backend and frontend tasks run in parallel (one task per agent type at a time),
   each in its own repo (work_path/backend, work_path/frontend) initialized by Git Setup Agent
"""

from __future__ import annotations

import logging
import threading
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
    """Lazy init agents including the code review, documentation, and DbC comments agents."""
    from architecture_agent import ArchitectureExpertAgent, ArchitectureInput
    from backend_agent import BackendExpertAgent, BackendInput
    from code_review_agent import CodeReviewAgent, CodeReviewInput
    from dbc_comments_agent import DbcCommentsAgent, DbcCommentsInput
    from devops_agent import DevOpsExpertAgent, DevOpsInput
    from documentation_agent import DocumentationAgent, DocumentationInput
    from frontend_agent import FrontendExpertAgent, FrontendInput
    from git_setup_agent import GitSetupAgent
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
        "dbc_comments": DbcCommentsAgent(llm),
        "documentation": DocumentationAgent(llm),
        "git_setup": GitSetupAgent(),
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
    """Read code files from repo, concatenated. Excludes .git to avoid corrupt object errors."""
    if extensions is None:
        extensions = [".py", ".ts", ".tsx", ".java", ".yml", ".yaml"]
    parts = []
    for f in repo_path.rglob("*"):
        if ".git" in f.parts:
            continue
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


def _run_dbc_comments_review(
    agents: dict,
    repo_path: Path,
    task_id: str,
    language: str,
    task_description: str,
    architecture,
) -> None:
    """
    Run the Design by Contract Comments agent on the current feature branch.
    Adds DbC-compliant comments to all methods, functions, and classes.
    Commits changes to the branch if any comments were added.

    Preconditions:
        - The current branch is the feature branch with code to review
        - agents dict contains a "dbc_comments" key

    Postconditions:
        - If comments were added, they are committed to the current branch
        - If code was already compliant, a praise message is logged
        - Any failures are logged but do not block the pipeline
    """
    from dbc_comments_agent.models import DbcCommentsInput
    from shared.git_utils import write_files_and_commit

    try:
        dbc_code = _read_repo_code(repo_path)
        if not dbc_code or dbc_code == "# No code files found":
            logger.info("[%s] DbC: no code files to review, skipping", task_id)
            return

        dbc_result = agents["dbc_comments"].run(DbcCommentsInput(
            code=dbc_code,
            language=language,
            task_description=task_description,
            architecture=architecture,
        ))

        if not dbc_result.already_compliant and dbc_result.files:
            ok, msg = write_files_and_commit(
                repo_path,
                dbc_result.files,
                dbc_result.suggested_commit_message,
            )
            if ok:
                logger.info(
                    "[%s] DbC: added %s comments, updated %s -- committed to branch",
                    task_id,
                    dbc_result.comments_added,
                    dbc_result.comments_updated,
                )
            else:
                logger.warning("[%s] DbC: commit failed: %s", task_id, msg)
        else:
            logger.info(
                "[%s] DbC: code complies with Design by Contract -- great job coding!",
                task_id,
            )
    except Exception as e:
        # Non-blocking: DbC failure should never stop the pipeline
        logger.warning("[%s] DbC: review failed (non-blocking): %s", task_id, e)


def _run_tech_lead_review(
    tech_lead,
    task_update: TaskUpdate,
    spec_content: str,
    architecture,
    all_tasks: dict,
    completed: set,
    execution_queue: list,
    repo_path: Path,
    doc_agent=None,
    append_task_id_fn=None,
) -> None:
    """
    Ask the Tech Lead to review progress after a task completes.
    If the Tech Lead identifies gaps, new tasks are added to the execution queue
    (or to the queue provided via append_task_id_fn when running in a worker).
    After review, the Tech Lead triggers the Documentation Agent if available.
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
                if append_task_id_fn is not None:
                    append_task_id_fn(nt.id)
                else:
                    execution_queue.append(nt.id)
        logger.info(
            "Tech Lead review: added %s new tasks from progress review: %s",
            len(new_tasks),
            [t.id for t in new_tasks],
        )

    # Tech Lead triggers the Documentation Agent to update project docs
    if doc_agent:
        tech_lead.trigger_documentation_update(
            doc_agent=doc_agent,
            repo_path=repo_path,
            task_update=task_update,
            spec_content=spec_content,
            architecture=architecture,
            codebase_summary=codebase_summary,
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
    from shared.command_runner import run_ng_build_with_nvm_fallback, run_python_syntax_check, run_pytest

    if agent_type == "frontend":
        # repo_path may be frontend repo root (package.json here) or work path (frontend/ subdir)
        frontend_dir = repo_path if (repo_path / "package.json").exists() else (repo_path / "frontend")
        if not (frontend_dir / "package.json").exists():
            logger.info("Build verification: no Angular project found, skipping ng build")
            return True, ""
        from shared.command_runner import is_ng_build_environment_failure
        result = run_ng_build_with_nvm_fallback(frontend_dir)
        if not result.success:
            if is_ng_build_environment_failure(result):
                # Environment (e.g. Node version) - caller should fail task, not retry
                return False, "ENV:" + result.error_summary
            logger.warning("Build verification failed for task %s: %s", task_id, result.error_summary[:200])
            return False, result.error_summary
        logger.info("Build verification passed for frontend task %s", task_id)
        return True, ""

    elif agent_type == "backend":
        # repo_path may be backend repo root (py files here) or work path (backend/ subdir)
        backend_dir = repo_path if any(repo_path.rglob("*.py")) else (repo_path / "backend")
        if not backend_dir.exists() or not any(backend_dir.rglob("*.py")):
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

    Work path (repo_path) is the folder where work is saved; it does not need to be a git repo.
    Backend and frontend each have their own repo at work_path/backend and work_path/frontend,
    initialized by the Git Setup Agent before first use. Backend and frontend tasks run in parallel.
    """
    path = Path(repo_path).resolve()
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)

        from shared.llm import get_llm_client
        llm = get_llm_client()
        agents = _get_agents(llm)

        # 1. Read spec from work path (no git required at root)
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

        # 5. Execute tasks: partition into prefix (devops/git_setup), backend, frontend
        completed = set()
        failed: Dict[str, str] = {}
        completed_code_task_ids: List[str] = []
        all_tasks = {t.id: t for t in assignment.tasks}
        full_order = list(assignment.execution_order)
        prefix_queue = [tid for tid in full_order if all_tasks.get(tid) and (
            all_tasks[tid].type.value == "git_setup" or all_tasks[tid].assignee == "devops"
        )]
        backend_queue: List[str] = [tid for tid in full_order if all_tasks.get(tid) and all_tasks[tid].assignee == "backend"]
        frontend_queue: List[str] = [tid for tid in full_order if all_tasks.get(tid) and all_tasks[tid].assignee == "frontend"]
        total_tasks = len(prefix_queue) + len(backend_queue) + len(frontend_queue)
        state_lock = threading.Lock()

        # Remaining task ids (still in backend/frontend queues) for Tech Lead
        def _remaining_queue_ids() -> List[str]:
            with state_lock:
                return list(backend_queue) + list(frontend_queue)

        logger.info(
            "=== Starting task execution: prefix=%s, backend=%s, frontend=%s ===",
            len(prefix_queue), len(backend_queue), len(frontend_queue),
        )

        # Run prefix tasks sequentially (work path; devops writes to path/devops, no git)
        for task_id in prefix_queue:
            task = all_tasks.get(task_id)
            if not task:
                continue
            update_job(job_id, current_task=task_id)
            if task.type.value == "git_setup":
                completed.add(task_id)
                logger.info("[%s] Git setup task auto-completed", task_id)
                continue
            if task.assignee == "devops":
                # Defer containerization to after backend and frontend complete; skip early devops run.
                completed.add(task_id)
                logger.info("[%s] DevOps task deferred; will run after backend and frontend complete", task_id)
                continue

        # Backend and frontend workers run in parallel (one task per agent type at a time)
        def _backend_worker() -> None:
            while True:
                with state_lock:
                    if not backend_queue:
                        break
                    task_id = backend_queue.pop(0)
                    task = all_tasks.get(task_id)
                if not task:
                    continue
                update_job(job_id, current_task=task_id)
                logger.info("[%s] >>> Backend worker starting task %s", task_id, task_id)
                task_start_time = time.monotonic()
                try:
                    from shared.command_runner import ensure_backend_project_initialized
                    init_result = ensure_backend_project_initialized(backend_dir)
                    if not init_result.success:
                        with state_lock:
                            failed[task_id] = f"Backend init failed: {init_result.error_summary}"
                        continue
                    if not (backend_dir / ".git").exists():
                        gs_result = agents["git_setup"].run(backend_dir)
                        if not gs_result.success:
                            with state_lock:
                                failed[task_id] = f"Git setup failed: {gs_result.message}"
                            continue
                    completed_tasks_list = [t for tid, t in all_tasks.items() if tid in completed]
                    remaining_ids = set(_remaining_queue_ids()) - {task_id}
                    remaining_tasks_list = [t for tid, t in all_tasks.items() if tid in remaining_ids]

                    def _append_backend_task(nt) -> None:
                        with state_lock:
                            all_tasks[nt.id] = nt
                            backend_queue.append(nt.id)

                    workflow_result = agents["backend"].run_workflow(
                        repo_path=backend_dir,
                        task=task,
                        spec_content=spec_content,
                        architecture=architecture,
                        qa_agent=agents["qa"],
                        dbc_agent=agents["dbc_comments"],
                        code_review_agent=agents["code_review"],
                        tech_lead=tech_lead,
                        build_verifier=_run_build_verification,
                        doc_agent=agents.get("documentation"),
                        completed_tasks=completed_tasks_list,
                        remaining_tasks=remaining_tasks_list,
                        all_tasks=all_tasks,
                        execution_queue=backend_queue,
                        append_task_fn=_append_backend_task,
                    )
                    elapsed = time.monotonic() - task_start_time
                    with state_lock:
                        if workflow_result.success:
                            completed.add(task_id)
                            completed_code_task_ids.append(task_id)
                            logger.info("[%s] Backend COMPLETED in %.1fs", task_id, elapsed)
                        else:
                            failed[task_id] = workflow_result.failure_reason or "Backend workflow failed"
                            logger.warning("[%s] Backend FAILED after %.1fs: %s", task_id, elapsed, failed[task_id])
                except Exception as e:
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.exception("[%s] Backend task exception", task_id)
                logger.info("[%s] <<< Backend worker done", task_id)

            # After backend agent is done with all tasks for this repo, containerize it
            devops_agent = agents.get("devops")
            if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
                existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
                tech_lead.trigger_devops_for_backend(
                    devops_agent, backend_dir, architecture, spec_content,
                    existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                )

        def _frontend_worker() -> None:
            while True:
                with state_lock:
                    if not frontend_queue:
                        break
                    task_id = frontend_queue.pop(0)
                    task = all_tasks.get(task_id)
                if not task:
                    continue
                update_job(job_id, current_task=task_id)
                logger.info("[%s] >>> Frontend worker starting task %s", task_id, task_id)
                task_start_time = time.monotonic()
                branch_name = f"feature/{task_id}"
                try:
                    from shared.command_runner import ensure_frontend_project_initialized
                    init_result = ensure_frontend_project_initialized(frontend_dir)
                    if not init_result.success:
                        with state_lock:
                            failed[task_id] = f"Frontend init failed: {init_result.error_summary}"
                        continue
                    if not (frontend_dir / ".git").exists():
                        gs_result = agents["git_setup"].run(frontend_dir)
                        if not gs_result.success:
                            with state_lock:
                                failed[task_id] = f"Git setup failed: {gs_result.message}"
                            continue
                    ok, msg = create_feature_branch(frontend_dir, DEVELOPMENT_BRANCH, task_id)
                    if not ok:
                        with state_lock:
                            failed[task_id] = f"Feature branch failed: {msg}"
                        continue

                    from shared.command_runner import ensure_frontend_dependencies_installed
                    install_result = ensure_frontend_dependencies_installed(frontend_dir)
                    if not install_result.success:
                        with state_lock:
                            failed[task_id] = "Frontend dependency install failed: " + (
                                install_result.error_summary or install_result.stderr or "unknown"
                            )
                        continue

                    from frontend_agent.models import FrontendInput
                    from qa_agent.models import QAInput
                    qa_issues, sec_issues = [], []
                    code_review_issues = []
                    result = None
                    merged = False
                    task_completed = False
                    failure_reason = ""
                    current_task = task

                    for iteration_round in range(MAX_CODE_REVIEW_ITERATIONS):
                        existing_code = _truncate_for_context(
                            _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"]),
                            MAX_EXISTING_CODE_CHARS,
                        )
                        api_endpoints = _truncate_for_context(
                            _read_repo_code(backend_dir, [".py"]),
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
                        if result.needs_clarification and result.clarification_requests:
                            if iteration_round < MAX_CLARIFICATION_REFINEMENTS:
                                current_task = tech_lead.refine_task(
                                    current_task, result.clarification_requests, spec_content, architecture,
                                )
                                code_review_issues = []
                                continue
                            failure_reason = "Agent needs clarification after max refinements"
                            checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
                            break

                        ok, msg = write_agent_output(frontend_dir, result, subdir="")
                        if not ok:
                            failure_reason = f"Write failed: {msg}"
                            checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
                            break

                        build_ok, build_errors = _run_build_verification(frontend_dir, "frontend", task_id)
                        if not build_ok:
                            if build_errors.startswith("ENV:"):
                                failure_reason = "Unsupported environment: " + build_errors[4:].strip()[:500]
                                checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
                                break
                            code_review_issues = [{"severity": "critical", "category": "logic",
                                                   "file_path": "", "description": f"ng build failed: {build_errors[:2000]}",
                                                   "suggestion": "Fix the Angular compilation errors"}]
                            continue

                        code_on_branch = _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"])
                        existing_code = _truncate_for_context(code_on_branch, MAX_EXISTING_CODE_CHARS)
                        review_result = _run_code_review(
                            agents, code_on_branch, spec_content, current_task,
                            "typescript", architecture, existing_code,
                        )
                        _log_code_review_result(review_result, task_id)
                        if not review_result.approved:
                            code_review_issues = _code_review_issues_to_dicts(review_result.issues)
                            if iteration_round < MAX_CODE_REVIEW_ITERATIONS - 1:
                                continue

                        code_to_review = _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"])
                        qa_result = agents["qa"].run(QAInput(
                            code=code_to_review, language="typescript",
                            task_description=current_task.description, architecture=architecture,
                        ))
                        fix_tasks = tech_lead.evaluate_qa_and_create_fix_tasks(
                            current_task, qa_result, spec_content, architecture,
                        )
                        if fix_tasks:
                            with state_lock:
                                for ft in fix_tasks:
                                    all_tasks[ft.id] = ft
                                    if getattr(ft, "assignee", None) == "frontend":
                                        frontend_queue.insert(0, ft.id)
                                    else:
                                        backend_queue.insert(0, ft.id)
                            logger.info("Tech Lead created %s fix tasks from QA feedback", len(fix_tasks))

                        _run_dbc_comments_review(
                            agents, frontend_dir, task_id, "typescript",
                            current_task.description, architecture,
                        )
                        merge_ok, merge_msg = merge_branch(frontend_dir, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(frontend_dir, branch_name)
                            merged = True
                            task_completed = True
                            with state_lock:
                                completed.add(task_id)
                                completed_code_task_ids.append(task_id)
                        else:
                            failure_reason = f"Merge failed: {merge_msg}"
                        checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
                        break

                    elapsed = time.monotonic() - task_start_time
                    if not merged:
                        checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
                    if task_completed:
                        with state_lock:
                            completed.add(task_id)
                        task_update = _build_task_update(task_id, "frontend", result)
                        def _append_frontend_task_id(tid: str) -> None:
                            with state_lock:
                                frontend_queue.append(tid)
                        _run_tech_lead_review(
                            tech_lead, task_update, spec_content, architecture,
                            all_tasks, completed, _remaining_queue_ids(), frontend_dir,
                            doc_agent=agents.get("documentation"),
                            append_task_id_fn=_append_frontend_task_id,
                        )
                    else:
                        with state_lock:
                            failed[task_id] = failure_reason or "Frontend agent produced no output"
                    logger.info("[%s] <<< Frontend worker done (completed=%s)", task_id, task_completed)
                except Exception as e:
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.exception("[%s] Frontend task exception", task_id)
                    checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)

            # After frontend agent is done with all tasks for this repo, containerize it
            devops_agent = agents.get("devops")
            if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
                existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
                tech_lead.trigger_devops_for_frontend(
                    devops_agent, frontend_dir, architecture, spec_content,
                    existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                )

        t_backend = threading.Thread(target=_backend_worker)
        t_frontend = threading.Thread(target=_frontend_worker)
        t_backend.start()
        t_frontend.start()
        t_backend.join()
        t_frontend.join()

        remaining_in_queues = len(backend_queue) + len(frontend_queue)
        # Log final execution summary
        logger.info(
            "=== Task execution finished: %s completed, %s failed, %s remaining (of %s total) ===",
            len(completed), len(failed), remaining_in_queues, total_tasks,
        )
        if failed:
            logger.warning("=== Failed task report ===")
            for tid, reason in sorted(failed.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)
        if remaining_in_queues:
            logger.warning(
                "Unprocessed tasks still in queues: backend=%s, frontend=%s",
                len(backend_queue), len(frontend_queue),
            )

        # DevOps: containerize every git repo created by the pipeline (backend and frontend)
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_frontend(
                devops_agent, frontend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
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

        # Security: run on backend repo and frontend repo separately
        if completed_code_task_ids and tech_lead.should_run_security(
            completed_code_task_ids, spec_content, tech_lead_output.requirement_task_mapping
        ):
            from security_agent.models import SecurityInput
            has_backend = any(all_tasks.get(tid) and all_tasks[tid].assignee == "backend" for tid in completed_code_task_ids)
            has_frontend = any(all_tasks.get(tid) and all_tasks[tid].assignee == "frontend" for tid in completed_code_task_ids)
            if has_backend:
                logger.info("Tech Lead requested security review - running Security agent on backend repo")
                code_backend = _read_repo_code(backend_dir)
                if code_backend and code_backend != "# No code files found":
                    sec_result = agents["security"].run(SecurityInput(
                        code=code_backend,
                        language="python",
                        task_description="Full codebase security review",
                        architecture=architecture,
                    ))
                    if sec_result.vulnerabilities:
                        logger.warning("Security (backend) found %s vulnerabilities", len(sec_result.vulnerabilities))
            if has_frontend:
                logger.info("Tech Lead requested security review - running Security agent on frontend repo")
                code_frontend = _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"])
                if code_frontend and code_frontend != "# No code files found":
                    sec_result = agents["security"].run(SecurityInput(
                        code=code_frontend,
                        language="typescript",
                        task_description="Full codebase security review",
                        architecture=architecture,
                    ))
                    if sec_result.vulnerabilities:
                        logger.warning("Security (frontend) found %s vulnerabilities", len(sec_result.vulnerabilities))

        # Final documentation pass: ensure README exists in backend and frontend repos if missing/empty
        doc_agent = agents.get("documentation")
        if doc_agent and completed_code_task_ids:
            for repo_name, repo_dir in [("backend", backend_dir), ("frontend", frontend_dir)]:
                if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
                    continue
                readme_path = repo_dir / "README.md"
                if readme_path.exists() and readme_path.read_text(encoding="utf-8", errors="replace").strip():
                    continue
                logger.info(
                    "Final documentation pass: %s repo README missing or empty, running Documentation Agent",
                    repo_name,
                )
                try:
                    codebase_content = _truncate_for_context(
                        _read_repo_code(
                            repo_dir,
                            [".py"] if repo_name == "backend" else [".ts", ".tsx", ".html", ".scss"],
                        ),
                        MAX_EXISTING_CODE_CHARS,
                    )
                    doc_agent.run_full_workflow(
                        repo_path=repo_dir,
                        task_id=f"final-docs-{repo_name}",
                        task_summary="Update all project documentation; ensure README and key sections exist.",
                        agent_type=repo_name,
                        spec_content=spec_content,
                        architecture=architecture,
                        codebase_content=codebase_content,
                    )
                except Exception as doc_err:
                    logger.warning(
                        "Final documentation pass failed for %s repo (non-blocking): %s",
                        repo_name,
                        doc_err,
                    )

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
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING, failed_tasks=[], error=None)

        from shared.llm import get_llm_client
        llm = get_llm_client()
        agents = _get_agents(llm)

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

        # Execute only the failed tasks (sequential retry; use correct repo per assignee)
        completed = set()
        failed_retry: Dict[str, str] = {}
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

                if task.assignee == "backend":
                    if not (backend_dir / ".git").exists():
                        gs_result = agents["git_setup"].run(backend_dir)
                        if not gs_result.success:
                            failed_retry[task_id] = f"Git setup failed: {gs_result.message}"
                            continue
                    completed_tasks_list = [t for tid, t in all_tasks.items() if tid in completed]
                    remaining_ids = set(execution_queue)
                    remaining_tasks_list = [t for tid, t in all_tasks.items() if tid in remaining_ids]

                    workflow_result = agents["backend"].run_workflow(
                        repo_path=backend_dir,
                        task=task,
                        spec_content=spec_content,
                        architecture=architecture,
                        qa_agent=agents["qa"],
                        dbc_agent=agents["dbc_comments"],
                        code_review_agent=agents["code_review"],
                        tech_lead=tech_lead,
                        build_verifier=_run_build_verification,
                        doc_agent=agents.get("documentation"),
                        completed_tasks=completed_tasks_list,
                        remaining_tasks=remaining_tasks_list,
                        all_tasks=all_tasks,
                        execution_queue=execution_queue,
                    )
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                    else:
                        failed_retry[task_id] = workflow_result.failure_reason or "Backend workflow produced no output"

                elif task.assignee == "devops":
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
                        completed.add(task_id)
                    else:
                        failed_retry[task_id] = f"Write failed: {msg}"

                elif task.assignee == "frontend":
                    if not (frontend_dir / ".git").exists():
                        gs_result = agents["git_setup"].run(frontend_dir)
                        if not gs_result.success:
                            failed_retry[task_id] = f"Git setup failed: {gs_result.message}"
                            continue
                    ok, msg = create_feature_branch(frontend_dir, DEVELOPMENT_BRANCH, task_id)
                    if not ok:
                        failed_retry[task_id] = f"Feature branch failed: {msg}"
                        continue

                    from frontend_agent.models import FrontendInput
                    from qa_agent.models import QAInput
                    existing_code = _truncate_for_context(
                        _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"]),
                        MAX_EXISTING_CODE_CHARS,
                    )
                    api_endpoints = _truncate_for_context(
                        _read_repo_code(backend_dir, [".py"]),
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
                        ok, msg = write_agent_output(frontend_dir, result, subdir="")
                        if not ok:
                            failure_reason_fe = f"Write failed: {msg}"
                            break
                        merge_ok, merge_msg = merge_branch(frontend_dir, branch_name, DEVELOPMENT_BRANCH)
                        if merge_ok:
                            delete_branch(frontend_dir, branch_name)
                            task_completed_fe = True
                        else:
                            failure_reason_fe = f"Merge failed: {merge_msg}"
                        break
                    if task_completed_fe:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                    else:
                        failed_retry[task_id] = failure_reason_fe or "Frontend agent produced no output"
                    checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)

                else:
                    completed.add(task_id)

                elapsed = time.monotonic() - task_start_time
                if task_id in completed:
                    logger.info("[%s] Retry COMPLETED in %.1fs", task_id, elapsed)
                else:
                    logger.warning("[%s] Retry FAILED after %.1fs: %s", task_id, elapsed, failed_retry.get(task_id, "unknown"))

            except Exception as e:
                elapsed = time.monotonic() - task_start_time
                logger.exception("[%s] Retry FAILED with exception after %.1fs", task_id, elapsed)
                failed_retry[task_id] = f"Unhandled exception: {e}"
                if task.assignee == "backend":
                    checkout_branch(backend_dir, DEVELOPMENT_BRANCH)
                elif task.assignee == "frontend":
                    checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)

        # Final summary
        logger.info(
            "=== Retry finished: %s completed, %s still failed (of %s retried) ===",
            len(completed), len(failed_retry), total_tasks,
        )
        if failed_retry:
            logger.warning("=== Still-failed task report ===")
            for tid, reason in sorted(failed_retry.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)

        # DevOps: containerize every git repo that exists (same as main run)
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_frontend(
                devops_agent, frontend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
            )

        failed_details = [
            {"task_id": tid, "reason": reason, "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid)}
            for tid, reason in failed_retry.items()
        ]
        update_job(job_id, failed_tasks=failed_details, status=JOB_STATUS_COMPLETED, current_task=None)

    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


