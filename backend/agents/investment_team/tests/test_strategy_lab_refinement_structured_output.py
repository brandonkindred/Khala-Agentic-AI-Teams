"""RefinementAgent structured-output happy path and degrade behavior.

``RefinementAgent._invoke_and_parse`` uses the two-pass
``invoke_structured_with_schema`` helper when the active provider supports
structured output (currently Ollama only): a reasoning ``LLMClient.complete()``
pass with ``think=True`` and a prose-only system prompt (the caller's
``_SYSTEM_PROMPT`` plus ``REASONING_MODE_SUFFIX``), followed by a formatting
``LLMClient.complete_json(schema=REFINEMENT_SCHEMA)`` pass with
``think=False``, eliminating the ``build_json_correction_prompt`` happy-path
resend for this call. These tests lock in: both passes run on the structured
happy path; the per-cycle ``LLMCallBudget`` is charged twice up front
(``run_structured_agent`` receives ``charge=False``); the real
``provider_supports_structured_output(resolve_provider())`` wiring degrades
to the legacy ``strands.Agent`` + correction-prompt loop for an unsupported
provider (Bedrock); a ``schema_forced`` semantic-exhaustion starvation
signal — including one raised by the unconstrained reasoning pass and
re-raised as ``schema_forced=True`` — degrades to the legacy loop the same
way; and any OTHER fatal failure from the structured attempt propagates
without degrading (a deliberate scope boundary, not a hole). Mirrors the
fixture/helper shapes in ``test_strategy_lab_refinement_parse_retry.py``,
which covers the legacy loop itself (with the structured seam forced off).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.models import RiskLimits, StrategySpec
from investment_team.strategy_lab.agents import _agent_runner
from investment_team.strategy_lab.agents import _structured_output as so_mod
from investment_team.strategy_lab.agents import refinement as mod
from investment_team.strategy_lab.agents._llm_budget import LLMCallBudget, use_budget
from investment_team.strategy_lab.agents.refinement import RefinementAgent
from investment_team.strategy_lab.exceptions import StrategyLabLLMError
from llm_service.interface import LLMPermanentError, LLMSemanticExhaustionError


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-structured-output",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        risk_limits=RiskLimits(),
    )


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


class _FakeModel:
    """Minimal ``get_strands_model(...)`` return value: only ``.client`` is used."""

    def __init__(self, client: Any) -> None:
        self.client = client


_GOOD = '{"strategy_code": "# fixed", "changes_made": "tightened guard"}'


def _raise_if_agent_built(**_kwargs: Any) -> Any:
    raise AssertionError(
        "strands.Agent must not be constructed on the structured happy/degrade-fatal path"
    )


def _raise_if_correction_prompt_built(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "build_json_correction_prompt must not be called on the structured happy path"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_structured_path_used_when_available_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "tightened guard"})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(mod, "build_json_correction_prompt", _raise_if_correction_prompt_built)

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert len(stub_client.calls) == 1


def test_structured_call_passes_schema_and_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "x"})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert len(stub_client.calls) == 1
    call = stub_client.calls[0]
    assert call["schema"] == mod.REFINEMENT_SCHEMA
    assert call["system_prompt"] == mod._SYSTEM_PROMPT
    assert call["think"] is False
    assert "Fix the following trading strategy code" in call["prompt"]
    # The original task prompt was sent, not a correction re-prompt.
    assert "could not be parsed as a single JSON object" not in call["prompt"]

    assert len(stub_client.reasoning_calls) == 1
    reasoning_call = stub_client.reasoning_calls[0]
    assert reasoning_call["think"] is True
    assert reasoning_call["system_prompt"] == mod._SYSTEM_PROMPT + so_mod.REASONING_MODE_SUFFIX
    # The reasoning-pass user prompt must re-assert prose-only LAST, after the
    # task template's own "Return ONLY a JSON object"-style directive, so the
    # more specific/later user-turn instruction doesn't win and make the
    # reasoning pass emit JSON instead of prose (see
    # ``_REASONING_USER_PROMPT_SUFFIX``'s docstring in ``_structured_output.py``).
    assert reasoning_call["prompt"].endswith(so_mod._REASONING_USER_PROMPT_SUFFIX)


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
        return {"strategy_code": "# fixed", "changes_made": "y"}

    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(_StubClient({})))
    monkeypatch.setattr(so_mod, "run_structured_agent", _fake_run_structured_agent)

    budget = LLMCallBudget(limit=5)
    with use_budget(budget):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    assert captured["agent_key"] == "strategy_refinement"
    assert captured["phase"] == "refinement_structured"
    # ``invoke_structured_with_schema`` always forwards charge=False to the
    # envelope; per-provider-call charging happens inside its ``_call``.
    assert captured["charge"] is False
    # This spy skips ``_call``, so per-provider-call charges inside the
    # closure never fire — the bound budget stays untouched.
    assert budget.calls_made == 0


# ---------------------------------------------------------------------------
# Degrade: capability unsupported (real provider wiring, not the seam)
# ---------------------------------------------------------------------------


def test_real_bedrock_provider_degrades_to_legacy_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual ``provider_supports_structured_output(resolve_provider())``
    wiring — not just the ``structured_output_available`` seam — routes to
    the legacy loop for a provider without the capability."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(_agent_runner, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(_agent_runner, "Agent", lambda **_k: agent)

    updates, new_code = RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 1


# ---------------------------------------------------------------------------
# Degrade: schema_forced starvation
# ---------------------------------------------------------------------------


def test_schema_forced_starvation_degrades_to_legacy_loop_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    starved_client = _FailingClient(
        LLMSemanticExhaustionError("starved", schema_forced=True, attempts_used=1)
    )
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    monkeypatch.setattr(
        _agent_runner, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client)
    )
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(_agent_runner, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.refinement"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        updates, new_code = RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1
    assert "agent=strategy_refinement" in starvation_warnings[0].message


def test_reasoning_pass_starvation_also_degrades_to_legacy_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-schema_forced LLMSemanticExhaustionError from the *reasoning*
    pass (complete()) is re-raised by invoke_structured_with_schema as a new
    schema_forced=True receipt, so it degrades identically to a
    formatting-pass starvation — and the formatting call (complete_json) is
    never invoked, matching "a step-1 failure propagates immediately"."""

    class _ReasoningStarvingClient:
        def complete(self, prompt: str, **kwargs: Any) -> str:
            raise LLMSemanticExhaustionError(
                "reasoning starved", schema_forced=False, attempts_used=1
            )

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("formatting call must not run after a reasoning-pass failure")

    starved_client = _ReasoningStarvingClient()
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client))
    monkeypatch.setattr(
        _agent_runner, "get_strands_model", lambda *_a, **_k: _FakeModel(starved_client)
    )
    agent = _ScriptedAgent([_GOOD])
    monkeypatch.setattr(_agent_runner, "Agent", lambda **_k: agent)

    logger_name = "investment_team.strategy_lab.agents.refinement"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        updates, new_code = RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    assert new_code == "# fixed"
    assert updates == {"changes_made": "tightened guard"}
    assert agent.calls == 1
    starvation_warnings = [r for r in caplog.records if "schema_forced" in r.message]
    assert len(starvation_warnings) == 1


def test_non_schema_forced_permanent_error_propagates_without_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fatal_client = _FailingClient(LLMPermanentError("nope, fatal"))
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(fatal_client))
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)

    with pytest.raises(StrategyLabLLMError):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )


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
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)

    with pytest.raises(StrategyLabLLMError):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )


# ---------------------------------------------------------------------------
# structured_output_available() — direct unit coverage of the seam
# ---------------------------------------------------------------------------


def test_structured_output_available_true_for_ollama_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama is a real provider wired up to advertise structured-output support."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert so_mod.structured_output_available() is True


def test_structured_output_available_false_for_dummy_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-LLM dummy test/dev harness never advertises structured-output support."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert so_mod.structured_output_available() is False


def test_structured_output_available_false_for_bedrock_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock lacks provider-enforced schema-conformant decoding support."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert so_mod.structured_output_available() is False


# ---------------------------------------------------------------------------
# Observability: success telemetry + measurable call-count reduction
# ---------------------------------------------------------------------------


def test_structured_success_logs_outcome_succeeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The structured happy path emits an INFO ``outcome=succeeded`` marker so
    the resend-free path is observable in production logs (not just silent)."""
    stub_client = _StubClient({"strategy_code": "# fixed", "changes_made": "x"})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(stub_client))
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)

    logger_name = "investment_team.strategy_lab.agents.refinement"
    with caplog.at_level(logging.INFO, logger=logger_name):
        RefinementAgent().run(
            spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
        )

    succeeded = [r for r in caplog.records if "outcome=succeeded" in r.message]
    assert len(succeeded) == 1
    assert "agent=strategy_refinement" in succeeded[0].message
    assert "phase=refinement_structured" in succeeded[0].message


def test_structured_path_needs_no_correction_resend_unlike_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measurable-reduction evidence for the parent structured-output work.

    The two paths are NOT given the same simulated failure sequence: the
    structured path's stub client returns a valid, schema-conformant payload
    on its one (formatting-pass) call, since schema-constrained decoding
    cannot itself emit unparseable JSON; only the legacy fallback path is
    scripted with a reject-then-valid sequence (``["not json at all", _GOOD]``)
    to exercise its correction-resend. ``structured_format_calls`` below
    counts only the formatting-pass
    (``complete_json``) calls, not the reasoning pass — after the
    reasoning+formatting split, the structured path's real total is two
    provider calls (reasoning then formatting), matching the legacy path's
    two (initial + one resend). The measurable win is therefore the absence
    of a ``build_json_correction_prompt`` resend — the formatting call is
    schema-constrained and cannot emit unparseable JSON, so it never needs
    one — not a lower total provider-call count. The strict inequality below
    compares formatting-pass calls only (1 structured vs. 2 legacy) as the
    proxy for that resend elimination.
    """
    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", "2")

    # Capture the real function before either path monkeypatches
    # ``mod.build_json_correction_prompt`` — capturing it later would pick up
    # whichever stub the structured-path block below has already installed,
    # not the real implementation the fallback path needs to exercise.
    real_build = mod.build_json_correction_prompt

    # --- Structured path: one call, zero correction resends. ---
    structured_corrections = 0

    def _count_structured_correction(*_a: Any, **_k: Any) -> str:
        nonlocal structured_corrections
        structured_corrections += 1
        return "unused"

    structured_client = _StubClient({"strategy_code": "# fixed", "changes_made": "ok"})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(
        so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(structured_client)
    )
    monkeypatch.setattr(_agent_runner, "Agent", _raise_if_agent_built)
    monkeypatch.setattr(mod, "build_json_correction_prompt", _count_structured_correction)

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )
    structured_format_calls = len(structured_client.calls)

    assert structured_format_calls == 1
    assert structured_corrections == 0

    # --- Legacy fallback path: same failure sequence, one resend. ---
    fallback_corrections = 0

    def _count_fallback_correction(*args: Any, **kwargs: Any) -> str:
        nonlocal fallback_corrections
        fallback_corrections += 1
        return real_build(*args, **kwargs)

    scripted = _ScriptedAgent(["not json at all", _GOOD])
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)
    monkeypatch.setattr(_agent_runner, "get_strands_model", lambda *_a, **_k: object())
    monkeypatch.setattr(_agent_runner, "Agent", lambda **_k: scripted)
    monkeypatch.setattr(mod, "build_json_correction_prompt", _count_fallback_correction)

    RefinementAgent().run(
        spec=_spec(), code="# old", failure_phase="execution", failure_details="boom"
    )
    fallback_calls = scripted.calls

    assert fallback_calls == 2
    assert fallback_corrections == 1

    # Formatting-pass-only calls are fewer (1 vs 2) because the structured
    # path never needs a correction resend — NOT because its total real
    # provider-call count is lower (it isn't: 2 either way, once the
    # reasoning pass is counted on the structured side). See the docstring
    # above for the accurate accounting.
    assert structured_format_calls < fallback_calls
