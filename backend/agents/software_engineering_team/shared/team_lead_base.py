"""
Shared base for the code-v2 Team Leads (backend + frontend).

``TeamLeadSharedState`` is the minimal shared construct for swarm-style
orchestrators: LLM resolution, an opaque ``shared_config`` bag, and the
optional per-run status callback — state only, with no phase sequencing.

``BaseTeamLead`` builds on that mixin for code-v2 single-pass leads.
``BackendCodeV2TeamLead`` and ``FrontendCodeV2TeamLead`` share their
constructor, their per-repo incremental briefing cache lookup
(:meth:`BaseTeamLead._repo_context_cache_for`), the field-copy tail that
overlays their inner ``*DevelopmentAgent`` result onto their own result
object, and the setup → lint/test-gate → delegate sequence
(:meth:`BaseTeamLead._run_setup_and_delegate`). This base also provides a
gate-based phase-sequencing helper via :meth:`BaseTeamLead._run_gated_phases`,
an intra-phase multi-gate hook via :meth:`BaseTeamLead._run_phase_gates`,
and a bounded retry/patch-loop via :meth:`BaseTeamLead._run_bounded_retry_loop`.

Each team subclasses this base and supplies a thin ``run_workflow`` that
passes its module-level ``run_setup``, ``*DevelopmentAgent``, and
``*WorkflowResult`` into the shared helper. Those names are looked up in the
subclass orchestrator module at call time so tests can monkeypatch
``orchestrator.run_setup`` / ``orchestrator.*DevelopmentAgent`` as module-level
attributes (see ``test_team_lead_propagates_development_handoff_fields``).

Shared failure-envelope helpers (:func:`build_team_failure_result`,
:func:`apply_team_failure`) construct or mutate team results with
``success=False`` and a ``failure_reason`` for phase-sequential leads.
``CodingTeamSwarm`` deliberately does not use that envelope (see its class
docstring).
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple, TypeVar

from llm_service import LLMClient
from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.v2_models import Phase

logger = logging.getLogger(__name__)

T = TypeVar("T")

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


def warn_doc_agent_deprecated(doc_agent: Any) -> None:
    """Warn once per call site when a caller passes a non-None ``doc_agent``.

    ``doc_agent`` is accepted by ``run_workflow``/``_run_setup_and_delegate``
    for backward compatibility but is never forwarded to
    ``ConfigDrivenV2DevelopmentAgent._run_development_workflow`` (which has no
    such parameter) — real per-microtask documentation is produced instead by
    the ``ToolAgentKind.DOCUMENTATION`` tool agent each team builds
    internally. The four call sites (frontend/backend dev-agent and
    team-lead ``run_workflow``) and ``_run_setup_and_delegate`` share this
    helper so the deprecation message cannot drift between them.

    Preconditions: none.
    Postconditions: emits a ``DeprecationWarning`` via ``warnings.warn`` iff
      ``doc_agent is not None``; otherwise a no-op.
    """
    if doc_agent is not None:
        warnings.warn(
            "doc_agent is deprecated and has no effect: it is not forwarded to "
            "the development workflow. Documentation is generated via the "
            "DocumentationToolAgent (ToolAgentKind.DOCUMENTATION) instead.",
            DeprecationWarning,
            stacklevel=2,
        )


def make_job_updater(
    job_updater: Optional[Callable[..., None]],
    task_id: str,
    logger: logging.Logger,
) -> Callable[..., None]:
    """Build a ``**kwargs``-forwarding wrapper around an optional job-update callback.

    The three code-v2 orchestrators (the shared base and the backend/frontend
    development agents) each need to report progress through an
    externally-supplied ``job_updater`` without letting a broken callback abort
    the workflow. This factory is the single place that behavior lives, so the
    three call sites cannot drift from each other the way they previously did.

    Preconditions: ``task_id`` is a non-empty str; ``logger`` is a
      ``logging.Logger``; ``job_updater``, if provided, accepts arbitrary
      keyword arguments.
    Postconditions: returns a callable ``_update_job(**kwargs)`` that, when
      ``job_updater`` is falsy, is a no-op; otherwise invokes
      ``job_updater(**kwargs)`` and, if that raises, swallows the exception and
      logs it at DEBUG via ``logger`` rather than propagating it.
    """
    assert task_id, "task_id is required"

    def _update_job(**kwargs: Any) -> None:
        if job_updater:
            try:
                job_updater(**kwargs)
            except Exception as exc:
                logger.debug("[%s] job_updater failed: %s", task_id, exc)

    return _update_job


def copy_development_result_fields(dst: Any, src: Any) -> None:
    """Overlay the shared development-handoff fields from ``src`` onto ``dst``.

    Preconditions: ``dst`` and ``src`` each expose every attribute named in
      ``_DEVELOPMENT_RESULT_FIELDS`` (both backend and frontend
      ``*CodeV2WorkflowResult`` models do, despite being distinct classes).
    Postconditions: each of the 13 shared fields on ``dst`` equals the
      corresponding field on ``src``; every other attribute of ``dst``
      (notably ``setup_result``) is left untouched.
    """
    assert dst is not None, "dst is required"
    assert src is not None, "src is required"
    for field in _DEVELOPMENT_RESULT_FIELDS:
        assert hasattr(src, field), f"src must expose {field}"
        assert hasattr(dst, field), f"dst must expose {field}"
        setattr(dst, field, getattr(src, field))


def build_team_failure_result(
    result_cls: Callable[..., T],
    failure_reason: str,
    **partial_state: Any,
) -> T:
    """Construct a failure envelope: success=False + failure_reason + optional partial state.

    Preconditions: ``result_cls`` is callable as
      ``result_cls(success=False, failure_reason=..., **partial_state)``;
      ``failure_reason`` is a str; ``partial_state`` must not include ``success``
      or ``failure_reason``.
    Postconditions: returns an instance with ``success is False`` and
      ``failure_reason`` equal to the given string; each ``partial_state`` key is
      forwarded to the constructor.
    """
    assert callable(result_cls), "result_cls must be callable"
    assert isinstance(failure_reason, str), "failure_reason must be a str"
    assert "success" not in partial_state, "success is fixed to False"
    assert "failure_reason" not in partial_state, (
        "pass failure_reason as the dedicated argument, not in kwargs"
    )
    return result_cls(success=False, failure_reason=failure_reason, **partial_state)


def apply_team_failure(
    result: Any,
    failure_reason: str,
    **partial_fields: Any,
) -> Any:
    """Mutate an existing result into the failure envelope; return the same object.

    Preconditions: ``result`` is not None and exposes assignable ``success`` /
      ``failure_reason`` attributes (and any keys in ``partial_fields``);
      ``failure_reason`` is a str; ``partial_fields`` must not include ``success``
      or ``failure_reason``.
    Postconditions: ``result.success is False``; ``result.failure_reason`` equals
      the given string; each ``partial_fields`` key is set via ``setattr``;
      returns ``result`` (same identity). Unrelated attributes are left untouched.
    """
    assert result is not None, "result is required"
    assert isinstance(failure_reason, str), "failure_reason must be a str"
    assert "success" not in partial_fields, "success is fixed to False"
    assert "failure_reason" not in partial_fields, (
        "pass failure_reason as the dedicated argument, not in kwargs"
    )
    result.success = False
    result.failure_reason = failure_reason
    for key, value in partial_fields.items():
        setattr(result, key, value)
    return result


class TeamLeadSharedState:
    """Minimal shared state for team leads and swarm-style orchestrators.

    Holds LLM resolution (``llm_getter``), an opaque ``shared_config`` bag, and
    the optional per-run status callback. Intentionally excludes phase
    sequencing, setup/delegate, worktree management, and swarm locking — those
    stay on the concrete orchestrator (or on ``BaseTeamLead`` for single-pass
    leads). ``CodingTeamSwarm`` is the canonical adopter: it shares this state
    storage only and does not take ``BaseTeamLead``'s gated phase-sequencing
    template or the ``build_team_failure_result`` / ``apply_team_failure``
    envelope (round-based swarm loop + task-graph failures vs. single-pass
    phase model + ``success`` / ``failure_reason`` results).

    Invariants: ``llm_getter`` is callable; ``shared_config`` is a dict owned by
    this instance (shallow-copied at init); ``_status_callback`` defaults to None.
    """

    def __init__(
        self,
        llm_getter: Callable[[str], Any],
        *,
        shared_config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        assert callable(llm_getter), "llm_getter must be callable"
        if shared_config is not None:
            assert isinstance(shared_config, Mapping), "shared_config must be a mapping"
        self.llm_getter = llm_getter
        self.shared_config: Dict[str, Any] = dict(shared_config or {})
        # Optional per-run status/progress callback. Consumers assign this for a
        # run (and should clear it when the run ends); not accepted via constructor.
        self._status_callback: Optional[Callable[..., None]] = None

    def _llm_for(self, agent_id: str) -> Any:
        """Resolve an LLM client for ``agent_id`` via ``llm_getter``.

        Preconditions: ``agent_id`` is a str (may be empty for constant getters).
        Postconditions: returns whatever ``llm_getter(agent_id)`` returns.
        """
        assert isinstance(agent_id, str), "agent_id must be a str"
        return self.llm_getter(agent_id)

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
          if the callback is None, this is a no-op. Never raises when
          preconditions hold.
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


class BaseTeamLead(TeamLeadSharedState):
    """Shared base for the code-v2 Team Leads.

    Subclasses provide the per-team repo-briefing filter constants (via
    ``__init__``) and a thin ``run_workflow`` that delegates to
    :meth:`_run_setup_and_delegate` with late-bound module globals.

    Inherits ``TeamLeadSharedState`` for LLM resolution / shared_config / status
    hook storage. Adapts the historical single ``llm_client`` into a constant
    ``llm_getter`` while exposing ``self.llm`` for existing callers.

    Invariants: instance state includes ``llm``, the injected
    extensions/exclude_dirs/max_chars, ``_repo_context_caches``, plus the mixin
    fields (``llm_getter``, ``shared_config``, ``_status_callback``).
    Also exposes :meth:`_run_gated_phases`, :meth:`_run_phase_gates`, and
    :meth:`_run_bounded_retry_loop` as reusable helpers.
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
        TeamLeadSharedState.__init__(
            self,
            llm_getter=lambda _agent_id: llm_client,
            shared_config={},
        )
        self.llm = llm_client
        self._extensions = extensions
        self._exclude_dirs = exclude_dirs
        self._max_chars = max_chars
        # Per-repo incremental briefing cache, reused across the team lead's
        # run_workflow calls (the coding-team worker reuses one team lead for all
        # tasks in a job), so the N tasks of a job re-read only the files each
        # merge touched instead of re-walking the whole repo N times.
        self._repo_context_caches: Dict[Path, RepoContextCache] = {}

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

    def _run_phase_gates(
        self,
        gates: Sequence[Callable[[], Optional[T]]],
    ) -> Optional[T]:
        """Run intra-phase gate callables; return the first failure payload.

        Preconditions: ``gates`` is a sequence (may be empty); each element is
          a zero-arg callable returning ``Optional[T]``.
        Postconditions: same as :meth:`_run_gated_phases` — first non-``None``
          wins; all-``None`` / empty → ``None``; exceptions propagate.
        """
        return self._run_gated_phases(gates)

    def _run_bounded_retry_loop(
        self,
        *,
        max_iterations: int,
        attempt: Callable[[int], Optional[T]],
        is_success: Callable[[T], bool],
    ) -> Tuple[bool, Optional[T]]:
        """Run ``attempt`` up to ``max_iterations`` times until success or abort.

        Preconditions: ``max_iterations >= 1``; ``attempt`` and ``is_success`` are callable.
        Postconditions:
          - On success: returns ``(True, result)`` where ``is_success(result)`` is True.
          - On abort (``attempt`` returns ``None``): returns ``(False, None)`` and does
            not call further iterations.
          - On exhausted retries: returns ``(False, last_non_none_result)``.
          - Exceptions from ``attempt`` / ``is_success`` propagate unchanged.
        """
        assert max_iterations >= 1, "max_iterations must be >= 1"
        assert callable(attempt), "attempt must be callable"
        assert callable(is_success), "is_success must be callable"

        last: Optional[T] = None
        for i in range(max_iterations):
            result = attempt(i)
            if result is None:
                return False, None
            if is_success(result):
                return True, result
            last = result
        return False, last

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
        ``doc_agent`` is deprecated and ignored; it is forwarded unchanged to the
        development agent's ``run_workflow``, which is the single place the
        deprecation warning (:func:`warn_doc_agent_deprecated`) fires.

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
            the inner agent result. The development agent also receives
            ``repo_context_cache`` from ``self._repo_context_cache_for(repo_path)``, so
            subsequent tasks on the same resolved repo reuse the incremental briefing
            cache instead of re-walking the tree.
          - Progress 2/3/5 ``job_updater`` calls include the canonical ``status_text``
            strings when ``job_updater`` is provided; updater exceptions are logged
            at DEBUG and do not abort the workflow.
        """
        assert task.id, "task.id is required"

        task_id = task.id
        result = result_cls(task_id=task_id)

        _update_job = make_job_updater(job_updater, task_id, logger)

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
