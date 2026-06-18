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
from software_engineering_team.shared.llm_review import run_llm_review
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.review_progress import call_code_review_agent
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REVIEW_CODE_CHARS = 60_000  # Generous limit; review all files, not just first 20
# A file this many chunks deep means an unusually large review; log a warning
# for cost/rate-limit visibility, but still review every chunk (dropping any
# would re-introduce the tail truncation this fallback exists to avoid).
MANY_CHUNKS_WARN_THRESHOLD = 20


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


def _run_chunked_agent_review(
    *,
    run_chunk: Callable[[str], Any],
    files: Dict[str, str],
    source: str,
    default_severity: str,
    label: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Run a quality agent over each file's raw source, one file at a time.

    QA and security agents analyze *source*, so they are fed each file's raw
    content — not the code-review renderer's ``### path ###`` headers or ``N:``
    line-number prefixes, which would make the code syntactically invalid and
    provoke bogus findings. A file larger than ``MAX_REVIEW_CODE_CHARS`` is
    hard-split at character boundaries so no over-budget string is ever sent.

    Preconditions:
        - ``run_chunk(code)`` invokes the agent on one piece of raw source and
          returns its raw issue/vulnerability items.
        - ``files`` maps file paths to their full source text.

    Postconditions:
        - Each non-blank file's raw content is reviewed in full; a file over
          ``MAX_REVIEW_CODE_CHARS`` is split into ≤-cap character pieces and every
          piece is reviewed, so its tail is reviewed rather than truncated away.
          Blank files contribute nothing.
        - A finding's ``file_path`` defaults to the file actually sent when the
          agent does not report a location, so every piece stays attributable.
        - A piece whose ``run_chunk`` call fails is logged and skipped; issues
          from the other pieces are still returned (one bad piece never aborts
          the whole review).
    """
    from software_engineering_team.code_review_agent.coordinator import cap_chunk_content

    blocks = [(path, content) for path, content in files.items() if content and content.strip()]
    if not blocks:
        return []
    # One raw piece per file (split only when a file exceeds the per-call cap).
    pieces = [
        (path, piece)
        for path, content in blocks
        for piece in cap_chunk_content(content, MAX_REVIEW_CODE_CHARS)
    ]
    if len(pieces) > MANY_CHUNKS_WARN_THRESHOLD:
        logger.warning(
            "[%s] %s: %d pieces for %d file(s) — large review, many calls%s",
            task_id,
            label,
            len(pieces),
            len(blocks),
            context,
        )
    issues: List[ReviewIssue] = []
    for idx, (path, piece) in enumerate(pieces, start=1):
        try:
            items = run_chunk(piece)
        except Exception as exc:
            logger.warning(
                "[%s] %s failed (piece %d/%d)%s: %s",
                task_id,
                label,
                idx,
                len(pieces),
                context,
                exc,
            )
            continue
        for item in items or []:
            issues.append(
                ReviewIssue(
                    source=source,
                    severity=getattr(item, "severity", default_severity),
                    description=getattr(item, "description", str(item)),
                    # `location` may be present but None; fall back to file_path
                    # then to the file we sent, so file_path is always a useful
                    # string and tail pieces stay attributable.
                    file_path=getattr(item, "location", None)
                    or getattr(item, "file_path", None)
                    or path,
                    recommendation=getattr(item, "recommendation", ""),
                )
            )
    return issues


def _run_qa_agent(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    context: str = "",
) -> List[ReviewIssue]:
    """Run the external QA agent over function-aware chunks of ``files``.

    Preconditions:
        - ``qa_agent`` is not None and exposes ``.run(QAInput) -> QAOutput``.

    Postconditions: see ``_run_chunked_agent_review``; QA bugs become
    ``ReviewIssue``s with ``source="qa"``.
    """
    from qa_agent.models import QAInput as _QAInput

    def _run_chunk(code: str) -> Any:
        result = qa_agent.run(
            _QAInput(code=code, language=language, task_description=task_description)
        )
        return getattr(result, "bugs_found", getattr(result, "issues", []))

    return _run_chunked_agent_review(
        run_chunk=_run_chunk,
        files=files,
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id=task_id,
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
    """Run the external security agent over function-aware chunks of ``files``.

    Preconditions:
        - ``security_agent`` is not None and exposes
          ``.run(SecurityInput) -> SecurityOutput``.

    Postconditions: see ``_run_chunked_agent_review``; vulnerabilities become
    ``ReviewIssue``s with ``source="security"``.
    """
    from security_agent.models import SecurityInput as _SecInput

    def _run_chunk(code: str) -> Any:
        result = security_agent.run(
            _SecInput(code=code, language=language, task_description=task_description)
        )
        return getattr(result, "vulnerabilities", getattr(result, "issues", []))

    return _run_chunked_agent_review(
        run_chunk=_run_chunk,
        files=files,
        source="security",
        default_severity="high",
        label="Security agent",
        task_id=task_id,
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

# Per-call code-context budget for the documentation self-review. Smaller than
# MAX_REVIEW_CODE_CHARS because the doc-review prompt also carries the full
# documentation being refined plus the template.
MAX_DOC_REVIEW_CHUNK_CHARS = 40_000


def _doc_review_code_chunks(code_files: Dict[str, str]) -> List[str]:
    """Render the code context as bounded, function-aware chunks.

    Preconditions:
        - ``code_files`` maps file paths to their full source text.

    Postconditions:
        - Every non-blank file is covered exactly once across the returned
          strings, split only on function/method/class boundaries; no file is
          dropped and no file is clipped mid-content. Each string's length is
          bounded by ``MAX_DOC_REVIEW_CHUNK_CHARS`` (except a single over-budget
          segment placed alone, per ``build_review_chunks``' contract).
        - Returns ``["(No code context)"]`` when there is no non-blank code, so
          the review still runs one pass.
    """
    # Imported lazily, fully-qualified, to match this module's existing
    # convention for code_review_agent imports (see _run_llm_review).
    from software_engineering_team.code_review_agent.coordinator import build_review_chunks

    blocks = [(p, c) for p, c in code_files.items() if c and c.strip()]
    if not blocks:
        return ["(No code context)"]
    chunks = list(build_review_chunks(blocks, MAX_DOC_REVIEW_CHUNK_CHARS))
    if len(chunks) > MANY_CHUNKS_WARN_THRESHOLD:
        logger.warning(
            "Documentation self-review: %d code chunk(s) for %d file(s) — large input",
            len(chunks),
            len(blocks),
        )
    return [chunk.content for chunk in chunks]


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

    # Function-aware, bounded code context: every file is covered, none clipped
    # mid-function, and no prompt exceeds the per-call budget. Computed once —
    # the code being documented does not change across iterations.
    code_chunks = _doc_review_code_chunks(code_files)

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

        # One LLM call per code chunk, threading the evolving docs through so
        # every chunk of code informs the refinement. The iteration's score is
        # the minimum across chunks (conservative: a later code slice exposing a
        # documentation gap must not let us stop early). If any chunk fails this
        # iteration, the early-stop gate is suppressed so the next iteration
        # re-reviews every chunk — a transient failure on one chunk must not let
        # high scores on the others end the review with that chunk's code unseen.
        iteration_score: Optional[float] = None
        iteration_improvements = 0
        iteration_updates = 0
        chunk_failures = 0
        # Render the evolving documentation once per iteration, then re-render
        # only when a chunk actually updates a file (below) so later chunks still
        # see earlier refinements. Documentation is passed in full (no clip) so
        # the model can rewrite any file's tail; callers are expected to keep
        # per-microtask documentation within the model's context budget. Rebuilding
        # this for every chunk when nothing changed was an O(chunks x docs) waste
        # for large doc sets.
        doc_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_docs.items())
        for chunk_idx, code_chunk in enumerate(code_chunks, start=1):
            prompt = DOCUMENTATION_SELF_REVIEW_PROMPT.format(
                iteration=iteration,
                max_iterations=max_iterations,
                task_description=task_description or "No specific task description",
                documentation=doc_text if doc_text else "(No documentation files yet)",
                code=code_chunk,
            )

            try:
                raw = str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()
                parsed = parse_documentation_self_review_template(raw)
            except Exception as exc:
                # Covers both the LLM call and parsing: a malformed response must
                # not abort the review — log and move to the next chunk.
                logger.warning(
                    "Documentation self-review chunk failed (iteration %d, chunk %d/%d): %s",
                    iteration,
                    chunk_idx,
                    len(code_chunks),
                    exc,
                )
                chunk_failures += 1
                continue

            quality_score = parsed.get("quality_score", 0.5)
            improvements = parsed.get("improvements", [])
            updated_files = parsed.get("files", {})

            iteration_score = (
                quality_score if iteration_score is None else min(iteration_score, quality_score)
            )
            all_improvements.extend(improvements)
            iteration_improvements += len(improvements)

            if updated_files:
                current_docs.update(updated_files)
                iteration_updates += len(updated_files)
                # Docs changed; re-render so subsequent chunks see the refinement.
                doc_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in current_docs.items())

        if iteration_score is None:
            # Every chunk's LLM call failed this iteration; keep prior score.
            logger.info(
                "Documentation self-review iteration %d: all %d chunk(s) failed, score unchanged",
                iteration,
                len(code_chunks),
            )
            continue

        final_score = iteration_score
        logger.info(
            "Documentation self-review iteration %d: score=%.2f, updated %d file(s), %d improvements",
            iteration,
            final_score,
            iteration_updates,
            iteration_improvements,
        )

        if iteration >= min_iterations and final_score >= quality_threshold and chunk_failures == 0:
            logger.info(
                "Documentation self-review complete: reached quality threshold %.2f >= %.2f after %d iterations",
                final_score,
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
