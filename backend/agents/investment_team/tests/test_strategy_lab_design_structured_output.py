"""DesignAgent structured-output happy path and degrade behavior.

``DesignAgent._invoke_and_parse`` uses ``invoke_structured_with_schema``,
which first runs a prose reasoning pass with ``think=True`` and then a
schema-conformant formatting pass with ``think=False`` and
``schema=DESIGN_SPEC_SCHEMA`` when the active provider supports structured
output (currently Ollama only), eliminating the ``build_json_correction_prompt``
happy-path resend for the design generate/revise loop. ``DesignAgent._self_review``
gets the same treatment for ``CRITIQUE_SCHEMA``.

These tests lock in: the structured call is used and skips the legacy
``strands.Agent`` machinery on success; a schema-valid-but-DSL-invalid
response still triggers the ``_build_correction_prompt`` DSL-validation
retry (structured output constrains JSON shape, not DSL semantics); the real
``provider_supports_structured_output(resolve_provider())`` wiring degrades
to the legacy parse-retry loop for an unsupported provider (Bedrock); a
``schema_forced`` semantic-exhaustion starvation signal degrades to the
legacy loop the same way (both for the generate call and the self-review
call independently); and any OTHER fatal failure from the structured
attempt propagates without degrading. Mirrors
``test_strategy_lab_refinement_structured_output.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import _agent_runner as agent_runner_mod
from investment_team.strategy_lab.agents import _structured_output as so_mod
from investment_team.strategy_lab.agents import design as design_mod
from investment_team.strategy_lab.agents._llm_budget import LLMCallBudget, use_budget
from investment_team.strategy_lab.agents._response_schemas import (
    CRITIQUE_SCHEMA,
    DESIGN_SPEC_SCHEMA,
)
from investment_team.strategy_lab.agents.design import DesignAgent
from investment_team.strategy_lab.exceptions import StrategyLabLLMError
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _payload(
    *,
    entry_rules: List[Dict[str, Any]],
    exit_rules: List[Dict[str, Any]],
    sizing: Dict[str, Any],
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a complete design-agent payload dict, no strategy_code."""
    body: Dict[str, Any] = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "sizing": sizing,
        "target_symbols": [],
        "risk_limits": {"max_position_pct": 5},
        "speculative": False,
        "rationale": "scripted",
    }
    if extra:
        body.update(extra)
    return body


def _structured_entry_rule() -> Dict[str, Any]:
    return {
        "kind": "entry",
        "side": "long",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": "<",
            "rhs": 30,
        },
    }


def _structured_signal_exit_rule() -> Dict[str, Any]:
    return {
        "kind": "signal_exit",
        "when": {
            "lhs": {"name": "rsi", "params": {"period": 14}},
            "op": ">",
            "rhs": 70,
        },
    }


def _structured_sizing() -> Dict[str, Any]:
    return {"kind": "fixed_fraction", "fraction": 0.02}


def _good_design_payload() -> Dict[str, Any]:
    return _payload(
        entry_rules=[_structured_entry_rule()],
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )


class _ScriptedAgent:
    """Strands ``Agent`` replacement returning a scripted payload per call.

    Invariants:
        ``self._payloads`` is non-empty for the lifetime of the instance —
        enforced by ``__init__``, so ``__call__`` never indexes an empty list.
    """

    def __init__(self, payloads: List[str]) -> None:
        """Preconditions: ``payloads`` is non-empty (an empty list would make
        ``__call__``'s ``len(self._payloads) - 1`` clamp negative and raise
        an unhelpful ``IndexError`` on first use).

        Postconditions: ``self._payloads is payloads`` and ``self.calls == 0``.
        """
        if not payloads:
            raise AssertionError("_ScriptedAgent requires a non-empty payloads list")
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        """Postconditions: returns ``self._payloads[idx]``, where ``idx``
        advances with each call and then clamps to the final payload once
        ``self.calls`` reaches ``len(self._payloads)``; ``self.calls`` is
        incremented by one.
        """
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


class _RecordingAgent:
    """Strands ``Agent`` replacement that records every prompt it receives.

    Invariants:
        ``self._payloads`` is non-empty for the lifetime of the instance —
        enforced by ``__init__``, so ``__call__`` never indexes an empty list.
    """

    def __init__(self, payloads: List[str]) -> None:
        """Preconditions: ``payloads`` is non-empty (an empty list would make
        ``__call__``'s ``len(self._payloads) - 1`` clamp negative and raise
        an unhelpful ``IndexError`` on first use).

        Postconditions: ``self._payloads is payloads`` and ``self.seen == []``.
        """
        if not payloads:
            raise AssertionError("_RecordingAgent requires a non-empty payloads list")
        self._payloads = payloads
        self.seen: List[str] = []

    def __call__(self, prompt: str) -> str:
        """Postconditions: ``prompt`` is appended to ``self.seen``, and the
        method returns ``self._payloads[idx]``, where ``idx`` tracks
        ``len(self.seen)`` and then clamps to the final payload once that
        exceeds ``len(self._payloads)``.
        """
        idx = min(len(self.seen), len(self._payloads) - 1)
        self.seen.append(prompt)
        return self._payloads[idx]


# ---------------------------------------------------------------------------
# Precondition guards — empty payloads rejected
# ---------------------------------------------------------------------------


def test_scripted_agent_rejects_empty_payloads() -> None:
    """Construction fails fast on an empty payloads list instead of deferring
    to an ``IndexError`` from ``__call__``'s negative clamp on first use."""
    with pytest.raises(AssertionError, match="non-empty"):
        _ScriptedAgent([])


def test_recording_agent_rejects_empty_payloads() -> None:
    """Construction fails fast on an empty payloads list instead of deferring
    to an ``IndexError`` from ``__call__``'s negative clamp on first use."""
    with pytest.raises(AssertionError, match="non-empty"):
        _RecordingAgent([])


class _StubClient:
    """Backing ``LLMClient`` stand-in that records every ``complete()``
    (reasoning pass) and ``complete_json()`` (formatting pass) call."""

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # invoke_structured_with_schema's think=True reasoning pass, run
        # before the schema-conformant complete_json call below.
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


class _FailingClient:
    """Backing client stand-in whose ``complete_json`` always raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise self._exc


class _SchemaRoutedClient:
    """Backing client whose ``complete_json`` behavior depends on ``schema``.

    ``DesignAgent`` routes both the design-generate call and the self-review
    call through ``so.invoke_structured_with_schema`` / the shared
    ``get_strands_model("strategy_design").client`` — the same agent key — so
    a single stub must disambiguate by schema content (the helper passes
    ``dict(schema)``, so identity checks are unreliable).
    """

    def __init__(self, design_result: Dict[str, Any], critique_result_or_exc: Any) -> None:
        self._design_result = design_result
        self._critique_result_or_exc = critique_result_or_exc
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # invoke_structured_with_schema's think=True reasoning pass, run
        # before the schema-conformant complete_json call below.
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        if kwargs.get("schema") == CRITIQUE_SCHEMA:
            if isinstance(self._critique_result_or_exc, BaseException):
                raise self._critique_result_or_exc
            return self._critique_result_or_exc
        return self._design_result


class _FakeModel:
    """Minimal ``get_strands_model(...)`` return value: only ``.client`` is used."""

    def __init__(self, client: Any) -> None:
        self.client = client


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


def _raise_if_correction_prompt_built(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "build_json_correction_prompt must not be called on the structured happy path"
    )


# ---------------------------------------------------------------------------
# Happy path — generate
# ---------------------------------------------------------------------------


def test_structured_path_used_when_available_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(_good_design_payload())
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(
        design_mod, "build_json_correction_prompt", _raise_if_correction_prompt_built
    )
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert "strategy_code" not in parsed
    assert rationale == "scripted"
    assert parsed["asset_class"] == "stocks"
    assert len(stub_client.calls) == 1
    assert len(stub_client.reasoning_calls) == 1


def test_structured_success_logs_outcome_succeeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The design happy path emits an INFO ``outcome=succeeded`` marker so the
    resend-free path is observable in production logs."""
    stub_client = _StubClient(_good_design_payload())
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.INFO, logger=logger_name):
        DesignAgent().run(prior_records=[])

    succeeded = [
        r
        for r in caplog.records
        if "outcome=succeeded" in r.message and "phase=design_generate_structured" in r.message
    ]
    assert len(succeeded) == 1
    assert "agent=strategy_design" in succeeded[0].message


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(_good_design_payload())
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    DesignAgent().run(prior_records=[])

    assert len(stub_client.reasoning_calls) == 1
    assert len(stub_client.calls) == 1
    reasoning_call = stub_client.reasoning_calls[0]
    call = stub_client.calls[0]

    # Reasoning pass (think=True, reasoning_temperature): receives the
    # original design-task prompt directly, not a correction re-prompt.
    assert reasoning_call["think"] is True
    assert reasoning_call["temperature"] == so_mod._DEFAULT_REASONING_TEMPERATURE
    assert "Design ONE novel swing-style strategy" in reasoning_call["prompt"]
    assert "could not be parsed as a single JSON object" not in reasoning_call["prompt"]

    # Formatting pass (think=False, temperature=0.0): schema-conformant,
    # original (non-reasoning) system prompt.
    assert call["think"] is False
    assert call["temperature"] == 0.0
    assert call["schema"] == DESIGN_SPEC_SCHEMA
    assert call["system_prompt"] == design_mod._get_design_system_prompt()
    # The original task prompt was sent, not a correction re-prompt.
    assert "could not be parsed as a single JSON object" not in call["prompt"]


def test_structured_agent_key_and_phase_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def _fake_run_structured_agent(
        _agent_callable: Any,
        _prompt: str,
        *,
        agent_key: str,
        phase: str,
        parse: Any,
        coerce: Any = None,
        charge: bool = True,
        logger: Any = None,
        **_invoke_kwargs: Any,
    ) -> Dict[str, Any]:
        captured["agent_key"] = agent_key
        captured["phase"] = phase
        captured["charge"] = charge
        return _good_design_payload()

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(so_mod, "run_structured_agent", _fake_run_structured_agent)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    budget = LLMCallBudget(limit=5)
    with use_budget(budget):
        DesignAgent().run(prior_records=[])

    assert captured["agent_key"] == "strategy_design"
    assert captured["phase"] == "design_generate_structured"
    # ``invoke_structured_with_schema`` always forwards charge=False to the
    # envelope; per-provider-call charging happens inside its ``_call``.
    assert captured["charge"] is False
    # This spy skips ``_call``, so per-provider-call charges inside the
    # closure never fire — the bound budget stays untouched.
    assert budget.calls_made == 0


def test_revise_also_uses_structured_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``revise()`` shares ``_invoke_and_parse`` with ``run()``, so it gets
    the structured path for free — no separate wiring needed."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents.design_review import CritiqueIssue, SpecCritique
    from investment_team.strategy_lab.spec_dsl import (
        EntryRule,
        IndicatorRef,
        Predicate,
        SignalExitRule,
    )

    prior_spec = StrategySpec(
        strategy_id="strat-structured-revise",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5},
    )
    critique = SpecCritique(
        ready=False,
        rationale="Sizing too aggressive.",
        issues=[
            CritiqueIssue(
                field="sizing",
                severity="warning",
                description="2% is high.",
                suggested_fix="Use 1%.",
            )
        ],
    )
    stub_client = _StubClient(_good_design_payload())
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(
        design_mod, "build_json_correction_prompt", _raise_if_correction_prompt_built
    )
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    parsed, _ = DesignAgent().revise(prior_spec, critique)

    assert parsed["asset_class"] == "stocks"
    # One structured invocation = one reasoning-pass (.complete) call plus
    # one formatting-pass (.complete_json) call, recorded in separate lists.
    assert len(stub_client.reasoning_calls) == 1
    assert len(stub_client.calls) == 1


# ---------------------------------------------------------------------------
# DSL-validation correction path must still fire (not conflated with shape)
# ---------------------------------------------------------------------------


def test_structured_success_dsl_invalid_falls_through_to_correction_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured decode that is JSON-shape-valid but DSL-invalid (prose
    entry rules, not the structured DSL objects) must still trigger the
    ``_build_correction_prompt`` retry path, distinct from the
    unparseable-JSON ``build_json_correction_prompt`` path."""
    dsl_invalid_payload = _payload(
        entry_rules=["close > sma(20)"],  # prose — DSL-invalid, still JSON-shape-valid
        exit_rules=[_structured_signal_exit_rule()],
        sizing=_structured_sizing(),
    )
    stub_client = _StubClient(dsl_invalid_payload)
    legacy_agent = _RecordingAgent([json.dumps(_good_design_payload())])
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_mod, "Agent", lambda **_k: legacy_agent)
    # The legacy fallback loop builds its Agent via `_agent_runner`, not `design`.
    monkeypatch.setattr(agent_runner_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_runner_mod, "Agent", lambda **_k: legacy_agent)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")

    parsed, rationale = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert rationale == "scripted"
    # Exactly one structured attempt, then exactly one legacy correction retry.
    # The one structured attempt is a reasoning-pass (.complete) call plus a
    # formatting-pass (.complete_json) call, recorded in separate lists.
    assert len(stub_client.reasoning_calls) == 1
    assert len(stub_client.calls) == 1
    assert len(legacy_agent.seen) == 1
    assert "rejected by the DSL validator" in legacy_agent.seen[0]
    assert "close > sma(20)" in legacy_agent.seen[0]


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``structured_output_available`` seam — routes to
    the legacy loop for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")
    agent = _ScriptedAgent([json.dumps(_good_design_payload())])
    monkeypatch.setattr(design_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_mod, "Agent", lambda **_k: agent)
    monkeypatch.setattr(agent_runner_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_runner_mod, "Agent", lambda **_k: agent)

    parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert agent.calls == 1


# ---------------------------------------------------------------------------
# Degrade: schema_forced starvation — generate call
# ---------------------------------------------------------------------------


def test_schema_forced_starvation_degrades_to_legacy_loop_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "false")
    agent = _ScriptedAgent([json.dumps(_good_design_payload())])
    monkeypatch.setattr(design_mod, "Agent", lambda **_k: agent)
    monkeypatch.setattr(agent_runner_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_runner_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced_degrade" in r.message]
    assert len(starvation_warnings) == 1


# ---------------------------------------------------------------------------
# No degrade: a non-schema_forced fatal failure propagates unchanged
# ---------------------------------------------------------------------------


def test_non_schema_forced_permanent_error_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)

    with pytest.raises(StrategyLabLLMError):
        DesignAgent().run(prior_records=[])


def test_non_schema_forced_semantic_exhaustion_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks ``.schema_forced`` specifically, not just the exception
    type — an ordinary (non-schema-forced) semantic exhaustion must NOT
    degrade to the legacy loop either."""
    exhausted_client = _FailingClient(
        LLMSemanticExhaustionError(
            "empty, but not schema forced", schema_forced=False, attempts_used=1
        )
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(exhausted_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)

    with pytest.raises(StrategyLabLLMError):
        DesignAgent().run(prior_records=[])


# ---------------------------------------------------------------------------
# Self-review — separate structured wiring (CRITIQUE_SCHEMA)
# ---------------------------------------------------------------------------


def test_self_review_uses_structured_path_when_available(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Both the design-generate call and the self-review critique call use
    structured decoding, with zero ``strands.Agent`` construction — proving
    ``_self_review`` needed its own wiring rather than falling out for free.
    Each independently emits its own ``outcome=succeeded`` marker."""
    client = _SchemaRoutedClient(
        _good_design_payload(),
        {"ready": True, "rationale": "looks fine", "issues": []},
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "true")

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.INFO, logger=logger_name):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    # Two structured invocations (design-generate + self-review critique) =
    # two reasoning-pass calls plus two formatting-pass calls.
    assert len(client.reasoning_calls) == 2
    assert len(client.calls) == 2
    assert client.calls[0]["schema"] == DESIGN_SPEC_SCHEMA
    assert client.calls[1]["schema"] == CRITIQUE_SCHEMA

    phases = {
        "design_generate_structured": 0,
        "design_self_review_structured": 0,
    }
    for record in caplog.records:
        if "outcome=succeeded" not in record.message:
            continue
        for phase in phases:
            if f"phase={phase}" in record.message:
                phases[phase] += 1
    assert phases == {"design_generate_structured": 1, "design_self_review_structured": 1}


def test_self_review_schema_forced_starvation_degrades_to_legacy_agent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The design-generate call succeeds structured; the self-review call
    starves and independently degrades to a legacy ``Agent`` critique call."""
    client = _SchemaRoutedClient(
        _good_design_payload(),
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1),
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))
    critique_agent = _ScriptedAgent(['{"ready": true, "rationale": "fine", "issues": []}'])
    monkeypatch.setattr(design_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_mod, "Agent", lambda **_k: critique_agent)
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED", "true")

    logger_name = "investment_team.strategy_lab.agents.design"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        parsed, _ = DesignAgent().run(prior_records=[])

    assert parsed["asset_class"] == "stocks"
    assert critique_agent.calls == 1
    warnings = [r for r in caplog.records if "schema_forced_degrade" in r.message]
    assert len(warnings) == 1


def test_self_review_non_schema_forced_failure_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the design-generate call's "no degrade" boundary
    (``test_non_schema_forced_permanent_error_propagates_without_degrading``)
    for ``_self_review`` specifically: a non-``schema_forced`` structured
    failure must propagate — not be silently absorbed into a legacy
    ``Agent`` fallback — so ``_with_self_review``'s best-effort catch is the
    only thing that decides how it's handled, not this seam."""
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(design_mod, "Agent", _raise_if_agent_built)

    with pytest.raises(StrategyLabLLMError):
        DesignAgent()._self_review(_good_design_payload())


# ---------------------------------------------------------------------------
# structured_output_available() — direct unit coverage of the seam
# ---------------------------------------------------------------------------


def test_structured_output_available_true_for_ollama_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert so_mod.structured_output_available() is True


def test_structured_output_available_false_for_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert so_mod.structured_output_available() is False


def test_structured_output_available_false_for_bedrock_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert so_mod.structured_output_available() is False
