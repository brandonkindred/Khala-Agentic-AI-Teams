"""
Execution phase: run each microtask via tool agents or general code gen.

No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
Supports per-microtask review gates with configurable retry behavior.

The non-gated helpers (issue dedup, review-dependency container, file writer,
general microtask coder, and ``run_execution``) *and* the gated per-microtask
loop skeleton (``run_gated_execution_impl``) are shared across the code-v2 teams
(see ``shared/phases/execution.py``); this module wires in the backend team's
models/prompt/profile and its review-gate architecture (three separate
``run_{code_review,qa,security}_testing_phase`` functions returning a
``PhaseReviewResult``) via the gate adapters and ``GATE_CONFIG`` below, keeping
``run_execution_with_review_gates`` as a thin per-team wrapper.
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
    """Use the LLM to implement a general (non-specialist) microtask (backend models).

    Delegates to the shared implementation; keeps ``Agent`` /
    ``resolve_text_mode_strands_model`` as this module's LLM boundary.

    Preconditions: ``llm`` is an ``LLMClient``; ``microtask``/``task`` are
      fully-formed domain objects; ``architecture`` is ``None`` or a
      ``SystemArchitecture``.
    Postconditions: returns the parsed ``{path: content}`` map produced by
      ``_run_general_microtask_impl`` using this team's ``EXECUTION_PROMPT`` /
      ``parse_files_and_summary_template`` / ``PROFILE`` and a fresh
      ``_llm_runner()``; see that shared implementation for the full contract
      (a ``.py`` file whose content fails to parse is dropped, not returned).
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
    """Execute microtasks in dependency order (backend models).

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
# Review-gate adapters (backend architecture: three PhaseReviewResult gates)
# ---------------------------------------------------------------------------
#
# The shared ``run_gated_execution_impl`` calls these to normalise the backend
# team's split gate functions into a ``GateOutcome``. ``.review`` /
# ``.problem_solving`` are imported lazily inside each adapter to keep the
# module import free of the circular ``review`` <-> ``execution`` dependency
# (unchanged from the previous inline loop).


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
    """Run the backend code-review phase (code review only).

    Preconditions: ``deps.code_review_agent`` is not skippable: when unset,
      ``run_code_review_phase`` runs the LLM reviewer directly instead of an
      external agent, so a code-review LLM call always happens regardless.
      ``files`` is the microtask's current ``{path: content}`` output.
    Postconditions: the review/agent logic itself never raises (code-review
      failures are all contained to synthetic issues or logged warnings); an
      exception from ``detail_callback`` — which is invoked outside that
      containment and, in the gated loop, forwards to the caller-supplied
      ``progress_callback`` — is not caught here and propagates uncaught.
      Returns a ``GateOutcome`` with ``passed``/``issues``/``summary`` copied
      from the resulting ``PhaseReviewResult``, ``raw_issue_count`` defaulting
      to ``None`` if absent.
    """
    from .review import run_code_review_phase

    r = run_code_review_phase(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        code_review_agent=deps.code_review_agent,
        detail_callback=detail_callback,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
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
    """Run the backend QA-testing phase.

    Preconditions: ``deps.qa_agent``/``deps.tool_agents`` are set consistently
      with what the caller wants exercised; ``files`` is the microtask's
      current ``{path: content}`` output. ``llm`` is accepted for signature
      uniformity with the gate interface (``GatedExecutionConfig.run_qa_gate``)
      but is not used by this gate — the QA phase runs via ``deps.qa_agent``/
      ``deps.tool_agents`` instead.
    Postconditions: the review/agent logic itself never raises (an outright
      QA-agent or tool-agent failure is contained to a synthetic issue or a
      logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Returns a ``GateOutcome`` with
      ``passed``/``issues``/``summary`` copied directly from the resulting
      ``PhaseReviewResult`` (no filtering — the backend's phase call is
      already scoped to QA only, unlike the frontend's ``_qa_gate``).
    """
    from .review import run_qa_testing_phase

    r = run_qa_testing_phase(
        task=task,
        microtask=microtask,
        files=files,
        qa_agent=deps.qa_agent,
        tool_agents=deps.tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        cache=cache,
    )
    return GateOutcome(passed=r.passed, issues=r.issues, summary=r.summary)


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
    """Run the backend security-testing phase.

    Preconditions: ``deps.security_agent``/``deps.tool_agents`` are set
      consistently with what the caller wants exercised; ``files`` is the
      microtask's current ``{path: content}`` output. ``llm`` is accepted for
      signature uniformity with the gate interface
      (``GatedExecutionConfig.run_security_gate``) but is not used by this
      gate — the security phase runs via ``deps.security_agent``/
      ``deps.tool_agents`` instead.
    Postconditions: the review/agent logic itself never raises (an outright
      security-agent or tool-agent failure is contained to a synthetic issue
      or a logged warning); an exception from ``detail_callback`` — which is
      invoked outside that containment and, in the gated loop, forwards to
      the caller-supplied ``progress_callback`` — is not caught here and
      propagates uncaught. Returns a ``GateOutcome`` with
      ``passed``/``issues``/``summary`` copied directly from the resulting
      ``PhaseReviewResult`` (no filtering — the backend's phase call is
      already scoped to security only, unlike the frontend's
      ``_security_gate``).
    """
    from .review import run_security_testing_phase

    r = run_security_testing_phase(
        task=task,
        microtask=microtask,
        files=files,
        security_agent=deps.security_agent,
        tool_agents=deps.tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        cache=cache,
    )
    return GateOutcome(passed=r.passed, issues=r.issues, summary=r.summary)


def _run_batch_coding_fixes(**kwargs: Any) -> Any:
    """Lazy binding of the backend batch-fix runner (kept per-team for its Agent patch surface).

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
    """Lazy binding of the backend documentation self-review runner.

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
    status_code_review=MicrotaskStatus.IN_CODE_REVIEW,
    status_qa=MicrotaskStatus.IN_QA_TESTING,
    status_security=MicrotaskStatus.IN_SECURITY_TESTING,
    status_qa_security=MicrotaskStatus.IN_QA_SECURITY_TESTING,
    max_total_cycles=lambda config: (
        config.code_review_max_retries + config.qa_max_retries + config.security_max_retries
    ),
    code_review_retry_cap=lambda config: config.code_review_max_retries,
    max_cycles_requires_failing_gate=True,
    startup_log_message=lambda task_id, total, config: (
        f"[{task_id}] Starting execution with batch review flow: "
        f"{total} microtasks, on_failure={config.on_failure}"
    ),
    gate_issue_log_verb="failed with",
    # QA and Security are independent analysis calls over the same
    # post-Code-Review snapshot on the backend (each scopes its tool-agent call
    # to its own spec.tool_kind) -- see docs/GATE_DEPENDENCY_GRAPH.md. The
    # frontend also enables this, since it scopes its own QA/security
    # tool-agent fan-out per gate the same way.
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
    1. Code Review (code review only) - batch fix all issues
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
        ``code_review_agent``/``qa_agent``/``security_agent``/``tool_agents``
        the configured gates need; unset ones mean "not available" to the
        underlying phase calls, not an error.
    Postconditions:
      - Returns an ``ExecutionResult``; each microtask ends COMPLETED,
        SKIPPED, FAILED or REVIEW_FAILED.
      - Raises ``MicrotaskReviewFailedError`` when a microtask's review fails
        and ``on_failure == "stop"`` (or a security failure with
        ``security_failure_always_stops``).
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
