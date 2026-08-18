"""
Execution phase: run each microtask via tool agents or general code gen.

No code from frontend_team is used. Uses template-based output parsing.
Supports per-microtask review gates with configurable retry behavior.

The non-gated helpers (issue dedup, review-dependency container, file writer,
general microtask coder, and ``run_execution``) *and* the gated per-microtask
loop skeleton (``run_gated_execution_impl``) are shared across the code-v2 teams
(see ``shared/phases/execution.py``); this module wires in the frontend team's
models/prompt/profile and its review-gate architecture (one unified
``run_microtask_review`` called three times, filtering issues by ``source``) via
the gate adapters and ``GATE_CONFIG`` below, keeping
``run_execution_with_review_gates`` as a thin per-team wrapper. The QA and
security gates each scope the ``tool_agents`` mapping down to their own kind
(``testing_qa`` / ``security``) before calling ``run_microtask_review``, so
their tool-agent fan-out no longer runs every wired tool agent on every gate
call (matching ``backend_code_v2_team``'s per-gate scoping).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import (
    LlmRunner,
    resolve_text_mode_strands_model,
)
from shared.dev_models.models import ReviewContext, SystemArchitecture, Task
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.phases.dbc_phase import run_dbc_comments_review
from software_engineering_team.shared.phases.execution import (
    GatedExecutionConfig,
    GateOutcome,
    ReviewDependencies,
    _run_general_microtask_impl,
    run_execution_impl,
    run_gated_execution_impl,
)

from .. import models as _models
from ..models import (
    ExecutionResult,
    Microtask,
    MicrotaskReviewConfig,
    MicrotaskStatus,
    PlanningResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)
from ..output_templates import parse_files_and_summary_template
from ..prompts import EXECUTION_PROMPT
from ._profile import PROFILE

logger = logging.getLogger(__name__)

ToolAgentRunner = Callable[[ToolAgentInput], ToolAgentOutput]


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them.

    Preconditions: none.
    Postconditions: returns a freshly constructed ``LlmRunner`` bound to this
      module's current ``Agent`` / ``resolve_text_mode_strands_model``
      globals, looked up at call time (not cached), so monkeypatching either
      name before the call takes effect.
    """
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


__all__ = [
    "ReviewDependencies",
    "ToolAgentRunner",
    "run_execution",
    "run_execution_with_review_gates",
]


def _run_general_microtask(
    *,
    llm: LLMClient,
    microtask: Microtask,
    task: Task,
    language: str,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
) -> Dict[str, str]:
    """Use the LLM to implement a general (non-specialist) microtask (frontend models).

    Delegates to the shared implementation; keeps ``Agent`` /
    ``resolve_text_mode_strands_model`` as this module's LLM boundary.

    Preconditions: ``llm`` is an ``LLMClient``; ``microtask``/``task`` are
      fully-formed domain objects; ``architecture`` is ``None`` or a
      ``SystemArchitecture``.
    Postconditions: returns the parsed ``{path: content}`` map produced by
      ``_run_general_microtask_impl`` using this team's ``EXECUTION_PROMPT`` /
      ``parse_files_and_summary_template`` / ``PROFILE`` and a fresh
      ``_llm_runner()``; see that shared implementation for the full contract.
      Its rejection guard is a generic, unconditional ``.py``-suffix check
      shared with the backend team (not scoped by ``language``), so it rarely
      fires here since this team's output is mostly ``.ts``/``.tsx``/etc. —
      but any stray ``.py`` file whose content fails to parse is still
      dropped, not returned.
    """
    return _run_general_microtask_impl(
        llm=llm,
        microtask=microtask,
        task=task,
        language=language,
        existing_code=existing_code,
        architecture=architecture,
        execution_prompt=EXECUTION_PROMPT,
        parse_files_and_summary=parse_files_and_summary_template,
        profile=PROFILE,
        runner=_llm_runner(),
    )


def run_execution(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
) -> ExecutionResult:
    """Execute microtasks in dependency order (frontend models).

    Delegates the non-gated loop to ``run_execution_impl``; see that shared
    implementation for the full contract.

    Preconditions: ``task``/``planning_result`` are fully-formed; ``repo_path``
      is a path the coding step can write under; ``only_microtask_ids``, if
      given, may reference any subset of ``planning_result.microtasks``.
    Postconditions: returns an ``ExecutionResult``; a failed microtask is
      marked FAILED and execution continues with the remaining microtasks; an
      unmet ``depends_on`` is logged but does not block the microtask from
      running.
    """
    return run_execution_impl(
        llm=llm,
        task=task,
        planning_result=planning_result,
        repo_path=repo_path,
        architecture=architecture,
        existing_code=existing_code,
        tool_runners=tool_runners,
        progress_callback=progress_callback,
        only_microtask_ids=only_microtask_ids,
        models=_models,
        run_general_microtask=_run_general_microtask,
    )


# ---------------------------------------------------------------------------
# Review-gate adapters (frontend architecture: one unified run_microtask_review)
# ---------------------------------------------------------------------------
#
# The shared ``run_gated_execution_impl`` calls these to normalise the frontend
# team's single review function into a ``GateOutcome``. The code-review gate
# enables the code-review agents plus every wired tool agent (it's the only gate
# that surfaces the non-QA/non-security tool-agent kinds, e.g. accessibility,
# ui_design). The QA/security gates enable only their own agent and scope
# ``tool_agents`` down to their own kind (``testing_qa`` / ``security``) before
# calling ``run_microtask_review``, then filter the returned issues by
# ``source`` (matching the previous inline loop) -- so each gate's tool-agent
# fan-out invokes exactly one tool agent instead of every wired kind, mirroring
# ``backend_code_v2_team``'s per-gate scoping and removing the shared-instance
# race that previously blocked enabling ``parallelize_qa_security`` for this
# team -- ``GATE_CONFIG`` below now sets it to ``True``, matching backend.
# Independently of that scoping, the CR gate's full ``deps.tool_agents`` fan-out
# still runs ``testing_qa``/``security`` a second time on top of each gate's own
# scoped call; all three gates read ``deps.tool_agent_cache`` (a per-microtask-
# cycle ``AgentReviewCache``, reset in ``_run_review_cycles``) and forward it
# into ``run_microtask_review`` so the second call within a cycle is served
# from cache instead of re-invoking the tool agent -- see the caching design in
# docs/GATE_DEPENDENCY_GRAPH.md.
# ``.review`` is imported lazily to keep the module import free of the circular
# ``review`` <-> ``execution`` dependency.


def _scoped_tool_agents(
    tool_agents: Optional[Dict[ToolAgentKind, Any]], kind: ToolAgentKind
) -> Optional[Dict[ToolAgentKind, Any]]:
    """Filter a tool-agent mapping down to a single kind.

    Used by the QA/security gates so their tool-agent fan-out invokes only the
    one tool agent that matches the gate, not every wired kind.

    Preconditions: none -- ``tool_agents`` may be ``None`` or empty.
    Postconditions: returns ``{kind: tool_agents[kind]}`` when ``kind`` is wired
    in ``tool_agents``, else ``None``.
    """
    if not tool_agents or kind not in tool_agents:
        return None
    return {kind: tool_agents[kind]}


def _code_review_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> GateOutcome:
    """Run the frontend code-review gate (build + lint + code review agents,
    plus every wired tool agent).

    Preconditions: ``deps.build_verifier``/``deps.code_review_agent``/
      ``deps.linting_tool_agent``/``deps.tool_agents`` are set consistently
      with what the caller wants exercised; ``files`` is the microtask's
      current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (build, lint,
      code-review, and tool-agent failures are all contained to synthetic
      issues or logged warnings); an exception from ``detail_callback`` —
      which is invoked outside that containment and, in the gated loop,
      forwards to the caller-supplied ``progress_callback`` — is not caught
      here and propagates uncaught. Calls ``run_microtask_review`` with
      ``qa_agent=None, security_agent=None`` (disabling only those two LLM
      review steps) and the full, unscoped ``deps.tool_agents`` mapping —
      unlike the QA/security gates, this call does not narrow ``tool_agents``
      to a single kind, so the returned ``issues`` can include
      build/lint/code-review findings *and* every wired tool agent's findings
      (e.g. accessibility, ui_design), not only code-review-sourced ones.
      Copies ``passed``/``issues``/``summary``/``raw_issue_count`` (defaulting
      to ``None``) from the result unfiltered. Also forwards
      ``deps.tool_agent_cache`` into ``run_microtask_review`` so that any
      ``testing_qa``/``security`` tool-agent calls made here are served from
      cache (rather than re-invoked) when the QA/Security gates already
      computed them earlier in the same cycle, or vice versa.
    """
    from .review import run_microtask_review

    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=deps.build_verifier,
        qa_agent=None,
        security_agent=None,
        code_review_agent=deps.code_review_agent,
        linting_tool_agent=deps.linting_tool_agent,
        tool_agents=deps.tool_agents,
        detail_callback=detail_callback,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        tool_agent_cache=deps.tool_agent_cache,
    )
    return GateOutcome(
        passed=r.passed,
        issues=r.issues,
        summary=r.summary,
        raw_issue_count=getattr(r, "raw_issue_count", None),
    )


def _qa_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    cache: Optional[AgentReviewCache] = None,
) -> GateOutcome:
    """Run the frontend QA gate, keeping only ``source == "qa"`` issues.

    Disables the external ``security_agent``/``code_review_agent``/
    ``linting_tool_agent`` and passes ``build_verifier=None`` — build and
    lint are then genuinely skipped by ``run_microtask_review``.
    ``code_review_agent=None`` does not skip code review, though: the shared
    ``_code_review_step`` still runs its LLM-fallback reviewer whenever no
    external agent is supplied, and the fan-out calls it unconditionally, so
    a code-review LLM call happens on every invocation of this gate; its
    issues are filtered out below, not never produced.

    ``cache``: forwarded to ``run_microtask_review`` as the pre-existing
    per-agent QA/security LLM cache (unrelated to ``tool_agent_cache``).
    ``deps.tool_agent_cache`` is forwarded separately so a ``testing_qa``
    tool-agent result already computed by the CR gate's fan-out this cycle is
    reused here instead of re-invoked.

    Preconditions: ``deps.qa_agent``/``deps.tool_agents`` are set consistently
      with what the caller wants exercised; ``files`` is the microtask's
      current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (an outright
      QA-agent or tool-agent failure is contained to a synthetic issue or a
      logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Calls ``run_microtask_review`` with only
      ``qa_agent`` enabled among the external review agents (build and lint
      skipped; the LLM-fallback code-review step still runs and contributes
      to ``r.issues``) and ``tool_agents`` scoped to
      ``ToolAgentKind.TESTING_QA`` via ``_scoped_tool_agents`` (``None`` when
      that kind isn't wired), then filters ``r.issues`` to ``source == "qa"``
      before returning, discarding the code-review issues and any other
      non-QA-sourced ones. ``passed`` is computed as ``not qa_issues`` (true
      iff no QA-sourced issue survives filtering) rather than taken from
      ``r.passed`` — a stray non-QA issue in ``r.issues`` cannot fail this
      gate.
    """
    from .review import run_microtask_review

    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=None,
        qa_agent=deps.qa_agent,
        security_agent=None,
        code_review_agent=None,
        linting_tool_agent=None,
        tool_agents=_scoped_tool_agents(deps.tool_agents, ToolAgentKind.TESTING_QA),
        detail_callback=detail_callback,
        cache=cache,
        tool_agent_cache=deps.tool_agent_cache,
    )
    qa_issues = [i for i in r.issues if i.source == "qa"]
    return GateOutcome(passed=not qa_issues, issues=qa_issues, summary=r.summary)


def _security_gate(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    deps: ReviewDependencies,
    detail_callback: Callable[[str], None],
    cache: Optional[AgentReviewCache] = None,
) -> GateOutcome:
    """Run the frontend security gate, keeping only ``source == "security"`` issues.

    Disables the external ``qa_agent``/``code_review_agent``/
    ``linting_tool_agent`` and passes ``build_verifier=None`` — build and
    lint are then genuinely skipped by ``run_microtask_review``.
    ``code_review_agent=None`` does not skip code review, though: the shared
    ``_code_review_step`` still runs its LLM-fallback reviewer whenever no
    external agent is supplied, and the fan-out calls it unconditionally, so
    a code-review LLM call happens on every invocation of this gate; its
    issues are filtered out below, not never produced.

    ``cache``: forwarded to ``run_microtask_review`` as the pre-existing
    per-agent QA/security LLM cache (unrelated to ``tool_agent_cache``).
    ``deps.tool_agent_cache`` is forwarded separately so a ``security``
    tool-agent result already computed by the CR gate's fan-out this cycle is
    reused here instead of re-invoked.

    Preconditions: ``deps.security_agent``/``deps.tool_agents`` are set
      consistently with what the caller wants exercised; ``files`` is the
      microtask's current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (an outright
      security-agent or tool-agent failure is contained to a synthetic issue
      or a logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Calls ``run_microtask_review`` with only
      ``security_agent`` enabled among the external review agents (build and
      lint skipped; the LLM-fallback code-review step still runs and
      contributes to ``r.issues``) and ``tool_agents`` scoped to
      ``ToolAgentKind.SECURITY`` via ``_scoped_tool_agents`` (``None`` when
      that kind isn't wired), then filters ``r.issues`` to
      ``source == "security"`` before returning, discarding the code-review
      issues and any other non-security-sourced ones. ``passed`` is computed
      as ``not sec_issues`` (true iff no security-sourced issue survives
      filtering) rather than taken from ``r.passed`` — a stray non-security
      issue in ``r.issues`` cannot fail this gate.
    """
    from .review import run_microtask_review

    r = run_microtask_review(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=None,
        qa_agent=None,
        security_agent=deps.security_agent,
        code_review_agent=None,
        linting_tool_agent=None,
        tool_agents=_scoped_tool_agents(deps.tool_agents, ToolAgentKind.SECURITY),
        detail_callback=detail_callback,
        cache=cache,
        tool_agent_cache=deps.tool_agent_cache,
    )
    sec_issues = [i for i in r.issues if i.source == "security"]
    return GateOutcome(passed=not sec_issues, issues=sec_issues, summary=r.summary)


def _run_batch_coding_fixes(**kwargs: Any) -> Any:
    """Lazy binding of the frontend batch-fix runner (kept per-team for its Agent patch surface).

    Preconditions: ``kwargs`` matches
      ``.problem_solving.run_batch_coding_fixes``'s signature.
    Postconditions: returns that function's result unchanged; the import is
      deferred to call time so this module has no import-time dependency on
      ``.problem_solving`` (avoiding the ``review`` <-> ``execution``
      circular import).
    """
    from .problem_solving import run_batch_coding_fixes

    return run_batch_coding_fixes(**kwargs)


def _run_documentation_self_review(**kwargs: Any) -> Any:
    """Lazy binding of the frontend documentation self-review runner.

    Preconditions: ``kwargs`` matches
      ``.review.run_documentation_self_review``'s signature.
    Postconditions: returns that function's result unchanged; the import is
      deferred to call time so this module has no import-time dependency on
      ``.review`` (avoiding the ``review`` <-> ``execution`` circular
      import).
    """
    from .review import run_documentation_self_review

    return run_documentation_self_review(**kwargs)


GATE_CONFIG = GatedExecutionConfig(
    models=_models,
    run_general_microtask=_run_general_microtask,
    run_code_review_gate=_code_review_gate,
    run_qa_gate=_qa_gate,
    run_security_gate=_security_gate,
    run_batch_coding_fixes=_run_batch_coding_fixes,
    run_documentation_self_review=_run_documentation_self_review,
    # DbC comments self-review: a non-blocking, best-effort step that inserts
    # Design-by-Contract comments into a completed microtask's files after the
    # review-gate cycles pass and before Documentation. The shared reusable
    # reviewer is assigned directly (not via a lazy wrapper): it lives in the
    # shared package with no circular-import constraint, and callers rely on it
    # being this exact callable. Gated at the call site by `enable_dbc_comments`.
    # Frontend's non-Python files have no AST-level insertion safety net of their
    # own, so the shared phase's post-insertion build-verification revert is the
    # sole guard against a bad DbC edit reaching a commit here.
    run_dbc_self_review=run_dbc_comments_review,
    status_code_review=MicrotaskStatus.IN_REVIEW,
    status_qa=MicrotaskStatus.IN_REVIEW,
    status_security=MicrotaskStatus.IN_REVIEW,
    status_qa_security=MicrotaskStatus.IN_QA_SECURITY_TESTING,
    max_total_cycles=lambda config: config.max_retries * 3,
    code_review_retry_cap=lambda config: config.max_retries,
    max_cycles_requires_failing_gate=False,
    startup_log_message=lambda task_id, total, config: (
        f"[{task_id}] Starting execution with batch review flow: "
        f"{total} microtasks, max_retries={config.max_retries}, on_failure={config.on_failure}"
    ),
    gate_issue_log_verb="found",
    # QA and Security are independent analysis calls over the same
    # post-Code-Review snapshot on the frontend too (each gate scopes its
    # tool-agent call to its own kind via ``_scoped_tool_agents`` above) --
    # matching backend_code_v2_team's existing concurrent behavior. The CR
    # gate's full ``deps.tool_agents`` fan-out still calls ``testing_qa``/
    # ``security`` a second time, but all three gates share
    # ``deps.tool_agent_cache`` (see the gate-adapter comment above), so the
    # second call within a cycle is served from cache instead of re-invoking
    # the tool agent -- see docs/GATE_DEPENDENCY_GRAPH.md.
    parallelize_qa_security=True,
)


def run_execution_with_review_gates(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    spec_content: str = "",
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
    review_config: Optional[MicrotaskReviewConfig] = None,
    review_deps: Optional[ReviewDependencies] = None,
) -> ExecutionResult:
    """
    Execute microtasks with batch-based review cycles.

    After each microtask is coded, it must pass through review phases:
    1. Code Review (build + lint + code review) - batch fix all issues
    2. QA Testing + Security Testing - independent, concurrent analysis passes
       over the same post-Code-Review snapshot (``GATE_CONFIG.parallelize_qa_security``
       is ``True``); batch fix all issues from either, then restart from Code Review
    3. Documentation - self-review loop (3-5 iterations, never fails)

    Key behavior:
    - Each review phase collects ALL issues and sends them to the coding agent at once
    - After QA and/or Security fixes, the flow restarts from Code Review
    - Documentation uses self-review iterations (no failure mode)

    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``current_index`` is the 1-based index of the currently executing microtask.
    ``microtask_phase`` is one of: "coding", "code_review", "qa_testing", "security_testing",
    "qa_security_testing", "documentation", "completed". "qa_security_testing" is reported
    while QA and Security run concurrently (see ``GATE_CONFIG.parallelize_qa_security``);
    it must not be read as "qa_testing has passed".
    ``phase_detail`` provides human-readable detail about the current action.

    Thin wrapper: the loop lives in the shared ``run_gated_execution_impl``,
    parameterised by this team's ``GATE_CONFIG``.

    Preconditions:
      - ``review_deps``, if given, supplies whichever of
        ``build_verifier``/``code_review_agent``/``linting_tool_agent``/
        ``qa_agent``/``security_agent``/``tool_agents`` the configured gates
        need; unset ones mean "not available" to ``run_microtask_review``,
        not an error.
    Postconditions:
      - Returns an ``ExecutionResult``; each microtask ends COMPLETED,
        SKIPPED, FAILED or REVIEW_FAILED.
      - Raises ``MicrotaskReviewFailedError`` when a microtask's review fails
        and ``on_failure == "stop"`` (or a security failure with
        ``security_failure_always_stops``).
      - Matching the backend, ``GATE_CONFIG.parallelize_qa_security=True``
        here too: QA and Security run concurrently over the same
        post-Code-Review snapshot, so ``progress_callback`` can report
        ``"qa_security_testing"`` for this team as well.
    """
    return run_gated_execution_impl(
        gate_config=GATE_CONFIG,
        llm=llm,
        task=task,
        planning_result=planning_result,
        repo_path=repo_path,
        architecture=architecture,
        spec_content=spec_content,
        existing_code=existing_code,
        tool_runners=tool_runners,
        progress_callback=progress_callback,
        only_microtask_ids=only_microtask_ids,
        review_config=review_config,
        review_deps=review_deps,
    )
