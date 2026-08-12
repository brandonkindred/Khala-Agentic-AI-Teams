"""Tests for objective/request_id attribution in LLM telemetry records."""

import llm_service.telemetry as telemetry
from llm_service.telemetry import LLMCallRecord, record_llm_call


def test_record_carries_objective_and_request_id() -> None:
    rec = record_llm_call(
        team="job_matching",
        agent_key="ranker",
        model="m",
        caller_tag="ranker.agent.rank",
        objective="rank job candidates",
        request_id="abc123def456",
        total_tokens=42,
    )
    assert rec.objective == "rank job candidates"
    assert rec.request_id == "abc123def456"
    d = rec.to_dict()
    assert d["objective"] == "rank job candidates"
    assert d["request_id"] == "abc123def456"


def test_to_dict_omits_empty_objective_and_request_id() -> None:
    rec = LLMCallRecord(
        timestamp=0.0,
        team="t",
        agent_key="a",
        model="m",
        caller_tag="c",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_ms=0,
        status="success",
    )
    d = rec.to_dict()
    assert "objective" not in d
    assert "request_id" not in d


def test_otel_span_includes_attribution(monkeypatch) -> None:
    captured: dict = {}

    class _FakeSpan:
        def set_status(self, *a, **k) -> None:  # pragma: no cover - not exercised here
            pass

        def end(self) -> None:
            pass

    class _FakeTracer:
        def start_span(self, name, attributes=None):
            captured["name"] = name
            captured["attributes"] = attributes or {}
            return _FakeSpan()

    # Force the otel path on with a fake tracer; disable the real metric instruments.
    monkeypatch.setattr(telemetry, "_otel_initialized", True)
    monkeypatch.setattr(telemetry, "_otel_tracer", _FakeTracer())
    monkeypatch.setattr(telemetry, "_otel_llm_calls", None)
    monkeypatch.setattr(telemetry, "_otel_llm_tokens", None)
    monkeypatch.setattr(telemetry, "_otel_llm_latency", None)

    record_llm_call(
        team="blogging",
        agent_key="writer",
        model="m",
        objective="draft section",
        request_id="rid-7",
    )
    attrs = captured["attributes"]
    assert attrs["khala.objective"] == "draft section"
    assert attrs["khala.request_id"] == "rid-7"
    assert attrs["khala.agent_key"] == "writer"


def test_otel_emission_noop_without_tracer(monkeypatch) -> None:
    # When no tracer is available the emit path is a silent no-op (does not raise).
    monkeypatch.setattr(telemetry, "_otel_initialized", True)
    monkeypatch.setattr(telemetry, "_otel_tracer", None)
    rec = record_llm_call(team="t", agent_key="a", model="m", objective="x", request_id="y")
    assert rec.objective == "x"


def test_record_carries_task_phase_cost_outcome() -> None:
    rec = record_llm_call(
        team="software_engineering",
        agent_key="backend",
        model="deepseek-v4-pro:cloud",
        prompt_tokens=1000,
        completion_tokens=1000,
        job_id="job-1",
        task_id="task-7",
        phase="execution",
    )
    assert rec.task_id == "task-7"
    assert rec.phase == "execution"
    assert rec.cost_usd > 0  # auto-estimated from pricing for a known model
    assert rec.outcome == "success"
    d = rec.to_dict()
    assert d["task_id"] == "task-7"
    assert d["phase"] == "execution"
    assert d["cost_usd"] == rec.cost_usd
    assert d["outcome"] == "success"


def test_cost_defaults_to_estimate_but_can_be_overridden() -> None:
    rec = record_llm_call(
        team="t", agent_key="a", model="deepseek-v4-pro:cloud", prompt_tokens=1000, cost_usd=0.5
    )
    assert rec.cost_usd == 0.5


def test_outcome_defaults_to_status() -> None:
    rec = record_llm_call(team="t", agent_key="a", model="m", status="rate_limited")
    assert rec.outcome == "rate_limited"


def test_invalid_caller_cost_is_sanitized() -> None:
    # Negative / non-finite caller-supplied cost must not poison the record.
    assert record_llm_call(team="t", agent_key="a", model="m", cost_usd=-5.0).cost_usd == 0.0
    assert (
        record_llm_call(team="t", agent_key="a", model="m", cost_usd=float("inf")).cost_usd == 0.0
    )
    assert (
        record_llm_call(team="t", agent_key="a", model="m", cost_usd=float("nan")).cost_usd == 0.0
    )


def test_otel_span_includes_cost_and_phase(monkeypatch) -> None:
    captured: dict = {}

    class _FakeSpan:
        def set_status(self, *a, **k) -> None:  # pragma: no cover - not exercised here
            pass

        def end(self) -> None:
            pass

    class _FakeTracer:
        def start_span(self, name, attributes=None):
            captured["attributes"] = attributes or {}
            return _FakeSpan()

    monkeypatch.setattr(telemetry, "_otel_initialized", True)
    monkeypatch.setattr(telemetry, "_otel_tracer", _FakeTracer())
    monkeypatch.setattr(telemetry, "_otel_llm_calls", None)
    monkeypatch.setattr(telemetry, "_otel_llm_tokens", None)
    monkeypatch.setattr(telemetry, "_otel_llm_latency", None)
    monkeypatch.setattr(telemetry, "_otel_llm_cost", None)

    record_llm_call(
        team="software_engineering",
        agent_key="backend",
        model="deepseek-v4-pro:cloud",
        prompt_tokens=1000,
        completion_tokens=1000,
        job_id="job-1",
        task_id="task-7",
        phase="execution",
    )
    attrs = captured["attributes"]
    assert attrs["cost.usd"] > 0
    assert attrs["task.id"] == "task-7"
    assert attrs["phase"] == "execution"
    assert attrs["job.id"] == "job-1"
    assert attrs["agent.name"] == "backend"
    assert attrs["llm.input_tokens"] == 1000
    assert attrs["llm.output_tokens"] == 1000
    assert attrs["outcome"] == "success"


def test_observers_notified_and_unregister(monkeypatch) -> None:
    seen: list = []

    def observer(rec) -> None:
        seen.append(rec)

    telemetry.register_call_observer(observer)
    # Idempotent re-registration.
    telemetry.register_call_observer(observer)
    try:
        record_llm_call(team="t", agent_key="a", model="m")
        assert len(seen) == 1
    finally:
        telemetry.unregister_call_observer(observer)
    record_llm_call(team="t", agent_key="a", model="m")
    assert len(seen) == 1  # no new notifications after unregister


def test_observer_exception_is_swallowed() -> None:
    def boom(rec) -> None:
        raise RuntimeError("observer blew up")

    telemetry.register_call_observer(boom)
    try:
        rec = record_llm_call(team="t", agent_key="a", model="m")
        assert rec is not None  # call still succeeds despite the failing observer
    finally:
        telemetry.unregister_call_observer(boom)


def test_usage_summary_by_model_includes_token_splits() -> None:
    telemetry.clear_call_log()
    telemetry.record_llm_call(
        team="blogging",
        agent_key="writer",
        model="m1",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    telemetry.record_llm_call(
        team="blogging",
        agent_key="writer",
        model="m2",
        prompt_tokens=20,
        completion_tokens=5,
        total_tokens=25,
    )
    summary = telemetry.get_usage_summary(team="blogging", window_hours=24)
    assert summary["by_model"]["m1"] == {
        "calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert summary["by_model"]["m2"]["calls"] == 1
    assert "tokens" not in summary["by_model"]["m1"]


def test_usage_summary_window_hours_zero_is_all_time() -> None:
    telemetry.clear_call_log()
    rec = telemetry.record_llm_call(team="t", agent_key="a", model="m", total_tokens=1)
    rec.timestamp = 1.0  # far in the past
    summary = telemetry.get_usage_summary(window_hours=0)
    assert summary["total_calls"] >= 1
