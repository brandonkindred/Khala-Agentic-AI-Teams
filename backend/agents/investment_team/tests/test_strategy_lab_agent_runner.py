"""Tests for the shared Agent construct/invoke/(parse) scaffold.

``invoke_json_agent`` / ``invoke_text_agent`` are exercised with a fake
``strands.Agent`` and a fake ``get_strands_model`` patched onto
``_agent_runner`` itself (matching the pattern used at every migrated call
site's own tests) while the real ``invoke_agent`` envelope — retries,
backoff, timeout, fail-closed classification — runs unmocked underneath.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from investment_team.strategy_lab.agents import _agent_runner as agent_runner
from investment_team.strategy_lab.agents import _llm_envelope as env
from investment_team.strategy_lab.agents._agent_runner import invoke_json_agent, invoke_text_agent
from investment_team.strategy_lab.exceptions import StrategyLabLLMError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the envelope's real backoff sleeps so retry tests run instantly."""
    monkeypatch.setattr(env.time, "sleep", lambda _s: None)


class _Stub:
    """Callable that scripts a sequence of raises/returns per invocation."""

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, prompt: str) -> Any:
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _patch_agent(monkeypatch: pytest.MonkeyPatch, responder: Any) -> List[Dict[str, Any]]:
    """Patch ``_agent_runner.Agent`` with a factory that builds a fresh fake
    instance per call, delegating ``__call__`` to ``responder``. Returns the
    list of constructor-kwargs dicts — one entry appended per construction,
    so its length is the construction count and its contents are what each
    ``Agent(...)`` call received.
    """
    constructions: List[Dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            constructions.append(kwargs)

        def __call__(self, prompt: str) -> Any:
            return responder(prompt)

    monkeypatch.setattr(agent_runner, "Agent", _FakeAgent)
    return constructions


def _patch_get_strands_model(monkeypatch: pytest.MonkeyPatch) -> Tuple[List[Dict[str, Any]], object]:
    """Patch ``_agent_runner.get_strands_model`` to record each call's
    ``agent_key``/``response_format`` and return a fixed sentinel "model"
    object. Returns ``(calls, sentinel)``.
    """
    calls: List[Dict[str, Any]] = []
    sentinel = object()

    def _fake(agent_key: str, **kwargs: Any) -> object:
        calls.append({"agent_key": agent_key, **kwargs})
        return sentinel

    monkeypatch.setattr(agent_runner, "get_strands_model", _fake)
    return calls, sentinel


# ---------------------------------------------------------------------------
# Fresh-Agent-per-call invariant
# ---------------------------------------------------------------------------


def test_invoke_json_agent_builds_fresh_agent_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    constructions = _patch_agent(monkeypatch, lambda _p: '{"ok": true}')
    _patch_get_strands_model(monkeypatch)

    invoke_json_agent("p1", agent_key="k", phase="ph", system_prompt="sys")
    invoke_json_agent("p2", agent_key="k", phase="ph", system_prompt="sys")

    assert len(constructions) == 2


def test_invoke_text_agent_builds_fresh_agent_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    constructions = _patch_agent(monkeypatch, lambda _p: "raw text")
    _patch_get_strands_model(monkeypatch)

    invoke_text_agent("p1", agent_key="k", phase="ph", system_prompt="sys")
    invoke_text_agent("p2", agent_key="k", phase="ph", system_prompt="sys")

    assert len(constructions) == 2


# ---------------------------------------------------------------------------
# Parameter passthrough
# ---------------------------------------------------------------------------


def test_invoke_json_agent_passes_system_prompt_and_empty_tools_to_agent_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructions = _patch_agent(monkeypatch, lambda _p: "{}")
    _model_calls, sentinel = _patch_get_strands_model(monkeypatch)

    invoke_json_agent("prompt", agent_key="strategy_ideation", phase="ph", system_prompt="SYS")

    assert constructions[0]["system_prompt"] == "SYS"
    assert constructions[0]["tools"] == []
    assert constructions[0]["model"] is sentinel


def test_invoke_json_agent_defaults_response_format_to_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_agent(monkeypatch, lambda _p: "{}")
    model_calls, _sentinel = _patch_get_strands_model(monkeypatch)

    invoke_json_agent("prompt", agent_key="strategy_ideation", phase="ph", system_prompt="sys")

    assert model_calls == [{"agent_key": "strategy_ideation", "response_format": "json"}]


def test_invoke_text_agent_defaults_response_format_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_agent(monkeypatch, lambda _p: "raw")
    model_calls, _sentinel = _patch_get_strands_model(monkeypatch)

    invoke_text_agent(
        "prompt", agent_key="strategy_code_synthesis", phase="ph", system_prompt="sys"
    )

    assert model_calls == [{"agent_key": "strategy_code_synthesis", "response_format": "text"}]


def test_invoke_text_agent_response_format_override_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_agent(monkeypatch, lambda _p: "raw")
    model_calls, _sentinel = _patch_get_strands_model(monkeypatch)

    invoke_text_agent(
        "prompt", agent_key="k", phase="ph", system_prompt="sys", response_format="json"
    )

    assert model_calls[0]["response_format"] == "json"


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_invoke_json_agent_returns_parsed_dict_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_agent(monkeypatch, lambda _p: '{"revised_narrative": "hello", "n": 3}')
    _patch_get_strands_model(monkeypatch)

    result = invoke_json_agent("prompt", agent_key="k", phase="ph", system_prompt="sys")

    assert result == {"revised_narrative": "hello", "n": 3}


def test_invoke_text_agent_returns_raw_text_unparsed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_code = "def f():\n    return {unbalanced\n"
    _patch_agent(monkeypatch, lambda _p: raw_code)
    _patch_get_strands_model(monkeypatch)

    result = invoke_text_agent("prompt", agent_key="k", phase="ph", system_prompt="sys")

    assert result == raw_code


# ---------------------------------------------------------------------------
# Failure propagation — never swallowed
# ---------------------------------------------------------------------------


def test_invoke_json_agent_propagates_value_error_on_unparseable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent(monkeypatch, lambda _p: "not json at all")
    _patch_get_strands_model(monkeypatch)

    with pytest.raises(ValueError):
        invoke_json_agent("prompt", agent_key="k", phase="ph", system_prompt="sys")


def test_invoke_json_agent_propagates_strategy_lab_llm_error_after_envelope_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(httpx.ConnectError("down"))
    _patch_agent(monkeypatch, stub)
    _patch_get_strands_model(monkeypatch)

    with pytest.raises(StrategyLabLLMError):
        invoke_json_agent("prompt", agent_key="k", phase="ph", system_prompt="sys", max_attempts=1)
    assert stub.calls == 1


def test_invoke_text_agent_propagates_strategy_lab_llm_error_after_envelope_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(httpx.ConnectError("down"))
    _patch_agent(monkeypatch, stub)
    _patch_get_strands_model(monkeypatch)

    with pytest.raises(StrategyLabLLMError):
        invoke_text_agent("prompt", agent_key="k", phase="ph", system_prompt="sys", max_attempts=1)
    assert stub.calls == 1


def test_invoke_json_agent_max_attempts_forwarded_to_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(httpx.ConnectError("1"), httpx.ConnectError("2"), httpx.ConnectError("3"))
    _patch_agent(monkeypatch, stub)
    _patch_get_strands_model(monkeypatch)

    with pytest.raises(StrategyLabLLMError):
        invoke_json_agent("prompt", agent_key="k", phase="ph", system_prompt="sys", max_attempts=3)
    assert stub.calls == 3


# ---------------------------------------------------------------------------
# Diagnostic labels — agent_key / phase / logger
# ---------------------------------------------------------------------------


def test_invoke_json_agent_forwards_agent_key_and_phase_into_failure_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_agent(monkeypatch, _Stub(httpx.ConnectError("down")))
    _patch_get_strands_model(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StrategyLabLLMError):
            invoke_json_agent(
                "prompt",
                agent_key="strategy_custom_key",
                phase="custom_phase",
                system_prompt="sys",
                max_attempts=1,
            )

    text = caplog.text
    assert "agent=strategy_custom_key" in text
    assert "phase=custom_phase" in text


def test_invoke_json_agent_forwards_caller_logger_not_module_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_agent(monkeypatch, _Stub(httpx.ConnectError("down")))
    _patch_get_strands_model(monkeypatch)
    custom_logger = logging.getLogger("test.custom.agent_runner_logger")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StrategyLabLLMError):
            invoke_json_agent(
                "prompt",
                agent_key="k",
                phase="ph",
                system_prompt="sys",
                max_attempts=1,
                logger=custom_logger,
            )

    failure_records = [r for r in caplog.records if "strategy_lab LLM call failed" in r.message]
    assert failure_records
    assert all(r.name == "test.custom.agent_runner_logger" for r in failure_records)


def test_invoke_json_agent_defaults_to_envelope_module_logger_when_none_passed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_agent(monkeypatch, _Stub(httpx.ConnectError("down")))
    _patch_get_strands_model(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StrategyLabLLMError):
            invoke_json_agent(
                "prompt", agent_key="k", phase="ph", system_prompt="sys", max_attempts=1
            )

    failure_records = [r for r in caplog.records if "strategy_lab LLM call failed" in r.message]
    assert failure_records
    # invoke_json_agent must not substitute a logger of its own — an omitted
    # logger falls through to invoke_agent's own default (_llm_envelope's
    # module logger), not to some _agent_runner-local default.
    assert all(
        r.name == "investment_team.strategy_lab.agents._llm_envelope" for r in failure_records
    )
