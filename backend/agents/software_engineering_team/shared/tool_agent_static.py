"""
Shared bases for the code-v2 "static phase" tool agents.

Beyond the review/single-issue-fix family (see :mod:`tool_agent_base`), both
``backend_code_v2_team`` and ``frontend_code_v2_team`` ship two other shapes of
tool agent that were previously copy-pasted module by module (and lived only in
the backend team):

* **File generators** (auth, data engineering, API/OpenAPI) — ``execute`` runs a
  single LLM prompt and parses ``## FILE ## / ## SUMMARY ##`` template output;
  ``plan``/``review``/``problem_solve``/``deliver`` are static advisory outputs.
* **Adapter stubs** (CI/CD, containerization) — every phase, including
  ``execute``, returns a static phase/tool output.

Both shapes only differ in a handful of label/recommendation strings (and, for
generators, the prompt and the team-specific template parser).
:class:`StaticPhaseToolAgent` captures the static
``plan``/``review``/``problem_solve``/``deliver`` lifecycle; the two concrete
bases add the differing ``execute``.

Team-agnostic by construction:

* The ``inp`` argument is duck-typed (no import of either team's ``models``); it
  must expose ``microtask`` (with ``id``/``title``/``description``) plus
  ``language``/``existing_code`` for the generator path.
* The output template parser differs per team (path-prefix normalization), so
  :class:`FileGeneratorToolAgent` reads it from the ``_parse_files_and_summary``
  class-attribute hook that the concrete subclass sets — mirroring the
  ``_parse_review``/``_parse_single_issue`` hook idiom in
  :class:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent`.

As with :class:`~software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent`,
generators resolve ``Agent`` from the *concrete subclass module* so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; concrete modules keep a
top-level ``from strands import Agent``.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from software_engineering_team.shared.v2_models import ToolAgentOutput, ToolAgentPhaseOutput

MAX_EXISTING_CODE_CHARS = 4_000


class StaticPhaseToolAgent:
    """Lifecycle template whose ``plan``/``review``/``problem_solve``/``deliver``
    return static advisory output declared via class attributes.

    Invariants: instances hold no state beyond what concrete subclasses add, so
    ``__new__``-constructed instances behave identically to constructed ones.
    """

    # Recommendations + summary for each static phase (subclasses override).
    plan_recommendations: Tuple[str, ...] = ()
    plan_summary: str = ""
    review_recommendations: Tuple[str, ...] = ()
    review_summary: str = ""
    problem_solve_recommendations: Tuple[str, ...] = ()
    problem_solve_summary: str = ""
    deliver_recommendations: Tuple[str, ...] = ()
    deliver_summary: str = ""

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def run(self, inp) -> ToolAgentOutput:
        """Delegate to :meth:`execute`.

        Preconditions: ``inp`` exposes ``microtask``.
        Postconditions: returns the :class:`ToolAgentOutput` from ``execute``.
        """
        return self.execute(inp)

    def execute(self, inp) -> ToolAgentOutput:  # pragma: no cover - overridden
        raise NotImplementedError

    def plan(self, inp) -> ToolAgentPhaseOutput:
        """Return the static plan advisory output.

        Postconditions: ``recommendations`` is a fresh copy of
        :attr:`plan_recommendations`; ``summary`` equals :attr:`plan_summary`.
        """
        return ToolAgentPhaseOutput(
            recommendations=list(self.plan_recommendations),
            summary=self.plan_summary,
        )

    def review(self, inp) -> ToolAgentPhaseOutput:
        """Return the static review advisory output."""
        return ToolAgentPhaseOutput(
            recommendations=list(self.review_recommendations),
            summary=self.review_summary,
        )

    def problem_solve(self, inp) -> ToolAgentPhaseOutput:
        """Return the static problem-solve advisory output."""
        return ToolAgentPhaseOutput(
            recommendations=list(self.problem_solve_recommendations),
            summary=self.problem_solve_summary,
        )

    def deliver(self, inp) -> ToolAgentPhaseOutput:
        """Return the static deliver advisory output."""
        return ToolAgentPhaseOutput(
            recommendations=list(self.deliver_recommendations),
            summary=self.deliver_summary,
        )


class FileGeneratorToolAgent(StaticPhaseToolAgent):
    """Base for tool agents that generate files from a single LLM prompt.

    Subclasses set :attr:`generation_prompt` (a ``str.format`` template taking
    ``description``/``language``/``existing_code``), :attr:`log_label`, and the
    :attr:`_parse_files_and_summary` parser hook (the team-specific
    ``parse_files_and_summary_template``).

    Invariants: instance state is limited to ``_model`` — so tests that build
    instances via ``__new__`` and set ``_model`` behave identically to
    constructed instances.
    """

    log_label: str = "Generator"
    generation_prompt: str = ""

    # Parser hook (set by subclass as a staticmethod): the team-specific
    # ``parse_files_and_summary_template``. Required for ``execute``.
    _parse_files_and_summary: Optional[Callable[[str], Dict[str, Any]]] = None

    def __init__(self, llm=None) -> None:
        from llm_service.strands_model import resolve_strands_model

        # v2 tool agents consume template-parsed output (parse_review_template /
        # parse_files_and_summary_template / parse_problem_solving_single_issue_template);
        # the mixed-mode ones (accessibility / performance / ux_usability) have
        # JSON paths with defensive fence-stripping fallbacks that work in text mode.
        self._model = resolve_strands_model(llm, response_format="text")

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass module (monkeypatchable)."""
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

    def execute(self, inp) -> ToolAgentOutput:
        """Generate files from a single LLM prompt and parse the template output.

        Preconditions: ``inp`` exposes ``microtask`` (with ``id``/``title``/
        ``description``), ``language`` and ``existing_code``; the subclass sets
        :attr:`generation_prompt` and :attr:`_parse_files_and_summary`.
        Postconditions: returns a :class:`ToolAgentOutput` whose ``files`` and
        ``summary`` come from parsing the model output (empty ``files`` when the
        model returns none).
        """
        existing = inp.existing_code or "(none)"
        prompt = self.generation_prompt.format(
            description=inp.microtask.description or inp.microtask.title,
            language=inp.language,
            existing_code=existing,
        )
        self._logger.info("%s: running for microtask %s", self.log_label, inp.microtask.id)
        raw = str(self._agent_factory()(model=self._model)(prompt)).strip()
        data = self._parse_files_and_summary(raw)
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
    execute_recommendations: Tuple[str, ...] = ()
    execute_summary: Optional[str] = None

    def execute(self, inp) -> ToolAgentOutput:
        """Return a static "not yet implemented" output.

        Preconditions: ``inp`` exposes ``microtask`` with ``id``.
        Postconditions: returns a :class:`ToolAgentOutput` with no files and a
        non-empty summary (``execute_summary`` or a label-derived default).
        """
        self._logger.info(
            "%s stub: microtask %s (not yet implemented)", self.label, inp.microtask.id
        )
        return ToolAgentOutput(
            summary=self.execute_summary or f"{self.label} adapter stub — no changes applied.",
            recommendations=list(self.execute_recommendations),
        )
