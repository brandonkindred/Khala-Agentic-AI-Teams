"""Tests for the Strategy Lab unified LLM fault-tolerance envelope.

The envelope is exercised directly with plain callables (no strands needed):
backoff/retry, fatal-vs-retriable classification, per-call wall-clock timeout,
total wall-time budget, structured five-field logging, and env-var resolution.
Plus fail-closed regressions for the two safety-critical surfaces (the
near-miss adjudicator guard and the alignment fix-proposer) and a design-budget
wiring check that transport retries do not double-charge the per-cycle budget.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import httpx
import pytest

from investment_team.strategy_lab.agents import _llm_envelope as env
from investment_team.strategy_lab.agents._llm_budget import (
    DesignBudgetExhausted,
    LLMCallBudget,
    use_budget,
)
from investment_team.strategy_lab.agents._llm_envelope import (
    _backoff_delay,
    _call_with_timeout,
    _EnvelopeTimeout,
    _is_rate_limit_kind,
    _resolve_config,
    classify_strands_exception,
    invoke_agent,
    run_structured_agent,
)
from investment_team.strategy_lab.exceptions import StrategyLabLLMError
from llm_service.interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSemanticExhaustionError,
    LLMTemporaryError,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    """Patch the envelope's ``time.sleep`` to record (and skip) backoff waits."""
    waits: List[float] = []
    monkeypatch.setattr(env.time, "sleep", lambda s: waits.append(s))
    return waits


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


# ---------------------------------------------------------------------------
# invoke_agent — happy paths and retries
# ---------------------------------------------------------------------------


def test_returns_on_first_success() -> None:
    stub = _Stub("hello")
    out = invoke_agent(stub, "p", agent_key="strategy_design", phase="x")
    assert out == "hello"
    assert stub.calls == 1


def test_flaky_then_success_retries_with_backoff(no_sleep: List[float]) -> None:
    stub = _Stub(httpx.ConnectError("boom"), "ok")
    out = invoke_agent(
        stub, "p", agent_key="strategy_design", phase="design_generate", max_attempts=3
    )
    assert out == "ok"
    assert stub.calls == 2
    # Exactly one backoff sleep between the two attempts.
    assert len(no_sleep) == 1
    assert no_sleep[0] >= 0


def test_str_coercion_of_non_string_result() -> None:
    stub = _Stub(12345)
    assert invoke_agent(stub, "p", agent_key="k", phase="x") == "12345"


def test_exhaustion_raises_after_max_attempts(
    no_sleep: List[float], caplog: pytest.LogCaptureFixture
) -> None:
    stub = _Stub(httpx.ConnectError("down"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StrategyLabLLMError) as ei:
            invoke_agent(
                stub, "p", agent_key="strategy_ideation", phase="refinement", max_attempts=3
            )
    assert stub.calls == 3
    assert ei.value.outcome == "exhausted"
    assert ei.value.attempts == 3
    assert ei.value.last_error_class == "ConnectError"
    # One structured five-field line per failed attempt.
    failure_lines = [r for r in caplog.records if "strategy_lab LLM call failed" in r.message]
    assert len(failure_lines) == 3
    text = caplog.text
    for field in ("agent=", "phase=", "attempt=", "latency_ms=", "error_class=ConnectError"):
        assert field in text


def test_fatal_classification_raises_immediately(
    no_sleep: List[float], caplog: pytest.LogCaptureFixture
) -> None:
    stub = _Stub(LLMPermanentError("nope"))
    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(stub, "p", agent_key="k", phase="x", max_attempts=5)
    assert stub.calls == 1
    assert ei.value.outcome == "fatal"
    assert no_sleep == []  # never backed off


def test_design_budget_exhausted_inside_callable_propagates_without_retry(
    no_sleep: List[float], caplog: pytest.LogCaptureFixture
) -> None:
    """A charge trip inside ``agent_callable`` must escape unmodified.

    Unknown exceptions default to retriable; without a dedicated carve-out,
    ``DesignBudgetExhausted`` would be retried and wrapped as
    ``StrategyLabLLMError``, hiding the cycle-level stop from callers that
    catch it distinctly (e.g. ``DesignReviewAgent.run``).
    """
    trip = DesignBudgetExhausted(limit=1, calls_made=1)
    stub = _Stub(trip)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DesignBudgetExhausted) as ei:
            invoke_agent(stub, "p", agent_key="k", phase="x", max_attempts=5)
    assert ei.value is trip
    assert stub.calls == 1
    assert no_sleep == []
    assert not any("strategy_lab LLM call failed" in r.message for r in caplog.records)


def test_custom_classifier_is_used(no_sleep: List[float]) -> None:
    stub = _Stub(RuntimeError("weird"))
    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(
            stub,
            "p",
            agent_key="k",
            phase="x",
            max_attempts=4,
            retriable_classifier=lambda _exc: False,
        )
    assert stub.calls == 1
    assert ei.value.outcome == "fatal"


# ---------------------------------------------------------------------------
# Timeout + budget
# ---------------------------------------------------------------------------


def test_wall_clock_timeout_guard_fires() -> None:
    def _slow(_prompt: str) -> str:
        time.sleep(1.0)
        return "never"

    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(_slow, "p", agent_key="k", phase="x", max_attempts=1, timeout_s=0.05)
    assert ei.value.last_error_class == "_EnvelopeTimeout"


def test_total_budget_stops_before_attempts_exhausted() -> None:
    stub = _Stub(httpx.ConnectError("down"))
    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(
            stub,
            "p",
            agent_key="k",
            phase="x",
            max_attempts=1000,
            timeout_s=10.0,
            total_budget_s=0.05,
            backoff_base=1.0,
            backoff_max=0.02,
        )
    assert ei.value.outcome == "budget_exhausted"
    assert stub.calls < 1000


# ---------------------------------------------------------------------------
# _call_with_timeout
# ---------------------------------------------------------------------------


def test_call_with_timeout_returns_value() -> None:
    assert _call_with_timeout(lambda: 7, 1.0) == 7


def test_call_with_timeout_propagates_error() -> None:
    def _boom() -> Any:
        raise ValueError("x")

    with pytest.raises(ValueError):
        _call_with_timeout(_boom, 1.0)


def test_call_with_timeout_raises_envelope_timeout() -> None:
    with pytest.raises(_EnvelopeTimeout):
        _call_with_timeout(lambda: time.sleep(1.0), 0.05)


# ---------------------------------------------------------------------------
# _backoff_delay
# ---------------------------------------------------------------------------


def test_backoff_delay_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env.random, "uniform", lambda _a, _b: 0.0)
    assert _backoff_delay(0, 2.0, 60.0) == 1.0
    assert _backoff_delay(3, 2.0, 60.0) == 8.0
    monkeypatch.setattr(env.random, "uniform", lambda _a, _b: 1.0)
    assert _backoff_delay(0, 2.0, 60.0) == 2.0
    # Cap applies.
    assert _backoff_delay(10, 2.0, 5.0) == 5.0


# ---------------------------------------------------------------------------
# classify_strands_exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (LLMTemporaryError("5xx"), True),
        (
            # Subclasses LLMTemporaryError, but the client already proved the
            # payload yields no content even after a reduced-thinking retry —
            # macro-retrying it would re-burn the thinking budget per attempt.
            LLMSemanticExhaustionError(
                "no content",
                attempts_used=2,
                original_thinking_level="max",
                retry_thinking_level="high",
                content_bytes_seen=False,
                payload_fingerprint="abc123",
            ),
            False,
        ),
        (LLMPermanentError("4xx"), False),
        (LLMJsonParseError("bad"), False),
        (httpx.ConnectError("c"), True),
        (httpx.ReadTimeout("t"), True),
        (ConnectionError("net"), True),
        (TimeoutError("slow"), True),
        (_EnvelopeTimeout(1.0), True),
        (LLMRateLimitError("429 slow down"), True),
        (LLMRateLimitError(OLLAMA_WEEKLY_LIMIT_MESSAGE), False),
        (LLMError("server", status_code=503), True),
        (LLMError("teapot", status_code=418), False),
        (LLMError("missing", status_code=404), False),
        (LLMError("rate", status_code=429), True),
        (type("FooAuthError", (Exception,), {})("unauthorized token"), False),
        (type("FooTimeout", (Exception,), {})("read timed out"), True),
        (RuntimeError("totally unknown"), True),
    ],
)
def test_classify_strands_exception(exc: BaseException, expected: bool) -> None:
    assert classify_strands_exception(exc) is expected


# ---------------------------------------------------------------------------
# 429 rate-limit schedule (slow backoff, separate from transient)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (LLMRateLimitError("429 slow down", status_code=429), True),
        (LLMError("rate", status_code=429), True),
        (RuntimeError("ModelThrottledException: too fast"), True),
        (RuntimeError("rate limit exceeded"), True),
        (LLMRateLimitError(OLLAMA_WEEKLY_LIMIT_MESSAGE, status_code=429), False),
        (httpx.ConnectError("net"), False),
        (LLMError("server", status_code=503), False),
        (RuntimeError("totally unknown"), False),
    ],
)
def test_is_rate_limit_kind(exc: BaseException, expected: bool) -> None:
    assert _is_rate_limit_kind(exc) is expected


def test_envelope_rate_limit_uses_slow_schedule(
    no_sleep: List[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retriable 429 backs off on the slow rate-limit schedule (~300s), not ~1-2s."""
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX", "3600")
    stub = _Stub(LLMRateLimitError("rate limited", status_code=429), "ok")
    out = invoke_agent(
        stub,
        "p",
        agent_key="strategy_design",
        phase="design_generate",
        max_attempts=2,
        total_budget_s=5000.0,  # large so the 300s delay is not budget-clamped
    )
    assert out == "ok"
    assert stub.calls == 2
    assert len(no_sleep) == 1
    assert no_sleep[0] >= 300.0


def test_envelope_transient_uses_fast_schedule(
    no_sleep: List[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient fault keeps the fast schedule even when the rate-limit floor is huge."""
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    stub = _Stub(httpx.ConnectError("boom"), "ok")
    out = invoke_agent(
        stub,
        "p",
        agent_key="strategy_design",
        phase="design_generate",
        max_attempts=2,
        backoff_base=2.0,
        backoff_max=60.0,
        total_budget_s=5000.0,
    )
    assert out == "ok"
    assert len(no_sleep) == 1
    assert no_sleep[0] <= 5.0  # fast transient backoff, not the 300s rate-limit floor


def test_envelope_weekly_cap_is_fatal(no_sleep: List[float]) -> None:
    """A weekly-usage cap 429 is fatal: no retry, no backoff sleep."""
    stub = _Stub(LLMRateLimitError(OLLAMA_WEEKLY_LIMIT_MESSAGE, status_code=429))
    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(stub, "p", agent_key="k", phase="x", max_attempts=5)
    assert ei.value.outcome == "fatal"
    assert stub.calls == 1
    assert no_sleep == []


def test_envelope_rate_limit_delay_clamped_to_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 300s rate-limit delay is clamped to the remaining budget and terminates the loop.

    Does NOT patch time.sleep — the clamp means the real sleep is tiny (<= the
    0.05s budget), proving the loop never actually waits 300s.
    """
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "300")
    stub = _Stub(LLMRateLimitError("rate limited", status_code=429))
    with pytest.raises(StrategyLabLLMError) as ei:
        invoke_agent(
            stub,
            "p",
            agent_key="k",
            phase="x",
            max_attempts=1000,
            timeout_s=10.0,
            total_budget_s=0.05,
        )
    assert ei.value.outcome == "budget_exhausted"
    assert stub.calls < 1000


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_resolve_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "STRATEGY_LAB_LLM_MAX_RETRIES",
        "LLM_MAX_RETRIES",
        "STRATEGY_LAB_LLM_TIMEOUT",
        "LLM_TIMEOUT",
        "STRATEGY_LAB_LLM_BACKOFF_BASE",
        "LLM_BACKOFF_BASE",
        "STRATEGY_LAB_LLM_BACKOFF_MAX",
        "LLM_BACKOFF_MAX",
        "STRATEGY_LAB_LLM_TOTAL_BUDGET",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = _resolve_config("strategy_ideation", None, None, None, None, None)
    assert cfg.max_attempts == 3  # 2 retries + 1
    assert cfg.timeout_s == 3600.0
    assert cfg.backoff_base == 2.0
    assert cfg.backoff_max == 60.0
    assert cfg.total_budget_s == pytest.approx(3 * 3600.0 * 1.5)


def test_resolve_config_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_LLM_MAX_RETRIES", "garbage")
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", "")
    monkeypatch.setenv("STRATEGY_LAB_LLM_BACKOFF_BASE", "not-a-number")
    cfg = _resolve_config("strategy_ideation", None, None, None, None, None)
    assert cfg.max_attempts == 3
    assert cfg.backoff_base == 2.0


def test_resolve_config_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("STRATEGY_LAB_LLM_TIMEOUT", "12.5")
    cfg = _resolve_config("strategy_ideation", None, None, None, None, None)
    assert cfg.max_attempts == 6
    assert cfg.timeout_s == 12.5
    # Explicit args win over env.
    cfg2 = _resolve_config("strategy_ideation", 2, 3.0, 9.0, 1.5, 4.0)
    assert cfg2.max_attempts == 2
    assert cfg2.timeout_s == 3.0
    assert cfg2.total_budget_s == 9.0


def test_resolve_config_floors_sub_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _resolve_config("k", 0, 0.0, 0.0, 0.0, -5.0)
    assert cfg.max_attempts == 1
    assert cfg.timeout_s >= 0.001
    assert cfg.total_budget_s >= 0.001
    assert cfg.backoff_base >= 1.0
    assert cfg.backoff_max >= 0.0


def test_resolve_config_rate_limit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL",
        "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX",
        "LLM_RATE_LIMIT_BACKOFF_INITIAL",
        "LLM_RATE_LIMIT_BACKOFF_MAX",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = _resolve_config("strategy_ideation", None, None, None, None, None)
    assert cfg.rl_initial == 30.0
    assert cfg.rl_cap == 120.0


def test_resolve_config_rate_limit_cascade_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Global LLM_RATE_LIMIT_* provides defaults; STRATEGY_LAB_* overrides win.
    monkeypatch.setenv("LLM_RATE_LIMIT_BACKOFF_INITIAL", "200")
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", "450")
    # A cap below the resolved initial is floored up so the schedule stays valid.
    monkeypatch.setenv("STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX", "100")
    cfg = _resolve_config("strategy_ideation", None, None, None, None, None)
    assert cfg.rl_initial == 450.0
    assert cfg.rl_cap >= cfg.rl_initial


# ---------------------------------------------------------------------------
# Fail-closed regressions on the safety-critical surfaces
# ---------------------------------------------------------------------------


def _fake_agent_class(stub: _Stub) -> type:
    """Build a stand-in for ``strands.Agent`` that delegates to ``stub``."""

    class _FakeAgent:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __call__(self, prompt: str) -> Any:
            return stub(prompt)

    return _FakeAgent


def test_near_miss_consult_fails_closed_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    from investment_team.strategy_lab.alignment_findings import NearMissVerdict
    from investment_team.strategy_lab.quality_gates.alignment_checks import (
        DeterministicAlignmentChecker,
    )

    gate = DeterministicAlignmentChecker()

    def _raising_adjudicator(**_kwargs: Any) -> NearMissVerdict:
        raise httpx.ConnectError("adjudicator unreachable")

    evaluation = {"rule_id": "r1", "predicate_repr": "rsi < 30", "lhs": 30.1, "rhs": 30.0}
    trade = type("T", (), {"symbol": "AAPL", "entry_date": "2023-01-03"})()

    with caplog.at_level(logging.WARNING):
        verdict = gate._consult_near_miss(_raising_adjudicator, evaluation, trade)

    assert verdict.legitimate is False
    text = caplog.text
    # Uses the envelope's canonical _FAILURE_FMT — one schema across the lab,
    # no bespoke "(fail-closed near-miss)" prefix.
    assert "strategy_lab LLM call failed:" in text
    assert "attempt=1/1" in text
    assert "phase=alignment_near_miss" in text
    assert "error_class=ConnectError" in text


def test_propose_code_fix_fails_closed_after_envelope_retries(
    monkeypatch: pytest.MonkeyPatch, no_sleep: List[float]
) -> None:
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.agents import _agent_runner as agent_runner_mod
    from investment_team.strategy_lab.agents.alignment import (
        AlignmentAuditError,
        TradeAlignmentAgent,
    )
    from investment_team.strategy_lab.alignment_findings import AlignmentFinding
    from investment_team.strategy_lab.spec_dsl import (
        DEFAULT_SIZING_PAYLOAD,
        EntryRule,
        IndicatorRef,
        Predicate,
    )

    stub = _Stub(httpx.ConnectError("alignment LLM down"))
    monkeypatch.setattr(agent_runner_mod, "Agent", _fake_agent_class(stub))
    monkeypatch.setattr(agent_runner_mod, "get_strands_model", lambda *_a, **_k: None)
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_RETRIES", "2")

    spec = StrategySpec(
        strategy_id="t-1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="rsi(14) < 30",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0
                ),
            )
        ],
        exit_rules=[],
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=["AAPL"],
    )
    findings = [
        AlignmentFinding(trade_num=1, check_name="entry_signal", passed=False, severity="critical")
    ]

    with pytest.raises(AlignmentAuditError):
        TradeAlignmentAgent().propose_code_fix(spec=spec, code="code", findings=findings)
    # STRATEGY_LAB_ALIGNMENT_RETRIES=2 → 3 envelope attempts.
    assert stub.calls == 3


def test_design_invoke_charges_once_despite_transport_retry(
    monkeypatch: pytest.MonkeyPatch, no_sleep: List[float]
) -> None:
    from investment_team.strategy_lab.agents import _agent_runner as agent_runner_mod
    from investment_team.strategy_lab.agents import _structured_output as so_mod
    from investment_team.strategy_lab.agents import design as design_mod

    # The test session's conftest pins LLM_MAX_RETRIES=0; enable one transport
    # retry so the flaky-then-success path is exercised.
    monkeypatch.setenv("STRATEGY_LAB_LLM_MAX_RETRIES", "2")
    stub = _Stub(httpx.ConnectError("flaky"), '{"asset_class": "stocks", "rationale": "r"}')
    charges = {"n": 0}
    # Exercise the legacy unconstrained loop directly (this test calls
    # `_invoke_and_parse` without going through the structured pre-flight).
    # The loop itself now delegates to `_agent_runner.run_json_with_parse_retry`,
    # which builds its `Agent` via that module's own `Agent`/`get_strands_model`
    # names — patch those rather than `design_mod`'s.
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)
    monkeypatch.setattr(agent_runner_mod, "Agent", _fake_agent_class(stub))
    monkeypatch.setattr(agent_runner_mod, "get_strands_model", lambda *_a, **_k: None)
    monkeypatch.setattr(design_mod, "validate_structured_rules", lambda _parsed: None)
    # `charge_active_budget` is invoked directly by the driver's `before_attempt`
    # hook using the name bound in `design_mod`'s own namespace, not via the
    # envelope module's internal charge call (which is now bypassed — the
    # driver always calls `run_structured_agent(..., charge=False, ...)`).
    monkeypatch.setattr(
        design_mod,
        "charge_active_budget",
        lambda: charges.__setitem__("n", charges["n"] + 1),
    )

    agent = design_mod.DesignAgent()
    parsed, rationale = agent._invoke_and_parse("system", "user")

    assert parsed == {"asset_class": "stocks"}
    assert rationale == "r"
    # Budget charged once (one parse iteration); the transport retry inside the
    # envelope must NOT re-charge.
    assert charges["n"] == 1
    assert stub.calls == 2


# ---------------------------------------------------------------------------
# run_structured_agent
# ---------------------------------------------------------------------------


def test_run_structured_agent_happy_path_no_coerce() -> None:
    stub = _Stub('{"a": 1}')
    result = run_structured_agent(
        stub,
        "p",
        agent_key="strategy_design",
        phase="x",
        parse=lambda raw: {"parsed": raw},
        charge=False,
    )
    assert result == {"parsed": '{"a": 1}'}


def test_run_structured_agent_applies_coerce_when_given() -> None:
    stub = _Stub("raw")
    result = run_structured_agent(
        stub,
        "p",
        agent_key="strategy_design",
        phase="x",
        parse=lambda raw: raw.upper(),
        coerce=lambda parsed: f"coerced:{parsed}",
        charge=False,
    )
    assert result == "coerced:RAW"


def test_run_structured_agent_charge_true_charges_before_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []

    monkeypatch.setattr(env, "charge_active_budget", lambda: calls.append("charge"))

    def _recording_agent(prompt: str) -> str:
        calls.append("invoke")
        return "raw"

    result = run_structured_agent(
        _recording_agent,
        "p",
        agent_key="strategy_design",
        phase="x",
        parse=lambda raw: raw,
        charge=True,
    )

    assert result == "raw"
    assert calls == ["charge", "invoke"]


def test_run_structured_agent_charge_false_never_charges(monkeypatch: pytest.MonkeyPatch) -> None:
    charges = {"n": 0}
    monkeypatch.setattr(
        env, "charge_active_budget", lambda: charges.__setitem__("n", charges["n"] + 1)
    )
    run_structured_agent(
        _Stub("raw"),
        "p",
        agent_key="strategy_design",
        phase="x",
        parse=lambda raw: raw,
        charge=False,
    )
    assert charges["n"] == 0


def test_run_structured_agent_parse_exception_propagates_unmodified() -> None:
    def _boom(_raw: str) -> Any:
        raise ValueError("bad json")

    with pytest.raises(ValueError, match="bad json"):
        run_structured_agent(
            _Stub("raw"), "p", agent_key="strategy_design", phase="x", parse=_boom, charge=False
        )


def test_run_structured_agent_coerce_exception_propagates_unmodified() -> None:
    def _boom(_parsed: Any) -> Any:
        raise RuntimeError("bad coerce")

    with pytest.raises(RuntimeError, match="bad coerce"):
        run_structured_agent(
            _Stub("raw"),
            "p",
            agent_key="strategy_design",
            phase="x",
            parse=lambda raw: raw,
            coerce=_boom,
            charge=False,
        )


def test_run_structured_agent_coerce_none_returns_parsed_verbatim() -> None:
    sentinel = object()
    result = run_structured_agent(
        _Stub("raw"),
        "p",
        agent_key="strategy_design",
        phase="x",
        parse=lambda _raw: sentinel,
        coerce=None,
        charge=False,
    )
    assert result is sentinel


def test_run_structured_agent_design_budget_exhausted_propagates_uncaught() -> None:
    """Charging must never be caught inside the helper — a caller wrapping the
    whole call in ``except Exception`` still sees ``DesignBudgetExhausted``
    escape unmodified when the helper itself adds no swallowing.
    """
    budget = LLMCallBudget(limit=1)
    budget.charge()  # exhaust the single admitted charge before the helper runs
    with use_budget(budget):
        with pytest.raises(DesignBudgetExhausted):
            run_structured_agent(
                _Stub("raw"),
                "p",
                agent_key="strategy_design",
                phase="x",
                parse=lambda raw: raw,
                charge=True,
            )


def test_run_structured_agent_agent_key_flows_through_to_invoke_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}
    real_invoke_agent = env.invoke_agent

    def _spy(agent_callable, prompt, *, agent_key, phase, **kwargs):
        captured["agent_key"] = agent_key
        captured["phase"] = phase
        return real_invoke_agent(agent_callable, prompt, agent_key=agent_key, phase=phase, **kwargs)

    monkeypatch.setattr(env, "invoke_agent", _spy)
    run_structured_agent(
        _Stub("raw"),
        "p",
        agent_key="strategy_zero_trade_repair",
        phase="zero_trade_repair",
        parse=lambda raw: raw,
        charge=False,
    )
    assert captured["agent_key"] == "strategy_zero_trade_repair"
    assert captured["phase"] == "zero_trade_repair"


# ---------------------------------------------------------------------------
# model_factory transport-timeout construction
# ---------------------------------------------------------------------------


def test_accepts_kwarg_ignores_var_keyword() -> None:
    """A name reachable only via ``**kwargs`` must NOT count as accepted — the
    strands constructors swallow such keys into ``**model_config`` and warn.
    """
    from investment_team.strategy_lab.agents.model_factory import _accepts_kwarg

    class _Explicit:
        def __init__(self, ollama_client_args=None) -> None: ...

    class _OnlyVarKw:
        def __init__(self, **model_config) -> None: ...

    assert _accepts_kwarg(_Explicit, "ollama_client_args") is True
    assert _accepts_kwarg(_OnlyVarKw, "ollama_client_args") is False
    assert _accepts_kwarg(_Explicit, "nope") is False
    # An un-introspectable target degrades to False rather than raising.
    assert _accepts_kwarg(42, "anything") is False


def test_bedrock_construct_forwards_timeout_via_boto_config() -> None:
    from investment_team.strategy_lab.agents.model_factory import (
        _construct_bedrock_with_timeout,
    )

    class _Model:
        def __init__(self, model_id=None, boto_client_config=None) -> None:
            self.model_id = model_id
            self.boto_client_config = boto_client_config

    # A fractional timeout must be preserved verbatim (regression: the old code
    # did int(timeout), truncating 12.5 -> 12 and a sub-second value -> 0).
    model = _construct_bedrock_with_timeout(_Model, 12.5, model_id="m")
    assert model.model_id == "m"
    assert model.boto_client_config is not None
    assert model.boto_client_config.read_timeout == 12.5
    assert model.boto_client_config.connect_timeout == 12.5


def test_bedrock_construct_falls_back_when_no_channel() -> None:
    from investment_team.strategy_lab.agents.model_factory import (
        _construct_bedrock_with_timeout,
    )

    class _Model:
        def __init__(self, model_id=None, **model_config) -> None:
            self.model_id = model_id
            self.model_config = model_config

    model = _construct_bedrock_with_timeout(_Model, 30.0, model_id="m")
    assert model.model_id == "m"
    assert "boto_client_config" not in model.model_config
