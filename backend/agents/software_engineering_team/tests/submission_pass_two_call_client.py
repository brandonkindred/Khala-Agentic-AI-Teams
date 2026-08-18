"""Test double for submission passes after the think-then-format migration.

This module is imported by name (``tests.submission_pass_two_call_client``)
rather than registered as a pytest plugin -- see the comment on
``wire_run_agent_via_reasoning_for_test_clients`` below for why a
module-level ``pytest_plugins = [...]`` here would leak this stub into
unrelated test files under pytest-xdist.
"""

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


_MUTATION_FINDING: dict[str, Any] = {
    "severity": "high",
    "category": "side-effects",
    "file_path": "app/main.py",
    "description": (
        "bar() now returns 2 instead of the shown before-image's 1; "
        "app/caller.py still expects the old contract"
    ),
    "suggestion": "update app/caller.py for the new return value",
}


def mutation_finding_payload() -> dict[str, Any]:
    """The synthetic mutation-vs-replaced-code side-effect finding shared by
    the standalone and merged pass's "fires with before-image" tests.

    Preconditions: none.

    Postconditions:
        Returns a fresh copy of :data:`_MUTATION_FINDING` -- never the same
        dict instance -- so no caller can mutate shared state across tests.
    """
    return dict(_MUTATION_FINDING)


class MutationFindingClient(SubmissionPassTwoCallClient):
    """Scripted client for a pass's mutation-vs-replaced-code sub-check.

    Shared by ``test_side_effect_impact_pass.py`` (the standalone Temporal
    pass) and ``test_merged_architecture_side_effect_pass.py`` (the in-process
    merged pass) so the guard condition and the synthetic finding payload used
    by each pass's "fires with before-image / silent without" test pair stay
    identical -- only each pass's own JSON response envelope shape differs,
    which the caller supplies.

    Preconditions:
        ``anchor`` is the calling pass's non-empty prompt anchor.
        ``response_with_finding``/``response_without_finding`` are that
        pass's own JSON envelope shapes (e.g. ``{"findings": [...]}`` for the
        standalone pass, or ``{"architecture_findings": [...],
        "side_effect_findings": [...]}`` for the merged pass).

    Postconditions:
        ``complete_json`` returns ``response_with_finding`` when the latest
        reasoning prompt contains both ``anchor`` and "Replaced (pre-change)
        content" (i.e. the pass actually showed the model a before-image for
        this call), else returns ``response_without_finding``.
    """

    def __init__(
        self,
        *,
        anchor: str,
        response_with_finding: dict[str, Any],
        response_without_finding: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._anchor = anchor
        self._response_with_finding = response_with_finding
        self._response_without_finding = response_without_finding

    def complete_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        reasoning_prompt = self.latest_reasoning_prompt()
        if self._anchor in reasoning_prompt and "Replaced (pre-change) content" in reasoning_prompt:
            return self._response_with_finding
        return self._response_without_finding


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
    a ``messages`` conversation and a formatting entry when this stub is
    active. Callers apply it via their own module-local
    ``@pytest.fixture(autouse=True)`` (see this module's trailing comment) so
    it wires every test in that module without leaking into sibling files.
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
    override a module's local wiring fixture (see
    ``wire_run_agent_via_reasoning_for_test_clients``) for that one test.

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


# Deliberately NOT a ``pytest.fixture`` here, and this module is deliberately
# never registered via a module-level ``pytest_plugins = [...]`` (as it once
# was): under pytest-xdist, each worker's own pytest session collects the
# *entire* test tree before running its assigned subset, so a plugin
# registered by any one file's ``pytest_plugins`` loads for that worker's
# whole session -- its autouse fixtures then apply to every test the worker
# runs, not just tests in the file that requested it. That silently swapped
# this two-call stub in for ``run_agent_via_reasoning`` in unrelated test
# modules that never imported this file, breaking any assertion elsewhere
# that inspects a real reasoning/formatting prompt (e.g. the merged
# architecture/side-effect tail pass's rendered prompt). Callers that want
# this wiring applied to every test in their module must define their own
# local ``@pytest.fixture(autouse=True)`` that calls
# ``wire_run_agent_via_reasoning_for_test_clients`` directly -- a fixture
# defined in a test module (as opposed to a conftest.py or a registered
# plugin) is scoped to that module only, so it cannot leak into sibling
# files the way the removed plugin registration did.
