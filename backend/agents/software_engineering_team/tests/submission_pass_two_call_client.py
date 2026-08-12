"""Test double for submission passes after the think-then-format migration."""

from __future__ import annotations

import json
from typing import Any

import pytest

from llm_service.clients.dummy import DummyLLMClient


class SubmissionPassTwoCallClient(DummyLLMClient):
    """Dummy client that records reasoning-pass prompts for submission-pass tests.

    Batch content is sent on call 1 (``complete`` / Agent). JSON formatting uses
    ``complete_json`` on call 2. Subclasses should gate stub logic on
    :meth:`latest_reasoning_prompt` rather than the format-pass prompt.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reasoning_prompts: list[str] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        self.reasoning_prompts.append(prompt)
        return "Structured prose review summary."

    def latest_reasoning_prompt(self) -> str:
        return self.reasoning_prompts[-1] if self.reasoning_prompts else ""


def _backing_client(model: Any) -> Any:
    if hasattr(model, "complete_json"):
        return model
    client = getattr(model, "client", None)
    if client is not None and hasattr(client, "complete_json"):
        return client
    raise TypeError(f"unsupported model for submission-pass test stub: {type(model)!r}")


def wire_run_agent_via_reasoning_for_test_clients(monkeypatch: pytest.MonkeyPatch, runner_mod: Any) -> None:
    """Drive ``run_agent_via_reasoning`` through ``complete`` + ``complete_json`` stubs."""

    def _fake(**kwargs: Any) -> Any:
        model = kwargs["model"]
        reasoning_prompt = kwargs["reasoning_prompt"]
        parse = kwargs["parse"]
        client = _backing_client(model)
        client.complete(reasoning_prompt, objective="submission-pass-test")
        data = client.complete_json("format", objective="submission-pass-test")
        return parse(json.dumps(data))

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _fake)


@pytest.fixture(autouse=True)
def _submission_pass_two_call_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    import code_review_agent.submission_pass_runner as runner_mod

    wire_run_agent_via_reasoning_for_test_clients(monkeypatch, runner_mod)
