"""Reusable test double for faking strands ``Agent`` construction.

Records the ``system_prompt`` / ``tools`` kwargs a caller constructed the model
with, and echoes a fixed reply — the standard way this codebase avoids a real
network/LLM call when testing manifest-first persona binding (see
``system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md``).

Lives under ``shared`` so both platform (``agent_sandbox_runtime``) and
domain-app (``agent_team_studio.agentic_team_provisioning``) test suites can
import one fake without either importing the other — the same reasoning that
keeps production ``shared.agent_invoke`` importable from both sides.
"""

from __future__ import annotations

import types
from typing import Any

import pytest


class FakeAgentResult:
    """Mimics a strands ``AgentResult``: text is obtained via ``str(result)``."""

    def __init__(self, text: str) -> None:
        self.message = {"role": "assistant", "content": [{"text": text}]}
        self._text = text

    def __str__(self) -> str:
        return self._text


class FakeStrandsAgent:
    """Records the system prompt + tools it was built with; echoes a fixed reply."""

    last_system_prompt: str | None = None
    last_tools: Any = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_system_prompt = kwargs.get("system_prompt")
        type(self).last_tools = kwargs.get("tools")

    def __call__(self, message: str) -> FakeAgentResult:
        return FakeAgentResult("ok")


def patch_strands_agent(monkeypatch: pytest.MonkeyPatch, target: str | types.ModuleType) -> type[FakeStrandsAgent]:
    """Monkeypatch ``target``'s ``StrandsAgent`` attribute with the recording fake.

    Preconditions:
        * ``target`` is either the ``agent_builder`` module object itself (callers
          that already import it) or a dotted string ending in ``.StrandsAgent``
          (callers — e.g. platform test suites — that must not statically import
          the module owning the attribute; ``monkeypatch.setattr`` resolves a
          string target via dynamic import, so the caller's own import graph
          never gains a static dependency on it).
    Postconditions:
        * ``FakeStrandsAgent``'s recorded ``last_system_prompt`` / ``last_tools``
          are reset to ``None``, the patch is applied, and the class is returned
          for the caller to assert against.
    """
    FakeStrandsAgent.last_system_prompt = None
    FakeStrandsAgent.last_tools = None
    if isinstance(target, str):
        monkeypatch.setattr(target, FakeStrandsAgent)
    else:
        monkeypatch.setattr(target, "StrandsAgent", FakeStrandsAgent)
    return FakeStrandsAgent
