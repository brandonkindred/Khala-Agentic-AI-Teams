"""Shared fixtures/helpers for job matching tests."""

from __future__ import annotations

from typing import Any, Callable, Dict

import pytest


class ScriptedLLM:
    """Minimal stand-in for ``llm_service.LLMClient``.

    ``handler(prompt, system_prompt)`` returns the dict that ``complete_json``
    should yield, letting each test script deterministic model output.
    """

    def __init__(self, handler: Callable[[str, str], Dict[str, Any]]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str]] = []

    def complete_json(
        self, prompt: str, *, temperature: float = 0.0, system_prompt: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        self.calls.append((prompt, system_prompt or ""))
        return self._handler(prompt, system_prompt or "")

    def complete(self, prompt: str, **kwargs: Any) -> str:  # pragma: no cover - unused
        return ""


@pytest.fixture
def scripted_llm() -> Callable[[Callable[[str, str], Dict[str, Any]]], ScriptedLLM]:
    def _make(handler: Callable[[str, str], Dict[str, Any]]) -> ScriptedLLM:
        return ScriptedLLM(handler)

    return _make
