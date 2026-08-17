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


class _StubReasoningAgent:
    """Minimal stand-in so ``on_reasoning_agent`` can serialize ``messages``.

    Postconditions: ``messages`` is a two-turn user/assistant conversation
        whose user text is the reasoning prompt that drove this stub call.
    """

    def __init__(self, prompt: str) -> None:
        self.messages = [
            {"role": "user", "content": [{"text": prompt}]},
            {
                "role": "assistant",
                "content": [{"text": "Structured prose review summary."}],
            },
        ]


def wire_run_agent_via_reasoning_for_test_clients(
    monkeypatch: pytest.MonkeyPatch, runner_mod: Any
) -> None:
    """Drive ``run_agent_via_reasoning`` through ``complete`` + ``complete_json`` stubs.

    Honors ``on_reasoning_agent`` and ``on_formatting`` the same way the real
    helper does after each pass, so transcript-recording call sites still see
    a ``messages`` conversation and a formatting entry when this autouse stub
    is active (pytest-xdist loads this plugin session-wide from sibling test
    modules).
    """

    def _fake(**kwargs: Any) -> Any:
        model = kwargs["model"]
        reasoning_prompt = kwargs["reasoning_prompt"]
        parse = kwargs["parse"]
        client = _backing_client(model)
        client.complete(reasoning_prompt, objective="submission-pass-test")
        on_reasoning_agent = kwargs.get("on_reasoning_agent")
        if on_reasoning_agent is not None:
            on_reasoning_agent(_StubReasoningAgent(reasoning_prompt))
        data = client.complete_json("format", objective="submission-pass-test")
        raw = json.dumps(data)
        on_formatting = kwargs.get("on_formatting")
        if on_formatting is not None:
            on_formatting("format", raw)
        return parse(raw)

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _fake)


def wire_run_agent_via_reasoning_with_raw(
    monkeypatch: pytest.MonkeyPatch, runner_mod: Any, raw: str
) -> None:
    """Drive ``run_agent_via_reasoning`` so the pass's ``parse`` callback sees ``raw`` verbatim.

    The default two-call wiring ``json.dumps``-es the formatting reply, which would
    escape away any markdown fence or prose prefix before ``parse`` runs. This
    helper hands the pass's own ``parse`` callback a raw string unchanged, so a
    test can exercise the canonical recovery ladder on genuinely fenced /
    prose-wrapped / trailing-comma output. Call it inside the test body to
    override the autouse wiring for that test.

    Preconditions:
        ``runner_mod`` exposes a ``run_agent_via_reasoning`` attribute (the
        submission-pass runner module). ``raw`` is the reply text to route
        through ``parse``.

    Postconditions:
        ``runner_mod.run_agent_via_reasoning`` is monkeypatched so every call
        returns ``parse(raw)``; the patch is reverted on fixture teardown.
    """

    def _fake(**kwargs: Any) -> Any:
        return kwargs["parse"](raw)

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _fake)


@pytest.fixture(autouse=True)
def _submission_pass_two_call_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    import code_review_agent.submission_pass_runner as runner_mod

    wire_run_agent_via_reasoning_for_test_clients(monkeypatch, runner_mod)
