"""
Shared bases for the backend-code-v2 "static phase" tool agents.

Beyond the review/single-issue-fix family (see ``base.py`` /
``shared.tool_agent_base``), the team ships two other shapes of tool agent that
were previously copy-pasted module by module:

* **File generators** (auth, data engineering, API/OpenAPI) — ``execute`` runs a
  single LLM prompt and parses ``## FILE ## / ## SUMMARY ##`` template output;
  ``plan``/``review``/``problem_solve``/``deliver`` are static advisory outputs.
* **Adapter stubs** (CI/CD, containerization) — every phase, including
  ``execute``, returns a static :class:`ToolAgentPhaseOutput`/``ToolAgentOutput``.

Both shapes only differ in a handful of label/recommendation strings (and, for
generators, the prompt). :class:`StaticPhaseToolAgent` captures the static
``plan``/``review``/``problem_solve``/``deliver`` lifecycle; the two concrete
bases add the differing ``execute``.

As with :class:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent`,
generators resolve ``Agent`` from the *concrete subclass module* so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; concrete modules keep a
top-level ``from strands import Agent``.
"""

from __future__ import annotations

import importlib
import logging
from typing import List, Optional

from ..models import (
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ..output_templates import parse_files_and_summary_template

MAX_EXISTING_CODE_CHARS = 4_000


class StaticPhaseToolAgent:
    """Lifecycle template whose ``plan``/``review``/``problem_solve``/``deliver``
    return static advisory output declared via class attributes.

    Invariants: instances hold no state beyond what concrete subclasses add, so
    ``__new__``-constructed instances behave identically to constructed ones.
    """

    # Recommendations + summary for each static phase (subclasses override).
    plan_recommendations: List[str] = []
    plan_summary: str = ""
    review_recommendations: List[str] = []
    review_summary: str = ""
    problem_solve_recommendations: List[str] = []
    problem_solve_summary: str = ""
    deliver_recommendations: List[str] = []
    deliver_summary: str = ""

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:  # pragma: no cover - overridden
        raise NotImplementedError

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=list(self.plan_recommendations),
            summary=self.plan_summary,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=list(self.review_recommendations),
            summary=self.review_summary,
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=list(self.problem_solve_recommendations),
            summary=self.problem_solve_summary,
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        return ToolAgentPhaseOutput(
            recommendations=list(self.deliver_recommendations),
            summary=self.deliver_summary,
        )


class FileGeneratorToolAgent(StaticPhaseToolAgent):
    """Base for tool agents that generate files from a single LLM prompt.

    Subclasses set :attr:`generation_prompt` (a ``str.format`` template taking
    ``description``/``language``/``existing_code``) and :attr:`log_label`.
    """

    log_label: str = "Generator"
    generation_prompt: str = ""

    def __init__(self, llm=None) -> None:
        from software_engineering_team.shared.strands_model import resolve_strands_model

        # v2 tool agents consume template-parsed output (parse_review_template /
        # parse_files_and_summary_template / parse_problem_solving_single_issue_template);
        # the mixed-mode ones (accessibility / performance / ux_usability) have
        # JSON paths with defensive fence-stripping fallbacks that work in text mode.
        self._model = resolve_strands_model(llm, response_format="text")

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass module (monkeypatchable)."""
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        existing = inp.existing_code[:MAX_EXISTING_CODE_CHARS] if inp.existing_code else "(none)"
        prompt = self.generation_prompt.format(
            description=inp.microtask.description or inp.microtask.title,
            language=inp.language,
            existing_code=existing,
        )
        self._logger.info("%s: running for microtask %s", self.log_label, inp.microtask.id)
        raw = str(self._agent_factory()(model=self._model)(prompt)).strip()
        data = parse_files_and_summary_template(raw)
        return ToolAgentOutput(
            files=data.get("files") or {},
            recommendations=[],
            summary=data.get("summary", ""),
        )


class StubToolAgent(StaticPhaseToolAgent):
    """Base for adapter stubs whose every phase returns a static message.

    Subclasses set :attr:`label` plus the per-phase recommendation/summary class
    attributes inherited from :class:`StaticPhaseToolAgent`.
    """

    label: str = "Stub"
    execute_recommendations: List[str] = []
    execute_summary: Optional[str] = None

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        self._logger.info(
            "%s stub: microtask %s (not yet implemented)", self.label, inp.microtask.id
        )
        return ToolAgentOutput(
            summary=self.execute_summary or f"{self.label} adapter stub — no changes applied.",
            recommendations=list(self.execute_recommendations),
        )
