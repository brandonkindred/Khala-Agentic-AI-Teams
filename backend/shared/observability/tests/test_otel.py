"""Smoke tests for the shared.observability OpenTelemetry bootstrap.

These tests verify the public contract of the shared.observability module
without requiring a real OTLP collector. They use the in-memory span
exporter so every assertion runs fully offline.

OpenTelemetry refuses to replace its global tracer provider once it has
been set. All tests therefore share one provider (configured by the
session-scoped ``_otel_ready`` fixture) and make assertions that are
independent of the specific service name used.
"""

from __future__ import annotations

import pytest

_SERVICE_NAME = "unit-test-team"
_TEAM_KEY = "unit_test"


@pytest.fixture(scope="session", autouse=True)
def _otel_ready() -> None:
    """Initialise OpenTelemetry once for the whole test session."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from shared.observability import init_otel

    init_otel(service_name=_SERVICE_NAME, team_key=_TEAM_KEY)


@pytest.fixture()
def span_exporter():
    """Yield a fresh InMemorySpanExporter attached to the current provider."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_init_otel_reports_enabled() -> None:
    from shared.observability import init_otel, is_otel_enabled

    assert init_otel(service_name=_SERVICE_NAME, team_key=_TEAM_KEY) is True
    assert is_otel_enabled() is True


def test_resolve_endpoint_for_log_reflects_export_state(monkeypatch) -> None:
    """The init log must distinguish 'no exporter' from 'real endpoint'."""
    from shared.observability.otel import _resolve_endpoint_for_log

    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)

    rendered = _resolve_endpoint_for_log()
    assert "<default>" not in rendered
    assert "none" in rendered.lower()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert _resolve_endpoint_for_log() == "http://collector:4318"

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4318")
    assert _resolve_endpoint_for_log() == "http://traces:4318"


def test_metric_exporter_opted_out_via_metrics_exporter_none(monkeypatch) -> None:
    """OTEL_METRICS_EXPORTER=none suppresses OTLP metric export even with an endpoint.

    Traces still ship to the OTLP endpoint (e.g. Tempo), but metrics stay off so a
    traces-only collector is not flooded with rejected metric exports.
    """
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from shared.observability.otel import (
        _build_metric_exporter,
        _build_span_exporter,
        _otlp_metrics_enabled,
    )

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)

    # Opted out: metrics suppressed, traces still export. OTEL_METRICS_EXPORTER is
    # a comma-separated list per the OTel spec, so "none" anywhere disables it.
    for val in ("none", " none ", "none,console", "otlp,none", "NONE"):
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", val)
        assert _otlp_metrics_enabled() is False, val
        assert _build_metric_exporter() is None, val
    assert _build_span_exporter() is not None

    # A list without "none" does not disable it.
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp,console")
    assert _otlp_metrics_enabled() is True

    # Default (var unset): endpoint configured → metric exporter is built.
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    assert _otlp_metrics_enabled() is True
    assert _build_metric_exporter() is not None


def test_get_tracer_and_meter_surface_is_usable() -> None:
    """Tracer and meter returned by the helpers must support the common API."""
    from shared.observability import get_meter, get_tracer

    tracer = get_tracer("unit-test")
    with tracer.start_as_current_span("noop") as span:
        span.set_attribute("foo", "bar")

    meter = get_meter("unit-test")
    counter = meter.create_counter("noop_counter")
    histogram = meter.create_histogram("noop_histogram")
    counter.add(1, {"team": _TEAM_KEY})
    histogram.record(42, {"team": _TEAM_KEY})


def test_span_attribute_exceeding_default_length_limit_is_truncated(span_exporter) -> None:
    """Non-compose runtime paths (pytest included) never source docker-compose.yml's
    OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT default, so init_otel must wire its own 2048
    default into the TracerProvider's SpanLimits directly (see _build_span_limits)."""
    from shared.observability import get_tracer

    tracer = get_tracer("unit-test-span-limits")
    with tracer.start_as_current_span("khala.test.oversized_attribute") as span:
        span.set_attribute("khala.test.long_value", "x" * 3000)

    spans = span_exporter.get_finished_spans()
    target = next(s for s in spans if s.name == "khala.test.oversized_attribute")
    attributes = dict(target.attributes)
    assert len(attributes["khala.test.long_value"]) == 2048


def test_build_span_limits_honors_env_overrides(monkeypatch) -> None:
    """OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT / OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT must override
    the 2048/64 defaults. init_otel is a session-wide singleton, so overrides can't be
    exercised through it once the session provider exists — this hits the resolution
    helper directly instead."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from shared.observability.otel import _build_span_limits

    monkeypatch.delenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", raising=False)
    monkeypatch.delenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", raising=False)
    default_limits = _build_span_limits()
    assert default_limits.max_attribute_length == 2048
    assert default_limits.max_span_attributes == 64

    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "100")
    monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "5")
    overridden = _build_span_limits()
    assert overridden.max_attribute_length == 100
    assert overridden.max_span_attributes == 5


def test_build_span_limits_clamps_garbage_and_negative_values(monkeypatch) -> None:
    """Garbage falls back to the documented default; a negative value clamps to 0 —
    matches CLAUDE.md's numeric-env-var contract, same as every other parse_int call
    site in this codebase."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from shared.observability.otel import _build_span_limits

    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "not-a-number")
    assert _build_span_limits().max_attribute_length == 2048

    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "-5")
    assert _build_span_limits().max_attribute_length == 0


def test_instrument_fastapi_app_attaches_server_spans(span_exporter) -> None:
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    pytest.importorskip("fastapi")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shared.observability import instrument_fastapi_app

    app = FastAPI()
    instrument_fastapi_app(app, team_key=_TEAM_KEY)

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/ping")
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    assert spans, "Expected FastAPI instrumentation to emit at least one span"
    root = next((s for s in spans if s.name == "GET /ping"), None)
    assert root is not None, f"Expected 'GET /ping' span, got {[s.name for s in spans]}"
    resource_attrs = dict(root.resource.attributes)
    assert resource_attrs.get("khala.team") == _TEAM_KEY
    assert resource_attrs.get("service.name")  # set, any value from first init_otel


def test_instrument_fastapi_app_is_idempotent(span_exporter, caplog) -> None:
    """A second instrument call is a quiet no-op — no re-instrumentation, and none
    of opentelemetry's 'already instrumented' WARNING that muddies crash logs."""
    import logging

    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    pytest.importorskip("fastapi")

    from fastapi import FastAPI

    from shared.observability import instrument_fastapi_app

    app = FastAPI()
    instrument_fastapi_app(app, team_key=_TEAM_KEY)
    assert getattr(app, "_khala_otel_instrumented", False) is True

    with caplog.at_level(logging.WARNING):
        instrument_fastapi_app(app, team_key=_TEAM_KEY)  # second call

    assert not any(
        "already instrumented" in record.getMessage().lower() for record in caplog.records
    ), "second instrument call must not trigger the 'already instrumented' warning"


def test_llm_service_record_call_emits_otel_span(span_exporter) -> None:
    import llm_service.telemetry as telemetry_module

    # Force the lazy instruments to re-resolve against the active provider.
    telemetry_module._otel_initialized = False
    telemetry_module._otel_tracer = None

    telemetry_module.record_llm_call(
        team=_TEAM_KEY,
        agent_key="unit_test_agent",
        model="test-model",
        caller_tag="tests.unit",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        latency_ms=4,
        status="success",
    )

    spans = span_exporter.get_finished_spans()
    llm_spans = [s for s in spans if s.name.startswith("llm.call")]
    assert llm_spans, f"Expected at least one llm.call span, got {[s.name for s in spans]}"

    attributes = dict(llm_spans[0].attributes)
    assert attributes["khala.team"] == _TEAM_KEY
    assert attributes["khala.agent_key"] == "unit_test_agent"
    assert attributes["llm.request.model"] == "test-model"
    assert attributes["llm.usage.total_tokens"] == 3
    assert attributes["llm.status"] == "success"
