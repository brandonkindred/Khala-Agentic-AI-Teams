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
