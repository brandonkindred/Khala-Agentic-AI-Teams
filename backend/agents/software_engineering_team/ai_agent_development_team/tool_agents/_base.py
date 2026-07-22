"""
Shared base for the AI-agent-development team's one-shot JSON tool agents.

The six domain tool agents (``agent_runtime``, ``safety_governance``,
``evaluation_harness``, ``memory_rag``, ``prompt_engineering``,
``mcp_server_connectivity``) previously copy-pasted an identical ``__init__`` +
``run`` pair that differed only in the ``PROMPT`` string and the class name.
Every ``run`` parsed the model output with a bare
``json.loads(str(agent(prompt)).strip())`` — so fenced or prose-wrapped output
raised :class:`json.JSONDecodeError` and crashed the microtask. This base
captures the shared shape once as a thin
:class:`~software_engineering_team.shared.llm_tool_agent_base.LlmToolAgentBase`
specialization (Plan/Json recipe: JSON model resolution, inline invocation,
``extract_json_object`` salvage, plus no-model / call-error fallbacks) so
malformed output and LLM failures degrade to an empty result instead of
raising.

``LlmToolAgentBase`` deliberately imports nothing from ``code_review_agent``;
any coupling there is a lazy, non-load-bearing dependency and does not block
reuse of the shared base from this team.

Like the code-v2 tool-agent bases, the concrete ``agent.py`` keeps a top-level
``from strands import Agent`` so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; the inherited
:meth:`LlmToolAgentBase._agent_factory` resolves ``Agent`` from the concrete
subclass module.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from llm_service import get_strands_model
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase

from ..models import ToolAgentInput, ToolAgentOutput

# Spec context is truncated to this many characters before prompting (the value
# every concrete agent used inline).
MAX_SPEC_CHARS = 5_000


class JsonGeneratorToolAgent(LlmToolAgentBase):
    """Base for one-shot tool agents that return a JSON ``files``/``recommendations``/``summary`` object.

    Subclasses set :attr:`PROMPT` — a ``str.format`` template taking ``microtask``
    and ``spec`` — and nothing else.

    Preconditions:
        Subclasses that call :meth:`run` set a non-empty :attr:`PROMPT`.

    Postconditions:
        Construction (when :attr:`resolve_models` is true) sets ``self.llm`` and
        ``self._model`` via the shared Plan/Json recipe.

    Invariants:
        Instance state is limited to ``_model`` and ``llm`` — so tests that build
        an instance via ``__new__`` and set those attributes behave identically
        to a constructed instance. Plan/Json recipe attrs stay
        ``resolve_models=True``, ``response_format="json"``,
        ``get_strands_model_fn=get_strands_model``, ``use_run_strands_agent=False``,
        ``json_parse_strategy="extract"``. Fallback class attrs stay empty so
        no-model / call-error / empty-parse all degrade to empty
        ``files``/``recommendations``/``summary``.
    """

    # --- LlmToolAgentBase Plan/Json recipe --------------------------------
    resolve_models: bool = True
    response_format: str = "json"
    get_strands_model_fn = get_strands_model
    json_parse_strategy: str = "extract"
    # use_run_strands_agent remains False (inline Agent(prompt) path)

    PROMPT: str = ""

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def _empty_output(
        self,
        *,
        recommendations: Optional[Sequence[str]] = None,
        summary: str = "",
    ) -> ToolAgentOutput:
        """Build the shared empty tool-agent shape (files always ``{}``).

        Preconditions:
            ``recommendations``, when provided, is a sequence of strings;
            ``summary`` is a string.

        Postconditions:
            Returns a :class:`ToolAgentOutput` with ``files={}`` and the given
            recommendations/summary (defaulting recommendations to ``[]``).
        """
        return ToolAgentOutput(
            files={},
            recommendations=list(recommendations) if recommendations else [],
            summary=summary,
        )

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        """Run a single LLM prompt and parse its JSON object into a :class:`ToolAgentOutput`.

        Preconditions:
            ``inp`` exposes ``microtask`` (with ``title``/``description``) and
            ``spec_context``; the subclass sets a non-empty :attr:`PROMPT`.
            ``self._model`` is set (possibly falsy).

        Postconditions:
            Returns a :class:`ToolAgentOutput`. A missing model, an LLM call
            exception, or model output that is not a clean JSON object (fenced,
            prose-wrapped, or empty) each yield empty ``files``/``recommendations``
            and an empty ``summary`` rather than raising.
        """
        no_model = self._fallback_no_model(self._model)
        if no_model is not None:
            return self._empty_output(
                recommendations=no_model.recommendations,
                summary=no_model.summary,
            )

        prompt = self.PROMPT.format(
            microtask=inp.microtask.description or inp.microtask.title,
            spec=inp.spec_context,
        )
        status, result = self._call_with_single_fallback(
            lambda: self._invoke_llm(self._model, prompt),
            log_label=type(self).__name__,
        )
        if status == "error":
            return self._empty_output(
                recommendations=result.recommendations,
                summary=result.summary,
            )

        raw = result
        data = self._parse_llm_json(raw)
        if data is None:
            self._logger.warning(
                "%s: model output did not parse as JSON: %r; returning empty tool-agent output",
                type(self).__name__,
                raw,
            )
            data = {}
        return ToolAgentOutput(
            files=data.get("files") or {},
            recommendations=data.get("recommendations") or [],
            summary=data.get("summary", ""),
        )
