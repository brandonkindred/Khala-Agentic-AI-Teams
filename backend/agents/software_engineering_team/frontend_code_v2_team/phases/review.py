"""
Review phase: code review, build verification, lint, QA, security.

Uses passed-in quality agents when available; LLM-based review otherwise.
No code from frontend_team is used.
"""

from __future__ import annotations

import logging
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
from software_engineering_team.shared.security_service import any_blocking
from software_engineering_team.shared.strands_model import resolve_text_mode_strands_model

from ..models import (
    DocumentationSelfReviewResult,
    ExecutionResult,
    Microtask,
    Phase,
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
    if build_verifier is None:
        return True, "No build verifier provided; skipping."
    try:
        return build_verifier(repo_path, "frontend_code_v2", task_id)
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
    language: str = "typescript",
) -> ReviewResult:
    """Execute the Review phase. Uses passed-in quality agents when available."""
    task_id = task.id
    issues: List[ReviewIssue] = []

    build_ok, build_msg = _run_build_verification(repo_path, build_verifier, task_id)
    if not build_ok:
        issues.append(
            ReviewIssue(
                source="build",
                severity="critical",
                description=f"Build failed: {build_msg}",
                recommendation="Fix build errors; consider triggering Build Specialist.",
            )
        )

    lint_ok = True
    if linting_tool_agent is not None:
        try:
            from linting_tool_agent.models import LintToolInput as _LintInput

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type="frontend",
                    task_id=task_id,
                    task_description=task.description or "",
                )
            )
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
                lint_ok = False
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=getattr(li, "severity", "medium"),
                            description=getattr(li, "message", str(li)),
                            file_path=getattr(li, "file_path", ""),
                            recommendation="",
                        )
                    )
        except Exception as exc:
            logger.warning("[%s] Linting tool agent failed: %s", task_id, exc)

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

    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.REVIEW,
            repo_path=str(repo_path),
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
                    for r in out.recommendations:
                        issues.append(
                            ReviewIssue(
                                source=kind.value, severity="info", description=r, recommendation=""
                            )
                        )
            except Exception as exc:
                logger.warning("[%s] Tool agent %s review() failed: %s", task_id, kind.value, exc)

    passed = build_ok and lint_ok and not any_blocking(issues)
    summary = f"Review {'passed' if passed else 'failed'}; {len(issues)} issue(s)."
    return ReviewResult(
        passed=passed, issues=issues, build_ok=build_ok, lint_ok=lint_ok, summary=summary
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
    language: str = "typescript",
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
        "[%s] Microtask review for %s (%d files). Next step -> Build verification, lint, code review",
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
                    agent_type="frontend",
                    task_id=task_id,
                    task_description=f"Microtask: {microtask.title or microtask_id}",
                )
            )
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
                lint_ok = False
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    file_path = getattr(li, "file_path", "")
                    if files and file_path and file_path not in files:
                        continue
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=getattr(li, "severity", "medium"),
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
                    for r in out.recommendations:
                        issues.append(
                            ReviewIssue(
                                source=kind.value, severity="info", description=r, recommendation=""
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

    passed = build_ok and lint_ok and not any_blocking(issues)
    summary = f"Microtask {microtask_id} review {'passed' if passed else 'failed'}; {len(issues)} issue(s)."
    logger.info("[%s] %s", task_id, summary)
    return ReviewResult(
        passed=passed, issues=issues, build_ok=build_ok, lint_ok=lint_ok, summary=summary
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
