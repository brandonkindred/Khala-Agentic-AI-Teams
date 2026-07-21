"""
Shared code-review-phase implementation for the code-v2 teams.

``run_code_review_phase_impl`` runs the first quality gate after coding: build
verification, then an optional lint pass, then the (already-shared)
code-review step. It has no internal dependency on the QA/security
testing-phase functions, so it is extractable on its own.

Unlike the other ``shared/phases/*.py`` modules, this one does not take a
``models: PhaseModels`` bundle: the body only ever constructs ``ReviewIssue``
(one shared definition, imported directly — not team-varying) and the team's
phase-result type (``PhaseReviewResult`` on backend; there is no frontend
equivalent yet, so it cannot be a ``PhaseModels`` member without breaking
frontend's conformance to that Protocol elsewhere). The result type is instead
injected as a narrow constructor, ``phase_review_result_cls`` — the same
one-off-constructor idiom :class:`~software_engineering_team.shared.v2_review.ReviewConfig`
already uses for ``tool_phase_input_factory``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import ReviewContext, Task
from software_engineering_team.shared.security_service import is_blocking
from software_engineering_team.shared.v2_models import ReviewIssue
from software_engineering_team.shared.v2_review import (
    ReviewConfig,
    _code_review_step,
    _lint_passed,
    _lint_severity,
)

logger = logging.getLogger(__name__)


def run_code_review_phase_impl(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    config: ReviewConfig,
    llm_review_fn: Callable[..., Any],
    build_verify_fn: Callable[..., Tuple[bool, str]],
    phase_review_result_cls: Callable[..., Any],
) -> Any:
    """Run the code-review-only phase: build verification + lint + code review.

    This is the first phase after coding, focusing on code quality, syntax,
    and adherence to coding standards.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
        - ``config`` is the team's :class:`ReviewConfig`; only ``lint_agent_type``
          and ``lint_severity_remap`` (via ``_lint_severity``) are read here.
        - ``llm_review_fn`` is the team's chunking/prompt/parse reviewer,
          forwarded verbatim to the shared ``_code_review_step``.
        - ``build_verify_fn`` is the team's build-verification runner. Callers
          must pass their own module-global reference (not a value captured at
          import time) so that patching it on the caller's module still takes
          effect on the next call.
        - ``phase_review_result_cls`` constructs the team's phase-result type
          from ``passed``/``issues``/``summary``/``phase_name``/``raw_issue_count``.

    Postconditions:
        - Returns a ``phase_review_result_cls`` instance whose ``passed`` is
          build success AND lint success AND no critical/high issue. Never
          raises: every failure path is contained to a synthetic issue or a
          logged warning, mirroring ``_code_review_step``'s existing containment.
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
    build_ok, build_msg = build_verify_fn(repo_path, build_verifier, task_id)
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
            from software_engineering_team.linting_tool_agent.models import (
                LintToolInput as _LintInput,
            )

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type=config.lint_agent_type,
                    task_id=task_id,
                    task_description=f"Microtask: {microtask.title or microtask_id}",
                )
            )
            if lint_result and not _lint_passed(lint_result):
                lint_ok = False
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    file_path = getattr(li, "file_path", "")
                    if files and file_path and file_path not in files:
                        continue
                    sev = getattr(li, "severity", "medium")
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=_lint_severity(config, sev),
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
        llm_review_fn=llm_review_fn,
        review_context=review_context,
        detail_callback=detail_callback,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )
    issues.extend(cr_out.issues)

    critical_or_high = [i for i in issues if is_blocking(i.severity)]
    passed = build_ok and lint_ok and len(critical_or_high) == 0

    summary = (
        f"Code review phase for {microtask_id}: build={'OK' if build_ok else 'FAIL'}, "
        f"lint={'OK' if lint_ok else 'FAIL'}, {len(issues)} issues "
        f"({len(critical_or_high)} critical/high). {'PASSED' if passed else 'FAILED'}"
    )
    logger.info("[%s] %s", task_id, summary)

    return phase_review_result_cls(
        passed=passed,
        issues=issues,
        summary=summary,
        phase_name="code_review",
        raw_issue_count=cr_out.raw_issue_count,
    )
