"""
Blog planning agent: structured content plan + refine loop until definition-of-done.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.content_plan import PlanningInput, PlanningPhaseResult
from agents.blogging.shared.content_planning_loop import (
    PlanningError,  # noqa: F401 (re-exported: documented in run()'s docstring as a raised exception)
    complete_plan_json,
    run_content_planning_loop,
)
from agents.blogging.shared.content_profile import LengthPolicy
from agents.blogging.shared.planning_config import (
    planning_max_iterations,
    planning_max_parse_retries,
)
from agents.blogging.shared.prompt_budget import resolve_model_context_tokens
from strands import Agent

from llm_service import extract_json_from_response

from .prompts import GENERATE_PLAN_SYSTEM, REFINE_PLAN_SYSTEM

logger = logging.getLogger(__name__)


class BlogPlanningAgent(_BlogAgentBase):
    """Generates and refines a ContentPlan until acceptance criteria or max iterations.

    When constructed with ``plan_critic``, its approval gates the refine loop
    alongside the planner's own self-evaluation, and its violations drive the
    refine feedback passed back into the model.

    Preconditions:
        - llm_client is not None.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        plan_critic: Optional[Any] = None,
        brand_spec_prompt: str = "",
        writing_guidelines: str = "",
    ) -> None:
        """Initialize the planning agent.

        Args:
            llm_client: Configured LLM client. Must not be None; enforced by ``_BlogAgentBase``.
            plan_critic: Optional critic agent whose approval gates the refine loop.
            brand_spec_prompt: Optional brand-specific instructions (stripped on store).
            writing_guidelines: Optional writing-style guidelines (stripped on store).

        Preconditions:
            - llm_client is not None.
        """
        super().__init__(llm_client)
        self._plan_critic = plan_critic
        self._brand_spec_prompt = (brand_spec_prompt or "").strip()
        self._writing_guidelines = (writing_guidelines or "").strip()

    def _call_agent(self, prompt: str, system: str) -> str:
        """Call a Strands Agent with the given prompt and system prompt, return raw text."""
        agent = Agent(model=self._model, system_prompt=system)
        result = agent(prompt)
        return str(result).strip()

    def _complete_plan_json(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]:
        """Return (parsed dict, parse_retry_count)."""

        def call_json_fn(p: str, s: str) -> dict[str, Any]:
            raw = self._call_agent(p + "\n\nRespond with valid JSON only, no markdown fences.", s)
            return extract_json_from_response(raw)

        return complete_plan_json(
            prompt,
            system=system,
            on_llm_request=on_llm_request,
            max_parse_retries=max_parse_retries,
            call_json_fn=call_json_fn,
            call_raw_fn=self._call_agent,
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
        """Generate and refine a ContentPlan until acceptance criteria or max iterations.

        Preconditions:
            ``planning_input`` is a valid :class:`PlanningInput`; ``length_policy``
            is a valid :class:`LengthPolicy`. ``max_iterations`` and
            ``max_parse_retries``, when omitted, default to
            :func:`planning_max_iterations` and :func:`planning_max_parse_retries`
            respectively.

        Postconditions:
            Returns a :class:`PlanningPhaseResult` wrapping a ``ContentPlan``
            whose ``requirements_analysis`` satisfies the planner's own
            self-evaluation (``plan_acceptable`` and ``scope_feasible``) and,
            when this agent was constructed with a ``plan_critic``, that
            critic's approval as well.

        Raises:
            PlanningError: If the plan JSON fails schema validation, if JSON
                parsing fails after exhausting ``max_parse_retries`` attempts
                in a given iteration, or if the loop fails to converge within
                ``max_iterations`` iterations.
        """
        max_iter = max_iterations if max_iterations is not None else planning_max_iterations()
        max_parse = (
            max_parse_retries if max_parse_retries is not None else planning_max_parse_retries()
        )
        return run_content_planning_loop(
            planning_input,
            length_policy=length_policy,
            on_llm_request=on_llm_request,
            max_iterations=max_iter,
            max_parse_retries=max_parse,
            plan_critic=self._plan_critic,
            brand_spec_prompt=self._brand_spec_prompt,
            writing_guidelines=self._writing_guidelines,
            work_dir=work_dir,
            generate_system=GENERATE_PLAN_SYSTEM,
            refine_system=REFINE_PLAN_SYSTEM,
            complete_plan_json_fn=self._complete_plan_json,
            planner_context_tokens=resolve_model_context_tokens(self._model),
        )


class _BlogPlanningAgentRunner:
    """Zero-arg-constructor shim over :class:`BlogPlanningAgent`.

    The shared invoke dispatcher (``shared.agent_invoke.dispatch``) passes a
    single raw JSON ``body: dict`` to ``.run``. The real ``BlogPlanningAgent``
    takes a ``PlanningInput`` plus a required ``length_policy`` keyword. This
    runner bridges the two — for sandbox/Agent-Console usage only. Production
    code continues to instantiate :class:`BlogPlanningAgent` directly.
    """

    def __init__(self, agent: BlogPlanningAgent, length_policy: LengthPolicy) -> None:
        self._agent = agent
        self._default_length_policy = length_policy

    def run(self, body: dict) -> dict:
        """Run the wrapped :class:`BlogPlanningAgent` against a raw JSON request body.

        Preconditions:
            ``body`` must be a ``dict``. It may optionally contain a
            ``length_policy`` dict block (resolved via
            :func:`resolve_length_policy_from_request_dict`) and a
            ``planning_input`` block validated as :class:`PlanningInput`; if
            ``planning_input`` is absent, ``body`` itself is used as the
            planning input block. If ``length_policy`` is absent or empty,
            the runner's default length policy (set at construction) is used.

        Postconditions:
            Returns a JSON-serializable ``dict`` — the ``mode="json"`` dump
            of the resulting :class:`PlanningPhaseResult`.

        Raises:
            TypeError: If ``body`` is not a ``dict``.
        """
        from agents.blogging.shared.content_profile import resolve_length_policy_from_request_dict

        if not isinstance(body, dict):
            raise TypeError(f"blogging.planner body must be a dict, got {type(body).__name__}")

        lp_block = body.get("length_policy")
        if isinstance(lp_block, dict) and lp_block:
            length_policy = resolve_length_policy_from_request_dict(lp_block)
        else:
            length_policy = self._default_length_policy

        planning_input_block = body.get("planning_input", body)
        planning_input = PlanningInput.model_validate(planning_input_block)

        result = self._agent.run(planning_input, length_policy=length_policy)
        return result.model_dump(mode="json")


def make_blog_planning_agent() -> _BlogPlanningAgentRunner:
    """Zero-arg factory consumed by the Agent Console sandbox's dispatcher.

    Wires an ``llm_service``-provided LLM client (env-configured: Ollama in
    production, ``DummyLLMClient`` when ``LLM_PROVIDER=dummy``) into a
    :class:`BlogPlanningAgent` and returns a runner that accepts a JSON body
    from :class:`shared.agent_invoke.shim`.

    Preconditions:
        None (zero-arg).

    Postconditions:
        Returns a :class:`_BlogPlanningAgentRunner` wired to an
        env-resolved LLM client and a default ``standard_article`` length
        policy.
    """
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    from llm_service import get_client

    llm_client = get_client("blog_planning")
    agent = BlogPlanningAgent(llm_client=llm_client)
    default_policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    return _BlogPlanningAgentRunner(agent, default_policy)
