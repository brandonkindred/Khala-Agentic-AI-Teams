"""DevOps Expert agent: CI/CD, IaC, Docker, networking."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from shared.llm import LLMClient
from shared.prompt_utils import log_llm_prompt
from shared.task_plan import TaskPlan

from .models import DevOpsInput, DevOpsOutput, DevOpsWorkflowResult
from .prompts import DEVOPS_PLANNING_PROMPT, DEVOPS_PROMPT

logger = logging.getLogger(__name__)

MAX_WORKFLOW_ITERATIONS = 20


class DevOpsExpertAgent:
    """
    DevOps expert specializing in CI/CD pipelines, IaC, Dockerization, and networking.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: DevOpsInput) -> DevOpsOutput:
        """Create or extend CI/CD, IaC, and Docker configurations."""
        logger.info("DevOps: starting task '%s'", input_data.task_description[:60] + ("..." if len(input_data.task_description) > 60 else ""))
        context_parts = [
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
        ]
        if input_data.architecture:
            context_parts.extend([
                "",
                "**Architecture:**",
                input_data.architecture.overview,
                *[f"- {c.name} ({c.type}): {c.technology or 'TBD'}" for c in input_data.architecture.components],
            ])
        if input_data.existing_pipeline:
            context_parts.extend(["", "**Existing Pipeline:**", input_data.existing_pipeline])
        if input_data.tech_stack:
            context_parts.extend(["", "**Tech Stack:**", ", ".join(input_data.tech_stack)])
        if getattr(input_data, "target_repo", None):
            repo_val = input_data.target_repo.value if hasattr(input_data.target_repo, "value") else input_data.target_repo
            context_parts.extend([
                "",
                "**Target repo:** You are producing containerization and deployment artifacts for this application repo only.",
                f"- target_repo={repo_val}",
            ])
        if getattr(input_data, "task_plan", None) and input_data.task_plan:
            context_parts.extend(["", "**Implementation plan:**", input_data.task_plan])
        if getattr(input_data, "build_errors", None) and input_data.build_errors:
            context_parts.extend([
                "",
                "**Build/validation errors to fix:** The previous output failed verification. Fix these issues:",
                input_data.build_errors[:6000],
            ])

        prompt = DEVOPS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "DevOps: done, summary=%s chars, needs_clarification=%s",
            len(summary), needs_clarification,
        )
        return DevOpsOutput(
            pipeline_yaml=data.get("pipeline_yaml", ""),
            iac_content=data.get("iac_content", ""),
            dockerfile=data.get("dockerfile", ""),
            docker_compose=data.get("docker_compose", ""),
            summary=summary,
            artifacts=data.get("artifacts", {}),
            suggested_commit_message=data.get("suggested_commit_message", ""),
            needs_clarification=needs_clarification,
            clarification_requests=clarification_requests,
        )

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task_description: str,
        requirements: str,
        architecture: Optional[Any] = None,
        existing_pipeline: Optional[str] = None,
        target_repo: Optional[Any] = None,
        tech_stack: Optional[list[str]] = None,
        build_verifier: Callable[..., Tuple[bool, str]],
        task_id: str = "devops",
        subdir: str = "",
        max_iterations: int = MAX_WORKFLOW_ITERATIONS,
    ) -> DevOpsWorkflowResult:
        """
        Execute the DevOps workflow: plan -> generate -> write -> verify -> fix loop.

        No feature branch; writes directly to repo_path. On verification failure,
        re-generates with build_errors and retries up to max_iterations.
        """
        from shared.repo_writer import write_agent_output, NO_FILES_TO_WRITE_MSG

        path = Path(repo_path).resolve()
        if subdir:
            path_for_verify = path / subdir
        else:
            path_for_verify = path

        logger.info(
            "DevOps WORKFLOW: starting for task_id=%s, path=%s, subdir=%s",
            task_id,
            path,
            subdir or "(root)",
        )

        # Step 1: Plan
        plan_text = self._plan_task(
            task_description=task_description,
            requirements=requirements,
            architecture=architecture,
            existing_pipeline=existing_pipeline,
            target_repo=target_repo.value if target_repo and hasattr(target_repo, "value") else (target_repo or None),
        )
        if plan_text:
            plan_dir = path / "plan"
            if plan_dir.exists() and plan_dir.is_dir():
                try:
                    plan_file = plan_dir / f"devops_{task_id}.md"
                    plan_file.write_text(
                        f"# DevOps task plan: {task_id}\n\n{plan_text}",
                        encoding="utf-8",
                    )
                    logger.info("DevOps WORKFLOW: persisted plan to %s", plan_file)
                except Exception as e:
                    logger.warning("DevOps: failed to persist plan (non-blocking): %s", e)

        # Step 2: Generate initial output
        result = self.run(
            DevOpsInput(
                task_description=task_description,
                requirements=requirements,
                architecture=architecture,
                existing_pipeline=existing_pipeline,
                tech_stack=tech_stack,
                target_repo=target_repo,
                task_plan=plan_text if plan_text else None,
            )
        )
        if result.needs_clarification and result.clarification_requests:
            return DevOpsWorkflowResult(
                success=False,
                failure_reason=f"Clarification requested: {result.clarification_requests[0][:200]}",
                iterations=1,
            )

        # Step 3: Write
        ok, write_msg = write_agent_output(path, result, subdir=subdir)
        if not ok:
            return DevOpsWorkflowResult(
                success=False,
                failure_reason=write_msg or NO_FILES_TO_WRITE_MSG,
                iterations=1,
            )

        # Steps 4-5: Verify and fix loop
        for iteration in range(1, max_iterations + 1):
            build_ok, build_errors = build_verifier(path_for_verify, "devops", task_id)
            if build_ok:
                logger.info("DevOps WORKFLOW: verification passed after %d iteration(s)", iteration)
                return DevOpsWorkflowResult(success=True, iterations=iteration)

            # Fix pass
            logger.warning(
                "DevOps WORKFLOW: iteration %d/%d verification failed, re-generating with errors",
                iteration,
                max_iterations,
            )
            result = self.run(
                DevOpsInput(
                    task_description=task_description,
                    requirements=requirements,
                    architecture=architecture,
                    existing_pipeline=existing_pipeline,
                    tech_stack=tech_stack,
                    target_repo=target_repo,
                    task_plan=plan_text if plan_text else None,
                    build_errors=build_errors[:6000],
                )
            )
            if result.needs_clarification and result.clarification_requests:
                return DevOpsWorkflowResult(
                    success=False,
                    failure_reason=f"Clarification requested during fix: {result.clarification_requests[0][:200]}",
                    iterations=iteration + 1,
                )
            ok, write_msg = write_agent_output(path, result, subdir=subdir)
            if not ok:
                return DevOpsWorkflowResult(
                    success=False,
                    failure_reason=f"Write failed on fix iteration: {write_msg}",
                    iterations=iteration + 1,
                )

        return DevOpsWorkflowResult(
            success=False,
            failure_reason=f"Verification failed after {max_iterations} iterations",
            iterations=max_iterations,
        )
