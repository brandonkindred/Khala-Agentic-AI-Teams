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


def test_invoke_structured_with_schema_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
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
    # responsibility on this path (see design_review.py's explicit
    # charge_active_budget() calls around its charge=False invocation).
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


def test_invoke_structured_with_schema_charges_twice_up_front_and_forwards_charge_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invoke_structured_with_schema(charge=True)`` charges the active
    budget TWICE, both before ``run_structured_agent`` runs (i.e. before
    either provider call), and always forwards ``charge=False`` to
    ``run_structured_agent`` itself — the two-provider-call accounting lives
    entirely in this helper, never delegated to the inner envelope.
    """
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    charge_calls = 0
    charged_before_run_structured_agent: List[int] = []

    def _counting_charge() -> None:
        nonlocal charge_calls
        charge_calls += 1

    monkeypatch.setattr(so_mod, "charge_active_budget", _counting_charge)

    captured: Dict[str, Any] = {}
    real_run_structured_agent = so_mod.run_structured_agent

    def _spy_run_structured_agent(*args: Any, charge: bool, **kwargs: Any) -> Any:
        captured["charge"] = charge
        # Snapshot how many pre-charges already happened by the time the
        # envelope (and therefore either provider call) actually runs.
        charged_before_run_structured_agent.append(charge_calls)
        return real_run_structured_agent(*args, charge=charge, **kwargs)

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
    assert charge_calls == 2
    assert captured["charge"] is False
    assert charged_before_run_structured_agent == [2]


def test_invoke_structured_with_schema_requires_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


@pytest.mark.parametrize("field", ["agent_key", "system_prompt", "user_prompt"])
def test_invoke_structured_with_schema_rejects_empty_inputs(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
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
