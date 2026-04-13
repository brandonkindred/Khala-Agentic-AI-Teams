"""DevOps Expert agent: CI/CD, IaC, Docker, networking."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from strands import Agent

from llm_service import compact_text, get_client, get_strands_model
from software_engineering_team.shared.prompt_utils import log_llm_prompt
from software_engineering_team.shared.repo_utils import int_env as _int_env
from software_engineering_team.shared.task_plan import TaskPlan

from .models import DevOpsInput, DevOpsOutput, DevOpsWorkflowResult
from .prompts import DEVOPS_PLANNING_PROMPT, DEVOPS_PROMPT

logger = logging.getLogger(__name__)


MAX_WORKFLOW_ITERATIONS = 100
MAX_SAME_BUILD_FAILURES = _int_env("SW_MAX_SAME_BUILD_FAILURES", 6)


def _build_error_signature(build_errors: str) -> str:
    """Compute a signature for same-error detection. Uses first 800 chars."""
    return (build_errors[:800] or build_errors).strip()


def _gather_codebase_context(repo_path: Path, subdir: str = "") -> str:
    """
    Gather codebase context for planning: dependencies, existing CI, entry points.
    Uses repo_path as the app root (not repo_path/subdir) since subdir is where
    we write devops files; the app to containerize lives at repo_path.
    Returns a string to include in the planning prompt.
    """
    path = Path(repo_path).resolve()
    parts: List[str] = []

    # Python: requirements.txt, pyproject.toml
    for name in ("requirements.txt", "pyproject.toml"):
        f = path / name
        if f.exists() and f.is_file():
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                parts.append(f"**{name}:**\n```\n{content[:2000]}\n```")
            except (OSError, UnicodeDecodeError) as e:
                logger.debug("Could not read %s: %s", f, e)

    # Node: package.json
    pkg = path / "package.json"
    if pkg.exists() and pkg.is_file():
        try:
            content = pkg.read_text(encoding="utf-8", errors="replace")
            parts.append(f"**package.json:**\n```\n{content[:2000]}\n```")
        except (OSError, UnicodeDecodeError) as e:
            logger.debug("Could not read %s: %s", pkg, e)

    # Existing CI/CD
    workflows_dir = path / ".github" / "workflows"
    if workflows_dir.exists() and workflows_dir.is_dir():
        for wf in workflows_dir.glob("*.yml"):
            if wf.is_file():
                try:
                    content = wf.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"**Existing {wf.name}:**\n```yaml\n{content[:1500]}\n```")
                except (OSError, UnicodeDecodeError) as e:
                    logger.debug("Could not read workflow %s: %s", wf, e)
        for wf in workflows_dir.glob("*.yaml"):
            if wf.is_file():
                try:
                    content = wf.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"**Existing {wf.name}:**\n```yaml\n{content[:1500]}\n```")
                except (OSError, UnicodeDecodeError) as e:
                    logger.debug("Could not read workflow %s: %s", wf, e)

    # Main entry (Python)
    for main in ("main.py", "app/main.py"):
        m = path / main
        if m.exists() and m.is_file():
            try:
                content = m.read_text(encoding="utf-8", errors="replace")
                parts.append(f"**{main} (entry):**\n```\n{content[:800]}\n```")
            except (OSError, UnicodeDecodeError) as e:
                logger.debug("Could not read %s: %s", m, e)
            break

    if not parts:
        return ""
    return "\n\n".join(["**Codebase context (use to inform Dockerfile and CI):**", ""] + parts)


def _validate_devops_output(result: DevOpsOutput) -> Tuple[bool, List[str]]:
    """
    Validate DevOps output before writing. Returns (valid, list of error messages).
    - Dockerfile must have FROM and (CMD or ENTRYPOINT)
    - YAML (pipeline, docker_compose) must parse correctly
    - CI workflow must have name, on, jobs
    - Reject empty/stub outputs
    """
    errors: List[str] = []
    has_output = False

    # Check we have at least one meaningful output
    if result.dockerfile and result.dockerfile.strip():
        has_output = True
        df = result.dockerfile.strip()
        if "FROM" not in df.upper():
            errors.append("Dockerfile must contain a FROM instruction")
        if "CMD" not in df.upper() and "ENTRYPOINT" not in df.upper():
            errors.append("Dockerfile must contain CMD or ENTRYPOINT")
    if result.pipeline_yaml and result.pipeline_yaml.strip():
        has_output = True
        try:
            import yaml

            data = yaml.safe_load(result.pipeline_yaml)
            if data is None:
                errors.append("Pipeline YAML parsed as empty")
            elif isinstance(data, dict) and "jobs" not in data:
                errors.append("CI workflow must have 'jobs' key")
        except Exception as e:
            errors.append(f"Pipeline YAML parse error: {e}")
    if result.docker_compose and result.docker_compose.strip():
        has_output = True
        try:
            import yaml

            data = yaml.safe_load(result.docker_compose)
            if data is not None and isinstance(data, dict) and "services" not in data:
                errors.append("docker-compose.yml should have 'services' key")
        except Exception as e:
            errors.append(f"docker-compose YAML parse error: {e}")
    if result.iac_content and result.iac_content.strip():
        has_output = True
    if result.artifacts:
        has_output = True
        for path, content in result.artifacts.items():
            if content and content.strip():
                if path.endswith((".yml", ".yaml")):
                    try:
                        import yaml

                        yaml.safe_load(content)
                    except Exception as e:
                        errors.append(f"Artifact {path} YAML parse error: {e}")
                break

    if not has_output:
        errors.append(
            "No files to write: all outputs (dockerfile, pipeline_yaml, docker_compose, iac_content, artifacts) are empty"
        )

    return len(errors) == 0, errors


class DevOpsExpertAgent:
    """
    DevOps expert specializing in CI/CD pipelines, IaC, Dockerization, and networking.
    """

    def __init__(self, llm_client=None) -> None:
        from strands.models.model import Model as _StrandsModel
        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("devops")
        # Keep LLMClient for context_sizing / compact_text utilities
        self.llm = llm_client if llm_client is not None else get_client("devops")

    def _plan_task(
        self,
        *,
        task_description: str,
        requirements: str,
        architecture: Optional[Any] = None,
        existing_pipeline: Optional[str] = None,
        target_repo: Optional[Any] = None,
        repo_path: Optional[Path] = None,
        subdir: str = "",
    ) -> str:
        """Produce an implementation plan for the DevOps task. Returns plan markdown or empty string on failure."""
        context_parts = [
            f"**Task:** {task_description}",
            f"**Requirements:** {requirements}",
        ]
        if architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    getattr(architecture, "overview", str(architecture)),
                    *[
                        f"- {c.name} ({c.type}): {getattr(c, 'technology', None) or 'TBD'}"
                        for c in getattr(architecture, "components", [])
                    ],
                ]
            )
        if existing_pipeline:
            context_parts.extend(["", "**Existing Pipeline:**", existing_pipeline])
        if target_repo is not None:
            repo_val = target_repo.value if hasattr(target_repo, "value") else target_repo
            context_parts.extend(
                [
                    "",
                    "**Target repo:**",
                    f"target_repo={repo_val}",
                ]
            )
        # Codebase context: dependencies, entry points, existing CI
        if repo_path:
            codebase_ctx = _gather_codebase_context(repo_path, subdir)
            if codebase_ctx:
                context_parts.extend(["", codebase_ctx])
        context = "\n".join(context_parts)
        prompt = context
        log_llm_prompt(logger, "DevOps", "planning", (task_description or "")[:80], prompt)
        try:
            agent = Agent(model=self._model, system_prompt=DEVOPS_PLANNING_PROMPT)
            result = agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
            plan = TaskPlan.from_llm_json(data)
            return plan.to_markdown()
        except Exception as e:
            logger.warning("DevOps planning step failed, proceeding without plan: %s", e)
            return ""

    def run(self, input_data: DevOpsInput) -> DevOpsOutput:
        """Create or extend CI/CD, IaC, and Docker configurations."""
        logger.info(
            "DevOps: starting task '%s'",
            input_data.task_description[:60]
            + ("..." if len(input_data.task_description) > 60 else ""),
        )
        context_parts = [
            "Generate DevOps / pipeline / infrastructure artifacts for this task.",
            f"**Task:** {input_data.task_description}",
            f"**Requirements:** {input_data.requirements}",
        ]
        if input_data.architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture:**",
                    input_data.architecture.overview,
                    *[
                        f"- {c.name} ({c.type}): {c.technology or 'TBD'}"
                        for c in input_data.architecture.components
                    ],
                ]
            )
        if input_data.existing_pipeline:
            context_parts.extend(["", "**Existing Pipeline:**", input_data.existing_pipeline])
        if input_data.tech_stack:
            context_parts.extend(["", "**Tech Stack:**", ", ".join(input_data.tech_stack)])
        if getattr(input_data, "target_repo", None):
            repo_val = (
                input_data.target_repo.value
                if hasattr(input_data.target_repo, "value")
                else input_data.target_repo
            )
            context_parts.extend(
                [
                    "",
                    "**Target repo:** You are producing containerization and deployment artifacts for this application repo only.",
                    f"- target_repo={repo_val}",
                ]
            )
        if getattr(input_data, "task_plan", None) and input_data.task_plan:
            context_parts.extend(["", "**Implementation plan:**", input_data.task_plan])
        if getattr(input_data, "build_errors", None) and input_data.build_errors:
            from software_engineering_team.shared.error_parsing import (
                build_agent_feedback,
                parse_devops_failure,
            )

            raw_errors = input_data.build_errors
            # Pre-write validation errors are already structured; use as-is
            if raw_errors.startswith("Pre-write validation failed:"):
                errors_text = raw_errors
            else:
                # Use structured parsing for build/verify errors (Docker, YAML) for better feedback
                failures = parse_devops_failure(raw_errors)
                feedback = build_agent_feedback(failures)
                errors_text = feedback if feedback else raw_errors
            context_parts.extend(
                [
                    "",
                    "**Build/validation errors to fix:** The previous output failed verification. Fix these issues:",
                    errors_text,
                ]
            )

        prompt = "\n".join(context_parts)
        agent = Agent(model=self._model, system_prompt=DEVOPS_PROMPT)
        result = agent(prompt)
        raw = str(result).strip()
        data = json.loads(raw)

        summary = data.get("summary", "")
        needs_clarification = bool(data.get("needs_clarification", False))
        clarification_requests = data.get("clarification_requests") or []
        if not isinstance(clarification_requests, list):
            clarification_requests = [str(clarification_requests)] if clarification_requests else []

        logger.info(
            "DevOps: done, summary=%s chars, needs_clarification=%s",
            len(summary),
            needs_clarification,
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
        devops_review_agent: Optional[Any] = None,
    ) -> DevOpsWorkflowResult:
        """
        Execute the DevOps workflow: plan -> generate -> write -> verify -> fix loop.

        No feature branch; writes directly to repo_path. On verification failure,
        re-generates with build_errors and retries up to max_iterations.
        """
        from software_engineering_team.shared.context_sizing import compute_build_errors_chars
        from software_engineering_team.shared.repo_writer import (
            NO_FILES_TO_WRITE_MSG,
            write_agent_output,
        )

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

        # Step 1: Plan (with codebase context when repo_path available)
        plan_text = self._plan_task(
            task_description=task_description,
            requirements=requirements,
            architecture=architecture,
            existing_pipeline=existing_pipeline,
            target_repo=target_repo.value
            if target_repo and hasattr(target_repo, "value")
            else (target_repo or None),
            repo_path=path,
            subdir=subdir,
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

        # Steps 2-5: Generate -> Validate -> Write -> Verify loop
        last_build_error_sig: Optional[str] = None
        consecutive_same_build_failures = 0
        build_errors: str = ""

        for iteration in range(1, max_iterations + 1):
            logger.info(
                "DevOps WORKFLOW: iteration %d/%d. Next step -> Generating DevOps configuration",
                iteration,
                max_iterations,
            )
            # Generate (with build_errors from previous iteration if any)
            result = self.run(
                DevOpsInput(
                    task_description=task_description,
                    requirements=requirements,
                    architecture=architecture,
                    existing_pipeline=existing_pipeline,
                    tech_stack=tech_stack,
                    target_repo=target_repo,
                    task_plan=plan_text if plan_text else None,
                    build_errors=compact_text(
                        build_errors, compute_build_errors_chars(self.llm), self.llm, "build errors"
                    )
                    if build_errors
                    else None,
                )
            )
            if result.needs_clarification and result.clarification_requests:
                return DevOpsWorkflowResult(
                    success=False,
                    failure_reason=f"Clarification requested: {result.clarification_requests[0][:200]}",
                    iterations=iteration,
                )

            # Pre-write validation
            valid, validation_errors = _validate_devops_output(result)
            if not valid:
                build_errors = "Pre-write validation failed:\n" + "\n".join(validation_errors)
                build_error_sig = _build_error_signature(build_errors)
                if build_error_sig == last_build_error_sig:
                    consecutive_same_build_failures += 1
                else:
                    last_build_error_sig = build_error_sig
                    consecutive_same_build_failures = 1
                if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                    repeated_reason = (
                        f"Validation failed {MAX_SAME_BUILD_FAILURES} times with the same errors; "
                        "stopping to avoid loop."
                    )
                    logger.error(
                        "DevOps WORKFLOW: Recovery summary: 1) Attempted %d iterations, "
                        "2) Same validation error repeated %d times. %s",
                        iteration,
                        consecutive_same_build_failures,
                        repeated_reason,
                    )
                    return DevOpsWorkflowResult(
                        success=False,
                        failure_reason=repeated_reason + " " + build_errors[:500],
                        iterations=iteration,
                    )
                logger.warning(
                    "DevOps WORKFLOW: iteration %d/%d validation failed. Next step -> Re-generating with error context",
                    iteration,
                    max_iterations,
                )
                continue

            # Write
            ok, write_msg = write_agent_output(path, result, subdir=subdir)
            if not ok:
                return DevOpsWorkflowResult(
                    success=False,
                    failure_reason=write_msg or NO_FILES_TO_WRITE_MSG,
                    iterations=iteration,
                )

            # DevOps review (optional) - catch issues before build verification
            if devops_review_agent:
                from devops_review_agent import DevOpsReviewInput

                review_input = DevOpsReviewInput(
                    dockerfile=result.dockerfile,
                    pipeline_yaml=result.pipeline_yaml,
                    docker_compose=result.docker_compose,
                    iac_content=result.iac_content,
                    task_description=task_description,
                    requirements=requirements,
                    target_repo=target_repo.value
                    if target_repo and hasattr(target_repo, "value")
                    else str(target_repo)
                    if target_repo
                    else None,
                )
                review_output = devops_review_agent.run(review_input)
                if not review_output.approved:
                    critical_major = [
                        i for i in review_output.issues if i.severity in ("critical", "major")
                    ]
                    if critical_major:
                        build_errors = "DevOps review failed:\n" + "\n".join(
                            f"- [{i.artifact}] {i.description}\n  Suggestion: {i.suggestion}"
                            for i in critical_major[:5]
                        )
                        build_error_sig = _build_error_signature(build_errors)
                        if build_error_sig == last_build_error_sig:
                            consecutive_same_build_failures += 1
                        else:
                            last_build_error_sig = build_error_sig
                            consecutive_same_build_failures = 1
                        if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                            repeated_reason = f"DevOps review failed {MAX_SAME_BUILD_FAILURES} times with same issues; stopping."
                            logger.error("DevOps WORKFLOW: %s", repeated_reason)
                            return DevOpsWorkflowResult(
                                success=False,
                                failure_reason=repeated_reason + " " + build_errors[:500],
                                iterations=iteration,
                            )
                        logger.warning(
                            "DevOps WORKFLOW: iteration %d/%d review failed, re-generating",
                            iteration,
                            max_iterations,
                        )
                        continue

            # Verify
            build_ok, build_errors = build_verifier(path_for_verify, "devops", task_id)
            if build_ok:
                logger.info("DevOps WORKFLOW: verification passed after %d iteration(s)", iteration)
                return DevOpsWorkflowResult(success=True, iterations=iteration)

            # Stop if the same build error repeats (avoids infinite loop on env/config issues)
            build_error_sig = _build_error_signature(build_errors)
            if build_error_sig == last_build_error_sig:
                consecutive_same_build_failures += 1
            else:
                last_build_error_sig = build_error_sig
                consecutive_same_build_failures = 1
            if consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES:
                repeated_reason = (
                    f"Build failed {MAX_SAME_BUILD_FAILURES} times with the same error; "
                    "stopping to avoid loop. Last error: " + build_errors[:800]
                )
                logger.error("DevOps WORKFLOW: %s", repeated_reason[:800])
                return DevOpsWorkflowResult(
                    success=False,
                    failure_reason=repeated_reason,
                    iterations=iteration,
                )

            logger.warning(
                "DevOps WORKFLOW: iteration %d/%d verification failed, re-generating with errors",
                iteration,
                max_iterations,
            )

        return DevOpsWorkflowResult(
            success=False,
            failure_reason=f"Verification failed after {max_iterations} iterations",
            iterations=max_iterations,
        )
