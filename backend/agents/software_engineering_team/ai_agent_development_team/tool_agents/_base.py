"""
Shared base for the AI-agent-development team's one-shot JSON tool agents.

The six domain tool agents (``agent_runtime``, ``safety_governance``,
``evaluation_harness``, ``memory_rag``, ``prompt_engineering``,
``mcp_server_connectivity``) previously copy-pasted an identical ``__init__`` +
``run`` pair that differed only in the ``PROMPT`` string and the class name.
Every ``run`` parsed the model output with a bare
``json.loads(str(agent(prompt)).strip())`` — so fenced or prose-wrapped output
raised :class:`json.JSONDecodeError` and crashed the microtask. This base
captures the shared shape once and parses via the lightweight, stdlib-only
:func:`shared_llm_recovery.extract_json_object` salvage engine, so malformed
output degrades to an empty result instead of raising. (It deliberately does not
reuse ``shared.tool_agent_base.lenient_json_object``, whose module pulls in
``code_review_agent`` — a dependency not on the AI-agent-development team's path.)

Like the code-v2 tool-agent bases, the concrete ``agent.py`` keeps a top-level
``from strands import Agent`` so tests can
``monkeypatch.setattr(<agent_module>, "Agent", ...)``; the base resolves
``Agent`` from the concrete subclass module via :meth:`_agent_factory`.
"""

from __future__ import annotations

import importlib
import logging

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model
from shared_llm_recovery import extract_json_object

from ..models import ToolAgentInput, ToolAgentOutput

# Spec context is truncated to this many characters before prompting (the value
# every concrete agent used inline).
MAX_SPEC_CHARS = 5_000


class JsonGeneratorToolAgent:
    """Base for one-shot tool agents that return a JSON ``files``/``recommendations``/``summary`` object.

    Subclasses set :attr:`PROMPT` — a ``str.format`` template taking ``microtask``
    and ``spec`` — and nothing else.

    Invariants: instance state is limited to ``_model``, so tests that build an
    instance via ``__new__`` and set ``_model`` behave identically to a
    constructed instance.
    """

    PROMPT: str = ""

    def __init__(self, llm=None) -> None:
        self._model = resolve_strands_model(llm, get_strands_model_fn=get_strands_model)

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(type(self).__module__)

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass module (monkeypatchable)."""
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        """Run a single LLM prompt and parse its JSON object into a :class:`ToolAgentOutput`.

        Preconditions: ``inp`` exposes ``microtask`` (with ``title``/``description``)
        and ``spec_context``; the subclass sets a non-empty :attr:`PROMPT`.
        Postconditions: returns a :class:`ToolAgentOutput`; model output that is
        not a clean JSON object (fenced, prose-wrapped, or empty) yields empty
        ``files``/``recommendations`` and an empty ``summary`` rather than
        raising.
        """
        agent = self._agent_factory()(model=self._model)
        prompt = self.PROMPT.format(
            microtask=inp.microtask.description or inp.microtask.title,
            spec=inp.spec_context[:MAX_SPEC_CHARS],
        )
        raw = str(agent(prompt)).strip()
        data = extract_json_object(raw)
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
