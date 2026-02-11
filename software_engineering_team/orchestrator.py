"""
Tech Lead orchestrator: runs the full pipeline with feature branches.

Flow:
1. Ensure development branch
2. Read initial_spec.md, parse to requirements
3. Request architecture from Architect when needed
4. Tech Lead generates plan, saves to task cache
5. For each task: create feature branch, implement, (review for coding tasks), merge
"""

from __future__ import annotations

import logging
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
    add_task_result,
    create_job,
    get_job,
    update_job,
)
from shared.repo_writer import write_agent_output
from shared.task_cache import save_tasks, update_task

logger = logging.getLogger(__name__)

CACHE_DIR = ".agent_cache"


def _get_agents(llm):
    """Lazy init agents."""
    from architecture_agent import ArchitectureExpertAgent, ArchitectureInput
    from backend_agent import BackendExpertAgent, BackendInput
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
    }


def _task_requirements(task) -> str:
    """Build full requirements string including acceptance criteria."""
    parts = [task.requirements] if task.requirements else []
    if getattr(task, "acceptance_criteria", None):
        parts.append("Acceptance criteria: " + "; ".join(task.acceptance_criteria))
    return "\n\n".join(parts) if parts else task.description


MAX_REVIEW_ITERATIONS = 5


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


def run_orchestrator(job_id: str, repo_path: str | Path) -> None:
    """
    Main orchestration loop. Runs in background thread.
    """
    path = Path(repo_path).resolve()
    try:
        update_job(job_id, cache_dir=CACHE_DIR, status=JOB_STATUS_RUNNING)

        from shared.llm import DummyLLMClient, OllamaLLMClient
        llm = DummyLLMClient()  # TODO: configurable
        agents = _get_agents(llm)

        # 1. Ensure development branch
        ensure_development_branch(path)
        update_job(job_id, cache_dir=CACHE_DIR, current_task="git_setup")

        # 2. Read spec
        from spec_parser import load_spec_from_repo, parse_spec_heuristic, parse_spec_with_llm
        spec_content = load_spec_from_repo(path)
        try:
            requirements = parse_spec_with_llm(spec_content, llm)
        except Exception:
            requirements = parse_spec_heuristic(spec_content)
        update_job(job_id, cache_dir=CACHE_DIR, requirements_title=requirements.title)

        # 3. Architecture (Tech Lead needs it)
        from architecture_agent.models import ArchitectureInput
        arch_agent = agents["architecture"]
        arch_input = ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
        )
        arch_output = arch_agent.run(arch_input)
        architecture = arch_output.architecture
        update_job(job_id, cache_dir=CACHE_DIR, architecture_overview=architecture.overview)

        # 4. Tech Lead generates plan
        from tech_lead_agent.models import TechLeadInput
        tech_lead = agents["tech_lead"]
        tech_lead_output = tech_lead.run(TechLeadInput(
            requirements=requirements,
            architecture=architecture,
            repo_path=str(path),
            spec_content=spec_content,
        ))
        assignment = tech_lead_output.assignment

        tasks_data = [
            {
                "id": t.id,
                "type": t.type.value,
                "assignee": t.assignee,
                "description": t.description,
                "requirements": t.requirements,
                "dependencies": t.dependencies,
                "status": "pending",
                "feature_branch_name": f"feature/{t.id}",
                "label": getattr(t, "label", None),
                "acceptance_criteria": getattr(t, "acceptance_criteria", []),
            }
            for t in assignment.tasks
        ]
        save_tasks(job_id, tasks_data, assignment.execution_order, cache_dir=CACHE_DIR)

        # 5. Execute tasks
        completed = set()
        for task_id in assignment.execution_order:
            task = next((t for t in assignment.tasks if t.id == task_id), None)
            if not task:
                continue
            if any(dep not in completed for dep in task.dependencies):
                continue

            update_job(job_id, cache_dir=CACHE_DIR, current_task=task_id)
            branch_name = f"feature/{task_id}"

            try:
                if task.type.value == "git_setup":
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": "Done"}, CACHE_DIR)
                    completed.add(task_id)
                    continue

                # Create feature branch
                ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, task_id)
                if not ok:
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": f"Failed: {msg}"}, CACHE_DIR)
                    continue

                if task.assignee == "devops":
                    from devops_agent.models import DevOpsInput
                    result = agents["devops"].run(DevOpsInput(
                        task_description=task.description,
                        requirements=_task_requirements(task),
                        architecture=architecture,
                    ))
                    subdir = "devops"
                    ok, msg = write_agent_output(path, result, subdir=subdir)
                    if not ok:
                        logger.warning("DevOps write failed: %s", msg)
                    checkout_branch(path, DEVELOPMENT_BRANCH)
                    merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                    delete_branch(path, branch_name)
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": result.summary or "Done"}, CACHE_DIR)
                    completed.add(task_id)

                elif task.assignee == "backend":
                    from backend_agent.models import BackendInput
                    from qa_agent.models import QAInput
                    from security_agent.models import SecurityInput
                    qa_issues, sec_issues = [], []
                    result = None
                    merged = False
                    for iteration in range(MAX_REVIEW_ITERATIONS):
                        result = agents["backend"].run(BackendInput(
                            task_description=task.description,
                            requirements=_task_requirements(task),
                            architecture=architecture,
                            language="python",
                            qa_issues=qa_issues,
                            security_issues=sec_issues,
                        ))
                        ok, msg = write_agent_output(path, result, subdir="backend")
                        if not ok and iteration == 0:
                            logger.warning("Backend write failed: %s", msg)
                        code_to_review = _read_repo_code(path)
                        qa_result = agents["qa"].run(QAInput(code=code_to_review, language="python", task_description=task.description, architecture=architecture))
                        sec_result = agents["security"].run(SecurityInput(code=code_to_review, language="python", task_description=task.description, architecture=architecture))
                        if qa_result.approved and sec_result.approved:
                            checkout_branch(path, DEVELOPMENT_BRANCH)
                            merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                            delete_branch(path, branch_name)
                            logger.info("Feature branch %s was successfully merged to development", branch_name)
                            merged = True
                            break
                        qa_issues, sec_issues = _issues_to_dicts(qa_result.bugs_found, sec_result.vulnerabilities)
                        if not qa_issues and not sec_issues:
                            break
                    if not merged:
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": (result.summary if result else "Done") or "Done"}, CACHE_DIR)
                    completed.add(task_id)

                elif task.assignee == "frontend":
                    from frontend_agent.models import FrontendInput
                    from qa_agent.models import QAInput
                    from security_agent.models import SecurityInput
                    qa_issues, sec_issues = [], []
                    result = None
                    merged = False
                    for iteration in range(MAX_REVIEW_ITERATIONS):
                        result = agents["frontend"].run(FrontendInput(
                            task_description=task.description,
                            requirements=_task_requirements(task),
                            architecture=architecture,
                            qa_issues=qa_issues,
                            security_issues=sec_issues,
                        ))
                        ok, msg = write_agent_output(path, result, subdir="frontend")
                        if not ok and iteration == 0:
                            logger.warning("Frontend write failed: %s", msg)
                        code_to_review = _read_repo_code(path, [".ts", ".tsx", ".html", ".scss"])
                        qa_result = agents["qa"].run(QAInput(code=code_to_review, language="typescript", task_description=task.description, architecture=architecture))
                        sec_result = agents["security"].run(SecurityInput(code=code_to_review, language="typescript", task_description=task.description, architecture=architecture))
                        if qa_result.approved and sec_result.approved:
                            checkout_branch(path, DEVELOPMENT_BRANCH)
                            merge_branch(path, branch_name, DEVELOPMENT_BRANCH)
                            delete_branch(path, branch_name)
                            logger.info("Feature branch %s was successfully merged to development", branch_name)
                            merged = True
                            break
                        qa_issues, sec_issues = _issues_to_dicts(qa_result.bugs_found, sec_result.vulnerabilities)
                        if not qa_issues and not sec_issues:
                            break
                    if not merged:
                        checkout_branch(path, DEVELOPMENT_BRANCH)
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": (result.summary if result else "Done") or "Done"}, CACHE_DIR)
                    completed.add(task_id)

                else:
                    add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": "Skipped"}, CACHE_DIR)
                    completed.add(task_id)

            except Exception as e:
                logger.exception("Task %s failed", task_id)
                add_task_result(job_id, {"task_id": task_id, "assignee": task.assignee, "summary": f"Failed: {e}"}, CACHE_DIR)
                checkout_branch(path, DEVELOPMENT_BRANCH)

        update_job(job_id, cache_dir=CACHE_DIR, status=JOB_STATUS_COMPLETED, progress=100, current_task=None)

    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, cache_dir=CACHE_DIR, status=JOB_STATUS_FAILED, error=str(e))


