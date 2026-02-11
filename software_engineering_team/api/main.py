"""
FastAPI application for the software engineering team.

Exposes an endpoint that accepts a local git repo path. The repo must contain
initial_spec.md at the root with the full project specification.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Path setup for imports when run as uvicorn from project root
import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from shared.llm import DummyLLMClient, OllamaLLMClient
from shared.models import ProductRequirements, TaskType
from shared.git_utils import ensure_development_branch
from spec_parser import load_spec_from_repo, parse_spec_heuristic, parse_spec_with_llm, validate_repo_path
from architecture_agent import ArchitectureExpertAgent, ArchitectureInput
from tech_lead_agent import TechLeadAgent, TechLeadInput
from devops_agent import DevOpsExpertAgent, DevOpsInput
from security_agent import CybersecurityExpertAgent, SecurityInput
from backend_agent import BackendExpertAgent, BackendInput
from frontend_agent import FrontendExpertAgent, FrontendInput
from qa_agent import QAExpertAgent, QAInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Software Engineering Team API",
    description="Runs the software engineering team (Architecture, Tech Lead, DevOps, Security, Backend, Frontend, QA) "
    "on a git repo. The repo must contain initial_spec.md at the root with the full project specification.",
    version="0.1.0",
)


class RunTeamRequest(BaseModel):
    """Request body for the run-team endpoint."""

    repo_path: str = Field(
        ...,
        description="Local filesystem path to the git repository. Must contain initial_spec.md at the root.",
    )
    use_llm_for_spec: bool = Field(
        True,
        description="Use LLM to parse initial_spec.md into structured requirements. If False, uses heuristic parsing.",
    )


class TaskResult(BaseModel):
    """Result of a single task execution."""

    task_id: str
    assignee: str
    summary: str


class RunTeamResponse(BaseModel):
    """Response from the run-team endpoint."""

    repo_path: str = Field(..., description="Resolved path to the repo.")
    requirements_title: str = Field(..., description="Parsed project title from spec.")
    architecture_overview: str = Field(default="", description="Architecture overview from Architecture Expert.")
    task_ids: List[str] = Field(default_factory=list, description="Task IDs in execution order.")
    task_results: List[TaskResult] = Field(default_factory=list)
    status: str = Field(default="completed", description="Pipeline status.")
    git_branch_setup: str = Field(
        default="",
        description="Result of ensuring development branch exists (e.g. 'Created branch development from main').",
    )


# Shared LLM and agents (lazy init)
_llm_client: Optional[OllamaLLMClient] = None
_use_dummy: bool = True


def _get_llm():
    """Lazily initialize and return the LLM client."""
    global _llm_client, _use_dummy
    if _llm_client is None:
        _llm_client = DummyLLMClient() if _use_dummy else OllamaLLMClient(model="deepseek-r1", timeout=1800.0)
    return _llm_client


def _set_use_dummy(use_dummy: bool) -> None:
    """Set whether to use DummyLLMClient. Call before first request."""
    global _use_dummy, _llm_client
    _use_dummy = use_dummy
    _llm_client = None  # Reset so next request gets fresh client


@app.post(
    "/run-team",
    response_model=RunTeamResponse,
    summary="Run software engineering team on a git repo",
    description="Validates the repo path, reads initial_spec.md, parses it into requirements, "
    "and runs the full team pipeline (Architecture → Tech Lead → specialists).",
)
def run_team(request: RunTeamRequest) -> RunTeamResponse:
    """
    Run the software engineering team on a git repository.

    The repo must:
    - Exist and be a valid directory
    - Be a git repository (.git present)
    - Contain initial_spec.md at the root with the full project specification
    """
    # Validate repo path
    try:
        repo_path = validate_repo_path(request.repo_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Ensure development branch exists (branching strategy: all work on development branch)
    git_branch_msg = ""
    try:
        created, msg = ensure_development_branch(repo_path)
        git_branch_msg = msg
    except Exception as e:
        logger.warning("Git branch setup failed (non-fatal): %s", e)
        git_branch_msg = f"Git setup skipped: {e}"

    # Load and parse spec
    try:
        spec_content = load_spec_from_repo(repo_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    llm = _get_llm()
    if request.use_llm_for_spec:
        try:
            requirements = parse_spec_with_llm(spec_content, llm)
        except Exception as e:
            logger.warning("LLM spec parse failed, falling back to heuristic: %s", e)
            requirements = parse_spec_heuristic(spec_content)
    else:
        requirements = parse_spec_heuristic(spec_content)

    # Run pipeline
    try:
        arch_agent = ArchitectureExpertAgent(llm_client=llm)
        arch_input = ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
        )
        arch_output = arch_agent.run(arch_input)
        architecture = arch_output.architecture

        tech_lead = TechLeadAgent(llm_client=llm)
        tech_lead_output = tech_lead.run(TechLeadInput(requirements=requirements, architecture=architecture))
        assignment = tech_lead_output.assignment

        agent_map = {
            "devops": DevOpsExpertAgent(llm),
            "security": CybersecurityExpertAgent(llm),
            "backend": BackendExpertAgent(llm),
            "frontend": FrontendExpertAgent(llm),
            "qa": QAExpertAgent(llm),
        }

        task_results: List[TaskResult] = []
        for task_id in assignment.execution_order:
            task = next((t for t in assignment.tasks if t.id == task_id), None)
            if not task:
                continue

            # Git setup is executed by the platform, not an agent
            if task.type == TaskType.GIT_SETUP:
                _, msg = ensure_development_branch(repo_path)
                task_results.append(TaskResult(task_id=task.id, assignee=task.assignee, summary=msg))
                continue

            if task.assignee not in agent_map:
                continue

            agent = agent_map[task.assignee]
            summary = ""

            if task.assignee == "devops":
                result = agent.run(
                    DevOpsInput(
                        task_description=task.description,
                        requirements=task.requirements,
                        architecture=architecture,
                    )
                )
                summary = result.summary or "Done"
            elif task.assignee == "backend":
                result = agent.run(
                    BackendInput(
                        task_description=task.description,
                        requirements=task.requirements,
                        architecture=architecture,
                        language="python",
                    )
                )
                summary = result.summary or "Done"
            elif task.assignee == "frontend":
                result = agent.run(
                    FrontendInput(
                        task_description=task.description,
                        requirements=task.requirements,
                        architecture=architecture,
                    )
                )
                summary = result.summary or "Done"
            elif task.assignee == "security":
                result = agent.run(
                    SecurityInput(
                        code="",
                        task_description=task.description,
                        architecture=architecture,
                    )
                )
                summary = result.summary or "Done"
            elif task.assignee == "qa":
                result = agent.run(
                    QAInput(
                        code="",
                        task_description=task.description,
                        architecture=architecture,
                    )
                )
                summary = result.summary or "Done"

            task_results.append(TaskResult(task_id=task.id, assignee=task.assignee, summary=summary))

        return RunTeamResponse(
            repo_path=str(repo_path),
            requirements_title=requirements.title,
            architecture_overview=architecture.overview,
            task_ids=assignment.execution_order,
            task_results=task_results,
            status="completed",
            git_branch_setup=git_branch_msg,
        )

    except Exception as e:
        logger.exception("Team pipeline failed")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}") from e


@app.get("/health")
def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}
