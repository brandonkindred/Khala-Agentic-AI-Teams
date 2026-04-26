"""Shared test fixtures for the software engineering team."""

from __future__ import annotations

import os
from typing import Any

import pytest

# Mirror the env defaults from ``backend/conftest.py`` (not auto-discovered
# here because this team overrides pytest's rootdir).  The placeholder
# JOB_SERVICE_URL lets module-level ``JobServiceClient(team=…)`` construction
# succeed; real HTTP calls will fail loudly.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
os.environ.setdefault("JOB_SERVICE_URL", "http://127.0.0.1:1")

# Re-export the in-memory FakeJobServiceClient + ``fake_job_client`` fixture so
# unit tests in this team can use them.  The SE team's ``pyproject.toml``
# overrides pytest's rootdir, which means ``backend/conftest.py`` is not
# auto-discovered here, so we pull the fixture in explicitly (and re-register
# the ``integration`` marker / default-skip behaviour for the same reason).
from job_service_client_fake import fake_job_client  # noqa: F401, E402
from llm_service import DummyLLMClient  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires real Postgres + the central job service. "
        "Skipped unless invoked with `-m integration`.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selected = config.getoption("-m", default="") or ""
    if "integration" in selected:
        return
    skip = pytest.mark.skip(reason="integration test; run with `pytest -m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


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
        assert self.call_count == 0, (
            f"Expected not to have been called, but was called {self.call_count} time(s)"
        )

    def assert_called_once(self):
        assert self.call_count == 1, (
            f"Expected to be called once, but was called {self.call_count} time(s)"
        )


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
