"""
Review phase: code review, build verification, lint, QA, security.

Invokes passed-in quality agents when available; otherwise uses the team's
own LLM-based review. No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.agent_review import run_qa_agent, run_security_agent
from software_engineering_team.shared.llm_review import run_llm_review
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.review_progress import call_code_review_agent
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
from software_engineering_team.shared.strands_model import resolve_text_mode_strands_model

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

logger = logging.getLogger(__name__)


def _run_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
) -> List[ReviewIssue]:
    """LLM-based code review when no external review agent is available.

    Thin wrapper that delegates the chunking/prompt/parse orchestration to the
    shared ``run_llm_review`` helper, passing this team's prompt, parser, and
    ``ReviewIssue`` factory. The Strands ``Agent`` invocation is built here so
    this module stays the patch surface for ``Agent`` and
    ``resolve_text_mode_strands_model``.

    Preconditions:
        - ``files`` maps file paths to their full source text.

    Postconditions:
        - See ``software_engineering_team.shared.llm_review.run_llm_review``:
          function-aware chunking with no tail truncation, per-chunk
          skip-on-failure, single call for small inputs, and a header-preserving
          hard-split for any chunk that is itself over budget (a single line
          longer than the cap).
    """

    def _invoke(prompt: str) -> str:
        return str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()

    return run_llm_review(
        task=task,
        files=files,
        prompt_template=REVIEW_PROMPT,
        parse_template=parse_review_template,
        issue_factory=ReviewIssue,
        invoke_model=_invoke,
        max_chars=MAX_REVIEW_CODE_CHARS,
        warn_threshold=MANY_CHUNKS_WARN_THRESHOLD,
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
) -> ReviewResult:
    """
    Execute the Review phase.

    Uses passed-in quality agents (from the main orchestrator) when available,
    falls back to team-internal LLM review otherwise.
    """
    task_id = task.id
    issues: List[ReviewIssue] = []

    # 1. Build verification
    build_ok, build_msg = _run_build_verification(repo_path, build_verifier, task_id)
    if not build_ok:
        issues.append(
            ReviewIssue(
                source="build",
                severity="critical",
                description=f"Build failed: {build_msg}",
                recommendation="Fix compilation/test errors before proceeding.",
            )
        )

    # 2. Lint verification
    lint_ok = True
    if linting_tool_agent is not None:
        try:
            from linting_tool_agent.models import LintToolInput as _LintInput

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type="backend",
                    task_id=task_id,
                    task_description=task.description or "",
                )
            )
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
                lint_ok = False
                _lint_severity_map = {"error": "high", "warning": "medium", "info": "low"}
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    sev = getattr(li, "severity", "medium")
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=_lint_severity_map.get(sev, "medium"),
                            description=getattr(li, "message", str(li)),
                            file_path=getattr(li, "file_path", ""),
                            recommendation="",
                        )
                    )
        except Exception as exc:
            logger.warning("[%s] Linting tool agent failed: %s", task_id, exc)

    # 3. Code review agent (external) or LLM fallback
    if code_review_agent is not None:
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            # files= keeps per-file attribution and lets the coordinator bound
            # its own prompts — no header parsing, no upstream truncation.
            cr_input = _CRInput(
                files=execution_result.files,
                task_description=task.description or "",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
            )
            cr_result = call_code_review_agent(code_review_agent, cr_input, None)
            for item in getattr(cr_result, "issues", []):
                issues.append(
                    ReviewIssue(
                        source="code_review",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "file_path", ""),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning(
                "[%s] Code review agent failed: %s. Next step -> Using LLM fallback for code review",
                task_id,
                exc,
            )
            issues.extend(_run_llm_review(llm=llm, task=task, files=execution_result.files))
    else:
        issues.extend(_run_llm_review(llm=llm, task=task, files=execution_result.files))

    # 4. QA agent — chunked so large reviews are not truncated to the first
    #    MAX_REVIEW_CODE_CHARS (only the code review agent took untruncated files).
    if qa_agent is not None:
        issues.extend(
            _run_qa_agent(
                qa_agent=qa_agent,
                files=execution_result.files,
                language=language,
                task_description=task.description or "",
                task_id=task_id,
            )
        )

    # 5. Security agent
    if security_agent is not None:
        issues.extend(
            _run_security_agent(
                security_agent=security_agent,
                files=execution_result.files,
                language=language,
                task_description=task.description or "",
                task_id=task_id,
            )
        )

    # 6. Domain-specific review from tool agents
    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.REVIEW,
            repo_path=str(repo_path),
            existing_code="",
            spec_context=task.description or "",
            language=language,
            current_files=execution_result.files,
            review_issues=issues,
            task_title=task.title or "",
            task_description=task.description or "",
        )
        for kind, agent in tool_agents.items():
            if not hasattr(agent, "review"):
                continue
            try:
                out = agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
                if out.recommendations:
                    for rec in out.recommendations:
                        issues.append(
                            ReviewIssue(
                                source=f"tool_{kind.value}",
                                severity="info",
                                description=rec,
                                recommendation=rec,
                            )
                        )
            except Exception as exc:
                logger.warning("[%s] Tool agent %s review() failed: %s", task_id, kind.value, exc)

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = build_ok and len(critical_or_high) == 0

    summary = f"Review: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, {len(issues)} issues ({len(critical_or_high)} critical/high)."
    logger.info("[%s] %s passed=%s", task_id, summary, passed)

    return ReviewResult(
        passed=passed,
        issues=issues,
        build_ok=build_ok,
        lint_ok=lint_ok,
        summary=summary,
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
) -> ReviewResult:
    """
    Run full review on a single microtask's output files.

    This function performs the same checks as run_review() but is scoped to the
    files produced by a single microtask, enabling per-microtask quality gates.

    Args:
        detail_callback: Optional callback to report detailed status messages
            (e.g., "Running build verification...", "Running linter...").
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] Running microtask review for %s (%d files)", task_id, microtask_id, len(files)
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
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
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

    if code_review_agent is not None:
        if detail_callback:
            detail_callback("Running code review...")
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            # files= keeps per-file attribution and lets the coordinator bound
            # its own prompts — no header parsing, no upstream truncation.
            cr_input = _CRInput(
                files=files,
                task_description=f"Microtask: {microtask.description or microtask.title}",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
            )
            cr_result = call_code_review_agent(code_review_agent, cr_input, detail_callback)
            for item in getattr(cr_result, "issues", []):
                issues.append(
                    ReviewIssue(
                        source="code_review",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "file_path", ""),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning(
                "[%s] Code review agent failed for microtask %s: %s. Next step -> Using LLM fallback for code review",
                task_id,
                microtask_id,
                exc,
            )
            issues.extend(_run_llm_review(llm=llm, task=task, files=files))
    else:
        if detail_callback:
            detail_callback("Running code review...")
        issues.extend(_run_llm_review(llm=llm, task=task, files=files))

    microtask_desc = f"Microtask: {microtask.description or microtask.title}"
    microtask_ctx = f" for microtask {microtask_id}"

    if qa_agent is not None:
        if detail_callback:
            detail_callback("Running QA check...")
        issues.extend(
            _run_qa_agent(
                qa_agent=qa_agent,
                files=files,
                language=language,
                task_description=microtask_desc,
                task_id=task_id,
                context=microtask_ctx,
            )
        )

    if security_agent is not None:
        if detail_callback:
            detail_callback("Running security scan...")
        issues.extend(
            _run_security_agent(
                security_agent=security_agent,
                files=files,
                language=language,
                task_description=microtask_desc,
                task_id=task_id,
                context=microtask_ctx,
            )
        )

    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.REVIEW,
            microtask=microtask,
            repo_path=str(repo_path),
            existing_code="",
            spec_context=task.description or "",
            language=language,
            current_files=files,
            review_issues=issues,
            task_title=task.title or "",
            task_description=f"Microtask: {microtask.description or microtask.title}",
            task_id=task_id,
        )
        for kind, agent in tool_agents.items():
            if not hasattr(agent, "review"):
                continue
            try:
                out = agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
                if out.recommendations:
                    for rec in out.recommendations:
                        issues.append(
                            ReviewIssue(
                                source=f"tool_{kind.value}",
                                severity="info",
                                description=rec,
                                recommendation=rec,
                            )
                        )
            except Exception as exc:
                logger.warning(
                    "[%s] Tool agent %s review() failed for microtask %s: %s",
                    task_id,
                    kind.value,
                    microtask_id,
                    exc,
                )

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = build_ok and lint_ok and len(critical_or_high) == 0

    summary = f"Microtask {microtask_id} review: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return ReviewResult(
        passed=passed,
        issues=issues,
        build_ok=build_ok,
        lint_ok=lint_ok,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Phase-specific review functions
# ---------------------------------------------------------------------------


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
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
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

    if code_review_agent is not None:
        if detail_callback:
            detail_callback("Running code review...")
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            # files= keeps per-file attribution and lets the coordinator bound
            # its own prompts — no header parsing, no upstream truncation.
            cr_input = _CRInput(
                files=files,
                task_description=f"Microtask: {microtask.description or microtask.title}",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
            )
            cr_result = call_code_review_agent(code_review_agent, cr_input, detail_callback)
            for item in getattr(cr_result, "issues", []):
                issues.append(
                    ReviewIssue(
                        source="code_review",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "file_path", ""),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning(
                "[%s] Code review agent failed for microtask %s: %s. Next step -> Using LLM fallback for code review",
                task_id,
                microtask_id,
                exc,
            )
            issues.extend(_run_llm_review(llm=llm, task=task, files=files))
    else:
        if detail_callback:
            detail_callback("Running code review...")
        issues.extend(_run_llm_review(llm=llm, task=task, files=files))

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = build_ok and lint_ok and len(critical_or_high) == 0

    summary = f"Code review phase for {microtask_id}: build={'OK' if build_ok else 'FAIL'}, lint={'OK' if lint_ok else 'FAIL'}, {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="code_review",
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
    neither ``review_agent`` nor the spec's tool agent is available.
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
        issues.extend(
            agent_runner(
                files=files,
                language=language,
                task_description=f"Microtask: {microtask.description or microtask.title}",
                task_id=task_id,
                context=f" for microtask {microtask_id}",
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

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
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
        agent_runner=lambda **kw: _run_qa_agent(qa_agent=qa_agent, **kw),
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
        agent_runner=lambda **kw: _run_security_agent(security_agent=security_agent, **kw),
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
    )


def run_documentation_review_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> PhaseReviewResult:
    """
    Run documentation review phase: check for missing/incomplete documentation.

    This phase runs after security testing passes, ensuring all code
    has proper documentation (docstrings, comments, README updates).
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info("[%s] Running documentation review phase for %s", task_id, microtask_id)

    if tool_agents and ToolAgentKind.DOCUMENTATION in tool_agents:
        doc_agent = tool_agents[ToolAgentKind.DOCUMENTATION]
        if hasattr(doc_agent, "review"):
            if detail_callback:
                detail_callback("Running documentation review...")
            try:
                phase_inp = ToolAgentPhaseInput(
                    phase=Phase.REVIEW,
                    microtask=microtask,
                    repo_path=str(repo_path) if repo_path else "",
                    existing_code="",
                    spec_context=task.description or "",
                    language="python",
                    current_files=files,
                    review_issues=issues,
                    task_title=task.title or "",
                    task_description=f"Microtask: {microtask.description or microtask.title}",
                    task_id=task_id,
                )
                out = doc_agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
            except Exception as exc:
                logger.warning(
                    "[%s] Documentation tool agent review failed for microtask %s: %s",
                    task_id,
                    microtask_id,
                    exc,
                )

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = len(critical_or_high) == 0

    summary = f"Documentation review phase for {microtask_id}: {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="documentation",
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
