"""Shared ``run_review`` / ``run_microtask_review`` for the code-v2 teams.

The backend and frontend code-v2 teams each used to carry their own ~400-line
``run_review`` / ``run_microtask_review`` bodies that were ~90 % identical and
diverged only on a fixed set of knobs (lint agent type, build-verify label, lint
severity remap, tool-agent issue source prefix / recommendation, whether the
tool-agent phase input carries spec context, whether the run-review ``passed``
flag includes lint, and the summary / log strings). This module collapses that
fork into one parameterised implementation driven by :class:`ReviewConfig`.

The chunking/prompt/parse orchestration (``_run_llm_review``) and the external
QA / security / build-verify runners stay **per-team** (in each team's
``phases/review.py``) because they are the test patch surface for ``Agent`` /
``resolve_text_mode_strands_model`` and inject the team's own prompt/parser and
``ReviewIssue`` factory. The shared bodies here call back into those runners via
injected callables, so the per-team patch surface is preserved and existing
tests stay green without rewriting their patch targets.

Preconditions:
    - ``ReviewConfig`` is constructed once per team (see each team's
      ``phases/_profile.py``) and its callables are pure (no shared mutable
      state).
    - The injected runner callables match the per-team wrapper signatures
      (``_run_llm_review`` / ``_run_qa_agent`` / ``_run_security_agent`` /
      ``_run_build_verification``).

Invariants:
    - This module holds no mutable state; every function is pure with respect to
      its inputs (the only side effects are logging and the injected runners).
    - ``ReviewConfig`` is frozen after construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.review_progress import (
    build_disk_repo_reader,
    call_code_review_agent,
)
from software_engineering_team.shared.security_service import is_blocking
from software_engineering_team.shared.v2_models import Phase, ReviewIssue, ReviewResult

logger = logging.getLogger(__name__)

# The microtask build-failure recommendation is identical across teams, so it is
# a shared constant rather than a config knob.
_MICROTASK_BUILD_FAIL_RECOMMENDATION = "Fix build errors before proceeding."


@dataclass(frozen=True)
class ReviewConfig:
    """Per-team knobs that select backend vs frontend behaviour in the shared
    review bodies.

    Every field corresponds to a concrete divergence that used to live as a
    hard-coded difference between the two teams' ``run_review`` /
    ``run_microtask_review`` implementations. Adding a new divergence means
    adding a field here and a branch in the shared body, not a third fork.

    Invariants:
        - Frozen after construction; callers must not mutate.
        - ``lint_severity_remap`` is either ``None`` (use the raw linter
          severity) or a mapping whose fallback is the input severity itself.
        - ``tool_rec_source_prefix`` is either ``None`` (use ``kind.value``
          verbatim) or a prefix prepended to ``kind.value``.
    """

    # ``agent_type`` passed to the linting tool agent's ``LintToolInput``.
    lint_agent_type: str
    # Label passed to ``build_verifier(repo_path, label, task_id)``.
    build_verify_label: str
    # ``recommendation`` on the run-review (non-microtask) build-failure issue.
    build_fail_recommendation_review: str
    # Remap linter severities into review severities, or ``None`` to keep raw.
    lint_severity_remap: Optional[Mapping[str, str]]
    # Prefix for the ``source`` of a tool-agent recommendation issue, or ``None``
    # to use ``kind.value`` verbatim (backend prefixes ``tool_``).
    tool_rec_source_prefix: Optional[str]
    # Whether a tool-agent recommendation issue copies the recommendation text
    # into its ``recommendation`` field (backend) or leaves it blank (frontend).
    tool_rec_recommendation_uses_rec: bool
    # Whether the tool-agent phase input carries ``existing_code`` /
    # ``spec_context`` / ``language`` (backend) or omits them (frontend).
    tool_phase_includes_context: bool
    # Whether the run-review ``passed`` flag includes ``lint_ok`` (frontend) or
    # ignores it (backend). The microtask review always includes ``lint_ok``.
    passed_includes_lint_review: bool
    # Whether run-review logs its summary line at INFO (backend) or is silent
    # (frontend). The microtask review always logs its summary.
    log_review_summary: bool
    # Per-team ``ToolAgentPhaseInput`` constructor (the one per-team model that
    # the shared body must instantiate — it binds the team ``Microtask``/enum).
    tool_phase_input_factory: Callable[..., Any]
    # ``summary_review(passed, build_ok, lint_ok, n_issues, n_critical) -> str``.
    summary_review: Callable[..., str]
    # ``summary_microtask(microtask_id, passed, build_ok, lint_ok, n_issues,
    # n_critical) -> str``.
    summary_microtask: Callable[..., str]
    # ``microtask_intro(microtask_id, n_files) -> str`` for the opening INFO line.
    microtask_intro: Callable[..., str]


def _lint_severity(config: ReviewConfig, raw: str) -> str:
    """Map a raw linter severity to a review severity using the config remap.

    Preconditions: ``raw`` is a string (may be empty).
    Postconditions: returns the remapped severity when a remap is configured and
    has an entry, otherwise ``raw`` unchanged. Pure.
    """
    if config.lint_severity_remap is None:
        return raw
    return config.lint_severity_remap.get(raw, raw)


def _tool_rec_source(config: ReviewConfig, kind_value: str) -> str:
    """Build the ``source`` for a tool-agent recommendation issue.

    Preconditions: ``kind_value`` is a non-empty string.
    Postconditions: returns ``f"{prefix}{kind_value}"`` when a prefix is
    configured, else ``kind_value``. Pure.
    """
    if config.tool_rec_source_prefix is None:
        return kind_value
    return f"{config.tool_rec_source_prefix}{kind_value}"


def _tool_rec_recommendation(config: ReviewConfig, rec: str) -> str:
    """Return the ``recommendation`` for a tool-agent recommendation issue.

    Preconditions: ``rec`` is a string.
    Postconditions: returns ``rec`` when the team copies it through, else ``""``.
    Pure.
    """
    return rec if config.tool_rec_recommendation_uses_rec else ""


def _run_tool_agents_review(
    config: ReviewConfig,
    *,
    task: Task,
    issues: List[ReviewIssue],
    tool_agents: Optional[Dict[Any, Any]],
    task_id: str,
    task_description: str,
    current_files: Dict[str, str],
    tool_repo_path: str = "",
    microtask: Any = None,
    failure_context: str = "",
    language: str = "",
) -> None:
    """Run each wired tool agent's ``review`` and fold its output into issues.

    Preconditions:
        - ``tool_agents`` is ``None`` or a ``{ToolAgentKind: agent}`` mapping.
        - ``config.tool_phase_input_factory`` accepts the kwargs built here.

    Postconditions:
        - Each agent with a ``review`` method contributes its ``issues`` and a
          ``ReviewIssue`` per recommendation; a raising agent is logged and
          skipped (never aborts the review). Mutates ``issues`` in place.
    """
    if not tool_agents:
        return

    phase_inp_kwargs: Dict[str, Any] = {
        "phase": Phase.REVIEW,
        "repo_path": tool_repo_path,
        "current_files": current_files,
        "review_issues": issues,
        "task_title": task.title or "",
        "task_description": task_description,
    }
    if microtask is not None:
        phase_inp_kwargs["microtask"] = microtask
        phase_inp_kwargs["task_id"] = task_id
    if config.tool_phase_includes_context:
        phase_inp_kwargs["existing_code"] = ""
        phase_inp_kwargs["spec_context"] = task.description or ""
        phase_inp_kwargs["language"] = language

    phase_inp = config.tool_phase_input_factory(**phase_inp_kwargs)
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
                            source=_tool_rec_source(config, kind.value),
                            severity="info",
                            description=rec,
                            recommendation=_tool_rec_recommendation(config, rec),
                        )
                    )
        except Exception as exc:
            logger.warning(
                "[%s] Tool agent %s review() failed%s: %s",
                task_id,
                kind.value,
                failure_context,
                exc,
            )


def run_review(
    *,
    config: ReviewConfig,
    llm: LLMClient,
    task: Task,
    execution_result: Any,
    repo_path: Path,
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[Any, Any]] = None,
    language: str,
    llm_review_fn: Callable[..., List[ReviewIssue]],
    qa_agent_fn: Callable[..., List[ReviewIssue]],
    security_agent_fn: Callable[..., List[ReviewIssue]],
    build_verify_fn: Callable[..., Tuple[bool, str]],
) -> ReviewResult:
    """Execute the shared Review phase over an execution result's files.

    Preconditions:
        - ``execution_result`` exposes ``.files: Dict[str, str]``.
        - The injected runners match the per-team wrapper signatures.

    Postconditions:
        - Returns a :class:`ReviewResult` whose ``passed`` reflects the team's
          blocking + (optionally) lint policy; ``build_ok``/``lint_ok`` report
          the individual gate outcomes. Never raises on a tool/agent failure
          (those are logged and skipped).
    """
    task_id = task.id
    issues: List[ReviewIssue] = []

    # 1. Build verification
    build_ok, build_msg = build_verify_fn(repo_path, build_verifier, task_id)
    if not build_ok:
        issues.append(
            ReviewIssue(
                source="build",
                severity="critical",
                description=f"Build failed: {build_msg}",
                recommendation=config.build_fail_recommendation_review,
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
                    agent_type=config.lint_agent_type,
                    task_id=task_id,
                    task_description=task.description or "",
                )
            )
            if lint_result and not getattr(
                lint_result.execution_result, "success", getattr(lint_result, "passed", True)
            ):
                lint_ok = False
                for li in getattr(lint_result, "linter_issues", getattr(lint_result, "issues", [])):
                    sev = getattr(li, "severity", "medium")
                    issues.append(
                        ReviewIssue(
                            source="lint",
                            severity=_lint_severity(config, sev),
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
            cr_result = call_code_review_agent(
                code_review_agent, cr_input, None, repo_reader=build_disk_repo_reader(repo_path)
            )
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
            issues.extend(llm_review_fn(llm=llm, task=task, files=execution_result.files))
    else:
        issues.extend(llm_review_fn(llm=llm, task=task, files=execution_result.files))

    # 4. QA agent — chunked so large reviews are not truncated.
    if qa_agent is not None:
        issues.extend(
            qa_agent_fn(
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
            security_agent_fn(
                security_agent=security_agent,
                files=execution_result.files,
                language=language,
                task_description=task.description or "",
                task_id=task_id,
            )
        )

    # 6. Domain-specific review from tool agents
    _run_tool_agents_review(
        config,
        task=task,
        issues=issues,
        tool_agents=tool_agents,
        task_id=task_id,
        task_description=task.description or "",
        current_files=execution_result.files,
        language=language,
    )

    critical_or_high = [i for i in issues if is_blocking(i.severity)]
    blocking_ok = len(critical_or_high) == 0
    passed = build_ok and blocking_ok and (lint_ok if config.passed_includes_lint_review else True)

    summary = config.summary_review(passed, build_ok, lint_ok, len(issues), len(critical_or_high))
    if config.log_review_summary:
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
    config: ReviewConfig,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
    qa_agent: Any = None,
    security_agent: Any = None,
    code_review_agent: Any = None,
    linting_tool_agent: Any = None,
    tool_agents: Optional[Dict[Any, Any]] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str,
    llm_review_fn: Callable[..., List[ReviewIssue]],
    qa_agent_fn: Callable[..., List[ReviewIssue]],
    security_agent_fn: Callable[..., List[ReviewIssue]],
    build_verify_fn: Callable[..., Tuple[bool, str]],
) -> ReviewResult:
    """Run the shared full review on a single microtask's output files.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
        - The injected runners match the per-team wrapper signatures.

    Postconditions:
        - Returns a :class:`ReviewResult` scoped to ``files``; ``passed``
          includes ``build_ok`` AND ``lint_ok`` AND no blocking issue (both
          teams). Never raises on a tool/agent failure (logged and skipped).
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info("[%s] %s", task_id, config.microtask_intro(microtask_id, len(files)))

    if detail_callback:
        detail_callback("Running build verification...")
    build_ok, build_msg = build_verify_fn(repo_path, build_verifier, task_id)
    if not build_ok:
        issues.append(
            ReviewIssue(
                source="build",
                severity="critical",
                description=f"Build failed after microtask {microtask_id}: {build_msg}",
                recommendation=_MICROTASK_BUILD_FAIL_RECOMMENDATION,
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
                    agent_type=config.lint_agent_type,
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
            cr_result = call_code_review_agent(
                code_review_agent,
                cr_input,
                detail_callback,
                repo_reader=build_disk_repo_reader(repo_path),
            )
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
            issues.extend(llm_review_fn(llm=llm, task=task, files=files))
    else:
        if detail_callback:
            detail_callback("Running code review...")
        issues.extend(llm_review_fn(llm=llm, task=task, files=files))

    microtask_desc = f"Microtask: {microtask.description or microtask.title}"
    microtask_ctx = f" for microtask {microtask_id}"

    if qa_agent is not None:
        if detail_callback:
            detail_callback("Running QA check...")
        issues.extend(
            qa_agent_fn(
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
            security_agent_fn(
                security_agent=security_agent,
                files=files,
                language=language,
                task_description=microtask_desc,
                task_id=task_id,
                context=microtask_ctx,
            )
        )

    _run_tool_agents_review(
        config,
        task=task,
        issues=issues,
        tool_agents=tool_agents,
        task_id=task_id,
        task_description=f"Microtask: {microtask.description or microtask.title}",
        current_files=files,
        tool_repo_path=str(repo_path),
        microtask=microtask,
        failure_context=f" for microtask {microtask_id}",
        language=language,
    )

    critical_or_high = [i for i in issues if is_blocking(i.severity)]
    passed = build_ok and lint_ok and len(critical_or_high) == 0

    summary = config.summary_microtask(
        microtask_id, passed, build_ok, lint_ok, len(issues), len(critical_or_high)
    )
    logger.info("[%s] %s", task_id, summary)

    return ReviewResult(
        passed=passed,
        issues=issues,
        build_ok=build_ok,
        lint_ok=lint_ok,
        summary=summary,
    )


__all__ = ["ReviewConfig", "run_review", "run_microtask_review"]