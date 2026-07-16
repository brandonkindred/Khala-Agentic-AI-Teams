"""
Shared test fixtures and helpers for blogging agent tests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any

import pytest
from strands.models.model import Model
from strands.types.content import Message as StrandsMessage
from strands.types.content import SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

# Job-store helpers captured by reference inside ``api/main`` at import time. The
# ``patched_client`` fixture rebinds each to the fake-backed implementation so
# every endpoint hits the in-memory store.
_BLOG_JOB_HELPERS = (
    "create_blog_job",
    "delete_blog_job",
    "get_blog_job",
    "list_blog_jobs",
    "update_blog_job",
    "start_blog_job",
    "complete_blog_job",
    "fail_blog_job",
    "approve_blog_job",
    "unapprove_blog_job",
    "submit_title_selection",
    "submit_title_ratings",
    "submit_story_user_message",
    "skip_current_story_gap",
    "submit_blog_answers",
    "submit_draft_feedback",
    "is_waiting_for_draft_feedback",
)


@pytest.fixture(autouse=True)
def _patched_blog_client(monkeypatch, fake_job_client) -> Any:
    """Back ``shared.blog_job_store`` with the in-memory fake for every test in this package."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture
def patched_client(monkeypatch, fake_job_client) -> Any:
    """Back the blogging API with the in-memory fake job store.

    Replaces the ``blog_job_store`` module client and rebinds the job-store helper
    references captured inside ``api/main`` at import time, so every endpoint hits
    the fake. Imports the shared app module lazily so test modules that never use
    this fixture do not pay the ``api/main`` import cost.
    """
    from _api_test_utils import api_main
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    for name in _BLOG_JOB_HELPERS:
        helper = getattr(bjs, name, None)
        if helper is not None:
            monkeypatch.setattr(api_main, name, helper)
    return fake_job_client


@pytest.fixture
def client(patched_client) -> Any:
    """A ``TestClient`` for the blogging app, backed by the fake job store."""
    from _api_test_utils import app
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def patched_blog_job_store_client(monkeypatch, fake_job_client) -> Any:
    """Back ``shared.blog_job_store._client`` (and its ``blogging.shared`` alias, if loaded)."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    try:
        from blogging.shared import blog_job_store as bjs_alt

        monkeypatch.setattr(bjs_alt, "_client", lambda *a, **kw: fake_job_client)
    except ImportError:
        pass
    return fake_job_client


class SequencedMockModel(Model):
    """A Strands-compatible model that returns pre-configured text responses in order.

    Each call to ``stream()`` pops the next response from the sequence. If the
    sequence is exhausted, the last response is repeated.

    Usage::

        model = SequencedMockModel([
            "not valid json",                                    # first call fails JSON parse
            '{"status": "PASS", "violations": [], ...}',         # second call succeeds
        ])
        agent = BlogComplianceAgent(llm_client=model)
    """

    def __init__(self, responses: list[str | dict | Exception]) -> None:
        self._responses = list(responses)
        self._call_index = 0
        self._model_config: dict[str, Any] = {}
        self.call_count = 0

    def update_config(self, **model_config: Any) -> None:
        self._model_config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._model_config)

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        self.call_count += 1
        idx = min(self._call_index, len(self._responses) - 1)
        self._call_index += 1
        response = self._responses[idx]

        if isinstance(response, Exception):
            raise response

        if isinstance(response, dict):
            text = json.dumps(response)
        else:
            text = str(response)

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class CallTrackingMockModel(Model):
    """A Strands-compatible model that tracks calls and delegates to configurable handlers.

    Supports separate ``complete_json``-style and ``complete``-style response handlers
    so tests can verify call counts and inspect prompts.

    Usage::

        model = CallTrackingMockModel()
        model.json_handler = lambda prompt, sp: {"plan": "..."}
        model.text_handler = lambda prompt, sp: "# Revised\\n..."
    """

    def __init__(self) -> None:
        self._model_config: dict[str, Any] = {}
        self.call_count = 0
        self.prompts: list[str] = []
        self.system_prompts: list[str | None] = []
        # Default handlers return empty responses
        self.json_handler: Any = lambda prompt, system_prompt: {"output": "mock"}
        self.text_handler: Any = lambda prompt, system_prompt: "mock response"

    def update_config(self, **model_config: Any) -> None:
        self._model_config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._model_config)

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        self.call_count += 1
        # Extract user text
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                for block in msg.get("content", []):
                    if isinstance(block, dict) and "text" in block:
                        user_text = block["text"]
                        break
                    elif isinstance(block, str):
                        user_text = block
                        break
                break
        self.prompts.append(user_text)
        self.system_prompts.append(system_prompt)

        # Use json_handler by default; result is JSON-serialized
        try:
            result = self.json_handler(user_text, system_prompt)
        except Exception:
            result = self.text_handler(user_text, system_prompt)

        if isinstance(result, dict):
            text = json.dumps(result)
        else:
            text = str(result)

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}
