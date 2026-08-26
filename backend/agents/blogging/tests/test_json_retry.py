"""Tests for the shared call_json_with_retry() and run_json_gate() helpers."""

from __future__ import annotations

import pytest
from agents.blogging.shared import call_json_with_retry, run_json_gate
from agents.blogging.shared import json_retry as json_retry_module

from llm_service import LLMJsonParseError, LLMRateLimitError, LLMTemporaryError


class _FakeAgent:
    """Records prompts it was called with and returns/raises queued responses.

    ``responses`` is shared (not copied) with the owning factory so that
    successive agents built by ``fresh_agent_per_attempt`` continue draining
    the same queue instead of each restarting from the original responses.
    """

    def __init__(self, responses):
        self.responses = responses
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeAgentFactory:
    """Callable matching AgentFactory; builds a new _FakeAgent per call, tracking builds."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.agents = []

    def __call__(self):
        agent = _FakeAgent(self.responses)
        self.agents.append(agent)
        return agent


class _Wrapper(Exception):
    """A minimal stand-in for a framework wrapper like ``EventLoopException``."""

    def __init__(self, original_exception):
        super().__init__("wrapped")
        self.original_exception = original_exception


def _unwrap(e):
    return e.original_exception if isinstance(e, _Wrapper) else e


def test_invalid_max_attempts_raises_value_error():
    """max_attempts < 1 raises ValueError, not a bypassable assert."""
    factory = _FakeAgentFactory([])
    with pytest.raises(ValueError):
        call_json_with_retry(factory, "prompt", max_attempts=0)


def test_empty_prompt_raises_value_error():
    """An empty prompt raises ValueError, not a bypassable assert."""
    factory = _FakeAgentFactory([])
    with pytest.raises(ValueError):
        call_json_with_retry(factory, "")


def test_success_on_first_attempt():
    """A well-formed JSON response returns immediately, consuming one call."""
    factory = _FakeAgentFactory(['{"ok": true}'])
    data = call_json_with_retry(factory, "prompt")
    assert data == {"ok": True}
    assert len(factory.agents[0].prompts) == 1


def test_success_after_one_json_retry_uses_strict_suffix():
    """A JSON-parse failure retries once with the strict suffix appended."""
    factory = _FakeAgentFactory(["not json", '{"ok": true}'])
    data = call_json_with_retry(factory, "prompt", max_attempts=2)
    assert data == {"ok": True}
    agent = factory.agents[0]
    assert agent.prompts[0] == "prompt"
    assert agent.prompts[1].startswith("prompt")
    assert agent.prompts[1] != "prompt"


def test_exhausted_retries_use_on_exhausted_fallback():
    """When every attempt fails to parse, on_exhausted's dict is returned."""
    factory = _FakeAgentFactory(["not json", "still not json"])
    fallback = {"fallback": True}
    data = call_json_with_retry(factory, "prompt", max_attempts=2, on_exhausted=lambda e: fallback)
    assert data is fallback


def test_exhausted_retries_without_fallback_reraises():
    """Without on_exhausted, the last LLMJsonParseError propagates."""
    factory = _FakeAgentFactory(["not json", "still not json"])
    with pytest.raises(LLMJsonParseError):
        call_json_with_retry(factory, "prompt", max_attempts=2)


def test_transient_error_reraises_immediately_without_consuming_retry():
    """A transient LLM error escapes unwrapped on the first attempt, no retry consumed."""
    err = LLMRateLimitError("rate limited")
    factory = _FakeAgentFactory([err, '{"ok": true}'])
    with pytest.raises(LLMRateLimitError):
        call_json_with_retry(factory, "prompt", max_attempts=3)
    # Only the single failing call happened; the queued success was never consumed.
    assert len(factory.agents[0].prompts) == 1
    assert factory.agents[0].responses == ['{"ok": true}']


def test_unwrap_exception_hook_classifies_and_raises_unwrapped_cause():
    """A wrapped transient error is unwrapped, classified as transient, and raised unwrapped."""
    cause = LLMTemporaryError("temporary")
    factory = _FakeAgentFactory([_Wrapper(cause)])

    with pytest.raises(LLMTemporaryError) as exc_info:
        call_json_with_retry(factory, "prompt", unwrap_exception=_unwrap)
    assert exc_info.value is cause


def test_wrapped_json_parse_error_retries_with_strict_suffix():
    """An LLMJsonParseError raised *inside* the agent invocation and wrapped by the
    framework (e.g. Strands' EventLoopException, via a live model backend that
    validates JSON before returning) still triggers the same retry-with-strict-suffix
    policy as a directly-raised one, rather than skipping straight to
    on_unexpected_error."""
    wrapped = _Wrapper(LLMJsonParseError("bad json", response_preview="x"))
    factory = _FakeAgentFactory([wrapped, '{"ok": true}'])

    data = call_json_with_retry(factory, "prompt", max_attempts=2, unwrap_exception=_unwrap)

    assert data == {"ok": True}
    agent = factory.agents[0]
    assert agent.prompts[0] == "prompt"
    assert agent.prompts[1] != "prompt"


def test_wrapped_json_parse_error_exhausted_uses_on_exhausted():
    """Exhausted wrapped JSON-parse failures use on_exhausted, not on_unexpected_error."""
    factory = _FakeAgentFactory(
        [
            _Wrapper(LLMJsonParseError("bad json", response_preview="x")),
            _Wrapper(LLMJsonParseError("still bad", response_preview="y")),
        ]
    )
    fallback = {"fallback": True}

    data = call_json_with_retry(
        factory,
        "prompt",
        max_attempts=2,
        unwrap_exception=_unwrap,
        on_exhausted=lambda e: fallback,
        on_unexpected_error=lambda e: {"wrong_hook": True},
    )

    assert data is fallback


def test_fresh_agent_per_attempt_builds_a_new_agent_each_attempt():
    """fresh_agent_per_attempt=True calls agent_factory() once per attempt."""
    factory = _FakeAgentFactory(["not json", '{"ok": true}'])
    data = call_json_with_retry(factory, "prompt", max_attempts=2, fresh_agent_per_attempt=True)
    assert data == {"ok": True}
    assert len(factory.agents) == 2
    assert len(factory.agents[0].prompts) == 1
    assert len(factory.agents[1].prompts) == 1


def test_backoff_seconds_called_with_attempt_index_between_json_retries(monkeypatch):
    """backoff_seconds(attempt) is invoked (and its sleep applied) before a JSON retry."""
    sleeps = []
    monkeypatch.setattr(json_retry_module.time, "sleep", lambda s: sleeps.append(s))
    factory = _FakeAgentFactory(["not json", '{"ok": true}'])
    data = call_json_with_retry(
        factory, "prompt", max_attempts=2, backoff_seconds=lambda attempt: attempt + 0.5
    )
    assert data == {"ok": True}
    assert sleeps == [0.5]


def test_on_unexpected_error_fallback_for_non_transient_exception():
    """A non-transient, non-JSON exception is handled by on_unexpected_error."""
    factory = _FakeAgentFactory([RuntimeError("boom")])
    fallback = {"fallback": True}
    data = call_json_with_retry(factory, "prompt", on_unexpected_error=lambda e: fallback)
    assert data is fallback


def test_unexpected_error_without_fallback_reraises():
    """Without on_unexpected_error, the unexpected exception propagates."""
    factory = _FakeAgentFactory([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        call_json_with_retry(factory, "prompt")


def test_agent_factory_failure_uses_on_unexpected_error():
    """A raise from agent_factory() is classified like an invoke-time unexpected error."""

    def boom_factory():
        raise RuntimeError("rejected model config")

    fallback = {"fallback": True}
    data = call_json_with_retry(boom_factory, "prompt", on_unexpected_error=lambda e: fallback)
    assert data is fallback


def test_agent_factory_failure_without_fallback_reraises():
    """Without on_unexpected_error, a factory raise propagates unwrapped."""

    def boom_factory():
        raise RuntimeError("rejected model config")

    with pytest.raises(RuntimeError, match="rejected model config"):
        call_json_with_retry(boom_factory, "prompt")


def test_expected_keys_threaded_through_to_extract_json_from_response(monkeypatch):
    """expected_keys is forwarded to extract_json_from_response as a frozenset."""
    seen = {}

    def fake_extract(text, *, expected_keys=None):
        seen["expected_keys"] = expected_keys
        return {"ok": True}

    monkeypatch.setattr(json_retry_module, "extract_json_from_response", fake_extract)
    factory = _FakeAgentFactory(["irrelevant"])
    call_json_with_retry(factory, "prompt", expected_keys=["required_key"])
    assert seen["expected_keys"] == frozenset({"required_key"})


# ---------------------------------------------------------------------------
# run_json_gate()
# ---------------------------------------------------------------------------


class _FakeStrandsAgent:
    """Drop-in for strands.Agent's shape: ``Agent(model=..., system_prompt=...)(prompt)``.

    Shares ``calls``/``responses`` with the owning factory (see
    ``_FakeAgentFactory`` above) so successive agents built by
    ``fresh_agent_per_attempt`` keep draining the same response queue.
    """

    def __init__(self, model, system_prompt="", *, calls, responses):
        self.model = model
        self.system_prompt = system_prompt
        self.calls = calls
        self.responses = responses

    def __call__(self, prompt):
        self.calls.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeStrandsAgentFactory:
    """Callable matching ``strands.Agent(model, system_prompt=...)``, for patching json_retry's Agent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.agents: list[_FakeStrandsAgent] = []

    def __call__(self, model, system_prompt=""):
        agent = _FakeStrandsAgent(model, system_prompt, calls=[], responses=self.responses)
        self.agents.append(agent)
        return agent


def test_run_json_gate_success_on_first_attempt(monkeypatch):
    """A well-formed JSON response returns immediately, building the Agent from model/system_prompt."""
    factory = _FakeStrandsAgentFactory(['{"ok": true}'])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    data = run_json_gate("a-model", "a system prompt", "prompt")
    assert data == {"ok": True}
    assert factory.agents[0].model == "a-model"
    assert factory.agents[0].system_prompt == "a system prompt"


def test_run_json_gate_exhausted_uses_fallback_builder(monkeypatch):
    """When on_exhausted/on_unexpected_error are omitted, fallback_builder covers both."""
    factory = _FakeStrandsAgentFactory(["not json", "still not json"])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    fallback = {"fallback": True}
    data = run_json_gate(
        "model", "system", "prompt", max_attempts=2, fallback_builder=lambda e: fallback
    )
    assert data is fallback


def test_run_json_gate_unexpected_error_uses_fallback_builder(monkeypatch):
    """A non-transient, non-JSON exception is handled by fallback_builder too."""
    factory = _FakeStrandsAgentFactory([RuntimeError("boom")])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    fallback = {"fallback": True}
    data = run_json_gate("model", "system", "prompt", fallback_builder=lambda e: fallback)
    assert data is fallback


def test_run_json_gate_explicit_hooks_override_fallback_builder_independently(monkeypatch):
    """Explicit on_exhausted/on_unexpected_error win over fallback_builder, and may differ."""
    factory = _FakeStrandsAgentFactory(["not json", "not json either"])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    data = run_json_gate(
        "model",
        "system",
        "prompt",
        max_attempts=2,
        fallback_builder=lambda e: {"should": "not be used"},
        on_exhausted=lambda e: {"exhausted": True},
        on_unexpected_error=lambda e: {"unexpected": True},
    )
    assert data == {"exhausted": True}


def test_run_json_gate_event_loop_exception_unwraps_transient(monkeypatch):
    """The standard EventLoopException unwrap is applied without a caller-supplied hook."""
    from strands.types.exceptions import EventLoopException

    cause = LLMTemporaryError("temporary")
    factory = _FakeStrandsAgentFactory([EventLoopException(cause)])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    with pytest.raises(LLMTemporaryError) as exc_info:
        run_json_gate("model", "system", "prompt")
    assert exc_info.value is cause


def test_run_json_gate_fresh_agent_per_attempt_builds_a_new_agent_each_attempt(monkeypatch):
    """fresh_agent_per_attempt=True is threaded through to call_json_with_retry."""
    factory = _FakeStrandsAgentFactory(["not json", '{"ok": true}'])
    monkeypatch.setattr(json_retry_module, "Agent", factory)
    data = run_json_gate("model", "system", "prompt", max_attempts=2, fresh_agent_per_attempt=True)
    assert data == {"ok": True}
    assert len(factory.agents) == 2


def test_run_json_gate_agent_construction_failure_uses_fallback_builder(monkeypatch):
    """A raise from Agent(...) construction is classified like an invoke-time unexpected error."""

    def boom_ctor(model, system_prompt=""):
        raise RuntimeError("rejected model config")

    monkeypatch.setattr(json_retry_module, "Agent", boom_ctor)
    fallback = {"fallback": True}
    data = run_json_gate("model", "system", "prompt", fallback_builder=lambda e: fallback)
    assert data is fallback
