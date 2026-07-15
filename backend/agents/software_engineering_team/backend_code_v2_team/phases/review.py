"""
Review phase: code review, build verification, lint, QA, security.

Invokes passed-in quality agents when available; otherwise uses the team's
own LLM-based review. No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.agent_review import run_qa_agent, run_security_agent
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_spec_excerpt_chars,
)
from software_engineering_team.shared.llm_review import LlmReviewOutput, run_llm_review
from software_engineering_team.shared.models import ReviewContext, Task
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
from software_engineering_team.shared.security_service import is_blocking
from software_engineering_team.shared.strands_model import resolve_text_mode_strands_model
from software_engineering_team.shared.v2_review import (
    _code_review_step,
    _lint_passed,
    _review_steps_run_sequentially,  # noqa: F401  (re-exported for tests)
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
    Phase,
    PhaseReviewResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..output_templates import parse_documentation_self_review_template, parse_review_template
from ..prompts import DOCUMENTATION_SELF_REVIEW_PROMPT, REVIEW_PROMPT
from ._profile import REVIEW_CONFIG

logger = logging.getLogger(__name__)


def _run_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> LlmReviewOutput[ReviewIssue]:
    """LLM-based code review when no external review agent is available.

    Thin wrapper that delegates the chunking/prompt/parse orchestration to the
    shared ``run_llm_review`` helper, passing this team's prompt, parser, and
    ``ReviewIssue`` factory. The Strands ``Agent`` invocation is built here so
    this module stays the patch surface for ``Agent`` and
    ``resolve_text_mode_strands_model``.

    Preconditions:
        - ``files`` maps file paths to their full source text.
        - ``review_context`` bundles the caller's system architecture and project
          specification, when available; ``None`` means "nothing to add" so a
          caller without this context yet keeps working unchanged. Rendered and
          hard-truncated to the same per-chunk caps the coordinator's own
          architecture/spec excerpts use (this runs once per chunk, so an
          uncapped document would repeat its full size in every chunk's prompt).
        - ``enable_llm_review_grounding`` defaults True; when False, skips
          ungrounded-claim filtering in the shared helper (kill switch).

    Postconditions:
        - Returns the shared helper's :class:`LlmReviewOutput` unchanged (issues
          plus their pre-grounding ``raw_issue_count``); see
          ``software_engineering_team.shared.llm_review.run_llm_review`` for the
          full contract: function-aware chunking with no tail truncation,
          per-chunk skip-on-failure, single call for small inputs, and a
          header-preserving hard-split for any chunk that is itself over budget
          (a single line longer than the cap).
    """

    def _invoke(prompt: str) -> str:
        return str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()

    architecture_context = ""
    spec_content = ""
    if review_context is not None:
        if review_context.architecture is not None:
            # Lazy import: code_review_agent submodules are imported on demand
            # rather than at module scope elsewhere in the review call chain
            # (e.g. _code_review_step's CodeReviewInput import), so this module
            # follows the same convention rather than adding a new eager edge.
            from code_review_agent.architecture_context import render_architecture_context

            architecture_context = render_architecture_context(review_context.architecture)
        spec_content = review_context.spec_content or ""
        # Bounded here (only when there is context to bound): this runs once per
        # chunk, so an uncapped document would repeat its full size in every
        # chunk's prompt. Skipped entirely with no review_context so a caller's
        # bare llm handle (e.g. a test double without get_max_context_tokens)
        # is never touched when there is nothing to bound.
        architecture_context = architecture_context[: compute_code_review_arch_overview_chars(llm)]
        spec_content = spec_content[: compute_code_review_spec_excerpt_chars(llm)]

    return run_llm_review(
        task=task,
        files=files,
        prompt_template=REVIEW_PROMPT,
        parse_template=parse_review_template,
        issue_factory=ReviewIssue,
        invoke_model=_invoke,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
        architecture_context=architecture_context,
        spec_content=spec_content,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )


def _run_qa_agent(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Run the external QA agent over each file's raw, function-aware-split source.

    Thin wrapper that delegates to the shared ``run_qa_agent``, injecting this
    team's ``ReviewIssue`` factory and chunking constants.

    Preconditions:
        - ``qa_agent`` is not None and exposes ``.run(QAInput) -> QAOutput``.

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
    )


def _run_security_agent(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Run the external security agent over each file's raw, function-aware-split source.

    Thin wrapper that delegates to the shared ``run_security_agent``, injecting
    this team's ``ReviewIssue`` factory and chunking constants.

    Preconditions:
        - ``security_agent`` is not None and exposes
          ``.run(SecurityInput) -> SecurityOutput``.

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
        return build_verifier(repo_path, "backend_code_v2", task_id)
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
    language: str = "python",
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> ReviewResult:
    """Execute the Review phase.

    Thin wrapper over the shared parametrised reviewer
    (:func:`software_engineering_team.shared.v2_review.run_review`) driven by this
    team's :data:`REVIEW_CONFIG`. The per-team chunking/prompt/parse reviewer and
    the external QA/security/build runners are injected as module-level callables
    so this module stays the test patch surface for ``Agent`` /
    ``resolve_text_mode_strands_model`` / ``_run_qa_agent`` / ``_run_security_agent``.

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
    language: str = "python",
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> ReviewResult:
    """Run full review on a single microtask's output files.

    Thin wrapper over
    :func:`software_engineering_team.shared.v2_review.run_microtask_review`; see
    :func:`run_review` for the injection rationale.
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
    )


def run_code_review_phase(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Microtask,
    repo_path: Path,
    files: Dict[str, str],
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> PhaseReviewResult:
    """
    Run code review phase only: build verification + lint + code review.

    This is the first phase after coding, focusing on code quality, syntax,
    and adherence to coding standards.
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] Code review phase for %s (%d files). Next step -> Build verification, lint, code review",
        task_id,
        microtask_id,
        len(files),
    )

    if detail_callback:
        detail_callback("Running build verification...")
    build_ok, build_msg = _run_build_verification(repo_path, build_verifier, task_id)
    if not build_ok:
        issues.append(
            ReviewIssue(
                source="build",
                severity="critical",
                description=f"Build failed after microtask {microtask_id}: {build_msg}",
                recommendation="Fix build errors before proceeding.",
            )
        )

    lint_ok = True
    if linting_tool_agent is not None:
        if detail_callback:
            detail_callback("Running linter...")
        try:
            from linting_tool_agent.models import LintToolInput as _LintInput

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type="backend",
                    task_id=task_id,
                    task_description=f"Microtask: {microtask.title or microtask_id}",
                )
            )
            if lint_result and not _lint_passed(lint_result):
                lint_ok = False
                _lint_severity_map = {"error": "high", "warning": "medium", "info": "low"}
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    file_path = getattr(li, "file_path", "")
                    if files and file_path and file_path not in files:
                        continue
                    sev = getattr(li, "severity", "medium")
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=_lint_severity_map.get(sev, "medium"),
                            description=getattr(li, "message", str(li)),
                            file_path=file_path,
                            recommendation="",
                        )
                    )
        except Exception as exc:
            logger.warning(
                "[%s] Linting tool agent failed for microtask %s: %s", task_id, microtask_id, exc
            )

    if detail_callback:
        detail_callback("Running code review...")
    # Delegates to the shared code-review step (agent call + LLM fallback + outright-failure
    # containment) instead of reimplementing it, so this phase never diverges from run_review's/
    # run_microtask_review's behavior.
    cr_out = _code_review_step(
        llm=llm,
        task=task,
        files=files,
        repo_path=repo_path,
        code_review_agent=code_review_agent,
        language=language,
        task_id=task_id,
        task_description=f"Microtask: {microtask.description or microtask.title}",
        llm_review_fn=_run_llm_review,
        review_context=review_context,
        detail_callback=detail_callback,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )
    issues.extend(cr_out.issues)

    critical_or_high = [i for i in issues if is_blocking(i.severity)]
    passed = build_ok and lint_ok and len(critical_or_high) == 0

    summary = f"Code review phase for {microtask_id}: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="code_review",
        raw_issue_count=cr_out.raw_issue_count,
    )


@dataclass(frozen=True)
class _AgentTestingPhaseSpec:
    """Differences between the QA and security testing phases.

    Both phases share the same shape (external agent pass + optional tool-agent
    review + a "gate skipped" issue when neither is wired); only the labels,
    routed :class:`ToolAgentKind`, and the skipped-gate issue differ.
    """

    phase_name: str  # ReviewIssue.source + PhaseReviewResult.phase_name
    phase_label: str  # e.g. "QA testing" -> "<label> phase for <id>"
    next_step: str  # logged "Next step -> <...>"
    detail_run_msg: str
    tool_kind: "ToolAgentKind"
    tool_detail_msg: str
    tool_label: str  # used in the "<label> tool agent review failed" warning
    missing_agent_label: str  # e.g. "QA agent"
    gate_label: str  # e.g. "QA gate"
    missing_severity: str
    missing_description: str
    missing_recommendation: str


def _run_agent_testing_phase(
    *,
    spec: _AgentTestingPhaseSpec,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    review_agent: Any,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[ToolAgentKind, Any]],
    repo_path: Optional[Path],
    detail_callback: Optional[Callable[[str], None]],
    language: str,
) -> PhaseReviewResult:
    """Shared QA/security testing-phase body parameterised by ``spec``.

    Preconditions: when ``review_agent`` is not None, ``agent_runner`` runs it
    over ``files`` and returns ``ReviewIssue``s.
    Postconditions: returns a :class:`PhaseReviewResult` that fails on any
    critical/high issue, including a synthesised "gate skipped" issue when
    neither ``review_agent`` nor the spec's tool agent is available. An
    outright ``agent_runner`` failure never propagates: it is reported as a
    synthetic issue at ``spec.missing_severity`` instead, mirroring
    ``_qa_review_step``/``_security_review_step``'s identical containment.
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] %s phase for %s. Next step -> %s",
        task_id,
        spec.phase_label,
        microtask_id,
        spec.next_step,
    )

    if review_agent is not None:
        if detail_callback:
            detail_callback(spec.detail_run_msg)
        try:
            issues.extend(
                agent_runner(
                    files=files,
                    language=language,
                    task_description=f"Microtask: {microtask.description or microtask.title}",
                    task_id=task_id,
                    context=f" for microtask {microtask_id}",
                )
            )
        except Exception as exc:
            logger.warning(
                "[%s] %s failed outright for microtask %s: %s",
                task_id,
                spec.missing_agent_label,
                microtask_id,
                exc,
            )
            issues.append(
                ReviewIssue(
                    source=spec.phase_name,
                    severity=spec.missing_severity,
                    description=f"{spec.missing_agent_label} failed and could not complete review: {exc}",
                    recommendation=(
                        f"Investigate and re-run the {spec.missing_agent_label.lower()}; "
                        "findings from this run are incomplete."
                    ),
                )
            )

    has_tool_agent = bool(tool_agents and spec.tool_kind in tool_agents)
    if has_tool_agent:
        tool_agent = tool_agents[spec.tool_kind]
        if hasattr(tool_agent, "review"):
            if detail_callback:
                detail_callback(spec.tool_detail_msg)
            try:
                phase_inp = ToolAgentPhaseInput(
                    phase=Phase.REVIEW,
                    microtask=microtask,
                    repo_path=str(repo_path) if repo_path else "",
                    existing_code="",
                    spec_context=task.description or "",
                    language=language,
                    current_files=files,
                    review_issues=issues,
                    task_title=task.title or "",
                    task_description=f"Microtask: {microtask.description or microtask.title}",
                    task_id=task_id,
                )
                out = tool_agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
            except Exception as exc:
                logger.warning(
                    "[%s] %s tool agent review failed for microtask %s: %s",
                    task_id,
                    spec.tool_label,
                    microtask_id,
                    exc,
                )

    if review_agent is None and not has_tool_agent:
        logger.warning(
            "[%s] %s not available for microtask %s — %s skipped",
            task_id,
            spec.missing_agent_label,
            microtask_id,
            spec.gate_label,
        )
        issues.append(
            ReviewIssue(
                source=spec.phase_name,
                severity=spec.missing_severity,
                description=spec.missing_description,
                recommendation=spec.missing_recommendation,
            )
        )

    critical_or_high = [i for i in issues if is_blocking(i.severity)]
    passed = len(critical_or_high) == 0

    summary = f"{spec.phase_label} phase for {microtask_id}: {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name=spec.phase_name,
    )


_QA_TESTING_PHASE_SPEC = _AgentTestingPhaseSpec(
    phase_name="qa",
    phase_label="QA testing",
    next_step="Running QA agent analysis",
    detail_run_msg="Running QA testing...",
    tool_kind=ToolAgentKind.TESTING_QA,
    tool_detail_msg="Running QA tool agent review...",
    tool_label="QA",
    missing_agent_label="QA agent",
    gate_label="QA gate",
    missing_severity="high",
    missing_description="QA agent not available — QA review was skipped. This is a quality risk.",
    missing_recommendation="Ensure QA agent is configured before running the pipeline.",
)

_SECURITY_TESTING_PHASE_SPEC = _AgentTestingPhaseSpec(
    phase_name="security",
    phase_label="Security testing",
    next_step="Running security scan",
    detail_run_msg="Running security scan...",
    tool_kind=ToolAgentKind.SECURITY,
    tool_detail_msg="Running security tool agent review...",
    tool_label="Security",
    missing_agent_label="Security agent",
    gate_label="security gate",
    missing_severity="critical",
    missing_description="Security agent not available — security review was skipped. This is a critical risk.",
    missing_recommendation="Ensure security agent is configured before running the pipeline.",
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
    language: str = "python",
) -> PhaseReviewResult:
    """
    Run QA testing phase: bug detection, test coverage, quality assurance.

    This phase runs after code review passes, focusing on finding bugs
    and ensuring test coverage.
    """
    return _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=qa_agent,
        agent_runner=partial(_run_qa_agent, qa_agent=qa_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
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
    language: str = "python",
) -> PhaseReviewResult:
    """
    Run security testing phase: vulnerability scanning, security best practices.

    This phase runs after QA testing passes, focusing on identifying
    security vulnerabilities and ensuring secure coding practices.
    """
    return _run_agent_testing_phase(
        spec=_SECURITY_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=security_agent,
        agent_runner=partial(_run_security_agent, security_agent=security_agent),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
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
