"""
Review phase: code review, build verification, lint, QA, security.

Uses passed-in quality agents when available; LLM-based review otherwise.
No code from frontend_team is used.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import Task

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REVIEW_CODE_CHARS = 60_000  # Generous limit; review all files, not just first 20


def _run_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
) -> List[ReviewIssue]:
    """LLM-based code review when no external review agent is available."""
    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items()))
    prompt = REVIEW_PROMPT.format(
        requirements=task.requirements or task.description,
        acceptance_criteria=", ".join(task.acceptance_criteria)
        if task.acceptance_criteria
        else "N/A",
        code=code_text[:MAX_REVIEW_CODE_CHARS],
    )
    raw = llm.complete_text(prompt, think=True)
    data = parse_review_template(raw)
    issues: List[ReviewIssue] = []
    for item in data.get("issues") or []:
        if isinstance(item, dict):
            issues.append(
                ReviewIssue(
                    source=item.get("source", "code_review"),
                    severity=item.get("severity", "medium"),
                    description=item.get("description", ""),
                    file_path=item.get("file_path", ""),
                    recommendation=item.get("recommendation", ""),
                )
            )
    return issues


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
                description=f"Build failed: {build_msg[:300]}",
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

    code_text = "\n\n".join(
        f"--- {p} ---\n{c}" for p, c in list(execution_result.files.items())
    )
    code_text_12k = code_text[:MAX_REVIEW_CODE_CHARS]
    if code_review_agent is not None:
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            cr_input = _CRInput(
                code=code_text_12k,
                task_description=task.description or "",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
            )
            cr_result = code_review_agent.run(cr_input)
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
        try:
            from qa_agent.models import QAInput as _QAInput

            qa_input = _QAInput(
                code=code_text_12k,
                language=language,
                task_description=task.description or "",
            )
            qa_result = qa_agent.run(qa_input)
            for item in getattr(qa_result, "bugs_found", getattr(qa_result, "issues", [])):
                issues.append(
                    ReviewIssue(
                        source="qa",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "location", getattr(item, "file_path", "")),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning("[%s] QA agent failed: %s", task_id, exc)

    if security_agent is not None:
        try:
            from security_agent.models import SecurityInput as _SecInput

            sec_input = _SecInput(
                code=code_text_12k,
                language=language,
                task_description=task.description or "",
            )
            sec_result = security_agent.run(sec_input)
            for item in getattr(sec_result, "vulnerabilities", getattr(sec_result, "issues", [])):
                issues.append(
                    ReviewIssue(
                        source="security",
                        severity=getattr(item, "severity", "high"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "location", getattr(item, "file_path", "")),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning("[%s] Security agent failed: %s", task_id, exc)

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

    passed = (
        build_ok and lint_ok and len([i for i in issues if i.severity in ("critical", "high")]) == 0
    )
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
                description=f"Build failed after microtask {microtask_id}: {build_msg[:300]}",
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

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items()))
    code_text_12k = code_text[:MAX_REVIEW_CODE_CHARS]

    if code_review_agent is not None:
        if detail_callback:
            detail_callback("Running code review...")
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            cr_input = _CRInput(
                code=code_text_12k,
                task_description=f"Microtask: {microtask.description or microtask.title}",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
            )
            cr_result = code_review_agent.run(cr_input)
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

    if qa_agent is not None:
        if detail_callback:
            detail_callback("Running QA check...")
        try:
            from qa_agent.models import QAInput as _QAInput

            qa_input = _QAInput(
                code=code_text_12k,
                language=language,
                task_description=f"Microtask: {microtask.description or microtask.title}",
            )
            qa_result = qa_agent.run(qa_input)
            for item in getattr(qa_result, "bugs_found", getattr(qa_result, "issues", [])):
                issues.append(
                    ReviewIssue(
                        source="qa",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "location", getattr(item, "file_path", "")),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning("[%s] QA agent failed for microtask %s: %s", task_id, microtask_id, exc)

    if security_agent is not None:
        if detail_callback:
            detail_callback("Running security scan...")
        try:
            from security_agent.models import SecurityInput as _SecInput

            sec_input = _SecInput(
                code=code_text_12k,
                language=language,
                task_description=f"Microtask: {microtask.description or microtask.title}",
            )
            sec_result = security_agent.run(sec_input)
            for item in getattr(sec_result, "vulnerabilities", getattr(sec_result, "issues", [])):
                issues.append(
                    ReviewIssue(
                        source="security",
                        severity=getattr(item, "severity", "high"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "location", getattr(item, "file_path", "")),
                        recommendation=getattr(item, "recommendation", ""),
                    )
                )
        except Exception as exc:
            logger.warning(
                "[%s] Security agent failed for microtask %s: %s", task_id, microtask_id, exc
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

    passed = (
        build_ok and lint_ok and len([i for i in issues if i.severity in ("critical", "high")]) == 0
    )
    summary = f"Microtask {microtask_id} review {'passed' if passed else 'failed'}; {len(issues)} issue(s)."
    logger.info("[%s] %s", task_id, summary)
    return ReviewResult(
        passed=passed, issues=issues, build_ok=build_ok, lint_ok=lint_ok, summary=summary
    )


# ---------------------------------------------------------------------------
# Documentation self-review (3-5 iterations)
# ---------------------------------------------------------------------------

MIN_DOC_SELF_REVIEW_ITERATIONS = 3
MAX_DOC_SELF_REVIEW_ITERATIONS = 3
DOC_QUALITY_THRESHOLD = 0.9


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
    """
    Self-review documentation 3-5 times for quality refinement.

    This function iteratively reviews and improves documentation files.
    It always runs at least min_iterations times, and continues up to
    max_iterations unless the quality score exceeds the threshold.

    Unlike other review phases, this never "fails" - it always produces
    refined documentation after the specified number of iterations.

    Args:
        llm: LLM client for generating reviews
        documentation: Current documentation files (path -> content)
        code_files: Code files being documented (for context)
        task_description: Description of the task for context
        min_iterations: Minimum number of review iterations (default: 3)
        max_iterations: Maximum number of review iterations (default: 5)
        quality_threshold: Quality score at which to stop early (default: 0.9)
        detail_callback: Optional callback for status updates

    Returns:
        DocumentationSelfReviewResult with refined documentation
    """
    current_docs = dict(documentation)
    all_improvements: List[str] = []
    final_score = 0.5
    iterations_performed = 0

    code_text = "\n\n".join(f"--- {p} ---\n{c[:2000]}" for p, c in list(code_files.items())[:10])
    code_text_truncated = code_text[:8000]

    for iteration in range(1, max_iterations + 1):
        iterations_performed = iteration

        if detail_callback:
            detail_callback(f"Documentation self-review iteration {iteration}/{max_iterations}...")

        logger.info(
            "Documentation self-review iteration %d/%d. Quality threshold: %.2f",
            iteration,
            max_iterations,
            quality_threshold,
        )

        doc_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_docs.items())
        doc_text_truncated = doc_text[:12000]

        prompt = DOCUMENTATION_SELF_REVIEW_PROMPT.format(
            iteration=iteration,
            max_iterations=max_iterations,
            task_description=task_description or "No specific task description",
            documentation=doc_text_truncated
            if doc_text_truncated
            else "(No documentation files yet)",
            code=code_text_truncated if code_text_truncated else "(No code context)",
        )

        try:
            raw = llm.complete_text(prompt, think=True)
        except Exception as exc:
            logger.warning(
                "Documentation self-review LLM call failed (iteration %d): %s",
                iteration,
                exc,
            )
            continue

        parsed = parse_documentation_self_review_template(raw)
        quality_score = parsed.get("quality_score", 0.5)
        improvements = parsed.get("improvements", [])
        updated_files = parsed.get("files", {})

        final_score = quality_score
        all_improvements.extend(improvements)

        if updated_files:
            current_docs.update(updated_files)
            logger.info(
                "Documentation self-review iteration %d: score=%.2f, updated %d file(s), %d improvements",
                iteration,
                quality_score,
                len(updated_files),
                len(improvements),
            )
        else:
            logger.info(
                "Documentation self-review iteration %d: score=%.2f, no file changes, %d improvements noted",
                iteration,
                quality_score,
                len(improvements),
            )

        if iteration >= min_iterations and quality_score >= quality_threshold:
            logger.info(
                "Documentation self-review complete: reached quality threshold %.2f >= %.2f after %d iterations",
                quality_score,
                quality_threshold,
                iteration,
            )
            break

    summary = (
        f"Documentation self-review completed after {iterations_performed} iteration(s). "
        f"Final quality score: {final_score:.2f}. "
        f"Total improvements made: {len(all_improvements)}."
    )
    logger.info(summary)

    if detail_callback:
        detail_callback(f"Documentation self-review complete (score: {final_score:.2f})")

    return DocumentationSelfReviewResult(
        documentation=current_docs,
        iterations=iterations_performed,
        final_quality_score=final_score,
        improvements_made=all_improvements,
        summary=summary,
    )
