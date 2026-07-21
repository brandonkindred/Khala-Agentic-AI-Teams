"""
Shared base for the code-v2 Development Agents (backend + frontend).

``BackendDevelopmentAgent`` and ``FrontendDevelopmentAgent`` share their
constructor, their repo-briefing read (including the incremental
:class:`~software_engineering_team.shared.repo_context_cache.RepoContextCache`
fast path), and their tool-runner construction verbatim; only the per-team
tool-agent roster, tooling detection, repo extension/exclude sets, and the
integration-only ``run_workflow`` differ. This base holds the shared members;
each team subclasses it and supplies the divergent parts.

The bulk-divergent ``run_workflow`` bodies deliberately stay per-team: they are
``# pragma: no cover`` integration code carrying ~100 lines of team-specific
status/progress/result wiring, so converging them safely is a separate,
test-guarded change rather than part of this base.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.tool_agent_runners import build_tool_runners


class BaseV2DevelopmentAgent:
    """Shared base for the code-v2 Development Agents.

    Subclasses provide the per-team ``_read_repo_code`` (extension/exclude sets +
    briefing budget), ``_detect_tooling``, tool-agent roster, and ``run_workflow``.

    Invariants: instance state is limited to ``llm`` and ``_repo_context_cache``,
    so a subclass built via ``__new__`` and given those two attributes behaves
    identically to a constructed one.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        # Optional incremental repo-context cache threaded in by the team lead so
        # the per-task briefing re-reads only changed files instead of re-walking
        # the whole repo. None (the direct-construction/test path) falls back to
        # the fresh-walk ``_read_repo_code``.
        self._repo_context_cache: Optional[RepoContextCache] = None

    def _build_tool_runners(self, tool_agents: Dict[Any, Any]) -> Dict[Any, Callable[..., Any]]:
        """Build run callables from tool agent instances (for the Execution phase)."""
        return build_tool_runners(tool_agents)

    @staticmethod
    def _build_progress_callback(
        update_job: Callable[..., None], *, review_label: str = "Reviewing code"
    ) -> Callable[..., None]:
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
          the execution loop. The ``"review"`` phase label is ``review_label``
          (backend and frontend differ here — ``"Reviewing code"`` vs.
          ``"Reviewing"`` — so callers parameterize it rather than losing it).
        """
        phase_labels = {
            "coding": "Writing code",
            "code_review": "Code review",
            "qa_testing": "QA testing",
            "security_testing": "Security testing",
            "documentation": "Documentation",
            "review": review_label,
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
    def _run_preflight(
        *,
        task_id: str,
        repo_path: Path,
        feature_branch_name: Optional[str],
        detect_tooling: Callable[[Path], Tuple[bool, bool]],
        checkout_branch: Callable[[Path, str], Tuple[bool, str]],
        configure_quality_tooling: Callable[[Path], Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        emit_branch_ready_progress: bool = False,
    ) -> Optional[str]:
        """Check out the feature branch (if any) and verify lint/test tooling.

        Extracted from ``run_workflow`` so the branch-checkout + tooling-dispatch +
        missing-tooling failure-result block is defined once; ``detect_tooling``
        stays genuinely team-specific and is passed in rather than shared.

        Preconditions: ``repo_path`` is an existing directory. ``checkout_branch``
          and ``configure_quality_tooling`` are the caller module's own
          (monkeypatch-patchable) names, not imported fresh here, so tests that
          patch a team's orchestrator module keep working unchanged.
        Postconditions: returns ``None`` when checkout (if any) succeeded and
          both lint and test tooling are detected; otherwise returns the
          failure-reason string the caller should set on its result and return
          early with (already logged via ``logger.error``). Never raises.
          ``emit_branch_ready_progress`` controls whether a "Branch ... ready"
          progress update fires after a successful checkout — backend emits it,
          frontend does not, and this preserves that one real behavioral
          difference between the two teams explicitly rather than losing it.
        """
        if feature_branch_name:
            ok, checkout_msg = checkout_branch(repo_path, feature_branch_name)
            if not ok:
                failure_reason = f"Feature branch checkout failed: {checkout_msg}"
                logger.error("[%s] %s", task_id, failure_reason)
                return failure_reason
            logger.info("[%s] Reusing existing feature branch: %s", task_id, feature_branch_name)
            configure_quality_tooling(repo_path)
            if emit_branch_ready_progress:
                update_job(
                    current_phase="planning",
                    progress=4,
                    status_text=f"Branch {feature_branch_name} ready",
                )

        # ── Pre-flight: verify linting & testing are configured ───────
        # Runs after the feature-branch checkout so it validates the branch that
        # will actually be edited, not whatever branch setup last left checked out.
        has_lint, has_test = detect_tooling(repo_path)
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
            return (
                f"Pre-flight check failed: {' and '.join(missing)} not configured. "
                "The build process requires linting and testing to be set up before coding tasks begin."
            )
        logger.info("[%s] Pre-flight check passed: linting and testing configured", task_id)
        return None

    def _read_repo_code(self, repo_path: Path, max_chars: Optional[int] = None) -> str:
        """Per-team repo briefing reader.

        Subclasses override this (as a ``@staticmethod``) with their own extension
        / exclude sets and default briefing budget; the base only declares the
        contract that :meth:`_read_existing_code` depends on.
        """
        raise NotImplementedError  # pragma: no cover - always overridden

    def _read_existing_code(self, repo_path: Path) -> str:
        """Return the repo briefing, consulting the incremental cache when one is threaded in.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns a briefing byte-identical to
          ``_read_repo_code(repo_path)`` for the current on-disk state; when a
          cache is present it re-reads only changed eligible files. Raises
          ``AssertionError`` if the precondition is violated (caller bug).
        Invariants: with no cache the fresh walk runs each call; with a cache the
          output never differs from the fresh walk, only the amount of file I/O.

        The no-cache branch calls ``_read_repo_code(repo_path)`` with no kwargs
        deliberately: callers (and tests) monkeypatch it with a no-kwargs
        signature, and the cache carries its own char budget, so forwarding one
        here would both break that patch surface and be ignored.
        """
        assert repo_path.is_dir(), "repo_path must be an existing directory"
        if self._repo_context_cache is not None:
            return self._repo_context_cache.read(repo_path)
        return self._read_repo_code(repo_path)
