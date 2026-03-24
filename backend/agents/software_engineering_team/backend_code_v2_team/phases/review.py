"""
Review phase: code review, build verification, lint, QA, security.

Invokes passed-in quality agents when available; otherwise uses the team's
own LLM-based review. No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
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
    """LLM-based code review when no external review agent is available."""
    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:20])
    prompt = REVIEW_PROMPT.format(
        requirements=task.requirements or task.description,
        acceptance_criteria=", ".join(task.acceptance_criteria)
        if task.acceptance_criteria
        else "N/A",
        code=code_text[:12000],
    )
    raw = llm.complete_text(prompt)
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
                description=f"Build failed: {build_msg[:300]}",
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
    code_text = "\n\n".join(
        f"--- {p} ---\n{c}" for p, c in list(execution_result.files.items())[:20]
    )
    code_text_12k = code_text[:12000]
    if code_review_agent is not None:
        try:
            from code_review_agent.models import CodeReviewInput as _CRInput

            cr_input = _CRInput(
                code=code_text_12k,
                task_description=task.description or "",
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language="python",
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

    # 4. QA agent
    if qa_agent is not None:
        try:
            from qa_agent.models import QAInput as _QAInput

            qa_input = _QAInput(
                code=code_text_12k,
                language="python",
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

    # 5. Security agent
    if security_agent is not None:
        try:
            from security_agent.models import SecurityInput as _SecInput

            sec_input = _SecInput(
                code=code_text_12k,
                language="python",
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

    # 6. Domain-specific review from tool agents
    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.REVIEW,
            repo_path=str(repo_path),
            existing_code="",
            spec_context=task.description or "",
            language="python",
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

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:20])
    code_text_12k = code_text[:12000]

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
                language="python",
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
                language="python",
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
                language="python",
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
            existing_code="",
            spec_context=task.description or "",
            language="python",
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

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:20])
    code_text_12k = code_text[:12000]

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
                language="python",
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


def run_qa_testing_phase(
    *,
    task: Task,
    microtask: Microtask,
    files: Dict[str, str],
    qa_agent: Any = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> PhaseReviewResult:
    """
    Run QA testing phase: bug detection, test coverage, quality assurance.

    This phase runs after code review passes, focusing on finding bugs
    and ensuring test coverage.
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] QA testing phase for %s. Next step -> Running QA agent analysis",
        task_id,
        microtask_id,
    )

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:20])
    code_text_12k = code_text[:12000]

    if qa_agent is not None:
        if detail_callback:
            detail_callback("Running QA testing...")
        try:
            from qa_agent.models import QAInput as _QAInput

            qa_input = _QAInput(
                code=code_text_12k,
                language="python",
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

    if tool_agents and ToolAgentKind.TESTING_QA in tool_agents:
        qa_tool_agent = tool_agents[ToolAgentKind.TESTING_QA]
        if hasattr(qa_tool_agent, "review"):
            if detail_callback:
                detail_callback("Running QA tool agent review...")
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
                out = qa_tool_agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
            except Exception as exc:
                logger.warning(
                    "[%s] QA tool agent review failed for microtask %s: %s",
                    task_id,
                    microtask_id,
                    exc,
                )

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = len(critical_or_high) == 0

    summary = f"QA testing phase for {microtask_id}: {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="qa",
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
) -> PhaseReviewResult:
    """
    Run security testing phase: vulnerability scanning, security best practices.

    This phase runs after QA testing passes, focusing on identifying
    security vulnerabilities and ensuring secure coding practices.
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] Security testing phase for %s. Next step -> Running security scan",
        task_id,
        microtask_id,
    )

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(files.items())[:20])
    code_text_12k = code_text[:12000]

    if security_agent is not None:
        if detail_callback:
            detail_callback("Running security scan...")
        try:
            from security_agent.models import SecurityInput as _SecInput

            sec_input = _SecInput(
                code=code_text_12k,
                language="python",
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

    if tool_agents and ToolAgentKind.SECURITY in tool_agents:
        sec_tool_agent = tool_agents[ToolAgentKind.SECURITY]
        if hasattr(sec_tool_agent, "review"):
            if detail_callback:
                detail_callback("Running security tool agent review...")
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
                out = sec_tool_agent.review(phase_inp)
                if out.issues:
                    issues.extend(out.issues)
            except Exception as exc:
                logger.warning(
                    "[%s] Security tool agent review failed for microtask %s: %s",
                    task_id,
                    microtask_id,
                    exc,
                )

    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    passed = len(critical_or_high) == 0

    summary = f"Security testing phase for {microtask_id}: {len(issues)} issues ({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    logger.info("[%s] %s", task_id, summary)

    return PhaseReviewResult(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="security",
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
# Documentation self-review (3-5 iterations)
# ---------------------------------------------------------------------------

MIN_DOC_SELF_REVIEW_ITERATIONS = 3
MAX_DOC_SELF_REVIEW_ITERATIONS = 100
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

    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in code_files.items())

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

        prompt = DOCUMENTATION_SELF_REVIEW_PROMPT.format(
            iteration=iteration,
            max_iterations=max_iterations,
            task_description=task_description or "No specific task description",
            documentation=doc_text if doc_text else "(No documentation files yet)",
            code=code_text if code_text else "(No code context)",
        )

        try:
            raw = llm.complete_text(prompt)
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
