"""
Shared base for the frontend-code-v2 "plan-phase generator" tool agents.

Three tool agents — Branding/Theme, UI Design, and Architecture — do their real
work in the ``plan`` phase: format a single prompt, call one LLM, parse a JSON
object, and turn selected fields into ``recommendations``. Their ``run``,
``execute`` (stub), and ``review``/``problem_solve``/``deliver`` (static stubs)
were byte-aligned, and their three ``plan`` bodies were copy-pasted with an
inert ``(lambda _r: str(_r))`` wrapper and three subtly different inline JSON
fallbacks. :class:`PlanGeneratorToolAgent` captures the shared lifecycle once and
parses leniently via the shared
:func:`~software_engineering_team.shared.tool_agent_base.lenient_json_object`
helper; subclasses declare only the differing prompt, field→label map, and
fallback strings.

Like the other code-v2 tool-agent bases, the concrete ``agent.py`` keeps a
top-level ``from strands import Agent`` so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; the base resolves
``Agent`` from the concrete subclass module via :meth:`_agent_factory`.
"""

from __future__ import annotations

import importlib
import logging
from typing import List, Optional, Sequence, Tuple

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.tool_agent_base import lenient_json_object

from ..models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)


class PlanGeneratorToolAgent:
    """Base for plan-phase generator tool agents.

    Subclasses set :attr:`log_label`, the phase summaries, the no-model / LLM-error
    fallbacks, the ordered :attr:`field_labels` map, and implement
    :meth:`_build_plan_prompt`.

    Invariants: instance state is limited to ``_model`` and ``llm`` — so tests
    that build an instance via ``__new__`` and set those attributes behave
    identically to a constructed instance.
    """

    # Labels / summaries (subclasses override) --------------------------------
    log_label: str = "Plan"
    execute_summary: str = ""
    review_summary: str = ""
    problem_solve_summary: str = ""
    deliver_summary: str = ""

    # Plan-phase fallbacks (subclasses override) ------------------------------
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

    def __init__(self, llm=None) -> None:
        self._model = resolve_strands_model(llm, get_strands_model_fn=get_strands_model)
        self.llm = llm  # kept for backward compat checks

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass module (monkeypatchable)."""
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

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

        Preconditions: the subclass sets :attr:`field_labels` and implements
        :meth:`_build_plan_prompt`.
        Postconditions: returns a :class:`ToolAgentPhaseOutput`; a missing model,
        an LLM error, or unparseable output each yield the corresponding declared
        fallback rather than raising.
        """
        if not self._model:
            return ToolAgentPhaseOutput(
                recommendations=list(self.no_model_recommendations),
                summary=self.no_model_summary,
            )
        prompt = self._build_plan_prompt(inp)
        try:
            raw = str(self._agent_factory()(model=self._model)(prompt)).strip()
        except Exception as e:
            self._logger.warning("%s plan LLM call failed: %s", self.log_label, e)
            return ToolAgentPhaseOutput(
                recommendations=list(self.llm_error_recommendations),
                summary=self.llm_error_summary,
            )
        data = lenient_json_object(
            raw,
            logger=self._logger,
            context=f"{self.log_label} plan",
            on_fail_msg="using empty plan output",
        )
        recommendations = [
            f"{label}: {data[key]}" for key, label in self.field_labels if data.get(key)
        ]
        if not recommendations:
            recommendations = list(self.empty_recommendations)
        summary = data.get("summary", self.default_summary)
        if not summary and self.empty_summary_override is not None:
            summary = self.empty_summary_override
        return ToolAgentPhaseOutput(recommendations=recommendations, summary=summary)

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static review stub."""
        return ToolAgentPhaseOutput(summary=self.review_summary)

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static problem-solve stub."""
        return ToolAgentPhaseOutput(summary=self.problem_solve_summary)

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Return the static deliver stub."""
        return ToolAgentPhaseOutput(summary=self.deliver_summary)
