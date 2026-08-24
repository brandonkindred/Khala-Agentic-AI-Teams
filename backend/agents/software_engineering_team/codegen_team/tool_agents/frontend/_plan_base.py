"""
Shared base for the frontend-code-v2 "plan-phase generator" tool agents.

Three tool agents — Branding/Theme, UI Design, and Architecture — do their real
work in the ``plan`` phase: format a single prompt, call one LLM, parse a JSON
object, and turn selected fields into ``recommendations``. Their ``run``,
``execute`` (stub), and ``review``/``problem_solve``/``deliver`` (static stubs)
were byte-aligned, and their three ``plan`` bodies were copy-pasted with an
inert ``(lambda _r: str(_r))`` wrapper and three subtly different inline JSON
fallbacks. :class:`PlanGeneratorToolAgent` captures the shared lifecycle once as
a thin :class:`~software_engineering_team.shared.llm_tool_agent_base.LlmToolAgentBase`
specialization (Plan recipe: JSON model resolution, inline invocation, extract
JSON salvage, 3-tier fallbacks). Subclasses declare only the differing prompt,
field→label map, and fallback strings.

Like the other code-v2 tool-agent bases, the concrete ``agent.py`` keeps a
top-level ``from strands import Agent`` so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; the inherited
:meth:`LlmToolAgentBase._agent_factory` resolves ``Agent`` from the concrete
subclass module.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from llm_service import get_strands_model
from software_engineering_team.codegen_team.models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase


class PlanGeneratorToolAgent(LlmToolAgentBase):
    """Base for plan-phase generator tool agents.

    Subclasses set :attr:`log_label`, the phase summaries, the no-model / LLM-error
    fallbacks, the ordered :attr:`field_labels` map, and implement
    :meth:`_build_plan_prompt`.

    Preconditions:
        Subclasses that call :meth:`plan` set :attr:`field_labels` and implement
        :meth:`_build_plan_prompt`.

    Postconditions:
        Construction (when :attr:`resolve_models` is true) sets ``self.llm`` and
        ``self._model`` via the shared Plan recipe.

    Invariants:
        Instance state is limited to ``_model`` and ``llm`` — so tests that build
        an instance via ``__new__`` and set those attributes behave identically
        to a constructed instance. Plan recipe attrs stay
        ``resolve_models=True``, ``response_format="json"``,
        ``get_strands_model_fn=get_strands_model``, ``use_run_strands_agent=False``,
        ``json_parse_strategy="extract"``.
    """

    # --- LlmToolAgentBase Plan recipe ------------------------------------
    resolve_models: bool = True
    response_format: str = "json"
    get_strands_model_fn = get_strands_model
    json_parse_strategy: str = "extract"
    # use_run_strands_agent remains False (inline Agent(prompt) path)

    # Labels / summaries (subclasses override) --------------------------------
    log_label: str = "Plan"
    execute_summary: str = ""
    review_summary: str = ""
    problem_solve_summary: str = ""
    deliver_summary: str = ""

    # Plan-phase fallbacks (subclasses override; shared helpers read via type(self))
    no_model_recommendations: List[str] = []
    no_model_summary: str = ""
    llm_error_recommendations: List[str] = []
    llm_error_summary: str = ""
    empty_recommendations: List[str] = []
    default_summary: str = ""
    # When the model returns an explicit empty ``summary``, fall back to this if
    # set (Architecture keeps a distinct "missing" vs "empty" summary); ``None``
    # preserves the empty string.
    empty_summary_override: Optional[str] = None

    # Ordered (json_key, recommendation_label) pairs used to build the plan's
    # recommendations from the parsed model output.
    field_labels: Sequence[Tuple[str, str]] = ()

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        """Delegate to :meth:`execute`."""
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        """Return the static execute stub output.

        Preconditions: ``inp`` exposes ``microtask`` with ``id``.
        Postconditions: returns a :class:`ToolAgentOutput` with :attr:`execute_summary`.
        """
        self._logger.info("%s: microtask %s (execute stub)", self.log_label, inp.microtask.id)
        return ToolAgentOutput(summary=self.execute_summary)

    def _build_plan_prompt(self, inp: ToolAgentPhaseInput) -> str:  # pragma: no cover - overridden
        """Return the fully formatted plan prompt (subclass-specific)."""
        raise NotImplementedError

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Generate plan-phase artifacts from a single LLM prompt.

        Preconditions:
            The subclass sets :attr:`field_labels` and implements
            :meth:`_build_plan_prompt`. ``self._model`` is set (possibly falsy).

        Postconditions:
            Returns a :class:`ToolAgentPhaseOutput`. A missing model, an LLM
            error, or unparseable output each yield the corresponding declared
            fallback rather than raising. Empty-parse failures also emit the
            historical warning log before applying empty-tier messages.
        """
        no_model = self._fallback_no_model(self._model)
        if no_model is not None:
            return ToolAgentPhaseOutput(
                recommendations=no_model.recommendations,
                summary=no_model.summary,
            )

        prompt = self._build_plan_prompt(inp)
        status, result = self._call_with_single_fallback(
            lambda: self._invoke_llm(self._model, prompt),
            log_label=f"{self.log_label} plan",
        )
        if status == "error":
            return ToolAgentPhaseOutput(
                recommendations=result.recommendations,
                summary=result.summary,
            )

        raw = result
        data = self._parse_llm_json(raw)
        if data is None:
            self._logger.warning(
                "%s plan: model output did not parse as JSON; using empty plan output",
                self.log_label,
            )
            data = {}

        recommendations = [
            f"{label}: {data[key]}" for key, label in self.field_labels if data.get(key)
        ]
        summary = data.get("summary", self.default_summary)
        if summary is None:
            summary = ""
        payload = self._fallback_empty_parse(
            recommendations=recommendations,
            summary=summary,
        )
        return ToolAgentPhaseOutput(
            recommendations=payload.recommendations,
            summary=payload.summary,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static review stub."""
        return ToolAgentPhaseOutput(summary=self.review_summary)

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static problem-solve stub."""
        return ToolAgentPhaseOutput(summary=self.problem_solve_summary)

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static deliver stub."""
        return ToolAgentPhaseOutput(summary=self.deliver_summary)
