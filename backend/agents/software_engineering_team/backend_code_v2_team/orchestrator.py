"""
Backend-Code-V2 team orchestrator: 5-phase state machine.

Entry point used by the main orchestrator.
No code from ``backend_agent`` is imported or reused.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from llm_service import LLMClient
from shared_repo_context import read_repo_code_budgeted
from software_engineering_team.shared.git_utils import checkout_branch
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    copy_development_result_fields,
)
from software_engineering_team.shared.text_utils import has_section_header, toml_has_section
from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

from .models import (
    BackendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    Phase,
    ToolAgentKind,
)
from .phases.deliver import run_deliver
from .phases.execution import ReviewDependencies, run_execution_with_review_gates
from .phases.planning import run_planning
from .phases.setup import configure_quality_tooling, run_setup

logger = logging.getLogger(__name__)

# Backend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_BACKEND_REPO_EXTENSIONS = frozenset(
    {".py", ".java", ".kt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt"}
)
_BACKEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "__pycache__", "venv", ".venv"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_BACKEND_REPO_BRIEFING_MAX_CHARS = 30_000


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances (for plan/execute/review/problem_solve/deliver).

    The tool-agent imports are deferred to call time on purpose: each adapter
    pulls in heavy strands/llm_service machinery, and constructing them here at
    module import would make the orchestrator expensive to import (and would
    eagerly build agents even on paths that never run a workflow). Keeping them
    lazy bounds the import cost to actual runs, so they are not hoisted to the
    top of the module.
    """
    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.data_engineering import DataEngineeringToolAgent
    from .tool_agents.documentation import DocumentationToolAgent
    from .tool_agents.security import SecurityToolAgent
    from .tool_agents.testing_qa import TestingQAToolAgent

    return {
        ToolAgentKind.DATA_ENGINEERING: DataEngineeringToolAgent(llm),
        ToolAgentKind.API_OPENAPI: ApiOpenApiToolAgent(llm),
        ToolAgentKind.AUTH: AuthToolAgent(llm),
        ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        ToolAgentKind.BUILD_SPECIALIST: BuildSpecialistAdapterAgent(llm),
        ToolAgentKind.TESTING_QA: TestingQAToolAgent(llm),
        ToolAgentKind.SECURITY: SecurityToolAgent(llm),
        ToolAgentKind.DOCUMENTATION: DocumentationToolAgent(llm),
    }


class BackendDevelopmentAgent(BaseV2DevelopmentAgent):
    """
    Backend Development Agent: runs the 4-phase cycle (Planning → Execution →
    Documentation → Deliver) with per-microtask review gates embedded in the
    Execution phase. Used by BackendCodeV2TeamLead after it runs Setup.

    Inherits ``__init__`` / ``_build_tool_runners`` / ``_read_existing_code`` from
    :class:`BaseV2DevelopmentAgent`; supplies the backend tooling detection,
    repo-briefing sets, progress callback, and the integration-only ``run_workflow``.
    """

    @staticmethod
    def _build_progress_callback(update_job: Callable[..., None]) -> Callable[..., None]:
        """Build the per-microtask progress callback handed to the execution loop.

        Extracted from ``run_workflow`` so the phase-label mapping + progress
        math are unit-isolated from the workflow body and the closure no longer
        buries ~30 lines inside ``run_workflow``.

        Preconditions: ``update_job`` is the run_workflow job-update callable
          (forwards kwargs to the job updater; the run_workflow closure swallows
          its failures).
        Postconditions: returns a callback
          ``(current_index, done, total, title, microtask_phase, phase_detail)
          -> None`` that maps the microtask phase to a human label and reports
          progress (15..75%% of the job) via ``update_job``; never raises into
          the execution loop.
        """
        phase_labels = {
            "coding": "Writing code",
            "code_review": "Code review",
            "qa_testing": "QA testing",
            "security_testing": "Security testing",
            "documentation": "Documentation",
            "review": "Reviewing code",
            "problem_solving": "Fixing issues",
            "completed": "Completed",
        }

        def _progress_cb(
            current_index: int,
            done: int,
            total: int,
            title: str,
            microtask_phase: str = "coding",
            phase_detail: str = "",
        ) -> None:
            phase_label = phase_labels.get(
                microtask_phase, microtask_phase.replace("_", " ").title()
            )
            status = f"{phase_label}: {title} ({current_index}/{total})"
            if phase_detail:
                status = f"{status} — {phase_detail}"
            update_job(
                current_phase="execution",
                current_microtask=title,
                current_microtask_phase=microtask_phase,
                phase_detail=phase_detail,
                current_microtask_index=current_index,
                microtasks_completed=done,
                microtasks_total=total,
                progress=min(15 + int(done / max(total, 1) * 60), 75),
                status_text=status,
            )

        return _progress_cb

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = _BACKEND_REPO_BRIEFING_MAX_CHARS) -> str:
        """Read Python/Java source files from repo into a single string.

        Delegates to the shared budgeted scanner so every per-domain reader shares
        one implementation; the backend extension/exclude sets are the contract.
        """
        return read_repo_code_budgeted(
            repo_path,
            extensions=_BACKEND_REPO_EXTENSIONS,
            exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
            max_chars=max_chars,
        )

    @staticmethod
    def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
        """Return ``(has_lint, has_test)`` for the configured backend tooling.

        Detects ruff/flake8 (or a ``[tool.ruff]`` block in ``pyproject.toml``) as
        lint, and a ``tests`` dir with a pytest config (``pytest.ini`` or a
        ``[tool.pytest`` block in ``pyproject.toml``) as testing. Reads
        ``pyproject.toml`` once and reuses it for both probes. Lint also
        recognises a ``[flake8]`` section in ``setup.cfg`` — a common flake8
        config location that the file-name-only ``.flake8`` probe would miss.

        The ``[tool.ruff]`` / ``[tool.pytest`` pyproject checks use the shared
        ``toml_has_section`` helper: a real TOML parse (stdlib ``tomllib`` on
        Python 3.11+, the ``tomli`` backport if installed) that asks whether the
        table actually exists, so a section header appearing inside a
        multi-line string value can no longer produce a false positive; on
        Python 3.10 without ``tomli`` (or on unparseable TOML) it falls back to
        the line-anchored ``has_section_header`` text scan. The ``[flake8]``
        ``setup.cfg`` probe stays on ``has_section_header`` (INI has no
        multi-line strings, so the text scan is exact there). No hard dependency
        is added: 3.11+ stdlib covers the real runtime, and 3.10 keeps the prior
        best-effort text probe. The pre-flight only decides whether to fail the
        task early for missing tooling, so a residual false positive errs toward
        proceeding (a real build/lint gate still enforces correctness).

        Preconditions: ``repo_path`` is a directory.
        Postconditions: returns two booleans. Raises ``AssertionError`` if the
          precondition is violated (a non-directory ``repo_path`` is a caller
          bug, not a runtime failure mode this method recovers from).
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        pyproject_path = repo_path / "pyproject.toml"
        pyproject_text = (
            pyproject_path.read_text(encoding="utf-8", errors="replace")
            if pyproject_path.exists()
            else ""
        )
        setup_cfg_path = repo_path / "setup.cfg"
        setup_cfg_text = (
            setup_cfg_path.read_text(encoding="utf-8", errors="replace")
            if setup_cfg_path.exists()
            else ""
        )
        has_lint = (
            (repo_path / "ruff.toml").exists()
            or (repo_path / ".flake8").exists()
            or toml_has_section(pyproject_text, "[tool.ruff]")
            or has_section_header(setup_cfg_text, "[flake8]")
        )
        has_test = (repo_path / "tests").is_dir() and (
            (repo_path / "pytest.ini").exists() or toml_has_section(pyproject_text, "[tool.pytest")
        )
        return has_lint, has_test

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
        repo_context_cache: Optional[RepoContextCache] = None,
    ) -> BackendCodeV2WorkflowResult:
        """
        Execute the full 5-phase backend-code-v2 lifecycle with per-microtask review gates.

        Each microtask must pass full review (code quality, QA, security, build, lint)
        before the next microtask can begin.

        merge_to_development defaults to True. When False, the deliver phase commits
        a feature branch and leaves it ready for external Tech Lead review instead of
        merging it into the development branch.
        """
        self._repo_context_cache = repo_context_cache
        task_id = task.id
        start_time = time.monotonic()
        result = BackendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info(
            "[%s] WORKFLOW START: Backend Development Agent (per-microtask review gates)", task_id
        )

        # ── Check out the review feature branch FIRST, then ensure tooling ──
        # Setup commits lint/test scaffolding to ``development``, but a handoff
        # feature branch created before setup does not inherit it. Configure the
        # tooling on the branch we will actually edit so the pre-flight check and
        # later quality gates see the config (idempotent when already present).
        feature_branch_name = (task.feature_branch_name or "").strip() or None
        if feature_branch_name:
            ok, checkout_msg = checkout_branch(repo_path, feature_branch_name)
            if not ok:
                result.failure_reason = f"Feature branch checkout failed: {checkout_msg}"
                logger.error("[%s] %s", task_id, result.failure_reason)
                return result
            logger.info("[%s] Reusing existing feature branch: %s", task_id, feature_branch_name)
            configure_quality_tooling(repo_path)
            _update_job(
                current_phase="planning",
                progress=4,
                status_text=f"Branch {feature_branch_name} ready",
            )

        # ── Pre-flight: verify linting & testing are configured ───────
        # Runs after the feature-branch checkout so it validates the branch that
        # will actually be edited, not whatever branch setup last left checked out.
        has_lint, has_test = self._detect_tooling(repo_path)
        if not has_lint or not has_test:
            missing = []
            if not has_lint:
                missing.append("linting")
            if not has_test:
                missing.append("testing")
            logger.error(
                "[%s] Pre-flight check failed: %s not configured at %s",
                task_id,
                " and ".join(missing),
                repo_path,
            )
            result.failure_reason = (
                f"Pre-flight check failed: {' and '.join(missing)} not configured. "
                "The build process requires linting and testing to be set up before coding tasks begin."
            )
            return result
        logger.info("[%s] Pre-flight check passed: linting and testing configured", task_id)

        existing_code = self._read_existing_code(repo_path)
        tool_agents = _build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)

        # ── Phase 1: Planning ──────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase 1: Planning", task_id)
        result.current_phase = Phase.PLANNING
        _update_job(
            current_phase="planning",
            progress=5,
            status_text="Analyzing task and creating implementation plan",
        )

        try:
            planning_result = run_planning(
                llm=self.llm,
                task=task,
                repo_path=repo_path,
                architecture=architecture,
                existing_code=existing_code,
                tool_agents=tool_agents,
            )
            result.planning_result = planning_result
        except Exception as exc:
            result.failure_reason = f"Planning failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        total_microtasks = len(planning_result.microtasks)
        _update_job(
            current_phase="planning",
            progress=10,
            microtasks_total=total_microtasks,
            microtasks_completed=0,
            status_text=f"Plan created with {total_microtasks} microtasks",
        )

        # ── Create feature branch (Git agent) before first execution ───
        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        create_feature_branch_fn = (
            getattr(git_agent, "create_feature_branch", None) if git_agent is not None else None
        )
        if not feature_branch_name and callable(create_feature_branch_fn):
            _update_job(
                current_phase="planning", progress=12, status_text="Creating feature branch..."
            )
            try:
                ok, branch_name = create_feature_branch_fn(repo_path, task_id, task.title or "")
                if ok and branch_name:
                    feature_branch_name = branch_name
                    logger.info("[%s] Created feature branch: %s", task_id, feature_branch_name)
                    _update_job(
                        current_phase="planning",
                        progress=14,
                        status_text=f"Branch {feature_branch_name} ready",
                    )
                else:
                    logger.warning(
                        "[%s] Git agent create_feature_branch failed, deliver will create branch",
                        task_id,
                    )
            except Exception as exc:
                logger.warning("[%s] Git agent create_feature_branch raised: %s", task_id, exc)

        # ── Phase 2: Execution with per-microtask review gates ─────────
        logger.info("[%s] Next step -> Starting Phase 2: Execution", task_id)
        result.current_phase = Phase.EXECUTION
        _update_job(
            current_phase="execution",
            current_microtask="",
            progress=15,
            status_text="Starting code implementation",
        )

        progress_callback = self._build_progress_callback(_update_job)

        review_deps = ReviewDependencies(
            build_verifier=build_verifier,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            linting_tool_agent=linting_tool_agent,
            tool_agents=tool_agents,
        )

        config = review_config or MicrotaskReviewConfig()

        try:  # pragma: no cover  # integration-only: runs review-gated execution loop against live LLM + pytest/lint
            exec_result = run_execution_with_review_gates(
                llm=self.llm,
                task=task,
                planning_result=planning_result,
                repo_path=repo_path,
                architecture=architecture,
                existing_code=existing_code,
                tool_runners=tool_runners,
                progress_callback=progress_callback,
                review_config=config,
                review_deps=review_deps,
            )
            result.execution_result = exec_result
        except MicrotaskReviewFailedError as err:
            result.failure_reason = (
                f"Microtask {err.microtask.id} failed review: {err.review_result.summary}"
            )
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        except Exception as exc:
            result.failure_reason = f"Execution failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        current_files = exec_result.files
        if not current_files:
            result.failure_reason = "Execution produced no files."
            return result

        completed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.COMPLETED
        )
        failed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.REVIEW_FAILED
        )
        result.iterations_used = completed_count

        if (
            feature_branch_name
            and git_agent is not None
            and hasattr(git_agent, "commit_current_changes")
        ):
            try:
                git_agent.commit_current_changes(
                    repo_path, f"feat: {completed_count} microtasks completed"
                )
            except Exception as exc:
                logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

        result.final_files = current_files

        # ── Phase: Documentation ────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Documentation", task_id)
        result.current_phase = Phase.DOCUMENTATION
        _update_job(
            current_phase="documentation",
            progress=80,
            status_text="Generating documentation and API specs",
        )

        from .phases.documentation import run_documentation_phase

        try:
            doc_result = run_documentation_phase(
                llm=self.llm,
                task=task,
                repo_path=repo_path,
                execution_result=exec_result,
                planning_result=planning_result,
                tool_agents=tool_agents,
            )
            result.documentation_result = doc_result
            if doc_result.files:
                current_files.update(doc_result.files)
                result.final_files = current_files
            logger.info("[%s] Documentation phase complete: %s", task_id, doc_result.summary)
        except Exception as exc:
            logger.warning(
                "[%s] Documentation phase failed: %s. Next step -> Continuing to Deliver phase",
                task_id,
                exc,
            )

        # ── Phase: Deliver ───────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Deliver", task_id)
        result.current_phase = Phase.DELIVER
        _update_job(
            current_phase="deliver",
            progress=90,
            status_text="Committing changes and preparing delivery",
        )

        try:
            deliver_result = run_deliver(
                task_id=task_id,
                repo_path=repo_path,
                files=current_files,
                summary=exec_result.summary,
                task_title=task.title or "",
                tool_agents=tool_agents,
                task_description=task.description or "",
                feature_branch_name=feature_branch_name,
                merge_to_development=merge_to_development,
            )
            result.deliver_result = deliver_result
            delivered = (
                deliver_result.merged if merge_to_development else deliver_result.branch_ready
            )
            result.success = delivered and failed_count == 0
            result.summary = f"{exec_result.summary} {deliver_result.summary}"
            if failed_count > 0:
                result.needs_followup = True
                result.summary += f" ({failed_count} microtask(s) failed review)"
        except Exception as exc:
            result.failure_reason = f"Deliver failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        elapsed = time.monotonic() - start_time
        final_status = (
            "Backend task complete" if result.success else "Backend task completed with issues"
        )
        _update_job(
            current_phase="deliver",
            progress=100 if result.success else 95,
            status_text=final_status,
        )
        logger.info(
            "[%s] WORKFLOW %s in %.1fs (%d microtasks completed, %d failed review)",
            task_id,
            "SUCCEEDED" if result.success else "PARTIAL",
            elapsed,
            completed_count,
            failed_count,
        )
        return result


class BackendCodeV2TeamLead(BaseTeamLead):
    """
    Backend Tech Lead Agent: runs setup, verifies the repository, then executes
    the BackendDevelopmentAgent 5-phase workflow.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        super().__init__(
            llm_client,
            extensions=_BACKEND_REPO_EXTENSIONS,
            exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
            max_chars=_BACKEND_REPO_BRIEFING_MAX_CHARS,
        )

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
    ) -> BackendCodeV2WorkflowResult:
        """
        Run setup, verify lint/test readiness, then execute the backend 5-phase workflow.

        merge_to_development defaults to True. When False, delivery prepares a
        feature branch for external review instead of merging it.
        """
        task_id = task.id
        result = BackendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception as exc:
                    logger.debug("[%s] job_updater failed: %s", task_id, exc)

        # ── Setup phase (Backend Tech Lead) ─────────────────────────────
        result.current_phase = Phase.SETUP
        _update_job(
            current_phase="setup",
            progress=2,
            status_text="Setting up repository and development environment",
        )
        try:
            setup_result = run_setup(repo_path=repo_path, task_title=task.title or "")
            result.setup_result = setup_result
        except Exception as exc:
            result.failure_reason = f"Setup failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        _update_job(current_phase="setup", progress=3, status_text="Repository setup complete")

        # ── Verify linting and testing are configured ─────────────────
        if not getattr(setup_result, "linting_configured", False):
            logger.warning(
                "[%s] Linting not configured after setup — coding cannot proceed without linting",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but linting is not configured. "
                "Linting must be set up before any coding tasks can begin."
            )
            return result

        if not getattr(setup_result, "testing_configured", False):
            logger.warning(
                "[%s] Testing not configured after setup — coding cannot proceed without testing",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but testing is not configured. "
                "Testing must be set up before any coding tasks can begin."
            )
            return result

        logger.info("[%s] Linting and testing verified — proceeding to coding phase", task_id)
        _update_job(
            current_phase="setup",
            progress=5,
            status_text="Linting and testing verified; ready for development",
        )

        # ── Delegate to Backend Development Agent ──────────────────────
        dev_agent = BackendDevelopmentAgent(self.llm)
        inner = dev_agent.run_workflow(
            repo_path=repo_path,
            task=task,
            architecture=architecture,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
            repo_context_cache=self._repo_context_cache_for(repo_path),
        )
        copy_development_result_fields(result, inner)
        return result
