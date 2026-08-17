"""
Review phase: code review, QA, security.

Build verification and linting run via the full review paths
(``run_review``, ``run_microtask_review``) or the pre-review quality gate,
not via the standalone code-review phase wrapper.

Invokes passed-in quality agents when available; otherwise uses the team's
own LLM-based review. No code from ``backend_agent`` is used. The code-review
fallback calls the shared engine's coordinator directly (JSON output,
schema-validated); documentation self-review still uses template-based
output so parsing works across model providers.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import resolve_text_mode_strands_model
from shared.dev_models.models import ReviewContext, Task
from software_engineering_team.code_review_agent.coordinator import run_coordinator
from software_engineering_team.shared.agent_review import (
    AgentReviewCache,
    run_qa_agent,
    run_security_agent,
)
from software_engineering_team.shared.phases.review import (
    run_code_review_phase_impl,
    run_qa_testing_phase_impl,
    run_security_testing_phase_impl,
)
from software_engineering_team.shared.review_utils import (
    DOC_QUALITY_THRESHOLD,
    MANY_CHUNKS_WARN_THRESHOLD,
    MAX_DOC_SELF_REVIEW_ITERATIONS,
    MAX_REVIEW_CODE_CHARS,
    MIN_DOC_SELF_REVIEW_ITERATIONS,
)
from software_engineering_team.shared.review_utils import (
    run_documentation_self_review as _shared_run_documentation_self_review,
)
from software_engineering_team.shared.v2_review import (
    LlmReviewOutput,
    _review_steps_run_sequentially,  # noqa: F401  (re-exported for tests)
    run_coordinator_llm_review,
)
from software_engineering_team.shared.v2_review import (
    run_microtask_review as _shared_run_microtask_review,
)
from software_engineering_team.shared.v2_review import (
    run_review as _shared_run_review,
)

from ..models import (
    DocumentationSelfReviewResult,
    ExecutionResult,
    Microtask,
    PhaseReviewResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
)
from ..output_templates import parse_documentation_self_review_template
from ..prompts import DOCUMENTATION_SELF_REVIEW_PROMPT
from ._profile import PROFILE, REVIEW_CONFIG

logger = logging.getLogger(__name__)


def _run_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> LlmReviewOutput[ReviewIssue]:
    """Lightweight code review when no external review agent is available.

    Thin wrapper over the shared
    :func:`software_engineering_team.shared.v2_review.run_coordinator_llm_review`
    (see its docstring for the full contract: ``CodeReviewInput`` construction,
    issue translation, ``raw_issue_count``/grounding-breaker rationale,
    ``CodeReviewUnavailableError`` propagation, and the empty-``files``
    fail-closed behavior). ``run_coordinator`` is imported here (not into the
    shared module) and passed through as ``run_coordinator_fn``, so this
    module stays the test patch surface for the coordinator call, matching how
    ``Agent``/``resolve_text_mode_strands_model`` are patched for
    ``run_documentation_self_review`` below.

    Preconditions:
        - ``language`` defaults to this team's ``PROFILE.default_language``
          ("python") so an existing caller that does not pass it yet keeps
          reviewing under the correct language.
        - ``enable_llm_review_grounding`` is accepted for call-signature
          compatibility with ``llm_review_fn``'s contract (see
          ``shared.v2_review._code_review_step``) but is otherwise unused:
          the coordinator's chunk reviewer only ever reports on the literal
          code slice it was shown, so there is no free-text hallucinated-claim
          filter left to toggle.
    """
    return run_coordinator_llm_review(
        llm=llm,
        task=task,
        files=files,
        language=language,
        run_coordinator_fn=run_coordinator,
        review_context=review_context,
    )


def _run_qa_agent(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> List[ReviewIssue]:
    """Run the external QA agent over each file's raw, function-aware-split source.

    Thin wrapper that delegates to the shared ``run_qa_agent``, injecting this
    team's ``ReviewIssue`` factory and chunking constants.

    Preconditions:
        - ``qa_agent`` is not None and exposes ``.run(QAInput) -> QAOutput``.
        - ``cache``: see ``software_engineering_team.shared.agent_review``.

    Postconditions: see ``software_engineering_team.shared.agent_review``; QA bugs
    become ``ReviewIssue``s with ``source="qa"``.
    """
    return run_qa_agent(
        qa_agent=qa_agent,
        files=files,
        language=language,
        task_description=task_description,
        task_id=task_id,
        issue_factory=ReviewIssue,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
        context=context,
        cache=cache,
    )


def _run_security_agent(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> List[ReviewIssue]:
    """Run the external security agent over each file's raw, function-aware-split source.

    Thin wrapper that delegates to the shared ``run_security_agent``, injecting
    this team's ``ReviewIssue`` factory and chunking constants.

    Preconditions:
        - ``security_agent`` is not None and exposes
          ``.run(SecurityInput) -> SecurityOutput``.
        - ``cache``: see ``software_engineering_team.shared.agent_review``.

    Postconditions: see ``software_engineering_team.shared.agent_review``;
    vulnerabilities become ``ReviewIssue``s with ``source="security"``.
    """
    return run_security_agent(
        security_agent=security_agent,
        files=files,
        language=language,
        task_description=task_description,
        task_id=task_id,
        issue_factory=ReviewIssue,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
        context=context,
        cache=cache,
    )


def _run_build_verification(
    repo_path: Path,
    build_verifier: Optional[Callable[..., Tuple[bool, str]]],
    task_id: str,
) -> Tuple[bool, str]:
    """Run the build verifier if provided, else assume success."""
    if build_verifier is None:
        return True, "No build verifier provided; skipping."
    try:
        return build_verifier(repo_path, PROFILE.build_verify_label, task_id)
    except Exception as exc:
        logger.warning("[%s] Build verifier raised: %s", task_id, exc)
        return False, str(exc)


def run_review(
    *,
    llm: LLMClient,
    task: Task,
    execution_result: ExecutionResult,
    repo_path: Path,
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> ReviewResult:
    """Execute the Review phase.

    Thin wrapper over the shared parametrised reviewer
    (:func:`software_engineering_team.shared.v2_review.run_review`) driven by this
    team's :data:`REVIEW_CONFIG`. The per-team code-review/QA/security runners
    are injected as module-level callables so this module stays the test patch
    surface for ``_run_llm_review`` / ``_run_qa_agent`` / ``_run_security_agent``.

    Preconditions: ``execution_result`` exposes ``.files``.
    Postconditions: see the shared ``run_review``.
    """
    return _shared_run_review(
        config=REVIEW_CONFIG,
        llm=llm,
        task=task,
        execution_result=execution_result,
        repo_path=repo_path,
        build_verifier=build_verifier,
        qa_agent=qa_agent,
        security_agent=security_agent,
        code_review_agent=code_review_agent,
        linting_tool_agent=linting_tool_agent,
        tool_agents=tool_agents,
        language=language,
        llm_review_fn=_run_llm_review,
        qa_agent_fn=_run_qa_agent,
        security_agent_fn=_run_security_agent,
        build_verify_fn=_run_build_verification,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )


def run_microtask_review(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Microtask,
    repo_path: Path,
    files: Dict[str, str],
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    cache: Optional[AgentReviewCache] = None,
) -> ReviewResult:
    """Run full review on a single microtask's output files.

    Thin wrapper over
    :func:`software_engineering_team.shared.v2_review.run_microtask_review`; see
    :func:`run_review` for the injection rationale. Unlike that shared function
    (and unlike ``frontend_code_v2_team.phases.review.run_microtask_review``),
    this wrapper does not accept or forward a ``tool_agent_cache`` parameter:
    the backend team's gate callables never read ``deps.tool_agent_cache``
    (only the frontend team's do -- see ``ReviewDependencies`` in
    ``shared.phases.execution`` and the "residual 2x" caching design note in
    ``docs/GATE_DEPENDENCY_GRAPH.md``), so there is nothing for it to do here.
    Passing ``tool_agent_cache`` to this wrapper raises ``TypeError``.

    Preconditions:
        - ``microtask`` exposes ``.id``/``.title``/``.description``.
        - ``cache``: see ``software_engineering_team.shared.agent_review``;
          forwarded to the QA/security steps only.

    Postconditions:
        - Delegates to ``_shared_run_microtask_review``, which forwards
          ``review_context`` into the code-review step's ``CodeReviewInput``
          (``None`` when omitted, so existing callers are unaffected). See the
          shared function for the full review-result contract.
    """
    return _shared_run_microtask_review(
        config=REVIEW_CONFIG,
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        build_verifier=build_verifier,
        qa_agent=qa_agent,
        security_agent=security_agent,
        code_review_agent=code_review_agent,
        linting_tool_agent=linting_tool_agent,
        tool_agents=tool_agents,
        detail_callback=detail_callback,
        language=language,
        llm_review_fn=_run_llm_review,
        qa_agent_fn=_run_qa_agent,
        security_agent_fn=_run_security_agent,
        build_verify_fn=_run_build_verification,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        agent_review_cache=cache,
    )


def run_code_review_phase(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Microtask,
    repo_path: Path,
    files: Dict[str, str],
    code_review_agent: Any = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> PhaseReviewResult:
    """
    Run code review phase only: the code review step.

    This is the first phase after coding, focusing on code quality, syntax,
    and adherence to coding standards. Build verification and linting run
    elsewhere (this team's own pre-review quality gate, or the separate
    ``run_review``/``run_microtask_review`` path), not in this phase.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_code_review_phase_impl`)
    that injects this team's LLM-based reviewer via ``llm_review_fn`` and the
    per-team result class via ``phase_review_result_cls``. ``_run_llm_review``
    is referenced here by bare module-global name (resolved at call time, not
    captured at import time), so this module stays the test patch surface for
    ``_run_llm_review``/``run_coordinator`` itself -- exactly the technique
    ``run_review`` / ``run_microtask_review`` already use for the same reason.
    ``enable_llm_review_grounding`` is forwarded for call-signature
    compatibility with ``llm_review_fn``'s contract but is a no-op on this
    path: the coordinator's chunk reviewer only ever reports on the literal
    code it was shown, so there is no free-text hallucinated-claim filter
    left to toggle (see ``_run_llm_review``'s own docstring).

    Preconditions:
        - ``llm`` is a usable text-mode LLM client (forwarded to
          ``_run_llm_review`` when no ``code_review_agent`` is supplied or it
          fails).
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
        - ``files`` maps file paths to their full source text.
    Postconditions:
        - Returns a :class:`PhaseReviewResult` whose ``passed`` is true iff no
          critical/high code-review issue was found. Does not propagate an
          outright failure of the code-review agent or LLM fallback
          (including ``CodeReviewUnavailableError`` from ``_run_llm_review``):
          the shared ``_code_review_step`` catches it and reports a synthetic
          high-severity issue instead. This containment does not extend to
          caller-supplied ``detail_callback`` -- exceptions it raises still
          propagate to the caller.
    Invariants:
        - Does not run build verification or linting -- those run in this
          team's own pre-review quality gate, or the separate
          ``run_review``/``run_microtask_review`` path.
    """
    return run_code_review_phase_impl(
        llm=llm,
        task=task,
        microtask=microtask,
        repo_path=repo_path,
        files=files,
        code_review_agent=code_review_agent,
        detail_callback=detail_callback,
        language=language,
        review_context=review_context,
        enable_llm_review_grounding=enable_llm_review_grounding,
        llm_review_fn=_run_llm_review,
        phase_review_result_cls=PhaseReviewResult,
    )


def run_qa_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    qa_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """
    Run QA testing phase: bug detection, test coverage, quality assurance.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_qa_testing_phase_impl`).
    ``_run_qa_agent`` is referenced by bare module-global name inside ``partial``
    at call time so this module stays the test patch surface.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
    Postconditions:
        - Returns a :class:`PhaseReviewResult`; never raises (shared containment).
    """
    return run_qa_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=qa_agent,
        agent_runner=partial(_run_qa_agent, qa_agent=qa_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
        tool_phase_includes_context=REVIEW_CONFIG.tool_phase_includes_context,
    )


def run_security_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    security_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = PROFILE.default_language,
    cache: Optional[AgentReviewCache] = None,
) -> PhaseReviewResult:
    """
    Run security testing phase: vulnerability scanning, security best practices.

    Thin wrapper over the shared parametrised implementation
    (:func:`software_engineering_team.shared.phases.review.run_security_testing_phase_impl`).
    ``_run_security_agent`` is referenced by bare module-global name inside
    ``partial`` at call time so this module stays the test patch surface.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
    Postconditions:
        - Returns a :class:`PhaseReviewResult`; never raises (shared containment).
    """
    return run_security_testing_phase_impl(
        task=task,
        microtask=microtask,
        files=files,
        review_agent=security_agent,
        agent_runner=partial(_run_security_agent, security_agent=security_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=PhaseReviewResult,
        tool_phase_input_factory=REVIEW_CONFIG.tool_phase_input_factory,
        tool_phase_includes_context=REVIEW_CONFIG.tool_phase_includes_context,
    )


# ---------------------------------------------------------------------------
# Documentation self-review
# ---------------------------------------------------------------------------


def run_documentation_self_review(
    *,
    llm: LLMClient,
    documentation: Dict[str, str],
    code_files: Dict[str, str],
    task_description: str = "",
    min_iterations: int = MIN_DOC_SELF_REVIEW_ITERATIONS,
    max_iterations: int = MAX_DOC_SELF_REVIEW_ITERATIONS,
    quality_threshold: float = DOC_QUALITY_THRESHOLD,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> DocumentationSelfReviewResult:
    """Self-review documentation across iterations for quality refinement.

    Thin wrapper that delegates the chunking/iteration orchestration to the shared
    ``run_documentation_self_review`` helper, passing this team's prompt, parser,
    and ``DocumentationSelfReviewResult`` factory. The Strands ``Agent`` invocation
    is built here so this module stays the patch surface for ``Agent`` and
    ``resolve_text_mode_strands_model``.

    Preconditions:
        - ``documentation`` maps doc file paths to content; ``code_files`` maps
          code file paths to their full source text.

    Postconditions:
        - See ``software_engineering_team.shared.review_utils.run_documentation_self_review``:
          always runs at least ``min_iterations`` (when no chunk fails) and at most
          ``max_iterations``, one LLM call per function-aware code chunk per
          iteration, with per-chunk skip-on-failure and a chunk-failure early-stop
          suppression. Never "fails" — always returns refined documentation.
    """

    def _invoke(prompt: str) -> str:
        return str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()

    return _shared_run_documentation_self_review(
        documentation=documentation,
        code_files=code_files,
        prompt_template=DOCUMENTATION_SELF_REVIEW_PROMPT,
        parse_template=parse_documentation_self_review_template,
        result_factory=DocumentationSelfReviewResult,
        invoke_model=_invoke,
        task_description=task_description,
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        quality_threshold=quality_threshold,
        detail_callback=detail_callback,
    )
