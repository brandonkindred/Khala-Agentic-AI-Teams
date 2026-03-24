"""
Run the software engineering team pipeline.

Flow:
1. Architecture Expert designs system from product requirements
2. Tech Lead breaks down work and assigns tasks
3. Specialists (DevOps, Security, Backend, Frontend, QA) execute tasks in order
4. Each specialist uses the architecture when implementing or validating

Usage:
  cd software_engineering_team
  python -m agent_implementations.run_team

Or with path setup from project root:
  python software_engineering_team/agent_implementations/run_team.py
"""

import logging
from pathlib import Path

import _path_setup  # noqa: F401
from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
from backend_agent import BackendExpertAgent, BackendInput
from backend_agent.agent import _read_openapi_spec_from_repo
from devops_agent import DevOpsExpertAgent, DevOpsInput
from frontend_team.feature_agent import FrontendExpertAgent, FrontendInput
from qa_agent import QAExpertAgent, QAInput
from security_agent import CybersecurityExpertAgent, SecurityInput
from tech_lead_agent import TechLeadAgent, TechLeadInput

from llm_service import get_client
from software_engineering_team.shared.models import ProductRequirements, TaskType

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Uses get_llm_client() which reads SW_LLM_PROVIDER, SW_LLM_MODEL (default: qwen3.5:397b-cloud)
LLM = get_client()

# Example product requirements
REQUIREMENTS = ProductRequirements(
    title="User Authentication API",
    description="Build a REST API for user authentication with signup, login, and token refresh. "
    "Must support email/password and integrate with a frontend Angular app.",
    acceptance_criteria=[
        "POST /auth/signup creates a new user",
        "POST /auth/login returns JWT tokens",
        "POST /auth/refresh refreshes access token",
        "Protected routes require valid Bearer token",
        "Frontend can call all endpoints and display auth state",
    ],
    constraints=[
        "Use Python FastAPI or Java Spring Boot for backend",
        "Use Angular for frontend",
        "JWT for session management",
        "Docker for deployment",
    ],
    priority="high",
)


def main() -> None:
    # 1. Architecture Expert designs the system
    logger.info("=== Architecture Expert ===")
    arch_agent = ArchitectureExpertAgent(llm_client=get_client("architecture"))
    arch_input = ArchitectureInput(
        requirements=REQUIREMENTS,
        technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
    )
    arch_output = arch_agent.run(arch_input)
    architecture = arch_output.architecture
    logger.info(
        "Architecture: %s",
        architecture.overview[:200] + "..."
        if len(architecture.overview) > 200
        else architecture.overview,
    )

    # 2. Tech Lead plans and assigns tasks
    logger.info("=== Tech Lead ===")
    tech_lead = TechLeadAgent(llm_client=LLM)
    tech_lead_input = TechLeadInput(
        requirements=REQUIREMENTS,
        architecture=architecture,
        spec_content=REQUIREMENTS.description,
    )
    tech_lead_output = tech_lead.run(tech_lead_input)
    if tech_lead_output.spec_clarification_needed:
        logger.warning(
            "Spec is unclear. Clarification needed: %s", tech_lead_output.clarification_questions
        )
        return
    assignment = tech_lead_output.assignment
    logger.info("Tasks: %s", [t.id for t in assignment.tasks])

    # 3. Execute tasks by specialist
    agent_map = {
        "devops": DevOpsExpertAgent(get_client("devops")),
        "security": CybersecurityExpertAgent(get_client("security")),
        "backend": BackendExpertAgent(get_client("backend")),
        "frontend": FrontendExpertAgent(get_client("frontend")),
        "qa": QAExpertAgent(get_client("qa")),
    }

    artifacts = {}
    for task_id in assignment.execution_order:
        task = next((t for t in assignment.tasks if t.id == task_id), None)
        if not task:
            continue

        # Git setup: skip (platform handles at API level) or log for CLI
        if task.type == TaskType.GIT_SETUP:
            logger.info(
                "=== Task %s (git_setup) - skipped in CLI (run via API with repo_path) ===", task.id
            )
            continue

        if task.assignee not in agent_map:
            continue

        logger.info("=== Task %s (%s) -> %s ===", task.id, task.type.value, task.assignee)
        agent = agent_map[task.assignee]

        if task.assignee == "devops":
            result = agent.run(
                DevOpsInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    architecture=architecture,
                )
            )
            logger.info("DevOps: %s", result.summary[:150] if result.summary else "Done")

        elif task.assignee == "backend":
            api_spec = _read_openapi_spec_from_repo(Path.cwd())
            result = agent.run(
                BackendInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    user_story=getattr(task, "user_story", "") or "",
                    architecture=architecture,
                    language="python",
                    api_spec=api_spec,
                )
            )
            logger.info("Backend: %s", result.summary[:150] if result.summary else "Done")
            artifacts["backend_code"] = result.code or ""
            if result.files:
                artifacts["backend_files"] = result.files

        elif task.assignee == "frontend":
            result = agent.run(
                FrontendInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    user_story=getattr(task, "user_story", "") or "",
                    architecture=architecture,
                )
            )
            logger.info("Frontend: %s", result.summary[:150] if result.summary else "Done")
            artifacts["frontend_code"] = result.code or ""
            if result.files:
                artifacts["frontend_files"] = result.files

        elif task.assignee == "security":
            code_to_review = "\n\n---BACKEND---\n\n" + artifacts.get("backend_code", "")
            code_to_review += "\n\n---FRONTEND---\n\n" + artifacts.get("frontend_code", "")
            code_to_review = code_to_review.strip() or "# No code yet"
            result = agent.run(
                SecurityInput(
                    code=code_to_review,
                    language="python",
                    task_description=task.description,
                    architecture=architecture,
                )
            )
            logger.info("Security: %s vulnerabilities", len(result.vulnerabilities))
            artifacts["security_fixed_code"] = result.fixed_code or code_to_review

        elif task.assignee == "qa":
            code_to_test = (
                artifacts.get("security_fixed_code")
                or artifacts.get("backend_code", "")
                or artifacts.get("frontend_code", "")
            )
            if not code_to_test.strip():
                code_to_test = "# No code to test"
            result = agent.run(
                QAInput(
                    code=code_to_test,
                    language="python",
                    task_description=task.description,
                    architecture=architecture,
                )
            )
            logger.info(
                "QA: %s bugs, integration tests: %s chars",
                len(result.bugs_found),
                len(result.integration_tests),
            )

    print("\n--- Team pipeline complete ---")
    print(
        "Architecture:",
        architecture.overview[:300] + "..."
        if len(architecture.overview) > 300
        else architecture.overview,
    )
    print("\nTasks executed:", assignment.execution_order)


if __name__ == "__main__":
    main()
