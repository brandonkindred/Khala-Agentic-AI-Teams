"""Unit tests for the generic ``run_json_with_parse_retry`` and
``run_single_shot_agent`` drivers.

These tests exercise the drivers in isolation — no dependency on
``design.py``/``refinement.py``/the six migrated call sites — by
monkeypatching the module-level ``Agent``/``get_strands_model`` symbols the
same way the sibling call-site tests do (see
``test_strategy_lab_refinement_parse_retry.py``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import _agent_runner as mod
from investment_team.strategy_lab.agents._agent_runner import (
    run_json_with_parse_retry,
    run_single_shot_agent,
)
from investment_team.strategy_lab.exceptions import StrategyLabLLMError

_GOOD = '{"a": 1}'
_GOOD_PARSED: Dict[str, Any] = {"a": 1}


class _ScriptedAgent:
    """Strands ``Agent`` replacement returning a scripted payload per call."""

    def __init__(self, payloads: List[str]) -> None:
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


def _on_parse_error(base_prompt: str, exc: ValueError) -> str:
    return f"CORRECTED[{exc}]: {base_prompt}"


def _on_validation_error(base_prompt: str, exc: Exception) -> str:
    return f"VALIDATION-CORRECTED[{exc}]: {base_prompt}"


def _patch_agent(monkeypatch: pytest.MonkeyPatch, agent: Any) -> None:
    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)


def test_returns_parsed_on_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert result == _GOOD_PARSED
    assert agent.calls == 1


def test_parse_error_then_success_retries_and_calls_on_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_prompts: List[str] = []

    class _RecordingAgent:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt: str) -> str:
            seen_prompts.append(prompt)
            self.calls += 1
            return "not json" if self.calls == 1 else _GOOD

    agent = _RecordingAgent()
    _patch_agent(monkeypatch, agent)
    calls: List[tuple] = []

    def _tracking_on_parse_error(base_prompt: str, exc: ValueError) -> str:
        calls.append((base_prompt, exc))
        return _on_parse_error(base_prompt, exc)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="original task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_tracking_on_parse_error,
    )

    assert result == _GOOD_PARSED
    assert len(calls) == 1
    assert calls[0][0] == "original task"
    assert seen_prompts[0] == "original task"
    assert seen_prompts[1].startswith("CORRECTED[")


def test_validation_error_then_success_retries_and_calls_on_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD, _GOOD])
    _patch_agent(monkeypatch, agent)

    validation_calls = {"count": 0}

    def _validate(parsed: Dict[str, Any]) -> Dict[str, Any]:
        validation_calls["count"] += 1
        if validation_calls["count"] == 1:
            raise ValueError("bad shape")
        return {"finalized": True, **parsed}

    on_validation_calls: List[tuple] = []

    def _tracking_on_validation_error(base_prompt: str, exc: Exception) -> str:
        on_validation_calls.append((base_prompt, exc))
        return _on_validation_error(base_prompt, exc)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
        validate=_validate,
        on_validation_error=_tracking_on_validation_error,
    )

    assert result == {"finalized": True, "a": 1}
    assert validation_calls["count"] == 2
    assert len(on_validation_calls) == 1
    assert on_validation_calls[0][0] == "task"


def test_budget_exhaustion_parse_error_raises_terminal_exception_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent(["never json"])
    _patch_agent(monkeypatch, agent)

    with pytest.raises(ValueError, match="No JSON object found"):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=1,
            logger=logging.getLogger("test"),
            on_parse_error=_on_parse_error,
        )
    assert agent.calls == 2  # retry_budget + 1


def test_budget_exhaustion_validation_error_raises_terminal_exception_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    def _always_invalid(_parsed: Dict[str, Any]) -> Dict[str, Any]:
        raise ValueError("always invalid")

    with pytest.raises(ValueError, match="always invalid"):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=1,
            logger=logging.getLogger("test"),
            on_parse_error=_on_parse_error,
            validate=_always_invalid,
            on_validation_error=_on_validation_error,
        )
    assert agent.calls == 2  # retry_budget + 1


def test_before_attempt_called_once_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent(["bad", "bad", _GOOD])
    _patch_agent(monkeypatch, agent)
    counter = {"n": 0}

    def _before_attempt() -> None:
        counter["n"] += 1

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        before_attempt=_before_attempt,
        on_parse_error=_on_parse_error,
    )

    assert result == _GOOD_PARSED
    assert counter["n"] == 3
    assert agent.calls == 3


def test_before_attempt_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert result == _GOOD_PARSED


def test_fresh_agent_constructed_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    built: List[_ScriptedAgent] = []
    payload_script = ["bad", "bad", _GOOD]

    def _agent_factory(**_k: Any) -> _ScriptedAgent:
        agent = _ScriptedAgent([payload_script[len(built)]])
        built.append(agent)
        return agent

    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", _agent_factory)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert result == _GOOD_PARSED
    assert len(built) == 3
    for built_agent in built:
        assert built_agent.calls == 1


def test_agent_key_passed_to_get_strands_model(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    model_keys: List[str] = []
    monkeypatch.setattr(
        mod, "get_strands_model", lambda key, *_a, **_k: model_keys.append(key) or object()
    )
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)

    run_json_with_parse_retry(
        agent_key="my_special_agent_key",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert model_keys == ["my_special_agent_key"]


def test_charge_false_forwarded_to_run_structured_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)
    captured_kwargs: Dict[str, Any] = {}

    original = mod.run_structured_agent

    def _capturing(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "run_structured_agent", _capturing)

    run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert captured_kwargs["charge"] is False


def test_retry_budget_zero_disables_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent(["never json"])
    _patch_agent(monkeypatch, agent)
    calls: List[Any] = []

    def _tracking_on_parse_error(base_prompt: str, exc: ValueError) -> str:
        calls.append((base_prompt, exc))
        return _on_parse_error(base_prompt, exc)

    with pytest.raises(ValueError):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=0,
            logger=logging.getLogger("test"),
            on_parse_error=_tracking_on_parse_error,
        )

    assert agent.calls == 1
    assert calls == []


def test_validate_none_returns_parsed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    result = run_json_with_parse_retry(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        base_user_prompt="task",
        retry_budget=2,
        logger=logging.getLogger("test"),
        on_parse_error=_on_parse_error,
    )

    assert result == _GOOD_PARSED


def test_missing_on_validation_error_with_validate_given_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_builds = {"n": 0}

    def _agent_factory(**_k: Any) -> _ScriptedAgent:
        agent_builds["n"] += 1
        return _ScriptedAgent([_GOOD])

    monkeypatch.setattr(mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(mod, "Agent", _agent_factory)

    with pytest.raises(ValueError, match="on_validation_error is required"):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=2,
            logger=logging.getLogger("test"),
            on_parse_error=_on_parse_error,
            validate=lambda parsed: parsed,
        )

    assert agent_builds["n"] == 0


def test_transport_exhaustion_propagates_uncaught(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    def _raise_transport_error(*_a: Any, **_k: Any) -> Any:
        raise StrategyLabLLMError(
            "boom",
            agent_key="strategy_x",
            phase="phase_x",
            attempts=3,
            last_error_class="TimeoutError",
            outcome="exhausted",
            cause=None,
        )

    monkeypatch.setattr(mod, "run_structured_agent", _raise_transport_error)
    calls: List[Any] = []

    def _tracking_on_parse_error(base_prompt: str, exc: ValueError) -> str:
        calls.append((base_prompt, exc))
        return _on_parse_error(base_prompt, exc)

    with pytest.raises(StrategyLabLLMError):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=2,
            logger=logging.getLogger("test"),
            on_parse_error=_tracking_on_parse_error,
        )

    assert calls == []


def test_before_attempt_exception_propagates_uncaught(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    class _BudgetExhausted(Exception):
        pass

    def _before_attempt() -> None:
        raise _BudgetExhausted("no budget left")

    with pytest.raises(_BudgetExhausted):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=2,
            logger=logging.getLogger("test"),
            before_attempt=_before_attempt,
            on_parse_error=_on_parse_error,
        )

    assert agent.calls == 0


def test_logs_warning_on_each_unparseable_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _ScriptedAgent(["bad", _GOOD])
    _patch_agent(monkeypatch, agent)

    logger = logging.getLogger("investment_team.strategy_lab.agents._agent_runner.test")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        run_json_with_parse_retry(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            base_user_prompt="task",
            retry_budget=2,
            logger=logger,
            on_parse_error=_on_parse_error,
        )

    warnings = [r for r in caplog.records if "unparseable JSON" in r.message]
    assert len(warnings) == 1
    assert "attempt 1/3" in warnings[0].message


# ---------------------------------------------------------------------------
# run_single_shot_agent
# ---------------------------------------------------------------------------


def _unreachable_on_failure(exc: Exception) -> Any:
    raise AssertionError(f"on_failure should not have been called: {exc}")


def test_single_shot_returns_ok_true_and_parsed_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    ok, result = run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        on_failure=_unreachable_on_failure,
    )

    assert ok is True
    assert result == _GOOD_PARSED
    assert agent.calls == 1


def test_single_shot_generic_exception_calls_on_failure_and_returns_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent(["not json"])
    _patch_agent(monkeypatch, agent)
    seen: List[Exception] = []

    def _on_failure(exc: Exception) -> str:
        seen.append(exc)
        return "fallback-value"

    ok, result = run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        on_failure=_on_failure,
    )

    assert ok is False
    assert result == "fallback-value"
    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)


def test_single_shot_on_failure_raising_propagates_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent(["not json"])
    _patch_agent(monkeypatch, agent)

    class _DomainError(Exception):
        pass

    def _on_failure(exc: Exception) -> Any:
        raise _DomainError(f"wrapped: {exc}") from exc

    with pytest.raises(_DomainError, match="wrapped:"):
        run_single_shot_agent(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            user_prompt="task",
            logger=logging.getLogger("test"),
            on_failure=_on_failure,
        )


def test_single_shot_design_budget_exhausted_reraised_bare_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    def _raise_budget_exhausted(*_a: Any, **_k: Any) -> Any:
        raise mod.DesignBudgetExhausted(limit=3, calls_made=3)

    monkeypatch.setattr(mod, "run_structured_agent", _raise_budget_exhausted)

    with pytest.raises(mod.DesignBudgetExhausted, match="budget exhausted"):
        run_single_shot_agent(
            agent_key="strategy_x",
            phase="phase_x",
            system_prompt="sys",
            user_prompt="task",
            logger=logging.getLogger("test"),
            on_failure=_unreachable_on_failure,
        )


def test_single_shot_design_budget_exhausted_routed_through_on_failure_when_unguarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)

    budget_exc = mod.DesignBudgetExhausted(limit=3, calls_made=3)

    def _raise_budget_exhausted(*_a: Any, **_k: Any) -> Any:
        raise budget_exc

    monkeypatch.setattr(mod, "run_structured_agent", _raise_budget_exhausted)
    seen: List[Exception] = []

    def _on_failure(exc: Exception) -> str:
        seen.append(exc)
        return "fallback-value"

    ok, result = run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        guard_design_budget=False,
        on_failure=_on_failure,
    )

    assert ok is False
    assert result == "fallback-value"
    assert seen == [budget_exc]


def test_single_shot_max_attempts_forwarded_to_run_structured_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)
    captured_kwargs: Dict[str, Any] = {}

    original = mod.run_structured_agent

    def _capturing(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "run_structured_agent", _capturing)

    run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        max_attempts=5,
        on_failure=_unreachable_on_failure,
    )

    assert captured_kwargs["max_attempts"] == 5


def test_single_shot_model_kwargs_forwarded_to_get_strands_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(mod, "Agent", lambda **_k: agent)
    captured_kwargs: Dict[str, Any] = {}

    def _capturing_get_strands_model(_key: str, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(mod, "get_strands_model", _capturing_get_strands_model)

    run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        model_kwargs={"response_format": "text"},
        on_failure=_unreachable_on_failure,
    )

    assert captured_kwargs == {"response_format": "text"}


def test_single_shot_custom_parse_function_used_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent(["raw python source, not JSON"])
    _patch_agent(monkeypatch, agent)

    ok, result = run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        parse=lambda text: text,
        on_failure=_unreachable_on_failure,
    )

    assert ok is True
    assert result == "raw python source, not JSON"


def test_single_shot_charge_forwarded_to_run_structured_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _ScriptedAgent([_GOOD])
    _patch_agent(monkeypatch, agent)
    captured_kwargs: Dict[str, Any] = {}

    original = mod.run_structured_agent

    def _capturing(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "run_structured_agent", _capturing)

    run_single_shot_agent(
        agent_key="strategy_x",
        phase="phase_x",
        system_prompt="sys",
        user_prompt="task",
        logger=logging.getLogger("test"),
        charge=False,
        on_failure=_unreachable_on_failure,
    )

    assert captured_kwargs["charge"] is False
