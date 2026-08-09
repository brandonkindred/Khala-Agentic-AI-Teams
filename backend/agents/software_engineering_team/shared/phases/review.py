"""
Shared review-phase implementations for the code-v2 teams.

``run_code_review_phase_impl`` runs the first quality gate after coding: the
(already-shared) code-review step. Build verification and linting run
elsewhere (the team's own pre-review quality gate, or the separate
``run_review``/``run_microtask_review`` path) — not as part of this phase.

``run_qa_testing_phase_impl`` / ``run_security_testing_phase_impl`` share one
parameterised testing-phase body (``_run_agent_testing_phase``) for the QA and
security gates that follow code review.

Unlike the other ``shared/phases/*.py`` modules, this one does not take a
``models: PhaseModels`` bundle: the body only ever constructs ``ReviewIssue``
(one shared definition, imported directly — not team-varying) and the shared
:class:`~software_engineering_team.shared.v2_models.PhaseReviewResult` (or an
equivalent constructor). The result type is injected as a narrow constructor,
``phase_review_result_cls`` — the same one-off-constructor idiom
:class:`~software_engineering_team.shared.v2_review.ReviewConfig` already uses
for ``tool_phase_input_factory``. Both code-v2 teams re-export
``PhaseReviewResult`` from ``shared.v2_models`` and pass it here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_service import LLMClient
from shared.dev_models.models import ReviewContext, Task
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.security_service import is_blocking
from software_engineering_team.shared.v2_models import Phase, ReviewIssue
from software_engineering_team.shared.v2_review import _code_review_step

logger = logging.getLogger(__name__)


def run_code_review_phase_impl(
    *,
    llm: LLMClient,
    task: Task,
    microtask: Any,
    repo_path: Path,
    files: Dict[str, str],
    code_review_agent: Any = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    llm_review_fn: Callable[..., Any],
    phase_review_result_cls: Callable[..., Any],
) -> Any:
    """Run the code-review-only phase: the code-review step.

    This is the first phase after coding, focusing on code quality, syntax,
    and adherence to coding standards.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
        - ``llm_review_fn`` is the team's chunking/prompt/parse reviewer,
          forwarded verbatim to the shared ``_code_review_step``.
        - ``phase_review_result_cls`` constructs the team's phase-result type
          from ``passed``/``issues``/``summary``/``phase_name``/``raw_issue_count``.

    Postconditions:
        - Returns a ``phase_review_result_cls`` instance whose ``passed`` is
          true iff no critical/high code-review issue was found. Never raises
          on a tool/agent failure: those paths are contained to a synthetic
          issue or a logged warning, mirroring ``_code_review_step``'s
          existing containment. Caller-supplied ``detail_callback`` exceptions
          are not contained — they propagate to the caller.
    """
    task_id = task.id
    microtask_id = microtask.id
    issues: List[ReviewIssue] = []

    logger.info(
        "[%s] Code review phase for %s (%d files). Next step -> Code review",
        task_id,
        microtask_id,
        len(files),
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
    passed = len(critical_or_high) == 0

    summary = (
        f"Code review phase for {microtask_id}: {len(issues)} issues "
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


@dataclass(frozen=True)
class _AgentTestingPhaseSpec:
    """Differences between the QA and security testing phases.

    Both phases share the same shape (external agent pass + optional tool-agent
    review + a "gate skipped" issue when neither is wired); only the labels,
    routed tool kind, and the skipped-gate issue differ.
    """

    phase_name: str  # ReviewIssue.source + PhaseReviewResult.phase_name
    phase_label: str  # e.g. "QA testing" -> "<label> phase for <id>"
    next_step: str  # logged "Next step -> <...>"
    detail_run_msg: str
    tool_kind: str  # enum value, e.g. "testing_qa" / "security"
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
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]],
    repo_path: Optional[Path],
    detail_callback: Optional[Callable[[str], None]],
    language: str,
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
    tool_phase_includes_context: bool,
) -> Any:
    """Shared QA/security testing-phase body parameterised by ``spec``.

    Preconditions: when ``review_agent`` is not None, ``agent_runner`` runs it
    over ``files`` and returns ``ReviewIssue``s. ``cache``: see
    ``software_engineering_team.shared.agent_review``.
    ``tool_phase_includes_context`` matches
    :attr:`~software_engineering_team.shared.v2_review.ReviewConfig.tool_phase_includes_context`
    — when ``False``, ``existing_code`` / ``spec_context`` / ``language`` are
    omitted from the tool-phase input (same rule as ``_run_tool_agents_review``).
    Postconditions: returns a ``phase_review_result_cls`` instance that fails
    on any critical/high issue, including a synthesised "gate skipped" issue
    when neither ``review_agent`` nor the spec's tool agent is available. An
    outright ``agent_runner`` failure never propagates: it is reported as a
    synthetic issue at ``spec.missing_severity`` instead, mirroring
    ``_qa_review_step``/``_security_review_step``'s identical containment.
    An outright tool-agent ``.review()`` failure is contained to a logged
    warning only — no synthetic issue is added, so the phase may still pass
    when the tool agent is the only source of findings. Caller-supplied
    ``detail_callback`` exceptions are not contained — they propagate
    (the announce calls sit outside the agent/tool try/except blocks).
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
                    cache=cache,
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
                phase_inp_kwargs: Dict[str, Any] = {
                    "phase": Phase.REVIEW,
                    "microtask": microtask,
                    "repo_path": str(repo_path) if repo_path else "",
                    "current_files": files,
                    "review_issues": issues,
                    "task_title": task.title or "",
                    "task_description": f"Microtask: {microtask.description or microtask.title}",
                    "task_id": task_id,
                }
                if tool_phase_includes_context:
                    phase_inp_kwargs["existing_code"] = ""
                    phase_inp_kwargs["spec_context"] = task.description or ""
                    phase_inp_kwargs["language"] = language
                phase_inp = tool_phase_input_factory(**phase_inp_kwargs)
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

    return phase_review_result_cls(
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
    tool_kind="testing_qa",
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
    tool_kind="security",
    tool_detail_msg="Running security tool agent review...",
    tool_label="Security",
    missing_agent_label="Security agent",
    gate_label="security gate",
    missing_severity="critical",
    missing_description="Security agent not available — security review was skipped. This is a critical risk.",
    missing_recommendation="Ensure security agent is configured before running the pipeline.",
)


def run_qa_testing_phase_impl(
    *,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any = None,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
    tool_phase_includes_context: bool,
) -> Any:
    """Run QA testing phase: bug detection, test coverage, quality assurance.

    Preconditions:
        - ``agent_runner`` matches the shared helper's runner contract when
          ``review_agent`` is not None.
        - ``phase_review_result_cls`` / ``tool_phase_input_factory`` construct
          the team's result / tool-phase input types.
        - ``tool_phase_includes_context`` is the team's
          :attr:`~software_engineering_team.shared.v2_review.ReviewConfig.tool_phase_includes_context`
          flag (forwarded to ``_run_agent_testing_phase``).
    Postconditions:
        - Delegates to ``_run_agent_testing_phase`` with ``_QA_TESTING_PHASE_SPEC``.
          Never raises on a tool/agent failure (containment is the helper's);
          caller-supplied ``detail_callback`` exceptions propagate.
    """
    return _run_agent_testing_phase(
        spec=_QA_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=review_agent,
        agent_runner=agent_runner,
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=phase_review_result_cls,
        tool_phase_input_factory=tool_phase_input_factory,
        tool_phase_includes_context=tool_phase_includes_context,
    )


def run_security_testing_phase_impl(
    *,
    task: Task,
    microtask: Any,
    files: Dict[str, str],
    review_agent: Any = None,
    agent_runner: Callable[..., List[ReviewIssue]],
    tool_agents: Optional[Dict[Any, Any]] = None,
    repo_path: Optional[Path] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    language: str = "python",
    cache: Optional[AgentReviewCache] = None,
    phase_review_result_cls: Callable[..., Any],
    tool_phase_input_factory: Callable[..., Any],
    tool_phase_includes_context: bool,
) -> Any:
    """Run security testing phase: vulnerability scanning, security best practices.

    Preconditions / Postconditions: same as ``run_qa_testing_phase_impl``, but
    binds ``_SECURITY_TESTING_PHASE_SPEC``.
    """
    return _run_agent_testing_phase(
        spec=_SECURITY_TESTING_PHASE_SPEC,
        task=task,
        microtask=microtask,
        files=files,
        review_agent=review_agent,
        agent_runner=agent_runner,
        tool_agents=tool_agents,
        repo_path=repo_path,
        detail_callback=detail_callback,
        language=language,
        cache=cache,
        phase_review_result_cls=phase_review_result_cls,
        tool_phase_input_factory=tool_phase_input_factory,
        tool_phase_includes_context=tool_phase_includes_context,
    )
