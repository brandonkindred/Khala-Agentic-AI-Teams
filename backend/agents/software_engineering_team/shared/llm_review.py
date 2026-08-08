"""Shared LLM-based code-review fallback (retired from both V2 teams' production paths).

This module's ``run_llm_review``/``run_team_llm_review`` implement the
original chunk/prompt/parse-template review loop: function-aware chunking,
per-chunk prompt formatting, parsing, issue construction, and an
ungrounded-claim filter (``drop_ungrounded_issues``).

Both ``backend_code_v2_team`` and ``frontend_code_v2_team`` have since
migrated their ``_run_llm_review`` fallback to call
``code_review_agent.coordinator.run_coordinator`` directly, in its
lightweight mode (``skip_tail_passes=True``), trading this module's
ungrounded-claim filter for the coordinator's Pydantic-validated,
map-reduce-reviewed output. Neither team calls into this module for code
review anymore; it is kept only because ``run_llm_review``/
``run_team_llm_review`` are still directly unit-tested
(``tests/test_shared_llm_review.py``) pending their removal in a follow-up
cleanup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

from llm_service import LLMClient
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_spec_excerpt_chars,
)
from software_engineering_team.shared.issue_grounding import drop_ungrounded_issues
from software_engineering_team.shared.models import ReviewContext, Task

logger = logging.getLogger(__name__)

# The two V2 teams each own a distinct ``ReviewIssue`` type, so the helper is
# generic over whatever the caller's ``issue_factory`` produces.
IssueT = TypeVar("IssueT")


@dataclass(frozen=True)
class LlmReviewOutput(Generic[IssueT]):
    """Result of one code-review fallback call: kept issues plus the raw count.

    The grounding filter (``drop_ungrounded_issues``) can silently discard every
    finding a review produced; without the pre-filter count, a caller cannot
    distinguish "the LLM found nothing" from "the LLM found things but grounding
    rejected all of them" — the latter is the signal a circuit breaker needs
    (see ``shared.phases.review_cycle.grounding_rejection_ratio``, which treats
    ``None`` and ``<= 0`` identically as "no ratio available").

    Preconditions: constructed by ``run_llm_review`` (unused by either V2
    team's production path — see this module's docstring) or by either V2
    team's coordinator-backed ``_run_llm_review``
    (``backend_code_v2_team``/``frontend_code_v2_team``).

    Postconditions/Invariants:
        - From ``run_llm_review``: ``raw_issue_count == len(issues)`` measured
          before the grounding filter ran (or unconditionally when grounding is
          disabled/skipped), so ``raw_issue_count >= len(issues)`` always holds;
          empty input (no non-blank files) yields ``LlmReviewOutput([], 0)``.
        - From either V2 team's coordinator-backed fallback: ``raw_issue_count``
          is always ``None`` — that path has no separate grounding pass to
          report a pre-filter count for, and reporting a fabricated int (e.g.
          ``len(issues)``) would make the circuit breaker see a false "0%
          rejected" instead of "no data" for every call.
    """

    issues: List[IssueT]
    raw_issue_count: Optional[int]


def run_llm_review(
    *,
    task: Task,
    files: Dict[str, str],
    prompt_template: str,
    parse_template: Callable[[str], Dict[str, Any]],
    issue_factory: Callable[..., IssueT],
    invoke_model: Callable[[str], str],
    max_chars: int,
    warn_threshold: int,
    architecture_context: str = "",
    spec_content: str = "",
    enable_llm_review_grounding: bool = True,
) -> LlmReviewOutput[IssueT]:
    """LLM-based code review when no external review agent is available.

    Preconditions:
        - ``files`` maps file paths to their full source text.
        - ``prompt_template`` accepts ``requirements``, ``acceptance_criteria``,
          ``architecture_context``, ``spec_content``, and ``code`` format fields.
        - ``parse_template`` returns a dict that may contain an ``"issues"`` list
          of dicts.
        - ``issue_factory`` is a callable accepting keyword arguments ``source``,
          ``severity``, ``description``, ``file_path``, and ``recommendation``
          (e.g. each team's ``ReviewIssue``); an incompatible factory raises
          ``TypeError``.
        - ``invoke_model`` runs one prompt through the team's LLM and returns the
          raw text response.
        - ``max_chars`` > 0 and ``warn_threshold`` >= 0.
        - ``architecture_context``/``spec_content`` are the caller's already
          size-bounded excerpts (the caller is expected to have applied its own
          cap before calling, since this runs once per chunk); both default to
          ``""`` so a caller without this context yet is unaffected.
        - ``enable_llm_review_grounding`` defaults True; when False, findings are
          returned without the ungrounded-claim filter (kill switch).

    Postconditions:
        - Returns an :class:`LlmReviewOutput` whose ``raw_issue_count`` is the
          number of issues parsed across all chunks *before* any grounding
          filter runs, and whose ``issues`` are those same issues after the
          filter (or unchanged when grounding is disabled/skipped); see
          :class:`LlmReviewOutput`.
        - Inputs that exceed the per-call budget are split into function-aware
          chunks (cuts land between whole functions/methods, never mid-body)
          and every chunk is reviewed; issues from all chunks are returned.
          No file content is silently truncated away, so a large file's tail is
          reviewed rather than dropped. Blank files contribute nothing, so
          empty input returns ``LlmReviewOutput([], 0)``.
        - A chunk that is itself over budget (a single line longer than the cap,
          e.g. a minified bundle) is hard-split before the LLM call, with the
          ``### path ###`` header re-attached to every piece (``cap_review_chunk``)
          so it is never sent in one prompt that may overflow the context and be
          skipped, and a finding in any tail piece stays attributable to its file.
        - A chunk whose LLM call or parse fails is logged and skipped; issues
          from the other chunks are still returned (one bad chunk never aborts
          the whole review).
        - Small inputs are reviewed in a single call, as before.
        - When ``enable_llm_review_grounding`` is True, findings whose description
          or recommendation contain checkable proper-noun phrases absent from the
          task grounding corpus are dropped before return; unknown file paths
          are blanked rather than dropped.
    """
    # Imported lazily (not at module level) so importing this helper does not
    # pull in the whole code_review_agent package; this also matches the V2
    # teams' existing convention and avoids assuming the
    # software_engineering_team package dir is itself on sys.path.
    from software_engineering_team.code_review_agent.coordinator import (
        build_review_chunks,
        cap_review_chunk,
    )

    blocks = [(path, content) for path, content in files.items() if content and content.strip()]
    if not blocks:
        return LlmReviewOutput(issues=[], raw_issue_count=0)
    # Budget each prompt at the same per-call size the old code truncated to,
    # but split at function/method boundaries so no construct is severed and no
    # file tail is dropped.
    chunks = list(build_review_chunks(blocks, max_chars))
    if len(chunks) > warn_threshold:
        logger.warning(
            "LLM code review: %d chunks for %d file(s) — large review, many LLM calls",
            len(chunks),
            len(blocks),
        )
    else:
        logger.debug("LLM code review: %d chunk(s) for %d file(s)", len(chunks), len(blocks))
    issues: List[IssueT] = []
    for idx, chunk in enumerate(chunks, start=1):
        logger.debug("LLM code review: reviewing chunk %d/%d", idx, len(chunks))
        # A chunk holding a single line longer than the cap is over budget by
        # contract; hard-split it so the whole file is reviewed rather than sent
        # in one prompt that may overflow and be skipped — keeping the file
        # header on every piece so tail findings stay attributable.
        for piece in cap_review_chunk(chunk, max_chars):
            prompt = prompt_template.format(
                requirements=task.requirements or task.description,
                acceptance_criteria=", ".join(task.acceptance_criteria)
                if task.acceptance_criteria
                else "N/A",
                architecture_context=architecture_context or "(none provided)",
                spec_content=spec_content or "(none provided)",
                code=piece,
            )
            try:
                raw = invoke_model(prompt)
                data = parse_template(raw)
            except Exception as exc:
                logger.warning("LLM code review: chunk %d/%d failed: %s", idx, len(chunks), exc)
                continue
            for item in data.get("issues") or []:
                if isinstance(item, dict):
                    issues.append(
                        issue_factory(
                            source=item.get("source", "code_review"),
                            severity=item.get("severity", "medium"),
                            description=item.get("description", ""),
                            file_path=item.get("file_path", ""),
                            recommendation=item.get("recommendation", ""),
                        )
                    )
    raw_issue_count = len(issues)
    if enable_llm_review_grounding and issues:

        def _on_dropped(issue: Any) -> None:
            logger.warning("LLM code review: dropping ungrounded finding: %s", issue)

        requirements = task.requirements or task.description or ""
        issues = drop_ungrounded_issues(
            issues,
            files=files,
            requirements=requirements,
            acceptance_criteria=task.acceptance_criteria,
            spec_content=spec_content,
            architecture_context=architecture_context,
            on_dropped=_on_dropped,
        )
    return LlmReviewOutput(issues=issues, raw_issue_count=raw_issue_count)


def run_team_llm_review(
    *,
    llm: LLMClient,
    task: Task,
    files: Dict[str, str],
    prompt_template: str,
    parse_template: Callable[[str], Dict[str, Any]],
    issue_factory: Callable[..., IssueT],
    invoke_model: Callable[[str], str],
    max_chars: int,
    warn_threshold: int,
    review_context: Optional[ReviewContext] = None,
    enable_llm_review_grounding: bool = True,
) -> LlmReviewOutput[IssueT]:
    """Team-level entry point for the LLM review fallback (unused by either V2
    team's production path -- see this module's docstring).

    Combines the ``review_context`` -> ``architecture_context``/``spec_content``
    bounding step with :func:`run_llm_review`. Both ``backend_code_v2_team`` and
    ``frontend_code_v2_team`` now call
    ``code_review_agent.coordinator.run_coordinator`` directly instead (see
    this module's docstring); this function remains only for its direct unit
    test coverage (``tests/test_shared_llm_review.py``).

    Preconditions:
        - See :func:`run_llm_review` for ``files``/``prompt_template``/
          ``parse_template``/``issue_factory``/``invoke_model``/``max_chars``/
          ``warn_threshold``/``enable_llm_review_grounding``.
        - ``review_context`` bundles the caller's system architecture and project
          specification, when available; ``None`` means "nothing to add" so a
          caller without this context yet keeps working unchanged.

    Postconditions:
        - ``architecture_context``/``spec_content`` are rendered from
          ``review_context`` (when given) and hard-truncated to the same
          per-chunk caps the coordinator's own architecture/spec excerpts use
          (this runs once per chunk, so an uncapped document would repeat its
          full size in every chunk's prompt); both stay ``""`` when
          ``review_context`` is ``None``, so ``llm`` is never touched when there
          is nothing to bound (a caller's bare test double without
          ``get_max_context_tokens`` is never invoked).
        - Delegates to :func:`run_llm_review` with the bounded context; see that
          function's contract for the rest of the behavior.
    """
    architecture_context = ""
    spec_content = ""
    if review_context is not None:
        if review_context.architecture is not None:
            # Lazy import: code_review_agent submodules are imported on demand
            # rather than at module scope, matching this module's other lazy
            # import of code_review_agent.coordinator in run_llm_review above
            # (fully qualified, since code_review_agent is a subpackage of
            # software_engineering_team, not a top-level package).
            from software_engineering_team.code_review_agent.architecture_context import (
                render_architecture_context,
            )

            architecture_context = render_architecture_context(review_context.architecture)
        spec_content = review_context.spec_content or ""
        # Bounded here (only when there is context to bound): this runs once per
        # chunk, so an uncapped document would repeat its full size in every
        # chunk's prompt. Skipped entirely with no review_context so a caller's
        # bare llm handle (e.g. a test double without get_max_context_tokens)
        # is never touched when there is nothing to bound.
        architecture_context = architecture_context[: compute_code_review_arch_overview_chars(llm)]
        spec_content = spec_content[: compute_code_review_spec_excerpt_chars(llm)]

    return run_llm_review(
        task=task,
        files=files,
        prompt_template=prompt_template,
        parse_template=parse_template,
        issue_factory=issue_factory,
        invoke_model=invoke_model,
        max_chars=max_chars,
        warn_threshold=warn_threshold,
        architecture_context=architecture_context,
        spec_content=spec_content,
        enable_llm_review_grounding=enable_llm_review_grounding,
    )
