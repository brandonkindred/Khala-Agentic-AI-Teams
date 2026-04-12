"""Shared test fixtures for the software engineering team."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from llm_service import DummyLLMClient


class ConfigurableLLM(DummyLLMClient):
    """DummyLLMClient subclass that allows MagicMock-style ``complete_json.return_value`` configuration.

    Usage::

        llm = ConfigurableLLM()
        llm.complete_json.return_value = {"code": "...", "files": {...}}
        agent = BackendExpertAgent(llm_client=llm)

    The ``complete_json`` attribute is a MagicMock whose ``side_effect``
    delegates to the parent ``DummyLLMClient.complete_json`` when no
    ``return_value`` has been set.
    """

    def __init__(self) -> None:
        super().__init__()
        self._real_complete_json = super().complete_json
        self._mock_complete_json = MagicMock(side_effect=self._dispatch)
        self._mock_get_max_context_tokens = MagicMock(return_value=16384)

    def _dispatch(self, prompt: str, **kwargs: Any) -> Any:
        return self._real_complete_json(prompt, **kwargs)

    @property  # type: ignore[override]
    def complete_json(self) -> MagicMock:  # type: ignore[override]
        return self._mock_complete_json

    @complete_json.setter
    def complete_json(self, value: Any) -> None:
        self._mock_complete_json = value

    @property  # type: ignore[override]
    def get_max_context_tokens(self) -> MagicMock:  # type: ignore[override]
        return self._mock_get_max_context_tokens

    @get_max_context_tokens.setter
    def get_max_context_tokens(self, value: Any) -> None:
        self._mock_get_max_context_tokens = value
