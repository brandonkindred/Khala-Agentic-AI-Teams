"""
Blog planning agent: structured content plan + refine loop until definition-of-done.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from shared.content_plan import (
    ContentPlan,
    PlanningFailureReason,
    PlanningInput,
    PlanningPhaseResult,
    TitleCandidate,
    section_count_bounds_for_profile,
)
from shared.content_profile import LengthPolicy
from shared.errors import PlanningError
from shared.planning_config import planning_max_iterations, planning_max_parse_retries
from strands import Agent

from .json_utils import parse_json_object
from .prompts import GENERATE_PLAN_SYSTEM, REFINE_PLAN_SYSTEM

logger = logging.getLogger(__name__)


def _post_validate(plan: ContentPlan, policy: LengthPolicy) -> ContentPlan:
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


def _planning_done(plan: ContentPlan) -> bool:
    ra = plan.requirements_analysis
    return bool(ra.plan_acceptable and ra.scope_feasible)


def _build_generate_prompt(inp: PlanningInput) -> str:
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


def _build_refine_prompt(inp: PlanningInput, previous: ContentPlan, feedback: str) -> str:
    base = _build_generate_prompt(inp)
    prev_json = previous.model_dump(mode="json")
    return (
        base
        + "\n\n--- PREVIOUS PLAN (JSON) ---\n"
        + json.dumps(prev_json, indent=2)
        + "\n\n--- REFINEMENT FEEDBACK ---\n"
        + feedback
        + "\n\n--- TASK ---\nReturn an improved full JSON plan as specified."
    )


class BlogPlanningAgent:
    """Generates and refines a ContentPlan until acceptance criteria or max iterations.

    When constructed with ``plan_critic``, its approval gates the refine loop
    alongside the planner's own self-evaluation, and its violations drive the
    refine feedback passed back into the model.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        plan_critic: Optional[Any] = None,
        brand_spec_prompt: str = "",
        writing_guidelines: str = "",
    ) -> None:
        self._model = llm_client
        self._plan_critic = plan_critic
        self._brand_spec_prompt = (brand_spec_prompt or "").strip()
        self._writing_guidelines = (writing_guidelines or "").strip()

    def _call_agent(self, prompt: str, system: str) -> str:
        """Call a Strands Agent with the given prompt and system prompt, return raw text."""
        agent = Agent(model=self._model, system_prompt=system)
        result = agent(prompt)
        return str(result).strip()

    def _parse_json_response(self, raw: str) -> dict:
        """Strip markdown fences and parse JSON."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)

    def _complete_plan_json(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]:
        """Return (parsed dict, parse_retry_count)."""
        parse_retries = 0
        last_err: Optional[Exception] = None
        for attempt in range(max_parse_retries):
            if on_llm_request:
                on_llm_request("Planning: generating structured plan...")
            try:
                raw = self._call_agent(
                    prompt + "\n\nRespond with valid JSON only, no markdown fences.",
                    system,
                )
                data = self._parse_json_response(raw)
                if isinstance(data, dict) and data:
                    return data, parse_retries
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                parse_retries += 1
                logger.warning("JSON parse failed (attempt %s): %s", attempt + 1, e)
            try:
                raw = self._call_agent(
                    prompt + "\n\nRespond with a single JSON object only, no markdown fences.",
                    system,
                )
                data = parse_json_object(raw)
                return data, parse_retries
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                parse_retries += 1
                logger.warning("parse_json_object failed (attempt %s): %s", attempt + 1, e)
        msg = f"Planning JSON parse failed after {max_parse_retries} attempts"
        if last_err:
            msg += f": {last_err}"
        raise PlanningError(
            msg,
            failure_reason=PlanningFailureReason.PARSE_FAILURE.value,
            cause=last_err,
        )

    def run(
        self,
        planning_input: PlanningInput,
        *,
        length_policy: LengthPolicy,
        on_llm_request: Optional[Callable[[str], None]] = None,
        max_iterations: Optional[int] = None,
        max_parse_retries: Optional[int] = None,
        work_dir: Optional[Union[str, Path]] = None,
    ) -> PlanningPhaseResult:
        max_iter = max_iterations if max_iterations is not None else planning_max_iterations()
        max_parse = (
            max_parse_retries if max_parse_retries is not None else planning_max_parse_retries()
        )

        # Deferred import to keep this module dependency-free when the critic isn't wired.
        from blog_plan_critic_agent.agent import build_refine_feedback_from_critic

        t0 = time.monotonic()
        total_parse_retries = 0
        last_plan: Optional[ContentPlan] = None
        last_critic_report: Optional[Any] = None

        for iteration in range(1, max_iter + 1):
            if iteration == 1:
                prompt = _build_generate_prompt(planning_input)
                system = GENERATE_PLAN_SYSTEM
            else:
                assert last_plan is not None
                if last_critic_report is not None:
                    feedback = build_refine_feedback_from_critic(last_critic_report)
                else:
                    feedback = (
                        "The plan is not yet acceptable. "
                        f"requirements_analysis: plan_acceptable={last_plan.requirements_analysis.plan_acceptable}, "
                        f"scope_feasible={last_plan.requirements_analysis.scope_feasible}. "
                        "Fix gaps, scope, and research alignment."
                    )
                prompt = _build_refine_prompt(planning_input, last_plan, feedback)
                system = REFINE_PLAN_SYSTEM

            data, pr = self._complete_plan_json(
                prompt,
                system=system,
                on_llm_request=on_llm_request,
                max_parse_retries=max_parse,
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

            plan = _post_validate(plan, length_policy)
            if not plan.title_candidates:
                plan = plan.model_copy(
                    update={
                        "title_candidates": [
                            TitleCandidate(
                                title=plan.overarching_topic[:120],
                                probability_of_success=0.5,
                            )
                        ]
                    }
                )
            last_plan = plan
            plan = plan.model_copy(update={"plan_version": iteration})

            planner_ok = _planning_done(plan)
            critic_report = None
            if self._plan_critic is not None:
                critic_report = self._plan_critic.run(
                    plan=plan,
                    brand_spec_prompt=self._brand_spec_prompt,
                    writing_guidelines=self._writing_guidelines,
                    research_digest=planning_input.research_digest,
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
                    content_plan=plan,
                    planning_iterations_used=iteration,
                    parse_retry_count=total_parse_retries,
                    planning_wall_ms_total=wall_ms,
                    plan_critic_report=critic_dict,
                )

            logger.info(
                "Planning iteration %s not done: plan_acceptable=%s scope_feasible=%s critic_approved=%s",
                iteration,
                plan.requirements_analysis.plan_acceptable,
                plan.requirements_analysis.scope_feasible,
                getattr(critic_report, "approved", None),
            )

        assert last_plan is not None
        raise PlanningError(
            f"Planning did not converge after {max_iter} iterations",
            failure_reason=PlanningFailureReason.MAX_ITERATIONS_REACHED.value,
        )
