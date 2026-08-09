"""Shared ``run_review`` / ``run_microtask_review`` for the code-v2 teams.

The backend and frontend code-v2 teams each used to carry their own ~400-line
``run_review`` / ``run_microtask_review`` bodies that were ~90 % identical and
diverged only on a fixed set of knobs (lint agent type, build-verify label, lint
severity remap, tool-agent issue source prefix / recommendation, whether the
tool-agent phase input carries spec context, whether the run-review ``passed``
flag includes lint, and the summary / log strings). This module collapses that
fork into one parameterised implementation driven by :class:`ReviewConfig`.

The external QA / security / build-verify runners stay **per-team** (in each
team's ``phases/review.py``) so each team can inject its own prompt/parser and
``ReviewIssue`` factory. The code-review LLM fallback (``_run_llm_review``) is
a thin per-team wrapper over this module's :func:`run_coordinator_llm_review`,
which calls ``code_review_agent.coordinator.run_coordinator`` directly for
both teams — neither is a Strands ``Agent`` / ``resolve_text_mode_strands_model``
patch surface for code review (only ``run_documentation_self_review`` in each
team's module still is, for the parts of that module that remain
template-based). Each team's ``phases/review.py`` still imports
``run_coordinator`` itself and passes it into
:func:`run_coordinator_llm_review` as ``run_coordinator_fn``, so that module
stays the test patch surface for the coordinator call, and existing tests
stay green without rewriting their patch targets. The shared bodies here call
back into the per-team QA/security/build runners via injected callables the
same way.

The code-review / QA / security checks are independent — none reads another's
output, they only contribute to the shared ``issues`` list — so the shared body
fans them out concurrently via :func:`_run_review_steps` (a thin wrapper over
``shared.concurrency.parallel_map``), unless ``llm`` is a
:class:`~llm_service.clients.dummy.DummyLLMClient` (or a Strands wrapper around
one), which forces sequential execution because the scripted test doubles are
not thread-safe. Each step returns a :class:`_ReviewStepResult` (issues plus an
optional pre-grounding ``raw_issue_count`` from the CR LLM fallback) and never
raises: an outright runner failure is contained to a synthetic issue for that
step alone, so one step failing never drops the other two steps' findings
(see ``_code_review_step`` / ``_qa_review_step`` / ``_security_review_step``).

Preconditions:
    - ``ReviewConfig`` is constructed once per team (see each team's
      ``phases/_profile.py``) and its callables are pure (no shared mutable
      state).
    - The injected runner callables match the per-team wrapper signatures
      (``_run_llm_review`` / ``_run_qa_agent`` / ``_run_security_agent`` /
      ``_run_build_verification``).

Invariants:
    - This module holds no mutable state of its own; every function is pure
      with respect to its inputs, with two documented exceptions: logging and
      the injected runners' own side effects, and an optional caller-supplied
      cache (``agent_review_cache`` / ``tool_agent_cache``) that a function
      may read from and write to when given one — the cache object, not this
      module, owns that mutable state, and passing ``None`` (the default)
      restores pure behavior.
    - ``ReviewConfig`` is frozen after construction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Generic, List, Mapping, Optional, Tuple, TypeVar, Union

from llm_service import LLMClient
from shared.dev_models.models import ReviewContext, Task
from software_engineering_team.code_review_agent.change_surface import (
    ChangeSurface,
    build_change_surface_from_pairs,
)
from software_engineering_team.shared.agent_review import AgentReviewCache
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

# The two V2 teams each own a distinct ``ReviewIssue`` type, so the helper is
# generic over whatever the caller's ``issue_factory``/coordinator translation
# produces.
IssueT = TypeVar("IssueT")


@dataclass(frozen=True)
class LlmReviewOutput(Generic[IssueT]):
    """Result of one code-review fallback call: kept issues plus the raw count.

    Preconditions: constructed by :func:`run_coordinator_llm_review` (either
    V2 team's coordinator-backed ``_run_llm_review``).

    Postconditions/Invariants:
        - ``raw_issue_count`` is always ``None`` for the coordinator-backed
          fallback — that path has no separate grounding pass to report a
          pre-filter count for, and reporting a fabricated int (e.g.
          ``len(issues)``) would make the circuit breaker see a false "0%
          rejected" instead of "no data" for every call (see
          ``shared.phases.review_cycle.grounding_rejection_ratio``, which
          already treats ``None`` as "no ratio available").
    """

    issues: List[IssueT]
    raw_issue_count: Optional[int]


@dataclass(frozen=True)
class _ReviewStepResult:
    """Output of one independent review step (code review, QA, or security).

    Preconditions:
        - ``issues`` is the step's contribution (possibly empty); never ``None``.
    Postconditions / Invariants:
        - ``raw_issue_count`` is set only by the code-review LLM fallback
          (pre-grounding count); QA and security leave it ``None``.
    """

    issues: List[ReviewIssue]
    raw_issue_count: Optional[int] = None


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


def _lint_passed(lint_result: Any) -> bool:
    """Resolve whether a lint-tool result reports success, defensively.

    Preconditions: ``lint_result`` is any lint-tool return object shape.
    Postconditions: returns ``lint_result.execution_result.success`` when both
    attributes are present; else ``lint_result.passed`` when present; else
    ``True`` (assume success -- nothing to flag). Every attribute lookup is
    guarded by ``getattr`` (not just the innermost one), so a lint-tool object
    missing ``execution_result`` entirely never raises ``AttributeError``.
    """
    execution_result = getattr(lint_result, "execution_result", None)
    success = getattr(execution_result, "success", None) if execution_result is not None else None
    if success is None:
        success = getattr(lint_result, "passed", True)
    return bool(success)


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


def _review_steps_run_sequentially(llm: LLMClient) -> bool:
    """True when the code-review/QA/security fan-out must run one step at a time.

    Scripted ``DummyLLMClient`` doubles use a shared non-thread-safe response index,
    so they are not safe under concurrent fan-out. Mirrors
    ``devops_team.orchestrator``'s ``use_parallel = not isinstance(self.llm, _Dummy)``
    guard.

    Production coding-team callers often pass a Strands ``LLMClientModel`` wrapper
    (reasoning-stream capture via ``_make_reasoning_llm_getter``), which survives the
    ``worker_factory._v2_text_mode_llm`` clone path; the shared
    ``is_dummy_llm_client_wrapped`` helper (also used by
    ``code_review_agent.coordinator._tail_passes_run_sequentially``) unwraps it
    before checking.

    Preconditions: ``llm`` is the LLM client that will be handed to the step thunks.
    Postconditions: returns ``True`` iff ``llm`` is (or wraps) a ``DummyLLMClient``. Pure.
    """
    from llm_service.clients.dummy import is_dummy_llm_client_wrapped

    return is_dummy_llm_client_wrapped(llm)


def _unwrap_llm_review_result(result: Any) -> _ReviewStepResult:
    """Split an ``llm_review_fn`` return value into a :class:`_ReviewStepResult`.

    Preconditions:
        - ``result`` is either an :class:`LlmReviewOutput` (the real per-team LLM
          fallback, which always returns one now) or a bare list of issues (a
          test double / stub that has not adopted the new return type).

    Postconditions:
        - Returns ``result.issues`` (as a fresh list) and ``result.raw_issue_count``
          for an ``LlmReviewOutput``; returns the bare list with
          ``raw_issue_count=None`` when there is no raw/grounded distinction to report.
    """
    if isinstance(result, LlmReviewOutput):
        return _ReviewStepResult(
            issues=list(result.issues),
            raw_issue_count=result.raw_issue_count,
        )
    return _ReviewStepResult(issues=list(result))


def run_coordinator_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    language: str,
    run_coordinator_fn: Callable[..., Any],
    review_context: Optional[ReviewContext] = None,
    extra_task_requirements: str = "",
) -> LlmReviewOutput[ReviewIssue]:
    """Shared lightweight code-review fallback for both V2 teams' ``_run_llm_review``.

    Calls the shared code-review engine's coordinator directly in its
    lightweight mode (``skip_tail_passes=True``: no false-positive filter, no
    merged architecture/side-effect pass) instead of a hand-rolled
    chunk/prompt/parse loop. Both ``backend_code_v2_team`` and
    ``frontend_code_v2_team``'s ``_run_llm_review`` are thin wrappers over this
    function, mirroring how ``_run_qa_agent``/``_run_security_agent`` delegate
    to the shared ``run_qa_agent``/``run_security_agent`` in
    ``shared.agent_review`` — the translation logic is identical for both
    teams (``ReviewIssue`` is the same class either team imports from
    ``shared.v2_models``), so only the caller-supplied
    ``run_coordinator_fn``/``extra_task_requirements`` vary per team.

    Preconditions:
        - See ``code_review_agent.coordinator.run_coordinator`` for ``llm``.
        - ``files`` maps file paths to their full source text.
        - ``run_coordinator_fn`` is the caller's own module-global
          ``run_coordinator`` reference (imported directly into each team's
          ``phases/review.py``, not re-exported from here), so that module
          stays the test patch surface for the coordinator call, exactly as
          it already is for ``run_documentation_self_review``'s
          ``Agent``/``resolve_text_mode_strands_model`` patch surface.
        - ``language`` is forwarded to ``CodeReviewInput`` so the
          coordinator's chunk reviewer prompts against the caller's actual
          detected language instead of ``CodeReviewInput``'s ``typescript``
          default.
        - ``review_context`` bundles the caller's system architecture and
          project specification, when available; ``None`` means "nothing to
          add" so a caller without this context yet keeps working unchanged.
        - ``extra_task_requirements``, when non-empty, is appended to
          ``task.requirements`` (with a blank-line separator, or used
          verbatim when ``task.requirements`` is empty) before it reaches
          ``CodeReviewInput.task_requirements`` — the channel
          ``frontend_code_v2_team`` uses to restore the accessibility
          verification guidance its retired ``REVIEW_PROMPT`` used to state
          explicitly (semantic markup, ARIA, keyboard nav, contrast), since
          the shared engine's ``CODE_REVIEW`` profile has no per-team
          criteria slot to carry it instead.
        - Does NOT special-case ``files == {}``: ``CodeReviewInput`` itself
          deliberately raises ``ValueError`` on an empty mapping (a caller
          bug per its own docstring, "so a caller bug never silently
          becomes an approved empty review") rather than exposing a
          fail-open "nothing to review" shortcut here — the external
          ``code_review_agent`` branch of ``_code_review_step`` already
          relies on that same fail-closed validation for this exact edge
          case, and this function stays consistent with it rather than
          reintroducing the asymmetry the retired ``run_llm_review`` had
          (a silent clean pass on empty input, unlike the external-agent
          path).

    Postconditions:
        - Returns an ``LlmReviewOutput`` whose ``issues`` are
          ``result.issues`` translated to ``ReviewIssue``
          (``suggestion`` -> ``recommendation``; ``category``/``line``/
          ``start_line``/``title``/``pre_existing`` have no ``ReviewIssue``
          field and are dropped) and whose ``raw_issue_count`` is always
          ``None`` — the lightweight coordinator has no separate raw-vs-
          grounded distinction to report, and reporting a fabricated int
          (e.g. ``len(issues)``) would make
          ``shared.phases.review_cycle``'s grounding circuit breaker see a
          false "0% rejected" instead of "no grounding data" for every call
          (see ``grounding_rejection_ratio``, which already treats ``None``
          as "no ratio available"). This also means the grounding circuit
          breaker can no longer trip for this fallback path specifically
          (it still can for the external ``code_review_agent`` path) — an
          intentional, accepted trade-off first made for
          ``backend_code_v2_team``'s migration and now shared by both teams.
        - Propagates ``CodeReviewUnavailableError`` (no chunk could be
          reviewed at all) and ``ValueError`` (empty ``files``, see above)
          uncaught: the caller (``_code_review_step``) already converts any
          uncaught exception from this function into a synthetic
          high-severity "could not complete" issue, which is the correct,
          fail-closed signal for a total review failure.
        - Does not call ``run_coordinator_fn`` with the ``profile``/
          ``skip_false_positive_filter`` fields set; ``skip_tail_passes=True``
          does not gate the coordinator's separate, independently-configured
          post-dedupe spec-compliance synthesis pass (see
          ``CodeReviewInput.skip_tail_passes``'s own docstring) — if a
          deployment has ``CODE_REVIEW_SPEC_COMPLIANCE_PASS`` enabled, this
          "lightweight" fallback can still make that one additional LLM
          call beyond the map phase.
    """
    ctx = review_context or ReviewContext()
    task_requirements = task.requirements or ""
    if extra_task_requirements:
        task_requirements = (
            f"{task_requirements}\n\n{extra_task_requirements}"
            if task_requirements
            else extra_task_requirements
        )

    from software_engineering_team.code_review_agent.models import (
        CodeReviewInput as _CodeReviewInput,
    )

    cr_input = _CodeReviewInput(
        files=files,
        task_description=task.description or "",
        task_requirements=task_requirements,
        acceptance_criteria=task.acceptance_criteria or [],
        architecture=ctx.architecture,
        spec_content=ctx.spec_content or "",
        language=language,
        skip_tail_passes=True,
    )
    result = run_coordinator_fn(llm, cr_input)
    issues = [
        ReviewIssue(
            source="code_review",
            severity=issue.severity,
            description=issue.description,
            file_path=issue.file_path,
            recommendation=issue.suggestion,
        )
        for issue in result.issues
    ]
    return LlmReviewOutput(issues=issues, raw_issue_count=None)


def _maybe_build_change_surface_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> Optional[ChangeSurface]:
    """Call the shared change-surface builder only when a meaningful diff exists.

    Consumed by the ``CodeReviewInput`` ``code=``/``pre_numbered=True`` wiring
    (a separate change): this function only decides *whether* a surface is
    worth building from SE-style old/new content maps, not how it is used.

    Preconditions:
        - ``new_contents`` maps path -> new file text (may be empty).
        - ``old_contents``, when provided, maps path -> previously resolved
          file text (resolution itself happens elsewhere; this function does
          not read disk or git). ``None`` means "no old content available for
          any path" and is treated the same as by
          :func:`build_change_surface_from_pairs` (every path is wholly new).

    Postconditions:
        - Returns ``None`` without calling ``build_change_surface_from_pairs``
          when ``new_contents`` is empty, or when ``old_contents`` is exactly
          equal to ``new_contents`` (nothing changed) -- a cheap short-circuit
          ahead of the diff/expansion work.
        - Otherwise calls ``build_change_surface_from_pairs(new_contents,
          old_contents)`` exactly once and returns its result, unless that
          result is empty (``ChangeSurface.is_empty``), in which case returns
          ``None`` so an empty/identical diff never masquerades as a surface.
        - Never raises for well-typed string mappings.
    """
    if not new_contents:
        return None
    if old_contents is not None and dict(old_contents) == dict(new_contents):
        return None
    surface = build_change_surface_from_pairs(new_contents, old_contents)
    if surface.is_empty:
        return None
    return surface


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
    llm_review_fn: Callable[..., Any],
    review_context: Optional[ReviewContext] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
    enable_llm_review_grounding: bool = True,
    old_contents: Optional[Dict[str, str]] = None,
) -> _ReviewStepResult:
    """Independent code-review step: external agent (with LLM fallback), or LLM review alone.

    Preconditions:
        - ``files`` maps file paths to their full source text. ``task_description`` is the
          description surfaced to the external agent (the caller scopes this to the task or a
          single microtask; the LLM fallback always reasons over the full ``task``, unaffected).
        - ``llm_review_fn(llm=, task=, files=, language=, review_context=,
          enable_llm_review_grounding=)`` is the per-team reviewer. Both V2
          teams' versions call ``code_review_agent.coordinator.run_coordinator``
          directly (see each team's own ``_run_llm_review`` docstring), so
          neither is an ``Agent`` / ``resolve_text_mode_strands_model`` patch
          surface for code review. It must accept ``review_context`` so the
          fallback reviewer sees the same context the external agent path
          does, and ``language`` so it can forward it to ``CodeReviewInput``
          and review the code under its actual language instead of
          ``CodeReviewInput``'s ``typescript`` default. It returns an
          :class:`LlmReviewOutput` in production; a bare issue list is also
          accepted (see ``_unwrap_llm_review_result``) so a stub runner
          without a raw count is unaffected. ``files=`` is always what this
          fallback receives, regardless of ``old_contents`` -- only the
          external-agent's ``CodeReviewInput`` adopts the surface (see
          ``old_contents`` below).
        - ``review_context`` bundles the caller's system architecture and project specification,
          when available; ``None`` means "nothing to add" so a caller that does not have this
          context yet keeps working unchanged.
        - ``enable_llm_review_grounding`` defaults True; forwarded to the LLM fallback
          (kill switch for ungrounded-claim filtering).
        - ``old_contents``, when given, maps path -> previously resolved file text and is
          forwarded to :func:`_maybe_build_change_surface_from_pairs` to attempt a diff-derived
          surface for the external-agent's ``CodeReviewInput``. ``None`` (the default) means "no
          base to diff against" and is *not* forwarded to that function -- unlike
          ``_maybe_build_change_surface_from_pairs`` itself, which treats an explicit
          ``old_contents=None`` as "every path is wholly new" (a meaningful diff), this step
          only attempts a surface when a caller actually opts in with real (possibly empty)
          previous-content data, so every existing caller that has no ``old_contents`` wiring
          yet keeps today's ``files=`` behavior unchanged.

    Postconditions:
        - Returns a :class:`_ReviewStepResult`: ``issues`` from the agent or LLM fallback,
          and ``raw_issue_count`` from the LLM fallback when it ran (``None`` when the
          external agent succeeded or a bare-list stub reported no count).
        - The external agent's ``CodeReviewInput`` is built with ``code=<surface>,
          pre_numbered=True`` (no ``files=``) when ``old_contents`` is not ``None`` and
          :func:`_maybe_build_change_surface_from_pairs` returns a non-empty surface for
          ``(files, old_contents)``; otherwise it is built with ``files=files`` exactly as
          before this parameter existed.
        - Never raises: an external ``code_review_agent`` failure logs a warning and falls back
          to the LLM reviewer, matching this step's long-standing solo behavior. The LLM fallback
          itself (used both here and when ``code_review_agent`` is None) is also guarded — any
          outright failure there is reported as a synthetic high-severity issue rather than
          propagating, mirroring ``_qa_review_step``/``_security_review_step``. This must stay
          true when fanned out concurrently alongside the QA/security steps (see
          ``_review_steps_run_sequentially``'s caller), since one step raising must never drop
          the other two steps' issues.
    """
    try:
        if code_review_agent is None:
            return _unwrap_llm_review_result(
                llm_review_fn(
                    llm=llm,
                    task=task,
                    files=files,
                    language=language,
                    review_context=review_context,
                    enable_llm_review_grounding=enable_llm_review_grounding,
                ),
            )
        try:
            from software_engineering_team.code_review_agent.models import (
                CodeReviewInput as _CRInput,
            )

            ctx = review_context or ReviewContext()
            surface = (
                _maybe_build_change_surface_from_pairs(files, old_contents)
                if old_contents is not None
                else None
            )
            # repo_root carries the workspace path as a serializable field so a
            # durable Temporal review can rebuild the whole-repo reader worker-side
            # (a live repo_reader object cannot cross that boundary); the live
            # reader below still drives the in-process/thread-mode path.
            cr_input_kwargs: Dict[str, Any] = dict(
                task_description=task_description,
                task_requirements=task.requirements or "",
                acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
                language=language,
                architecture=ctx.architecture,
                spec_content=ctx.spec_content,
                repo_root=str(repo_path),
            )
            if surface is not None:
                # A meaningful diff was built: submit the bounded, pre-numbered
                # surface instead of full file bodies, matching api/pr_review.py's
                # established happy-path shape.
                cr_input_kwargs["code"] = surface.code
                cr_input_kwargs["pre_numbered"] = True
            else:
                # No base to diff against (or nothing meaningful changed): files=
                # keeps per-file attribution and lets the coordinator bound its own
                # prompts — no header parsing, no upstream truncation.
                cr_input_kwargs["files"] = files
            cr_input = _CRInput(**cr_input_kwargs)
            cr_result = call_code_review_agent(
                code_review_agent,
                cr_input,
                detail_callback,
                repo_reader=build_disk_repo_reader(repo_path),
            )
            return _ReviewStepResult(
                issues=[
                    ReviewIssue(
                        source="code_review",
                        severity=getattr(item, "severity", "medium"),
                        description=getattr(item, "description", str(item)),
                        file_path=getattr(item, "file_path", ""),
                        recommendation=getattr(
                            item, "suggestion", getattr(item, "recommendation", "")
                        ),
                    )
                    for item in getattr(cr_result, "issues", [])
                ]
            )
        except Exception as exc:
            logger.warning(
                "[%s] Code review agent failed: %s. Next step -> Using LLM fallback for code review",
                task_id,
                exc,
            )
            return _unwrap_llm_review_result(
                llm_review_fn(
                    llm=llm,
                    task=task,
                    files=files,
                    language=language,
                    review_context=review_context,
                    enable_llm_review_grounding=enable_llm_review_grounding,
                ),
            )
    except Exception as exc:
        logger.warning("[%s] Code review step failed outright: %s", task_id, exc)
        return _ReviewStepResult(
            issues=[
                ReviewIssue(
                    source="code_review",
                    severity="high",
                    description=f"Code review could not complete: {exc}",
                    recommendation="Investigate and re-run code review; findings from this run are incomplete.",
                )
            ]
        )


def _qa_review_step(
    *,
    qa_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    qa_agent_fn: Callable[..., List[ReviewIssue]],
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> _ReviewStepResult:
    """Independent QA step.

    Preconditions: ``qa_agent_fn`` is the per-team QA runner (the test patch
        surface for ``_run_qa_agent``) — it need not accept a ``cache``
        keyword unless a caller actually supplies one (see Postconditions).
        ``cache``: see
        ``software_engineering_team.shared.agent_review.run_chunked_agent_review``.
    Postconditions:
        - Returns an empty :class:`_ReviewStepResult` when ``qa_agent`` is None.
          Otherwise never raises: an outright QA-agent failure is reported as a
          synthetic high-severity issue rather than propagating — a bare exception
          here would previously have aborted the whole review; fanning this step
          out concurrently with code review/security must not make that worse.
        - ``cache`` is forwarded to ``qa_agent_fn`` only when not None, so a
          ``qa_agent_fn`` predating this parameter keeps working unchanged for
          callers that don't opt into caching.
    """
    if qa_agent is None:
        return _ReviewStepResult(issues=[])
    try:
        kwargs: Dict[str, Any] = dict(
            qa_agent=qa_agent,
            files=files,
            language=language,
            task_description=task_description,
            task_id=task_id,
            context=context,
        )
        # Only pass ``cache`` when it's actually in use: an injected
        # ``qa_agent_fn`` predating this parameter (e.g. a test's patch
        # surface) has no ``cache`` in its signature and no ``**kwargs``
        # catch-all, so an unconditional ``cache=None`` would still raise
        # TypeError on every call, not just when caching is requested.
        if cache is not None:
            kwargs["cache"] = cache
        return _ReviewStepResult(issues=qa_agent_fn(**kwargs))
    except Exception as exc:
        logger.warning("[%s] QA agent step failed outright: %s", task_id, exc)
        return _ReviewStepResult(
            issues=[
                ReviewIssue(
                    source="qa",
                    severity="high",
                    description=f"QA agent failed and could not complete review: {exc}",
                    recommendation="Investigate and re-run the QA agent; findings from this run are incomplete.",
                )
            ]
        )


def _security_review_step(
    *,
    security_agent: Any,
    files: Dict[str, str],
    language: str,
    task_description: str,
    task_id: str,
    security_agent_fn: Callable[..., List[ReviewIssue]],
    context: str = "",
    cache: Optional[AgentReviewCache] = None,
) -> _ReviewStepResult:
    """Independent security step.

    Preconditions: ``security_agent_fn`` is the per-team security runner (the
        test patch surface for ``_run_security_agent``) — it need not accept a
        ``cache`` keyword unless a caller actually supplies one (see
        Postconditions). ``cache``: see
        ``software_engineering_team.shared.agent_review.run_chunked_agent_review``.
    Postconditions:
        - Returns an empty :class:`_ReviewStepResult` when ``security_agent`` is None.
          Otherwise never raises: an outright security-agent failure is reported as
          a synthetic critical-severity issue rather than propagating (see
          ``_qa_review_step`` for the identical rationale).
        - ``cache`` is forwarded to ``security_agent_fn`` only when not None,
          mirroring ``_qa_review_step``'s identical backward-compatibility
          rationale.
    """
    if security_agent is None:
        return _ReviewStepResult(issues=[])
    try:
        kwargs: Dict[str, Any] = dict(
            security_agent=security_agent,
            files=files,
            language=language,
            task_description=task_description,
            task_id=task_id,
            context=context,
        )
        # See _qa_review_step's identical rationale: only pass ``cache`` when
        # it's actually in use, so an injected ``security_agent_fn`` predating
        # this parameter isn't broken by an unconditional ``cache=None``.
        if cache is not None:
            kwargs["cache"] = cache
        return _ReviewStepResult(issues=security_agent_fn(**kwargs))
    except Exception as exc:
        logger.warning("[%s] Security agent step failed outright: %s", task_id, exc)
        return _ReviewStepResult(
            issues=[
                ReviewIssue(
                    source="security",
                    severity="critical",
                    description=f"Security agent failed and could not complete review: {exc}",
                    recommendation=(
                        "Investigate and re-run the security agent; findings from this run are incomplete."
                    ),
                )
            ]
        )


def _run_review_steps(
    step_fns: List[Callable[[], _ReviewStepResult]], *, llm: LLMClient
) -> _ReviewStepResult:
    """Run the code-review/QA/security step thunks, fanned out unless ``llm`` requires sequencing.

    Preconditions:
        - Each element of ``step_fns`` returns a :class:`_ReviewStepResult` and never
          raises (see ``_code_review_step``/``_qa_review_step``/``_security_review_step``) —
          required because ``parallel_map`` fast-fails (cancels the round's other pending
          steps and re-raises) on the first worker exception.
    Postconditions:
        - Returns every step's issues concatenated in ``step_fns`` order, regardless of
          which step's underlying call actually completed first, plus the first non-``None``
          ``raw_issue_count`` among those steps (the CR LLM fallback).
    """
    if _review_steps_run_sequentially(llm) or len(step_fns) <= 1:
        results = [step() for step in step_fns]
    else:
        # Imported lazily, matching coding_team.swarm_review/coding_team.orchestrator's identical
        # parallel_map import — keeps the module import light for callers that never hit the
        # concurrent branch (e.g. every DummyLLMClient-backed test).
        from shared.concurrency import parallel_map

        results = parallel_map(
            step_fns, lambda fn: fn(), max_workers=len(step_fns), skip_none=False
        )
    issues = [issue for step_result in results for issue in step_result.issues]
    raw_issue_count = next(
        (
            step_result.raw_issue_count
            for step_result in results
            if step_result.raw_issue_count is not None
        ),
        None,
    )
    return _ReviewStepResult(issues=issues, raw_issue_count=raw_issue_count)


def _tool_agent_cache_key(
    kind_value: str,
    current_files: Dict[str, str],
    task_title: str,
    task_description: str,
    microtask_id: str,
) -> str:
    """Hash of one tool agent's exact review input (kind + files + task + microtask).

    Postconditions:
        - Two calls collide only when ``kind_value``, ``current_files``,
          ``task_title``, ``task_description``, and ``microtask_id`` are all
          identical — mirroring ``_piece_cache_key``'s "exact LLM input, keyed
          by content" design (``shared/agent_review.py``) — so any file edit
          between two calls changes the digest and naturally busts a prior
          entry with no explicit invalidation logic. ``sort_keys=True`` makes
          the digest independent of ``current_files``' iteration order.
    """
    body = json.dumps(
        [kind_value, current_files, task_title, task_description, microtask_id],
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _fold_tool_agent_output(
    config: ReviewConfig, issues: List[ReviewIssue], kind: Any, out: Any
) -> None:
    """Append ``out``'s issues and recommendation-derived issues onto ``issues``."""
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
    tool_agent_cache: Optional[AgentReviewCache] = None,
) -> None:
    """Run each wired tool agent's ``review`` and fold its output into issues.

    Preconditions:
        - ``tool_agents`` is ``None`` or a ``{ToolAgentKind: agent}`` mapping.
        - ``config.tool_phase_input_factory`` accepts the kwargs built here.
        - ``tool_agent_cache``, when given, is consulted/populated only for
          agents whose ``kind`` result was already computed for the identical
          ``(kind, current_files, task.title, task_description, microtask.id)``
          combination, computed by ``_tool_agent_cache_key(kind.value,
          current_files, task.title or "", task_description, microtask.id)``.
          ``current_files`` is content-addressed (path + file content, via
          ``json.dumps(..., sort_keys=True)``), so any file edit busts the
          cache with no explicit invalidation logic needed. Requires
          ``microtask`` to be given (the cache key folds in ``microtask.id``);
          when ``microtask`` is ``None`` (the non-microtask ``run_review``
          path), caching is skipped regardless of ``tool_agent_cache``. ``None``
          (the default) preserves today's unconditional-call behavior for
          every existing caller.

    Postconditions:
        - Each agent with a ``review`` method contributes its ``issues`` and a
          ``ReviewIssue`` per recommendation, either from a cache hit or a live
          ``review()`` call; a raising agent (or a cache hit whose stored
          output fails to fold, e.g. a malformed/``None`` result) is logged
          and skipped (never aborts the review) -- folding a cache hit is
          contained by the same ``try`` as a live call. Mutates ``issues`` in
          place. A live call's result is folded into ``issues`` *before* being
          stored in ``tool_agent_cache`` as a single-element list (``[out]``),
          matching ``AgentReviewCache.get``/``put``'s existing ``List[Any]``
          shape so it can be reused unmodified, so an output that fails to
          fold is never cached -- it is retried as a live call next time,
          mirroring ``AgentReviewCache``'s existing "failed piece is not
          cached" behavior for the QA/security steps.
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
        cache_key = None
        if tool_agent_cache is not None and microtask is not None:
            cache_key = _tool_agent_cache_key(
                kind.value, current_files, task.title or "", task_description, microtask.id
            )
        try:
            if cache_key is not None:
                cached = tool_agent_cache.get(cache_key)
                if cached is not None:
                    _fold_tool_agent_output(config, issues, kind, cached[0])
                    continue
            out = agent.review(phase_inp)
            _fold_tool_agent_output(config, issues, kind, out)
            if cache_key is not None:
                tool_agent_cache.put(cache_key, [out])
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
    llm_review_fn: Callable[..., Union[LlmReviewOutput[ReviewIssue], List[ReviewIssue]]],
    qa_agent_fn: Callable[..., List[ReviewIssue]],
    security_agent_fn: Callable[..., List[ReviewIssue]],
    build_verify_fn: Callable[..., Tuple[bool, str]],
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    old_contents: Optional[Dict[str, str]] = None,
) -> ReviewResult:
    """Execute the shared Review phase over an execution result's files.

    Preconditions:
        - ``execution_result`` exposes ``.files: Dict[str, str]``.
        - The injected runners match the per-team wrapper signatures.
        - ``review_context`` is forwarded to the code-review step only; ``None`` means
          "nothing to add" so an existing caller that does not have this context yet is
          unaffected.
        - ``enable_llm_review_grounding`` is forwarded to the LLM-fallback path
          (defaults True).
        - ``old_contents`` is forwarded to the code-review step only (see
          ``_code_review_step``'s ``old_contents``); ``None`` (the default) preserves the
          existing ``files=``-only behavior for every caller that has no base-content
          resolution wired in yet.

    Postconditions:
        - Returns a :class:`ReviewResult` whose ``passed`` reflects the team's
          blocking + (optionally) lint policy; ``build_ok``/``lint_ok`` report
          the individual gate outcomes. Never raises on a tool/agent failure
          (those are contained to synthetic issues and logged).
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
            from software_engineering_team.linting_tool_agent.models import (
                LintToolInput as _LintInput,
            )

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type=config.lint_agent_type,
                    task_id=task_id,
                    task_description=task.description or "",
                )
            )
            if lint_result and not _lint_passed(lint_result):
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

    # 3-5. Code review, QA, and security are independent LLM-backed checks — none reads another's
    # output, they only contribute to the shared `issues` list — so fan them out concurrently
    # (unless `llm` requires sequential calls; see _review_steps_run_sequentially). Step 6 (tool
    # agents) depends on the combined result of these three and must run after.
    fan_out = _run_review_steps(
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
                llm_review_fn=llm_review_fn,
                review_context=review_context,
                enable_llm_review_grounding=enable_llm_review_grounding,
                old_contents=old_contents,
            ),
            lambda: _qa_review_step(
                qa_agent=qa_agent,
                files=execution_result.files,
                language=language,
                task_description=task.description or "",
                task_id=task_id,
                qa_agent_fn=qa_agent_fn,
            ),
            lambda: _security_review_step(
                security_agent=security_agent,
                files=execution_result.files,
                language=language,
                task_description=task.description or "",
                task_id=task_id,
                security_agent_fn=security_agent_fn,
            ),
        ],
        llm=llm,
    )
    issues.extend(fan_out.issues)

    # 6. Domain-specific review from tool agents
    _run_tool_agents_review(
        config,
        task=task,
        issues=issues,
        tool_agents=tool_agents,
        task_id=task_id,
        task_description=task.description or "",
        current_files=execution_result.files,
        tool_repo_path=str(repo_path),
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
        raw_issue_count=fan_out.raw_issue_count,
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
    llm_review_fn: Callable[..., Union[LlmReviewOutput[ReviewIssue], List[ReviewIssue]]],
    qa_agent_fn: Callable[..., List[ReviewIssue]],
    security_agent_fn: Callable[..., List[ReviewIssue]],
    build_verify_fn: Callable[..., Tuple[bool, str]],
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
    agent_review_cache: Optional[AgentReviewCache] = None,
    tool_agent_cache: Optional[AgentReviewCache] = None,
    old_contents: Optional[Dict[str, str]] = None,
) -> ReviewResult:
    """Run the shared full review on a single microtask's output files.

    Preconditions:
        - ``microtask`` exposes ``.id`` / ``.title`` / ``.description``.
        - The injected runners match the per-team wrapper signatures.
        - ``review_context`` is forwarded to the code-review step only; ``None`` means
          "nothing to add" so an existing caller that does not have this context yet is
          unaffected.
        - ``enable_llm_review_grounding`` is forwarded to the LLM-fallback path
          (defaults True).
        - ``agent_review_cache``, when given, is forwarded to the QA and security
          steps only (not code review, which has its own cache) — see
          ``software_engineering_team.shared.agent_review.run_chunked_agent_review``.
        - ``tool_agent_cache``, when given, is forwarded to the tool-agent fan-out
          step only — see ``_run_tool_agents_review``.
        - ``old_contents`` is forwarded to the code-review step only (see
          ``_code_review_step``'s ``old_contents``); ``None`` (the default) preserves the
          existing ``files=``-only behavior for every caller that has no base-content
          resolution wired in yet.

    Postconditions:
        - Returns a :class:`ReviewResult` scoped to ``files``; ``passed``
          includes ``build_ok`` AND ``lint_ok`` AND no blocking issue (both
          teams). Never raises on a tool/agent failure (contained + logged).
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

    # Code review, QA, and security are independent LLM-backed checks — none reads another's
    # output — so fan them out concurrently (unless `llm` requires sequential calls; see
    # _review_steps_run_sequentially). Progress messages are announced up front, in their original
    # order, rather than from inside each step: decoupling "announce" from "complete" means the
    # messages appear in a stable order regardless of which step's call finishes first. Code
    # review's own detail_callback (chunk-level progress during the agent's multi-chunk execution)
    # still threads through — it is the only step that reports granular progress, so there is no
    # concurrent writer to race.
    if detail_callback:
        detail_callback("Running code review...")
    if qa_agent is not None and detail_callback:
        detail_callback("Running QA check...")
    if security_agent is not None and detail_callback:
        detail_callback("Running security scan...")

    microtask_desc = f"Microtask: {microtask.description or microtask.title}"
    microtask_ctx = f" for microtask {microtask_id}"

    fan_out = _run_review_steps(
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
                llm_review_fn=llm_review_fn,
                review_context=review_context,
                detail_callback=detail_callback,
                enable_llm_review_grounding=enable_llm_review_grounding,
                old_contents=old_contents,
            ),
            lambda: _qa_review_step(
                qa_agent=qa_agent,
                files=files,
                language=language,
                task_description=microtask_desc,
                task_id=task_id,
                qa_agent_fn=qa_agent_fn,
                context=microtask_ctx,
                cache=agent_review_cache,
            ),
            lambda: _security_review_step(
                security_agent=security_agent,
                files=files,
                language=language,
                task_description=microtask_desc,
                task_id=task_id,
                security_agent_fn=security_agent_fn,
                context=microtask_ctx,
                cache=agent_review_cache,
            ),
        ],
        llm=llm,
    )
    issues.extend(fan_out.issues)

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
        tool_agent_cache=tool_agent_cache,
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
        raw_issue_count=fan_out.raw_issue_count,
    )


__all__ = [
    "ReviewConfig",
    "run_review",
    "run_microtask_review",
    "_review_steps_run_sequentially",
]
