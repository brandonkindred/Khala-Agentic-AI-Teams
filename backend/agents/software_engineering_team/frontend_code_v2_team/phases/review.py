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
from software_engineering_team.shared.review_progress import (
    build_disk_repo_reader,
    call_code_review_agent,
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


def _code_review_step(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    repo_path: Path,
    code_review_agent: Any,
    language: str,
    task_id: str,
    task_description: str,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> List[ReviewIssue]:
    """Independent code-review step: external agent (with LLM fallback), or LLM review alone.

    Preconditions:
        - ``files`` maps file paths to their full source text. ``task_description`` is the
          description surfaced to the external agent (the caller scopes this to the task or a
          single microtask; the LLM fallback always reasons over the full ``task``, unaffected).

    Postconditions:
        - Never raises: an external ``code_review_agent`` failure logs a warning and falls back
          to the LLM reviewer, matching this step's long-standing solo behavior — this must stay
          true when fanned out concurrently alongside the QA/security steps (see
          ``_review_steps_run_sequentially``'s caller), since one step raising must never drop
          the other two steps' issues.
    """
    if code_review_agent is None:
        return _run_llm_review(llm=llm, task=task, files=files)
    try:
        from code_review_agent.models import CodeReviewInput as _CRInput

        # files= keeps per-file attribution and lets the coordinator bound
        # its own prompts — no header parsing, no upstream truncation.
        cr_input = _CRInput(
            files=files,
            task_description=task_description,
            task_requirements=task.requirements or "",
            acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
            language=language,
        )
        cr_result = call_code_review_agent(
            code_review_agent,
            cr_input,
            detail_callback,
            repo_reader=build_disk_repo_reader(repo_path),
        )
        return [
            ReviewIssue(
                source="code_review",
                severity=getattr(item, "severity", "medium"),
                description=getattr(item, "description", str(item)),
                file_path=getattr(item, "file_path", ""),
                recommendation=getattr(item, "recommendation", ""),
            )
            for item in getattr(cr_result, "issues", [])
        ]
    except Exception as exc:
        logger.warning(
            "[%s] Code review agent failed: %s. Next step -> Using LLM fallback for code review",
            task_id,
            exc,
        )
        return _run_llm_review(llm=llm, task=task, files=files)


def _qa_review_step(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Independent QA step.

    Postconditions:
        - Returns ``[]`` when ``qa_agent`` is None. Otherwise never raises: an outright QA-agent
          failure is reported as a synthetic high-severity issue rather than propagating — a bare
          exception here would previously have aborted the whole review; fanning this step out
          concurrently with code review/security must not make that worse.
    """
    if qa_agent is None:
        return []
    try:
        return _run_qa_agent(
            qa_agent=qa_agent,
            files=files,
            language=language,
            task_description=task_description,
            task_id=task_id,
            context=context,
        )
    except Exception as exc:
        logger.warning("[%s] QA agent step failed outright: %s", task_id, exc)
        return [
            ReviewIssue(
                source="qa",
                severity="high",
                description=f"QA agent failed and could not complete review: {exc}",
                recommendation="Investigate and re-run the QA agent; findings from this run are incomplete.",
            )
        ]


def _security_review_step(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Independent security step.

    Postconditions:
        - Returns ``[]`` when ``security_agent`` is None. Otherwise never raises: an outright
          security-agent failure is reported as a synthetic critical-severity issue rather than
          propagating (see ``_qa_review_step`` for the identical rationale).
    """
    if security_agent is None:
        return []
    try:
        return _run_security_agent(
            security_agent=security_agent,
            files=files,
            language=language,
            task_description=task_description,
            task_id=task_id,
            context=context,
        )
    except Exception as exc:
        logger.warning("[%s] Security agent step failed outright: %s", task_id, exc)
        return [
            ReviewIssue(
                source="security",
                severity="critical",
                description=f"Security agent failed and could not complete review: {exc}",
                recommendation=(
                    "Investigate and re-run the security agent; findings from this run are incomplete."
                ),
            )
        ]


def _review_steps_run_sequentially(llm: LLMClient) -> bool:
    """True when the code-review/QA/security fan-out must run one step at a time.

    Scripted test doubles (a ``DummyLLMClient`` subclass returning canned responses from a shared,
    non-thread-safe index counter — e.g. ``test_microtask_review_gates._ScriptedTextClient``) are
    not safe to call from concurrent threads. Mirrors ``devops_team.orchestrator``'s identical
    ``use_parallel = not isinstance(self.llm, _Dummy)`` guard.
    """
    from llm_service.clients.dummy import DummyLLMClient

    return isinstance(llm, DummyLLMClient)


def _run_review_steps(
    step_fns: List[Callable[[], List[ReviewIssue]]], *, llm: LLMClient
) -> List[ReviewIssue]:
    """Run the code-review/QA/security step thunks, fanned out unless ``llm`` requires sequencing.

    Preconditions:
        - Each element of ``step_fns`` never raises (see ``_code_review_step``/``_qa_review_step``/
          ``_security_review_step``) — required because ``parallel_map`` fast-fails (cancels the
          round's other pending steps and re-raises) on the first worker exception.
    Postconditions:
        - Returns every step's issues concatenated in ``step_fns`` order, regardless of which
          step's underlying call actually completed first.
    """
    if _review_steps_run_sequentially(llm) or len(step_fns) <= 1:
        return [issue for step in step_fns for issue in step()]
    from shared_concurrency import parallel_map

    results = parallel_map(step_fns, lambda fn: fn(), max_workers=len(step_fns), skip_none=False)
    return [issue for step_issues in results for issue in step_issues]


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

    # Code review, QA, and security are independent LLM-backed checks — none reads another's
    # output, they only contribute to the shared `issues` list — so fan them out concurrently
    # (unless `llm` requires sequential calls; see _review_steps_run_sequentially). The
    # tool-agent pass below depends on the combined result of these three and must run after.
    issues.extend(
        _run_review_steps(
            [
                lambda: _code_review_step(
                    llm=llm,
                    task=task,
                    files=execution_result.files,
                    repo_path=repo_path,
                    code_review_agent=code_review_agent,
                    language=language,
                    task_id=task_id,
                    task_description=task.description or "",
                ),
                lambda: _qa_review_step(
                    qa_agent=qa_agent,
                    files=execution_result.files,
                    language=language,
                    task_description=task.description or "",
                    task_id=task_id,
                ),
                lambda: _security_review_step(
                    security_agent=security_agent,
                    files=execution_result.files,
                    language=language,
                    task_description=task.description or "",
                    task_id=task_id,
                ),
            ],
            llm=llm,
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

    # Code review, QA, and security are independent LLM-backed checks — none reads another's
    # output — so fan them out concurrently (unless `llm` requires sequential calls; see
    # _review_steps_run_sequentially). Progress messages are announced up front, in their
    # original order, rather than from inside each step: decoupling "announce" from "complete"
    # means the messages appear in a stable order regardless of which step's call finishes
    # first. Code review's own detail_callback (chunk-level progress during the agent's
    # multi-chunk execution) still threads through — it is the only step that reports granular
    # progress, so there is no concurrent writer to race.
    if detail_callback:
        detail_callback("Running code review...")
    if qa_agent is not None and detail_callback:
        detail_callback("Running QA check...")
    if security_agent is not None and detail_callback:
        detail_callback("Running security scan...")

    microtask_desc = f"Microtask: {microtask.description or microtask.title}"
    microtask_ctx = f" for microtask {microtask_id}"

    issues.extend(
        _run_review_steps(
            [
                lambda: _code_review_step(
                    llm=llm,
                    task=task,
                    files=files,
                    repo_path=repo_path,
                    code_review_agent=code_review_agent,
                    language=language,
                    task_id=task_id,
                    task_description=microtask_desc,
                    detail_callback=detail_callback,
                ),
                lambda: _qa_review_step(
                    qa_agent=qa_agent,
                    files=files,
                    language=language,
                    task_description=microtask_desc,
                    task_id=task_id,
                    context=microtask_ctx,
                ),
                lambda: _security_review_step(
                    security_agent=security_agent,
                    files=files,
                    language=language,
                    task_description=microtask_desc,
                    task_id=task_id,
                    context=microtask_ctx,
                ),
            ],
            llm=llm,
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
