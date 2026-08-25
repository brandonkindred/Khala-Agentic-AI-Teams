"""DesignReviewAgent structured-output happy path and degrade behavior.

``DesignReviewAgent.run`` requests provider-enforced schema-conformant
decoding (``CRITIQUE_SCHEMA``) via ``LLMClient.complete_json(schema=...)``
when the active provider supports it (currently Ollama only). These tests
lock in: the structured call is used and skips the legacy ``strands.Agent``
machinery on success; the real
``provider_supports_structured_output(resolve_provider())`` wiring degrades
to the legacy single call for an unsupported provider (Bedrock); a
``schema_forced`` semantic-exhaustion starvation signal degrades to the
legacy call the same way; a non-schema_forced fatal failure still reaches
the SAME unchanged ``_fail_closed_critique`` terminal fallback (it is
re-raised out of the structured attempt, not swallowed there). Mirrors
``test_strategy_lab_refinement_structured_output.py`` and
``test_strategy_lab_design_structured_output.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.agents import _structured_output as so_mod
from investment_team.strategy_lab.agents import design_review as design_review_mod
from investment_team.strategy_lab.agents._llm_budget import LLMCallBudget, use_budget
from investment_team.strategy_lab.agents._response_schemas import CRITIQUE_SCHEMA
from investment_team.strategy_lab.agents.design_review import DesignReviewAgent
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-review-structured",
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
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )


_SAMPLE_CRITIQUE_RESULT = {"ready": True, "rationale": "spec is implementable", "issues": []}


class _ScriptedAgent:
    """Strands ``Agent`` replacement returning a scripted payload per call."""

    def __init__(self, payloads: List[str]) -> None:
        self._payloads = payloads
        self.calls = 0

    def __call__(self, _prompt: str) -> str:
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


class _StubClient:
    """Backing ``LLMClient`` stand-in that records every ``complete()``
    (reasoning pass) and ``complete_json()`` (formatting pass) call."""

    _REASONING_PROSE = "reasoning prose"

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []
        # Records "reasoning" / "formatting" in actual call order, so tests
        # can assert the reasoning pass ran strictly before the formatting
        # pass (not just that both ran once).
        self.call_order: List[str] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # invoke_structured_with_schema's think=True reasoning pass, run
        # before the schema-conformant complete_json call below.
        self.call_order.append("reasoning")
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return self._REASONING_PROSE

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.call_order.append("formatting")
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


class _FailingClient:
    """Backing client stand-in whose ``complete_json`` always raises.

    ``budget``, when set by the test, lets the client snapshot
    ``budget.calls_made`` at the moment ``complete_json`` runs — proving the
    structured attempt already charged for the reasoning call and for the
    formatting call (charge happens immediately before ``complete_json``).
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.budget: Any = None
        self.calls_made_at_formatting_call: int | None = None
        self.reasoning_ran = False

    def complete(self, prompt: str, **kwargs: Any) -> str:
        self.reasoning_ran = True
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        if self.budget is not None:
            self.calls_made_at_formatting_call = self.budget.calls_made
        raise self._exc


class _FakeModel:
    """Minimal ``get_strands_model(...)`` return value: only ``.client`` is used."""

    def __init__(self, client: Any) -> None:
        self.client = client


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_structured_path_used_when_available_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(dict(_SAMPLE_CRITIQUE_RESULT))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert critique.rationale == "spec is implementable"
    assert len(stub_client.calls) == 1
    assert len(stub_client.reasoning_calls) == 1
    # The reasoning pass runs strictly before the formatting pass, and the
    # formatting call receives the reasoning prose in its prompt.
    assert stub_client.call_order == ["reasoning", "formatting"]
    assert stub_client._REASONING_PROSE in stub_client.calls[0]["prompt"]


def test_structured_success_logs_outcome_succeeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The design-review happy path emits an INFO ``outcome=succeeded`` marker
    so the resend-free path is observable in production logs."""
    stub_client = _StubClient(dict(_SAMPLE_CRITIQUE_RESULT))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    logger_name = "investment_team.strategy_lab.agents.design_review"
    with caplog.at_level(logging.INFO, logger=logger_name):
        DesignReviewAgent().run(_spec(), readiness_results=[])

    succeeded = [r for r in caplog.records if "outcome=succeeded" in r.message]
    assert len(succeeded) == 1
    assert "agent=strategy_design_review" in succeeded[0].message
    assert "phase=design_review_structured" in succeeded[0].message


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient(dict(_SAMPLE_CRITIQUE_RESULT))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    DesignReviewAgent().run(_spec(), readiness_results=[])

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] == CRITIQUE_SCHEMA
    assert call["system_prompt"] == design_review_mod._get_system_prompt()
    assert "Review the strategy specification below" in call["prompt"]
    # Formatting pass: thinking off, deterministic (temperature 0.0).
    assert call["think"] is False
    assert call["temperature"] == 0.0

    # The reasoning pass is where this prompt is actually reviewed — the
    # formatting call above only re-embeds the same user_prompt alongside the
    # reasoning prose, so pin the prompt-content assertion there too.
    assert len(stub_client.reasoning_calls) == 1
    reasoning_call = stub_client.reasoning_calls[0]
    assert "Review the strategy specification below" in reasoning_call["prompt"]
    # The reasoning call gets the prose-only system-prompt variant and a
    # trailing override that neutralizes the shared template's "Return ONLY
    # a JSON object" directive — without it the reasoning pass would emit
    # JSON instead of prose, defeating the two-call split.
    assert (
        reasoning_call["system_prompt"]
        == design_review_mod._get_system_prompt() + so_mod.REASONING_MODE_SUFFIX
    )
    assert reasoning_call["prompt"].endswith(so_mod._REASONING_USER_PROMPT_SUFFIX)
    # Reasoning pass: thinking on, with the (higher, more exploratory)
    # reasoning-specific temperature — distinct from the formatting pass.
    assert reasoning_call["think"] is True
    assert reasoning_call["temperature"] == so_mod._DEFAULT_REASONING_TEMPERATURE
    assert reasoning_call["temperature"] != call["temperature"]


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
        return dict(_SAMPLE_CRITIQUE_RESULT)

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(so_mod, "run_structured_agent", _fake_run_structured_agent)

    budget = LLMCallBudget(limit=10)
    with use_budget(budget):
        DesignReviewAgent().run(_spec(), readiness_results=[])

    assert captured["agent_key"] == "strategy_design_review"
    assert captured["phase"] == "design_review_structured"
    # ``invoke_structured_with_schema`` always forwards charge=False to the
    # envelope; per-provider-call charging happens inside its ``_call`` when
    # charge=True. This spy skips ``_call``, so the active budget is untouched.
    assert captured["charge"] is False
    assert budget.calls_made == 0


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``_structured_output_available`` seam — routes to
    the legacy call for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")

    def _raise_if_structured_invoked(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(
            "invoke_structured_with_schema must not run for a provider that doesn't "
            "support structured output"
        )

    monkeypatch.setattr(so_mod, "invoke_structured_with_schema", _raise_if_structured_invoked)
    agent = _ScriptedAgent(['{"ready": true, "rationale": "spec is implementable", "issues": []}'])
    monkeypatch.setattr(design_review_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_review_mod, "Agent", lambda **_k: agent)

    budget = LLMCallBudget(limit=10)
    with use_budget(budget):
        critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert agent.calls == 1
    # No structured-path provider call or pre-charge on this path — only the
    # legacy call's single charge.
    assert budget.calls_made == 1


# ---------------------------------------------------------------------------
# Degrade: schema_forced starvation
# ---------------------------------------------------------------------------


def test_schema_forced_starvation_degrades_to_legacy_call_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    agent = _ScriptedAgent(['{"ready": true, "rationale": "spec is implementable", "issues": []}'])
    monkeypatch.setattr(design_review_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_review_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design_review"
    budget = LLMCallBudget(limit=10)
    starved_client.budget = budget
    with caplog.at_level(logging.WARNING, logger=logger_name):
        with use_budget(budget):
            critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    # The reasoning call succeeded (returned prose) before the schema_forced
    # formatting call raised — this is a formatting-pass starvation, not a
    # reasoning-pass one.
    assert starved_client.reasoning_ran is True
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced_degrade" in r.message]
    assert len(starvation_warnings) == 1
    # Reasoning + formatting charges both land before/at the failing
    # formatting call (charge-before-call); legacy fallback adds a third.
    assert starved_client.calls_made_at_formatting_call == 2
    assert budget.calls_made == 3


def test_reasoning_pass_starvation_also_degrades_to_legacy_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-schema_forced ``LLMSemanticExhaustionError`` from the *reasoning*
    pass (``complete()``) is re-raised by ``invoke_structured_with_schema`` as
    a new ``schema_forced=True`` receipt, so it degrades identically to a
    formatting-pass starvation — and the formatting call (``complete_json``)
    is never reached."""

    class _ReasoningStarvingClient:
        def __init__(self) -> None:
            self.budget: Any = None
            self.calls_made_at_reasoning_call: int | None = None

        def complete(self, prompt: str, **kwargs: Any) -> str:
            if self.budget is not None:
                self.calls_made_at_reasoning_call = self.budget.calls_made
            raise LLMSemanticExhaustionError(
                "reasoning starved", schema_forced=False, attempts_used=1
            )

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("formatting call must not run after a reasoning-pass failure")

    starved_client = _ReasoningStarvingClient()
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    agent = _ScriptedAgent(['{"ready": true, "rationale": "spec is implementable", "issues": []}'])
    monkeypatch.setattr(design_review_mod, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(design_review_mod, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.design_review"
    budget = LLMCallBudget(limit=10)
    starved_client.budget = budget
    with caplog.at_level(logging.WARNING, logger=logger_name):
        with use_budget(budget):
            critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is True
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced_degrade" in r.message]
    assert len(starvation_warnings) == 1
    # Only the reasoning unit is charged before the failing reasoning call;
    # the formatting charge never happens because complete_json is never reached.
    assert starved_client.calls_made_at_reasoning_call == 1
    # 2 real provider calls: structured reasoning (failed) + legacy fallback.
    assert budget.calls_made == 2


# ---------------------------------------------------------------------------
# No degrade: a non-schema_forced fatal failure still falls closed
# ---------------------------------------------------------------------------


def test_non_schema_forced_permanent_error_falls_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine (non-schema_forced) failure from the structured attempt is
    re-raised out of ``_invoke`` and still lands in the SAME unchanged
    ``_fail_closed_critique`` handler ``run()`` already had — not a hole,
    not a silent degrade."""
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in issue.description for issue in critique.issues)


def test_non_schema_forced_semantic_exhaustion_falls_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate checks ``.schema_forced`` specifically, not just the exception
    type — an ordinary (non-schema-forced) semantic exhaustion must NOT
    degrade to the legacy call either; it still falls closed."""
    exhausted_client = _FailingClient(
        LLMSemanticExhaustionError(
            "empty, but not schema forced", schema_forced=False, attempts_used=1
        )
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(exhausted_client))
    monkeypatch.setattr(design_review_mod, "Agent", _raise_if_agent_built)

    critique = DesignReviewAgent().run(_spec(), readiness_results=[])

    assert critique.ready is False
    assert any("review_parse_error" in issue.description for issue in critique.issues)


# ---------------------------------------------------------------------------
# _structured_output_available() — direct unit coverage of the seam
# ---------------------------------------------------------------------------


def test_structured_output_available_true_for_ollama_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``structured_output_available()`` returns True for the ollama provider."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert so_mod.structured_output_available() is True


def test_structured_output_available_false_for_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``structured_output_available()`` returns False for the dummy provider."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert so_mod.structured_output_available() is False


def test_structured_output_available_false_for_bedrock_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``structured_output_available()`` returns False for the bedrock provider."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert so_mod.structured_output_available() is False
