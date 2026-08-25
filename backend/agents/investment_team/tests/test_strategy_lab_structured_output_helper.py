"""Unit tests for shared Strategy Lab structured-output invoke helper."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import _structured_output as so_mod


class _StubClient:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # invoke_structured_with_schema's think=True reasoning pass, run
        # before the schema-conformant complete_json call below.
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return dict(self.payload)


class _FakeModel:
    def __init__(self, client: _StubClient) -> None:
        self.client = client


def test_structured_output_available_reflects_provider_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(so_mod, "resolve_provider", lambda: "openai")
    monkeypatch.setattr(
        so_mod, "provider_supports_structured_output", lambda provider: provider == "openai"
    )
    assert so_mod.structured_output_available() is True

    monkeypatch.setattr(so_mod, "provider_supports_structured_output", lambda _provider: False)
    assert so_mod.structured_output_available() is False


def test_invoke_structured_with_schema_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: structured output is available and we get the stubbed JSON."""
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    charge_calls = 0

    def _counting_charge() -> None:
        nonlocal charge_calls
        charge_calls += 1

    monkeypatch.setattr(so_mod, "charge_active_budget", _counting_charge)

    result = so_mod.invoke_structured_with_schema(
        "strategy_design_review",
        "sys",
        "user",
        phase="design_review_structured",
        schema={"type": "object"},
        charge=False,
        objective="strategy design review (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    # charge=False means invoke_structured_with_schema must not touch the
    # active budget itself at all — charging it is entirely the caller's
    # responsibility on this path.
    assert charge_calls == 0
    assert len(client.reasoning_calls) == 1
    assert client.reasoning_calls[0]["think"] is True
    assert (
        client.reasoning_calls[0]["objective"] == "strategy design review (structured) (reasoning)"
    )
    assert client.reasoning_calls[0]["system_prompt"] == "sys" + so_mod.REASONING_MODE_SUFFIX

    assert len(client.calls) == 1
    assert client.calls[0]["schema"] == {"type": "object"}
    assert client.calls[0]["objective"] == "strategy design review (structured) (format)"
    assert client.calls[0]["system_prompt"] == "sys"
    assert client.calls[0]["think"] is False
    # The formatting prompt carries both the original user prompt and the
    # reasoning-pass prose.
    assert "user" in client.calls[0]["prompt"]
    assert "reasoning prose" in client.calls[0]["prompt"]


def test_reasoning_prompt_overrides_the_templates_json_only_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four call sites embed "Return ONLY a JSON object" in their USER
    prompt, which would outrank the system prompt's prose-only instruction and
    make the reasoning pass emit JSON (wasting a call and a budget unit for no
    reasoning). The reasoning call's user prompt must re-assert prose last.
    """
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    user_prompt = "Design a spec.\nReturn ONLY a JSON object with no markdown."
    so_mod.invoke_structured_with_schema(
        "strategy_design",
        "sys",
        user_prompt,
        phase="design_generate_structured",
        schema={"type": "object"},
        charge=False,
        objective="strategy design (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    reasoning_prompt = client.reasoning_calls[0]["prompt"]
    # The task-specific content is preserved...
    assert "Design a spec." in reasoning_prompt
    # ...and the JSON directive is explicitly neutralized, after it.
    assert reasoning_prompt.index("Return ONLY a JSON object") < reasoning_prompt.index(
        "OVERRIDE FOR THIS PASS ONLY"
    )
    assert "emit NO JSON at all" in reasoning_prompt
    # The formatting pass still gets the unmodified directive (no override).
    assert "OVERRIDE FOR THIS PASS ONLY" not in client.calls[0]["prompt"]
    assert "Return ONLY a JSON object" in client.calls[0]["prompt"]


def test_reasoning_pass_starvation_presents_as_schema_forced_without_breaking_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reasoning-pass ``LLMSemanticExhaustionError`` must reach callers as
    ``schema_forced=True`` (so their degrade check fires) while still honoring
    ``interface.py``'s documented pairing that ``schema_forced=True`` implies
    ``retry_thinking_level is None`` — so a fresh receipt is raised rather than
    the original being mutated in place.
    """
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError
    from llm_service.interface import LLMSemanticExhaustionError

    original = LLMSemanticExhaustionError(
        "reasoning starved",
        attempts_used=3,
        original_thinking_level="max",
        retry_thinking_level="high",  # the ladder DID run on this path
        content_bytes_seen=True,
        payload_fingerprint="fp-123",
        finish_reason="length",
        schema_forced=False,
    )

    class _StarvingClient(_StubClient):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            raise original

    client = _StarvingClient({})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    with pytest.raises(StrategyLabLLMError) as excinfo:
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=False,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )

    # The envelope wraps it; callers (design.py et al.) inspect ``.cause`` and
    # gate their degrade on ``schema_forced`` — assert what they actually see.
    raised = excinfo.value.cause
    assert isinstance(raised, LLMSemanticExhaustionError)
    assert raised is not original, "must not mutate the client's receipt in place"
    assert raised.schema_forced is True
    # The documented invariant: schema_forced=True => retry_thinking_level is None.
    assert raised.retry_thinking_level is None
    # Diagnostics from the original are preserved rather than dropped.
    assert raised.cause is original
    assert raised.attempts_used == 3
    assert raised.original_thinking_level == "max"
    assert raised.content_bytes_seen is True
    assert raised.payload_fingerprint == "fp-123"
    assert raised.finish_reason == "length"
    assert raised.cause.retry_thinking_level == "high"  # the original ladder level stays visible
    # The original receipt is left untouched for any other holder of it.
    assert original.schema_forced is False
    assert original.retry_thinking_level == "high"
    # The formatting call never ran.
    assert client.calls == []


def test_invoke_structured_with_schema_doubles_timeout_for_two_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_call`` now makes two sequential provider calls (reasoning + format)
    under the envelope's single per-attempt timeout guard. Regression test
    for a real Codex review finding: without doubling, two individually
    healthy calls could together exceed a budget sized for one, aborting the
    attempt and abandoning a still-running daemon thread even though neither
    provider request was actually slow.
    """
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))
    monkeypatch.setattr(so_mod, "resolve_timeout", lambda agent_key: 30.0)
    monkeypatch.delenv("STRATEGY_LAB_LLM_TIMEOUT", raising=False)

    captured: Dict[str, Any] = {}

    def _spy_run_structured_agent(agent_callable, prompt, *, parse, **kwargs):
        captured.update(kwargs)
        return parse(agent_callable(prompt))

    monkeypatch.setattr(so_mod, "run_structured_agent", _spy_run_structured_agent)

    result = so_mod.invoke_structured_with_schema(
        "strategy_design_review",
        "sys",
        "user",
        phase="design_review_structured",
        schema={"type": "object"},
        charge=False,
        objective="strategy design review (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    assert captured["timeout_s"] == pytest.approx(60.0)


def test_invoke_structured_with_schema_charges_per_provider_call_inside_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invoke_structured_with_schema(charge=True)`` charges once immediately
    before each provider call inside the retried ``_call`` closure, and always
    forwards ``charge=False`` to ``run_structured_agent`` itself.

    Charging inside the closure (not once up front) means a transport retry
    that re-runs both calls also re-charges both units.
    """
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    charge_calls = 0
    charged_before_run_structured_agent: List[int] = []
    charges_per_call_invocation: List[int] = []

    def _counting_charge() -> None:
        nonlocal charge_calls
        charge_calls += 1

    monkeypatch.setattr(so_mod, "charge_active_budget", _counting_charge)

    captured: Dict[str, Any] = {}
    real_run_structured_agent = so_mod.run_structured_agent

    def _spy_run_structured_agent(fn: Any, prompt: str, *args: Any, charge: bool, **kwargs: Any) -> Any:
        captured["charge"] = charge
        # No up-front charges before the envelope starts.
        charged_before_run_structured_agent.append(charge_calls)

        # Simulate one transport retry: drive _call twice. Each attempt must
        # charge twice (reasoning + formatting).
        before_first = charge_calls
        fn(prompt)
        charges_per_call_invocation.append(charge_calls - before_first)
        before_second = charge_calls
        result = real_run_structured_agent(fn, prompt, *args, charge=charge, **kwargs)
        charges_per_call_invocation.append(charge_calls - before_second)
        return result

    monkeypatch.setattr(so_mod, "run_structured_agent", _spy_run_structured_agent)

    result = so_mod.invoke_structured_with_schema(
        "strategy_design_review",
        "sys",
        "user",
        phase="design_review_structured",
        schema={"type": "object"},
        charge=True,
        objective="strategy design review (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    assert captured["charge"] is False
    assert charged_before_run_structured_agent == [0]
    # Manual first attempt + real envelope attempt: 2 + 2 = 4 charges.
    assert charge_calls == 4
    assert charges_per_call_invocation == [2, 2]


def test_invoke_structured_with_schema_mid_attempt_budget_trip_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With one budget unit left, the reasoning charge consumes it and the
    formatting charge must raise ``DesignBudgetExhausted`` out of the envelope
    unmodified — not retried / wrapped as ``StrategyLabLLMError``.
    """
    from investment_team.strategy_lab.agents._llm_budget import (
        DesignBudgetExhausted,
        LLMCallBudget,
        use_budget,
    )
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    client = _StubClient({"ready": True})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    budget = LLMCallBudget(limit=1)
    with use_budget(budget):
        with pytest.raises(DesignBudgetExhausted) as ei:
            so_mod.invoke_structured_with_schema(
                "strategy_design_review",
                "sys",
                "user",
                phase="design_review_structured",
                schema={"type": "object"},
                charge=True,
                objective="strategy design review (structured)",
                logger=logging.getLogger("test.so"),
                reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            )
    assert ei.value.limit == 1
    assert ei.value.calls_made == 1
    # Reasoning call ran (and was charged); formatting never started.
    assert len(client.reasoning_calls) == 1
    assert client.calls == []
    # And it must not have been wrapped.
    assert not isinstance(ei.value, StrategyLabLLMError)


def test_invoke_structured_with_schema_requires_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If structured output is unavailable, the helper asserts a precondition."""
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)
    with pytest.raises(ValueError, match="precondition"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )


@pytest.mark.parametrize(
    "field",
    ["agent_key", "system_prompt", "user_prompt", "phase", "objective"],
)
def test_invoke_structured_with_schema_rejects_empty_inputs(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Empty required input fields must be rejected via a precondition assert."""
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    kwargs: Dict[str, Any] = {
        "agent_key": "strategy_design",
        "system_prompt": "sys",
        "user_prompt": "user",
        "phase": "design_generate_structured",
        "schema": {"type": "object"},
        "charge": True,
        "objective": "strategy design (structured)",
        "logger": logging.getLogger("test.so"),
        "reasoning_system_prompt": "sys" + so_mod.REASONING_MODE_SUFFIX,
    }
    kwargs[field] = ""
    with pytest.raises(ValueError, match="precondition"):
        so_mod.invoke_structured_with_schema(**kwargs)


def test_invoke_structured_with_schema_rejects_empty_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty mapping schema must be rejected via a precondition assert."""
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    with pytest.raises(ValueError, match="precondition"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )


def test_invoke_structured_with_schema_propagates_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``StrategyLabLLMError`` from the envelope must propagate unmodified."""
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(
        so_mod,
        "run_structured_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(StrategyLabLLMError("fatal")),
    )
    with pytest.raises(StrategyLabLLMError, match="fatal"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )


def test_invoke_structured_with_schema_propagates_parse_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw ``ValueError`` from parse must propagate unwrapped by the helper."""
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(
        so_mod,
        "run_structured_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad json")),
    )
    with pytest.raises(ValueError, match="bad json"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )


# ---------------------------------------------------------------------------
# try_structured_or_degrade
# ---------------------------------------------------------------------------

_TRY_STRUCTURED_OR_DEGRADE_KWARGS: Dict[str, Any] = {
    "agent_key": "strategy_design",
    "schema": {"type": "object"},
    "system_prompt": "sys",
    "user_prompt": "user",
    "phase": "design_generate_structured",
    "charge": True,
    "objective": "strategy design (structured)",
}


def test_try_structured_or_degrade_unavailable_returns_none_without_invoking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the provider doesn't support structured output, the helper must
    return ``None`` immediately without ever calling
    :func:`invoke_structured_with_schema`."""
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)

    def _fail_if_called(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("invoke_structured_with_schema must not be called")

    monkeypatch.setattr(so_mod, "invoke_structured_with_schema", _fail_if_called)

    result = so_mod.try_structured_or_degrade(
        **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        logger=logging.getLogger("test.so"),
    )
    assert result is None


def test_try_structured_or_degrade_success_returns_parsed_and_logs_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On success, the parsed dict is returned as-is and an INFO log naming
    the agent/phase is emitted."""
    parsed = {"ready": True, "rationale": "ok", "issues": []}
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "invoke_structured_with_schema", lambda *_a, **_k: dict(parsed))

    logger_name = "test.so.try_structured_or_degrade.success"
    with caplog.at_level(logging.INFO, logger=logger_name):
        result = so_mod.try_structured_or_degrade(
            **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            logger=logging.getLogger(logger_name),
        )

    assert result == parsed
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    message = info_records[0].getMessage()
    assert "outcome=succeeded" in message
    assert _TRY_STRUCTURED_OR_DEGRADE_KWARGS["agent_key"] in message
    assert _TRY_STRUCTURED_OR_DEGRADE_KWARGS["phase"] in message


def test_try_structured_or_degrade_schema_forced_degrades_to_none_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``StrategyLabLLMError`` whose cause is a schema-forced
    ``LLMSemanticExhaustionError`` must degrade to ``None`` with a WARNING
    log, rather than propagating."""
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError
    from llm_service.interface import LLMSemanticExhaustionError

    cause = LLMSemanticExhaustionError("starved", schema_forced=True)
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(
        so_mod,
        "invoke_structured_with_schema",
        lambda *_a, **_k: (_ for _ in ()).throw(StrategyLabLLMError("boom", cause=cause)),
    )

    logger_name = "test.so.try_structured_or_degrade.schema_forced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = so_mod.try_structured_or_degrade(
            **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            logger=logging.getLogger(logger_name),
        )

    assert result is None
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    message = warning_records[0].getMessage()
    assert "schema_forced_degrade" in message
    assert _TRY_STRUCTURED_OR_DEGRADE_KWARGS["agent_key"] in message
    assert _TRY_STRUCTURED_OR_DEGRADE_KWARGS["phase"] in message


def test_try_structured_or_degrade_non_schema_forced_cause_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``StrategyLabLLMError`` whose cause is an
    ``LLMSemanticExhaustionError`` with ``schema_forced=False`` is not
    degradable and must propagate unmodified."""
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError
    from llm_service.interface import LLMSemanticExhaustionError

    cause = LLMSemanticExhaustionError("starved", schema_forced=False)
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(
        so_mod,
        "invoke_structured_with_schema",
        lambda *_a, **_k: (_ for _ in ()).throw(StrategyLabLLMError("boom", cause=cause)),
    )

    with pytest.raises(StrategyLabLLMError, match="boom"):
        so_mod.try_structured_or_degrade(
            **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            logger=logging.getLogger("test.so"),
        )


def test_try_structured_or_degrade_non_semantic_exhaustion_cause_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``StrategyLabLLMError`` whose cause is not an
    ``LLMSemanticExhaustionError`` at all (e.g. a fatal transport error) is
    never degradable and must propagate unmodified."""
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(
        so_mod,
        "invoke_structured_with_schema",
        lambda *_a, **_k: (_ for _ in ()).throw(
            StrategyLabLLMError("fatal", cause=RuntimeError("transport down"))
        ),
    )

    with pytest.raises(StrategyLabLLMError, match="fatal"):
        so_mod.try_structured_or_degrade(
            **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            logger=logging.getLogger("test.so"),
        )


def test_try_structured_or_degrade_budget_exhausted_propagates_uncaught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DesignBudgetExhausted`` is not a ``StrategyLabLLMError`` and must
    propagate through :func:`try_structured_or_degrade` untouched — never
    caught, wrapped, or degraded to ``None``."""
    from investment_team.strategy_lab.agents._llm_budget import DesignBudgetExhausted
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(
        so_mod,
        "invoke_structured_with_schema",
        lambda *_a, **_k: (_ for _ in ()).throw(DesignBudgetExhausted(limit=1, calls_made=1)),
    )

    with pytest.raises(DesignBudgetExhausted) as excinfo:
        so_mod.try_structured_or_degrade(
            **_TRY_STRUCTURED_OR_DEGRADE_KWARGS,
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
            logger=logging.getLogger("test.so"),
        )

    assert excinfo.value.limit == 1
    assert excinfo.value.calls_made == 1
    assert not isinstance(excinfo.value, StrategyLabLLMError)
