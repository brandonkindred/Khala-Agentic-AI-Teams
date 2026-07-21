"""Dependency-light skeleton shared by the LLM tool-agent base classes.

Holds just the ``_agent_factory`` monkeypatch resolver and a bare constructor.
Deliberately imports nothing from ``code_review_agent`` (or any team-specific
model/parsing module) so it can be depended on from any team without pulling
in the code-review engine. Model resolution, LLM invocation, JSON parsing,
and fallback logic are intentionally out of scope for this skeleton.

Preconditions:
    None beyond standard Python import semantics.

Postconditions:
    Importing this module never triggers an import of ``code_review_agent``
    (verified by ``tests/test_llm_tool_agent_base.py``).

Invariants:
    ``LlmToolAgentBase`` instance state is limited to ``self.llm``.
"""

from __future__ import annotations

import importlib


class LlmToolAgentBase:
    """Bare constructor plus the shared ``_agent_factory`` resolver.

    Preconditions:
        ``llm``, if provided, is whatever the concrete subclass's model
        resolution expects (this base does not inspect it).

    Postconditions:
        ``self.llm`` holds the value passed to the constructor (``None`` by
        default).

    Invariants:
        No other instance state is introduced by this class.
    """

    def __init__(self, llm=None) -> None:
        self.llm = llm

    def _agent_factory(self):
        """Resolve ``Agent`` from the concrete subclass's defining module.

        This is what lets ``monkeypatch.setattr(<agent_module>, "Agent", ...)``
        intercept LLM calls made from this shared base.

        Preconditions:
            ``type(self).__module__`` names a module that defines an ``Agent``
            symbol (patched in tests, or the real Strands ``Agent`` in
            production).

        Postconditions:
            Returns the ``Agent`` symbol from that module.
        """
        mod = importlib.import_module(type(self).__module__)
        return getattr(mod, "Agent")
