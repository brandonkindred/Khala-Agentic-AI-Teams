"""Shared test fixtures for the software engineering team."""

from __future__ import annotations

from typing import Any

from llm_service import DummyLLMClient


class _TrackingMock:
    """Lightweight mock that tracks calls and supports return_value / side_effect."""

    def __init__(self, fallback):
        self._fallback = fallback
        self._return_value = _SENTINEL
        self._side_effect = _SENTINEL
        self.call_count = 0
        self.call_args = None
        self.call_args_list = []

    @property
    def return_value(self):
        return self._return_value

    @return_value.setter
    def return_value(self, value):
        self._return_value = value

    @property
    def side_effect(self):
        return self._side_effect

    @side_effect.setter
    def side_effect(self, value):
        self._side_effect = value

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.call_args = (args, kwargs)
        self.call_args_list.append((args, kwargs))
        if self._side_effect is not _SENTINEL:
            if isinstance(self._side_effect, list):
                if self._side_effect:
                    item = self._side_effect.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
            elif callable(self._side_effect):
                return self._side_effect(*args, **kwargs)
            elif isinstance(self._side_effect, Exception):
                raise self._side_effect
        if self._return_value is not _SENTINEL:
            return self._return_value
        return self._fallback(*args, **kwargs)

    def assert_called(self):
        assert self.call_count > 0, "Expected to have been called"

    def assert_not_called(self):
        assert self.call_count == 0, f"Expected not to have been called, but was called {self.call_count} time(s)"

    def assert_called_once(self):
        assert self.call_count == 1, f"Expected to be called once, but was called {self.call_count} time(s)"


_SENTINEL = object()


class ConfigurableLLM(DummyLLMClient):
    """DummyLLMClient subclass with MagicMock-style return_value support.

    Usage::

        llm = ConfigurableLLM()
        llm.complete_json_mock.return_value = {"code": "...", "files": {...}}
        agent = BackendExpertAgent(llm_client=llm)
        # ...
        assert llm.complete_json_mock.call_count == 1
    """

    def __init__(self) -> None:
        super().__init__()
        self.complete_json_mock = _TrackingMock(super().complete_json)
        self._max_context_tokens = 16384

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return self.complete_json_mock(prompt, **kwargs)

    def get_max_context_tokens(self) -> int:
        return self._max_context_tokens
