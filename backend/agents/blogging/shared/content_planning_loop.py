"""
Shared content-planning-loop logic for the blogging team.

Generates and refines a ``ContentPlan``: generate -> validate -> critic-check
-> refine or converge, via ``run_content_planning_loop``. The planning and
writer agents' underlying LLM-call primitives differ
(``BlogPlanningAgent._call_agent`` vs
``BlogWriterAgent._call_agent_json``/``_call_json_raw``), so callers inject
those as closures captured at call time (see ``complete_plan_json`` /
``CompletePlanJsonFn``) rather than this module calling an LLM client
directly.

``run_content_planning_loop`` deferred-imports
``blog_plan_critic_agent.agent.build_refine_feedback_from_critic`` (only when
``plan_critic`` is supplied) to keep this module free of a hard dependency on
``blog_plan_critic_agent`` when no critic is wired — a refactor of that
function's name or location needs a matching update here.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Union

from llm_service import LLMJsonParseError, extract_json_from_response

from .content_plan import (
    ContentPlan,
    PlanningFailureReason,
    PlanningInput,
    PlanningPhaseResult,
    TitleCandidate,
    section_count_bounds_for_profile,
)
from .content_profile import LengthPolicy
from .errors import PlanningError
from .prompt_budget import fit_optional_text_to_prompt

if TYPE_CHECKING:
    from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent, PlanCriticReport

logger = logging.getLogger(__name__)


def post_validate_plan(plan: ContentPlan, policy: LengthPolicy) -> ContentPlan:
    """Enforce section-count expectations vs content profile."""
    lo, hi = section_count_bounds_for_profile(policy.content_profile.value)
    n = len(plan.sections)
    ra = plan.requirements_analysis.model_copy(deep=True)
    if n < lo or n > hi:
        ra.plan_acceptable = False
        ra.gaps = [
            *list(ra.gaps),
            f"Section count {n} outside expected range [{lo},{hi}] for profile {policy.content_profile.value}.",
        ]
    return plan.model_copy(update={"requirements_analysis": ra})


def is_planner_self_eval_satisfied(plan: ContentPlan) -> bool:
    """True when the planner's own self-evaluation is satisfied.

    Does not reflect the critic's verdict — the loop's actual termination
    condition additionally requires the critic's approval when one is wired.
    """
    ra = plan.requirements_analysis
    return bool(ra.plan_acceptable and ra.scope_feasible)


def build_generate_plan_prompt(inp: PlanningInput) -> str:
    parts = [
        "Produce the JSON content plan for ONE blog post.",
        "[CONTENT_PLAN_JSON_V1]",
        "",
        "--- BRIEF ---",
        inp.brief.strip(),
        "",
        "--- LENGTH / PROFILE ---",
        inp.length_policy_context.strip(),
    ]
    if inp.audience:
        parts.extend(["", f"Audience: {inp.audience}"])
    if inp.tone_or_purpose:
        parts.append(f"Tone/Purpose: {inp.tone_or_purpose}")
    if inp.series_context_block and inp.series_context_block.strip():
        parts.extend(["", inp.series_context_block.strip()])
    parts.extend(
        [
            "",
            "--- RESEARCH DIGEST (ground the plan in this; flag gaps) ---",
            inp.research_digest.strip(),
        ]
    )
    return "\n".join(parts)


def build_refine_plan_prompt(inp: PlanningInput, previous: ContentPlan, feedback: str) -> str:
    base = build_generate_plan_prompt(inp)
    prev_json = previous.model_dump(mode="json")
    return (
        base
        + "\n\n--- PREVIOUS PLAN (JSON) ---\n"
        + json.dumps(prev_json, indent=2)
        + "\n\n--- REFINEMENT FEEDBACK ---\n"
        + feedback
        + "\n\n--- TASK ---\nReturn an improved full JSON plan as specified."
    )


def complete_plan_json(
    prompt: str,
    *,
    system: str,
    on_llm_request: Optional[Callable[[str], None]],
    max_parse_retries: int,
    call_json_fn: Callable[[str, str], dict],
    call_raw_fn: Callable[[str, str], str],
) -> tuple[dict[str, Any], int]:
    """Return (parsed dict, parse_retry_count).

    ``call_json_fn(prompt, system) -> dict`` is the first-attempt call and is
    fully self-contained: it owns its own completion-instruction suffix and
    JSON parsing (mirroring ``BlogWriterAgent._call_agent_json``).

    ``call_raw_fn(prompt, system) -> str`` is the retry-attempt call and
    returns raw text; this function appends the retry suffix and parses the
    result via ``extract_json_from_response``.

    Only ``LLMJsonParseError`` is treated as a retryable failure — other
    exceptions from ``call_json_fn``/``call_raw_fn`` (e.g. network/transport
    errors) propagate immediately rather than consuming a retry attempt.
    """
    last_err: Optional[Exception] = None
    for attempt in range(max_parse_retries):
        if on_llm_request:
            on_llm_request("Planning: generating structured plan...")
        try:
            data = call_json_fn(prompt, system)
            if isinstance(data, dict) and data:
                return data, attempt
            logger.warning(
                "call_json_fn returned a non-dict or empty result (attempt %s): %r; "
                "falling back to raw call",
                attempt + 1,
                data,
            )
        except LLMJsonParseError as e:
            last_err = e
            logger.warning("JSON parse failed (attempt %s): %s", attempt + 1, e)
        try:
            raw = call_raw_fn(
                prompt + "\n\nRespond with a single JSON object only, no markdown fences.",
                system,
            )
            data = extract_json_from_response(raw)
            return data, attempt
        except LLMJsonParseError as e:
            last_err = e
            logger.warning("JSON parse retry failed (attempt %s): %s", attempt + 1, e)
    msg = f"Planning JSON parse failed after {max_parse_retries} attempts"
    if last_err:
        msg += f": {last_err}"
    raise PlanningError(
        msg,
        failure_reason=PlanningFailureReason.PARSE_FAILURE.value,
        cause=last_err,
    )


class CompletePlanJsonFn(Protocol):
    """Signature of the LLM-call-and-parse step injected into the loop.

    Matches ``BlogPlanningAgent._complete_plan_json`` /
    ``BlogWriterAgent._complete_plan_json``, both of which wrap
    ``complete_plan_json`` with their own ``call_json_fn``/``call_raw_fn``
    closures.
    """

    def __call__(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]: ...


def run_content_planning_loop(
    planning_input: PlanningInput,
    *,
    length_policy: LengthPolicy,
    on_llm_request: Optional[Callable[[str], None]],
    max_iterations: int,
    max_parse_retries: int,
    plan_critic: Optional["BlogPlanCriticAgent"],
    brand_spec_prompt: str,
    writing_guidelines: str,
    work_dir: Optional[Union[str, Path]],
    generate_system: str,
    refine_system: str,
    complete_plan_json_fn: CompletePlanJsonFn,
    planner_context_tokens: Optional[int] = None,
) -> PlanningPhaseResult:
    """Generate and refine a ContentPlan until the planner (and optional critic) agree.

    When ``plan_critic`` is supplied, its verdict is authoritative: the loop
    terminates only when the planner's self-eval is done AND the critic
    approves. Refine feedback comes from the critic's structured violations
    instead of a generic string. When absent, legacy planner-self-eval only.
    When ``planner_context_tokens`` is supplied, the research digest is fitted
    independently to every concrete generate/refine prompt after charging that
    iteration's brief, prior plan, feedback, instructions, and response reserve.
    """
    t0 = time.monotonic()
    total_parse_retries = 0
    last_plan: Optional[ContentPlan] = None
    last_critic_report: Optional["PlanCriticReport"] = None

    # Deferred import: keeps this module (and blog_planning_agent, which has
    # historically stayed critic-dependency-free) from hard-importing
    # blog_plan_critic_agent when no critic is wired. Hoisted here (once,
    # rather than inside the loop body) since it only depends on whether a
    # critic is present at all, not on any per-iteration state.
    build_refine_feedback_from_critic: Optional[Callable[["PlanCriticReport"], str]] = None
    if plan_critic is not None:
        from agents.blogging.blog_plan_critic_agent.agent import build_refine_feedback_from_critic

    for iteration in range(1, max_iterations + 1):
        if iteration == 1:
            system = generate_system

            def build_prompt(digest: str) -> str:
                return build_generate_plan_prompt(
                    planning_input.model_copy(update={"research_digest": digest})
                )

        else:
            assert last_plan is not None
            if last_critic_report is not None:
                assert build_refine_feedback_from_critic is not None
                feedback = build_refine_feedback_from_critic(last_critic_report)
            else:
                feedback = (
                    "The plan is not yet acceptable. "
                    f"requirements_analysis: plan_acceptable={last_plan.requirements_analysis.plan_acceptable}, "
                    f"scope_feasible={last_plan.requirements_analysis.scope_feasible}. "
                    "Fix gaps, scope, and research alignment."
                )
            system = refine_system

            def build_prompt(digest: str) -> str:
                return build_refine_plan_prompt(
                    planning_input.model_copy(update={"research_digest": digest}),
                    last_plan,
                    feedback,
                )

        iteration_research_digest = planning_input.research_digest
        if planner_context_tokens is None:
            prompt = build_prompt(iteration_research_digest)
        else:
            # Account for this exact iteration's brief, previous-plan JSON, and
            # feedback before admitting research. Reserve the longest JSON-only
            # retry instruction appended by ``complete_plan_json`` as well.
            retry_suffix = "\n\nRespond with a single JSON object only, no markdown fences."
            prompt, iteration_research_digest = fit_optional_text_to_prompt(
                iteration_research_digest,
                build_prompt=build_prompt,
                system_prompt=system,
                context_tokens=planner_context_tokens,
                extra_prompt_reserve_chars=len(retry_suffix),
            )

        data, pr = complete_plan_json_fn(
            prompt,
            system=system,
            on_llm_request=on_llm_request,
            max_parse_retries=max_parse_retries,
        )
        total_parse_retries += pr

        try:
            plan = ContentPlan.model_validate(data)
        except Exception as e:
            raise PlanningError(
                f"Invalid content plan schema: {e}",
                failure_reason=PlanningFailureReason.PARSE_FAILURE.value,
                cause=e,
            ) from e

        plan = post_validate_plan(plan, length_policy)
        if not plan.title_candidates:
            plan = plan.model_copy(
                update={
                    "title_candidates": [
                        TitleCandidate(
                            title=plan.overarching_topic,
                            probability_of_success=0.5,
                        )
                    ]
                }
            )
        last_plan = plan.model_copy(update={"plan_version": iteration})

        planner_ok = is_planner_self_eval_satisfied(last_plan)
        critic_report: Optional["PlanCriticReport"] = None
        if plan_critic is not None:
            critic_report = plan_critic.run(
                plan=last_plan,
                brand_spec_prompt=brand_spec_prompt,
                writing_guidelines=writing_guidelines,
                research_digest=iteration_research_digest,
                on_llm_request=on_llm_request,
                work_dir=work_dir,
                artifact_name=f"plan_critic_report_v{iteration}.json",
            )
            last_critic_report = critic_report

        critic_ok = critic_report is None or getattr(critic_report, "approved", False)
        if planner_ok and critic_ok:
            wall_ms = (time.monotonic() - t0) * 1000.0
            critic_dict = (
                critic_report.to_dict()
                if critic_report is not None and hasattr(critic_report, "to_dict")
                else None
            )
            return PlanningPhaseResult(
                content_plan=last_plan,
                planning_iterations_used=iteration,
                parse_retry_count=total_parse_retries,
                planning_wall_ms_total=wall_ms,
                plan_critic_report=critic_dict,
            )

        logger.info(
            "Planning iteration %s not done: plan_acceptable=%s scope_feasible=%s critic_approved=%s",
            iteration,
            last_plan.requirements_analysis.plan_acceptable,
            last_plan.requirements_analysis.scope_feasible,
            getattr(critic_report, "approved", None),
        )

    assert last_plan is not None
    raise PlanningError(
        f"Planning did not converge after {max_iterations} iterations",
        failure_reason=PlanningFailureReason.MAX_ITERATIONS_REACHED.value,
    )
