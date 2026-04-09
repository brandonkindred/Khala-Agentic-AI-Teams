"""Smoke tests for the shared_observability OpenTelemetry bootstrap.

These tests verify the public contract of the shared_observability module
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
    from shared_observability import init_otel

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
    from shared_observability import init_otel, is_otel_enabled

    assert init_otel(service_name=_SERVICE_NAME, team_key=_TEAM_KEY) is True
    assert is_otel_enabled() is True


def test_get_tracer_and_meter_surface_is_usable() -> None:
    """Tracer and meter returned by the helpers must support the common API."""
    from shared_observability import get_meter, get_tracer

    tracer = get_tracer("unit-test")
    with tracer.start_as_current_span("noop") as span:
        span.set_attribute("foo", "bar")

    meter = get_meter("unit-test")
    counter = meter.create_counter("noop_counter")
    histogram = meter.create_histogram("noop_histogram")
    counter.add(1, {"team": _TEAM_KEY})
    histogram.record(42, {"team": _TEAM_KEY})


def test_instrument_fastapi_app_attaches_server_spans(span_exporter) -> None:
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    pytest.importorskip("fastapi")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shared_observability import instrument_fastapi_app

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
    assert resource_attrs.get("strands.team") == _TEAM_KEY
    assert resource_attrs.get("service.name")  # set, any value from first init_otel


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
    assert attributes["strands.team"] == _TEAM_KEY
    assert attributes["strands.agent_key"] == "unit_test_agent"
    assert attributes["llm.request.model"] == "test-model"
    assert attributes["llm.usage.total_tokens"] == 3
    assert attributes["llm.status"] == "success"
