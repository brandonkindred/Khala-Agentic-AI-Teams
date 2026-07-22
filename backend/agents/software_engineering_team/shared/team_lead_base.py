"""
Shared base for the code-v2 Team Leads (backend + frontend).

``BackendCodeV2TeamLead`` and ``FrontendCodeV2TeamLead`` share their
constructor, their per-repo incremental briefing cache lookup
(:meth:`BaseTeamLead._repo_context_cache_for`), the field-copy tail that
overlays their inner ``*DevelopmentAgent`` result onto their own result
object, and the setup → lint/test-gate → delegate sequence
(:meth:`BaseTeamLead._run_setup_and_delegate`). This base also provides an
optional per-run status/progress callback via
:meth:`BaseTeamLead._report_status` and a gate-based phase-sequencing helper
via :meth:`BaseTeamLead._run_gated_phases`.

Each team subclasses this base and supplies a thin ``run_workflow`` that
passes its module-level ``run_setup``, ``*DevelopmentAgent``, and
``*WorkflowResult`` into the shared helper. Those names are looked up in the
subclass orchestrator module at call time so tests can monkeypatch
``orchestrator.run_setup`` / ``orchestrator.*DevelopmentAgent`` as module-level
attributes (see ``test_team_lead_propagates_development_handoff_fields``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Sequence, Tuple, TypeVar

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.v2_models import Phase

logger = logging.getLogger(__name__)

# The 13 fields a code-v2 development-agent result hands off to its team
# lead's own result object. ``setup_result`` is deliberately excluded: it is
# the team lead's own setup phase (run before delegating), and copying the
# inner development agent's (always-empty) setup_result would clobber it.
_DEVELOPMENT_RESULT_FIELDS = (
    "success",
    "current_phase",
    "iterations_used",
    "planning_result",
    "execution_result",
    "review_result",
    "problem_solving_result",
    "documentation_result",
    "deliver_result",
    "final_files",
    "summary",
    "failure_reason",
    "needs_followup",
)

T = TypeVar("T")


def copy_development_result_fields(dst: Any, src: Any) -> None:
    """Overlay the shared development-handoff fields from ``src`` onto ``dst``.

    Preconditions: ``dst`` and ``src`` each expose every attribute named in
      ``_DEVELOPMENT_RESULT_FIELDS`` (both backend and frontend
      ``*CodeV2WorkflowResult`` models do, despite being distinct classes).
    Postconditions: each of the 13 shared fields on ``dst`` equals the
      corresponding field on ``src``; every other attribute of ``dst``
      (notably ``setup_result``) is left untouched.
    """
    for field in _DEVELOPMENT_RESULT_FIELDS:
        setattr(dst, field, getattr(src, field))


class BaseTeamLead:
    """Shared base for the code-v2 Team Leads.

    Subclasses provide the per-team repo-briefing filter constants (via
    ``__init__``) and a thin ``run_workflow`` that delegates to
    :meth:`_run_setup_and_delegate` with late-bound module globals.

    Invariants: instance state is limited to ``llm``, the injected
    extensions/exclude_dirs/max_chars, ``_repo_context_caches``, and
    ``_status_callback`` (optional per-run status hook; default None).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        extensions: FrozenSet[str],
        exclude_dirs: FrozenSet[str],
        max_chars: int,
    ) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._extensions = extensions
        self._exclude_dirs = exclude_dirs
        self._max_chars = max_chars
        # Per-repo incremental briefing cache, reused across the team lead's
        # run_workflow calls (the coding-team worker reuses one team lead for all
        # tasks in a job), so the N tasks of a job re-read only the files each
        # merge touched instead of re-walking the whole repo N times.
        self._repo_context_caches: Dict[Path, RepoContextCache] = {}
        # Optional per-run status/progress callback. Subclasses may assign this at
        # the start of run_workflow (and clear it when the run ends); BaseTeamLead
        # does not accept it via the constructor. Defaults to None (no-op hook).
        self._status_callback: Optional[Callable[..., None]] = None

    def _repo_context_cache_for(self, repo_path: Path) -> RepoContextCache:
        """Return the incremental briefing cache for ``repo_path``, creating it lazily.

        Preconditions: ``repo_path`` is a directory the development agent will scan.
        Postconditions: returns a ``RepoContextCache`` configured with this team's
          repo-briefing contract (extensions / exclude dirs / char budget); the same
          instance is returned for the same resolved repo across calls. Raises
          ``AssertionError`` if the precondition is violated (caller bug).
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        key = repo_path.resolve()
        cache = self._repo_context_caches.get(key)
        if cache is None:
            cache = RepoContextCache(
                extensions=self._extensions,
                exclude_dirs=self._exclude_dirs,
                max_chars=self._max_chars,
            )
            self._repo_context_caches[key] = cache
        return cache

    def _report_status(
        self,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Report phase progress via the optional per-run status callback.

        Preconditions: ``phase`` is a non-empty str.
        Postconditions: if ``_status_callback`` is set, it is invoked once with
          kwargs ``phase``, ``detail``, optional ``progress`` (omitted when
          None), and ``**extra``; callback exceptions are logged and swallowed;
          if the callback is None, this is a no-op. Never raises into the caller.
        """
        assert isinstance(phase, str) and phase, "phase must be a non-empty str"
        callback = self._status_callback
        if callback is None:
            return
        payload: Dict[str, Any] = {"phase": phase, "detail": detail, **extra}
        if progress is not None:
            payload["progress"] = progress
        try:
            callback(**payload)
        except Exception as e:
            logger.warning("team lead status callback failed (ignored): %s", e)

    def _run_gated_phases(
        self,
        phases: Sequence[Callable[[], Optional[T]]],
    ) -> Optional[T]:
        """Run phase callables in order; return the first failure payload.

        Preconditions: ``phases`` is a sequence (may be empty); each element is
          a zero-arg callable returning ``Optional[T]``.
        Postconditions: invokes phases in order; on the first non-``None``
          return value, returns that value and does not invoke later phases;
          if every phase returns ``None`` (or ``phases`` is empty), returns
          ``None``. Exceptions raised by a phase propagate to the caller.
        """
        for phase in phases:
            failure = phase()
            if failure is not None:
                return failure
        return None

    def _run_setup_and_delegate(
        self,
        *,
        repo_path: Path,
        task: Task,
        result_cls: Callable[..., Any],
        run_setup_fn: Callable[..., Any],
        development_agent_cls: Callable[..., Any],
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Any = None,
        merge_to_development: bool = True,
    ) -> Any:
        """Run setup, verify lint/test readiness, then delegate to the development agent.

        Passthrough kwargs (``architecture``, ``spec_content``, review/tool agents,
        ``build_verifier``, ``job_updater``, ``review_config``, and
        ``merge_to_development``) are forwarded unchanged to the development agent's
        ``run_workflow``. ``merge_to_development`` defaults to True; when False,
        delivery prepares a feature branch for external review instead of merging.

        Preconditions:
          - ``repo_path`` is a filesystem path the setup phase can operate on (created
            if missing — matching ``run_setup_impl``).
          - ``task`` has a non-empty ``id``.
          - ``result_cls`` is callable as ``result_cls(task_id=...)`` and returns an
            object exposing the development-handoff fields plus ``setup_result`` /
            ``failure_reason`` / ``current_phase``.
          - ``run_setup_fn`` is callable as ``run_setup_fn(repo_path=..., task_title=...)``.
          - ``development_agent_cls`` is callable as ``development_agent_cls(self.llm)``
            and returns an object with ``run_workflow(**kwargs)``.
        Postconditions:
          - On setup failure or missing lint/test config: returns a result with
            ``failure_reason`` set and without calling the development agent.
          - On success: ``repo_path`` is a directory; returns the team-lead result with
            ``setup_result`` preserved and the 13 development-handoff fields copied from
            the inner agent result.
          - Progress 2/3/5 ``job_updater`` calls include the canonical ``status_text``
            strings when ``job_updater`` is provided; updater exceptions are logged
            at DEBUG and do not abort the workflow.
        """
        assert task.id, "task.id is required"

        task_id = task.id
        result = result_cls(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception as exc:
                    logger.debug("[%s] job_updater failed: %s", task_id, exc)

        result.current_phase = Phase.SETUP
        _update_job(
            current_phase="setup",
            progress=2,
            status_text="Setting up repository and development environment",
        )
        try:
            setup_result = run_setup_fn(repo_path=repo_path, task_title=task.title or "")
            result.setup_result = setup_result
        except Exception as exc:
            result.failure_reason = f"Setup failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        assert repo_path.is_dir(), "repo_path must be a directory after setup"
        _update_job(current_phase="setup", progress=3, status_text="Repository setup complete")
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

        dev_agent = development_agent_cls(self.llm)
        inner = dev_agent.run_workflow(
            repo_path=repo_path,
            task=task,
            architecture=architecture,
            spec_content=spec_content,
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
