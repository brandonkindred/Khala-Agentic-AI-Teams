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

from typing import Any

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


# ---------------------------------------------------------------------------
# Exporter selection branches
# ---------------------------------------------------------------------------


def test_build_span_exporter_selects_the_grpc_transport(monkeypatch) -> None:
    """OTEL_EXPORTER_OTLP_PROTOCOL=grpc picks the gRPC exporter over the HTTP default."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    from shared.observability.otel import _build_span_exporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")

    exporter = _build_span_exporter()
    try:
        assert isinstance(exporter, OTLPSpanExporter)
    finally:
        # Only shut down a real exporter — calling .shutdown() unconditionally
        # here would, on assertion failure, raise from inside the finally block
        # and replace the AssertionError as the reported failure.
        if isinstance(exporter, OTLPSpanExporter):
            exporter.shutdown()


def test_build_metric_exporter_selects_the_grpc_transport(monkeypatch) -> None:
    """The metric path honors the same transport selector as the span path."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.metric_exporter")
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    from shared.observability.otel import _build_metric_exporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)

    exporter = _build_metric_exporter()
    try:
        assert isinstance(exporter, OTLPMetricExporter)
    finally:
        # Only shut down a real exporter — calling .shutdown() unconditionally
        # here would, on assertion failure, raise from inside the finally block
        # and replace the AssertionError as the reported failure.
        if isinstance(exporter, OTLPMetricExporter):
            exporter.shutdown()


def test_build_span_exporter_returns_none_when_the_package_is_missing(monkeypatch) -> None:
    """A missing exporter package degrades to 'no export', never to a crash."""
    import sys

    from shared.observability.otel import _build_span_exporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)

    assert _build_span_exporter() is None


def test_build_metric_exporter_returns_none_when_the_package_is_missing(monkeypatch) -> None:
    """The metric path degrades the same way as the span path when its package is absent."""
    import sys

    from shared.observability.otel import _build_metric_exporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.metric_exporter", None)

    assert _build_metric_exporter() is None


def test_install_global_instrumentors_swallows_missing_packages(monkeypatch) -> None:
    """Instrumentation is best-effort: absent packages must not fail init."""
    import sys

    from shared.observability.otel import _install_global_instrumentors

    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.httpx", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.logging", None)

    _install_global_instrumentors()


# ---------------------------------------------------------------------------
# instrument_fastapi_app guards
# ---------------------------------------------------------------------------


def test_instrument_fastapi_app_is_a_no_op_when_otel_is_disabled(monkeypatch) -> None:
    """When OTel is disabled the app is left completely untouched — no sentinel either."""
    from shared.observability import otel as otel_module

    monkeypatch.setattr(otel_module, "_enabled", False)
    app = type("App", (), {})()

    otel_module.instrument_fastapi_app(app, team_key=_TEAM_KEY)

    assert not hasattr(app, "_khala_otel_instrumented")


def test_instrument_fastapi_app_swallows_instrumentor_failures(monkeypatch) -> None:
    """A broken instrumentor must not take the app's startup down with it."""
    import sys

    from shared.observability import otel as otel_module

    monkeypatch.setattr(otel_module, "_enabled", True)
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", None)
    app = type("App", (), {})()

    otel_module.instrument_fastapi_app(app, team_key=_TEAM_KEY)

    assert not getattr(app, "_khala_otel_instrumented", False)


# ---------------------------------------------------------------------------
# get_tracer / get_meter fallbacks
# ---------------------------------------------------------------------------


def test_get_tracer_falls_back_to_a_noop_tracer(monkeypatch) -> None:
    """Callers may request a tracer unconditionally, SDK present or not."""
    import sys

    from shared.observability.otel import _NoopTracer, get_tracer

    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    assert isinstance(get_tracer("anything"), _NoopTracer)


def test_get_meter_falls_back_to_a_noop_meter(monkeypatch) -> None:
    """Same unconditional-call contract as get_tracer, on the metrics side."""
    import sys

    from shared.observability.otel import _NoopMeter, get_meter

    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    assert isinstance(get_meter("anything"), _NoopMeter)


def test_get_tracer_and_get_meter_return_sdk_objects_when_available() -> None:
    """The fallbacks are a last resort — a live SDK must yield real instruments."""
    from shared.observability.otel import _NoopMeter, _NoopTracer, get_meter, get_tracer

    assert not isinstance(get_tracer("shared.observability.tests"), _NoopTracer)
    assert not isinstance(get_meter("shared.observability.tests"), _NoopMeter)


# ---------------------------------------------------------------------------
# shutdown_otel
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Provider double that records how many times shutdown() is invoked."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FailingProvider:
    """Provider double whose shutdown() raises, simulating an already-closed exporter."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        raise RuntimeError("exporter already closed")


def test_shutdown_otel_flushes_both_providers(monkeypatch) -> None:
    """The happy path: each provider is flushed exactly once."""
    from shared.observability import otel as otel_module

    tracer_provider = _RecordingProvider()
    meter_provider = _RecordingProvider()
    monkeypatch.setattr(otel_module, "_tracer_provider", tracer_provider)
    monkeypatch.setattr(otel_module, "_meter_provider", meter_provider)

    otel_module.shutdown_otel()

    assert tracer_provider.shutdown_calls == 1
    assert meter_provider.shutdown_calls == 1


def test_shutdown_otel_swallows_provider_failures(monkeypatch) -> None:
    """Shutdown runs from a lifespan hook; it must never raise on the way out.

    Asserts both providers were actually invoked, not just that no exception
    escaped — otherwise the test would pass just as well if shutdown_otel
    stopped calling shutdown() on either provider entirely.
    """
    from shared.observability import otel as otel_module

    tracer_provider = _FailingProvider()
    meter_provider = _FailingProvider()
    monkeypatch.setattr(otel_module, "_tracer_provider", tracer_provider)
    monkeypatch.setattr(otel_module, "_meter_provider", meter_provider)

    otel_module.shutdown_otel()

    assert tracer_provider.shutdown_calls == 1
    assert meter_provider.shutdown_calls == 1


def test_shutdown_otel_is_safe_before_initialization(monkeypatch) -> None:
    """A lifespan hook may fire even when init never ran."""
    from shared.observability import otel as otel_module

    monkeypatch.setattr(otel_module, "_tracer_provider", None)
    monkeypatch.setattr(otel_module, "_meter_provider", None)

    otel_module.shutdown_otel()


def test_shutdown_otel_skips_providers_without_a_shutdown_hook(monkeypatch) -> None:
    """A provider that predates the shutdown protocol is skipped, not called blindly."""
    from shared.observability import otel as otel_module

    monkeypatch.setattr(otel_module, "_tracer_provider", object())
    monkeypatch.setattr(otel_module, "_meter_provider", object())

    otel_module.shutdown_otel()


# ---------------------------------------------------------------------------
# No-op fallbacks (used when the SDK is unavailable)
# ---------------------------------------------------------------------------


def test_noop_span_is_a_context_manager_that_absorbs_every_call() -> None:
    """Every span method is absorbed, so callers need no SDK guard."""
    from shared.observability.otel import _NoopSpan

    with _NoopSpan() as span:
        assert isinstance(span, _NoopSpan)
        assert span.set_attribute("k", "v") is None
        assert span.set_status("ok") is None
        assert span.record_exception(RuntimeError("boom")) is None
        assert span.end() is None


def test_noop_span_exit_does_not_suppress_exceptions() -> None:
    """The no-op must behave like a real span: it records, it never swallows."""
    from shared.observability.otel import _NoopSpan

    with pytest.raises(RuntimeError, match="propagated"):
        with _NoopSpan():
            raise RuntimeError("propagated")


def test_noop_tracer_yields_noop_spans() -> None:
    """Both span factories hand back something safe to use as a context manager."""
    from shared.observability.otel import _NoopSpan, _NoopTracer

    tracer = _NoopTracer()

    assert isinstance(tracer.start_as_current_span("work"), _NoopSpan)
    assert isinstance(tracer.start_span("work"), _NoopSpan)


def test_noop_meter_yields_noop_instruments() -> None:
    """Every instrument factory yields a recorder that absorbs add/record."""
    from shared.observability.otel import _NoopInstrument, _NoopMeter

    meter = _NoopMeter()

    for instrument in (
        meter.create_counter("c"),
        meter.create_histogram("h"),
        meter.create_up_down_counter("u"),
    ):
        assert isinstance(instrument, _NoopInstrument)
        assert instrument.add(1) is None
        assert instrument.record(1) is None


# ---------------------------------------------------------------------------
# init_otel branches
#
# ``init_otel`` latches on the module-level ``_initialized`` flag, so each of
# these resets it (and the globals it writes) through ``monkeypatch`` — which
# restores them afterwards, leaving the session-wide provider from
# ``_otel_ready`` intact. OpenTelemetry refuses to replace an already-set
# global provider, so the providers built below are local objects that never
# displace the session's.
# ---------------------------------------------------------------------------


@pytest.fixture()
def reinitializable_otel(monkeypatch):
    """Let one test call ``init_otel`` again without leaking module state."""
    from shared.observability import otel as otel_module

    for attr in ("_initialized", "_enabled", "_tracer_provider", "_meter_provider"):
        monkeypatch.setattr(otel_module, attr, getattr(otel_module, attr))
    monkeypatch.setattr(otel_module, "_initialized", False)
    return otel_module


def test_init_otel_honors_otel_sdk_disabled(reinitializable_otel, monkeypatch) -> None:
    """The standard opt-out short-circuits before any provider is built.

    Compares before/after rather than asserting ``None``: the module globals
    are process-wide and another test in this file (the exporter-wiring test)
    may already have populated them, restored only when that test tears down —
    the ``reinitializable_otel`` fixture snapshots current values for
    restoration, it does not reset them to ``None`` on entry.
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
    tracer_provider_before = reinitializable_otel._tracer_provider
    meter_provider_before = reinitializable_otel._meter_provider

    assert reinitializable_otel.init_otel(service_name="x", team_key="x") is False
    assert reinitializable_otel.is_otel_enabled() is False
    assert reinitializable_otel._tracer_provider is tracer_provider_before
    assert reinitializable_otel._meter_provider is meter_provider_before


def test_init_otel_returns_false_when_the_sdk_is_missing(reinitializable_otel, monkeypatch) -> None:
    """A missing opentelemetry.sdk.trace degrades init to a False verdict, never a crash.

    Poisoning the ``sys.modules`` entry with None is how the absent package is
    simulated: it makes the ``from … import …`` inside init_otel raise ImportError
    without uninstalling anything.
    """
    import sys

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)

    assert reinitializable_otel.init_otel(service_name="x", team_key="x") is False
    assert reinitializable_otel.is_otel_enabled() is False


def _make_discarding_metric_exporter() -> Any:
    """Build a metric exporter that accepts and drops every batch, silently.

    Stands in for ``ConsoleMetricExporter`` in tests: a real ``MetricExporter``
    subclass (so the reader can read its temporality/aggregation preferences),
    but its ``export`` never prints the payload to stdout on provider shutdown.
    """
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    class _DiscardingMetricExporter(MetricExporter):
        def export(self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any) -> Any:
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> bool:
            return True

    return _DiscardingMetricExporter()


def test_init_otel_wires_exporters_when_they_resolve(reinitializable_otel, monkeypatch) -> None:
    """The exporter-present branches actually deliver spans, not just build providers."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    span_exporter = InMemorySpanExporter()
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(reinitializable_otel, "_build_span_exporter", lambda: span_exporter)
    monkeypatch.setattr(reinitializable_otel, "_build_metric_exporter", _make_discarding_metric_exporter)

    assert reinitializable_otel.init_otel(service_name="x", team_key="exporters") is True

    tracer_provider = reinitializable_otel._tracer_provider
    meter_provider = reinitializable_otel._meter_provider
    try:
        assert tracer_provider is not None and meter_provider is not None
        tracer = tracer_provider.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            pass
        tracer_provider.force_flush()
        assert span_exporter.get_finished_spans(), "span never reached the wired exporter"
    finally:
        # Stop the periodic metric-export thread this test just started, even
        # if an assertion above failed.
        if meter_provider is not None:
            meter_provider.shutdown()
        if tracer_provider is not None:
            tracer_provider.shutdown()


def test_init_otel_is_latched_after_the_first_call(reinitializable_otel, monkeypatch) -> None:
    """A second call returns the first call's verdict without re-running init.

    Uses ``reinitializable_otel`` to force ``_initialized`` False on entry, so the
    test's own first call is guaranteed to run the real init path rather than
    inheriting an already-latched module — otherwise the assertion below would
    pass even with a broken latch, since the first call would already be a no-op.
    The spy is the load-bearing half: returning the same verdict proves nothing on
    its own, since a broken latch would re-run initialization and still report the
    same result. An exporter rebuild is the cheapest observable side effect of that
    re-run, so its absence on the *second* call is what pins the latch.
    """
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

    build_calls: list[int] = []
    monkeypatch.setattr(reinitializable_otel, "_build_span_exporter", lambda: build_calls.append(1))

    try:
        first = reinitializable_otel.init_otel(service_name="ignored", team_key="ignored")
        assert build_calls == [1], "the test's own first call must run the real init path"

        second = reinitializable_otel.init_otel(service_name="also-ignored", team_key="also")

        assert second is first
        assert build_calls == [1], "a second call must not rebuild the exporter"
    finally:
        # This call's own first init_otel builds real (local, non-global) provider
        # objects — stop them, mirroring test_init_otel_wires_exporters_when_they_resolve.
        if reinitializable_otel._meter_provider is not None:
            reinitializable_otel._meter_provider.shutdown()
        if reinitializable_otel._tracer_provider is not None:
            reinitializable_otel._tracer_provider.shutdown()
