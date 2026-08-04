"""OpenTelemetry bootstrap used by every Khala agent team.

The public API is intentionally tiny:

* ``init_otel(service_name, team_key)`` — set global tracer/meter providers.
* ``instrument_fastapi_app(app, team_key)`` — attach FastAPI middleware.
* ``get_tracer(name)`` / ``get_meter(name)`` — obtain instruments anywhere.
* ``shutdown_otel()`` — flush exporters during graceful shutdown.
* ``is_otel_enabled()`` — returns ``True`` once initialization succeeded.

The module never raises on missing OpenTelemetry packages; it logs a
warning and falls back to no-op ``trace.get_tracer`` / ``metrics.get_meter``
calls. Teams can therefore import and call these helpers unconditionally.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_init_lock = threading.Lock()
_initialized: bool = False
_enabled: bool = False
_service_name: str = "khala"
_team_key: str = "unknown"
_tracer_provider: Any = None
_meter_provider: Any = None


def is_otel_enabled() -> bool:
    """Return True when init_otel successfully configured the SDK."""
    return _enabled


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ATTRIBUTE_LENGTH = 2048
_DEFAULT_MAX_SPAN_ATTRIBUTES = 64


def init_otel(
    *,
    service_name: str,
    team_key: str,
    service_version: str = "1.0.0",
) -> bool:
    """Configure global OpenTelemetry providers for this process.

    Safe to call multiple times — only the first call has an effect.
    Returns True if initialization succeeded, False if the OpenTelemetry
    SDK packages are missing or initialization was disabled.
    """
    global _initialized, _enabled, _service_name, _team_key, _tracer_provider, _meter_provider

    with _init_lock:
        if _initialized:
            return _enabled
        _initialized = True
        _service_name = os.environ.get("OTEL_SERVICE_NAME", service_name)
        _team_key = team_key

        if os.environ.get("OTEL_SDK_DISABLED", "").lower() in ("true", "1", "yes"):
            logger.info("OpenTelemetry disabled via OTEL_SDK_DISABLED; skipping init")
            _enabled = False
            return False

        try:
            from opentelemetry import metrics, trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except Exception as exc:
            logger.warning(
                "opentelemetry SDK packages not available (%s); tracing disabled",
                exc,
            )
            _enabled = False
            return False

        resource_attrs = {
            "service.name": _service_name,
            "service.version": service_version,
            "service.namespace": "khala",
            "khala.team": team_key,
        }
        # Let users add extra resource attributes via the standard OTel env.
        resource = Resource.create(resource_attrs)

        span_limits = _build_span_limits()

        span_exporter = _build_span_exporter()
        if span_exporter is not None:
            tracer_provider = TracerProvider(resource=resource, span_limits=span_limits)
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            trace.set_tracer_provider(tracer_provider)
            _tracer_provider = tracer_provider
        else:
            # Still install a TracerProvider so spans are created correctly
            # even when no exporter is reachable — cheap and useful for tests.
            tracer_provider = TracerProvider(resource=resource, span_limits=span_limits)
            trace.set_tracer_provider(tracer_provider)
            _tracer_provider = tracer_provider

        metric_exporter = _build_metric_exporter()
        if metric_exporter is not None:
            reader = PeriodicExportingMetricReader(metric_exporter)
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
            metrics.set_meter_provider(meter_provider)
            _meter_provider = meter_provider
        else:
            meter_provider = MeterProvider(resource=resource)
            metrics.set_meter_provider(meter_provider)
            _meter_provider = meter_provider

        _install_global_instrumentors()

        _enabled = True
        logger.info(
            "OpenTelemetry initialized: service=%s team=%s endpoint=%s",
            _service_name,
            team_key,
            _resolve_endpoint_for_log(),
        )
        return True


def _resolve_endpoint_for_log() -> str:
    """Render the configured OTLP endpoint for the init log line.

    When no endpoint is set we skip exporter construction entirely (see
    ``_otlp_endpoint_configured``), so logging ``<default>`` is misleading —
    nothing is exported. Surface that explicitly so operators can tell at
    a glance whether spans are leaving the process.
    """
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "<none; export disabled>"


def _otlp_endpoint_configured() -> bool:
    """True when an OTLP collector endpoint is explicitly configured.

    Checks the standard OTel env vars. When none are set we skip
    exporter construction entirely — otherwise the SDK defaults to
    ``http://localhost:4318`` and floods the logs with retry warnings
    on stacks (like Khala's default Prometheus+Grafana setup) that
    have no OTLP collector.
    """
    return any(
        os.environ.get(var, "").strip()
        for var in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        )
    )


def _build_span_limits() -> Any:
    """Resolve SpanLimits for the TracerProvider, matching docker-compose's defaults.

    Non-compose runtime paths (thread-mode / local-dev / pytest) never source
    docker-compose.yml's env block, so without this helper TracerProvider falls
    back to the SDK's own unbounded default (max_attribute_length=None) and a
    single oversized LLM prompt/response attribute can blow past Tempo's
    max_bytes_per_trace ingest cap. Mirrors docker-compose.yml's defaults
    (OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT:-2048 / OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT:-64)
    so behavior is identical under compose or thread-mode/pytest.

    IMPORTANT precedence note: SpanLimits.__init__ only consults the env var
    itself when the corresponding constructor argument is None — passing an
    explicit value bypasses its internal env lookup entirely. We resolve the
    env vars ourselves first (env wins, else our default — same idiom as
    _service_name above) so the *effective* precedence still favors the env var.

    Preconditions:
        - None.
    Postconditions:
        - Returns a SpanLimits with max_attribute_length from
          OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT (default 2048, clamped >= 0) and
          max_span_attributes from OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT (default 64,
          clamped >= 0). Never raises: a missing/garbage env value falls back
          to the documented default.
    """
    from opentelemetry.sdk.trace import SpanLimits

    from shared.env import parse_int

    max_attribute_length = parse_int("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", _DEFAULT_MAX_ATTRIBUTE_LENGTH, minimum=0)
    max_span_attributes = parse_int("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", _DEFAULT_MAX_SPAN_ATTRIBUTES, minimum=0)
    return SpanLimits(
        max_attribute_length=max_attribute_length,
        max_span_attributes=max_span_attributes,
    )


def _build_span_exporter() -> Any:
    """Pick an OTLP span exporter based on OTEL_EXPORTER_OTLP_PROTOCOL.

    Returns ``None`` when no OTLP endpoint is configured, so the caller
    installs an in-process-only TracerProvider with no exporter (spans
    still work; nothing is shipped off-box).
    """
    if not _otlp_endpoint_configured():
        logger.info(
            "OTLP endpoint not configured (OTEL_EXPORTER_OTLP_ENDPOINT unset); "
            "spans will not be exported."
        )
        return None
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").lower()
    try:
        if protocol in ("grpc",):
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        return OTLPSpanExporter()
    except Exception as exc:
        logger.warning("OTLP span exporter unavailable (%s); spans will not export", exc)
        return None


def _otlp_metrics_enabled() -> bool:
    """True when OTLP metric export should be wired up.

    Honors the standard ``OTEL_METRICS_EXPORTER`` selector: when set to
    ``none`` we skip OTLP metric export entirely even if an OTLP endpoint is
    configured. This lets a stack ship traces to an OTLP backend (e.g. Tempo,
    which ingests traces only) while keeping metrics on a separate path —
    the stack scrapes ``/metrics`` with Prometheus, so pushing OTLP metrics at a
    traces-only collector would just 404 and spam the logs.

    ``OTEL_METRICS_EXPORTER`` is, per the OTel spec, a comma-separated list of
    exporters; ``none`` appearing anywhere in it disables OTLP metric export
    (so ``none``, ``none,console``, ``otlp,none`` are all honored).
    """
    exporters = [
        e.strip().lower()
        for e in os.environ.get("OTEL_METRICS_EXPORTER", "").split(",")
        if e.strip()
    ]
    if "none" in exporters:
        return False
    return _otlp_endpoint_configured()


def _build_metric_exporter() -> Any:
    """Pick an OTLP metric exporter based on OTEL_EXPORTER_OTLP_PROTOCOL.

    Returns ``None`` when no OTLP endpoint is configured, or when OTLP metric
    export is opted out via ``OTEL_METRICS_EXPORTER=none``. Prometheus-based
    stacks scrape ``/metrics`` directly, so OTLP metric export is redundant
    there; see ``_otlp_endpoint_configured`` and ``_otlp_metrics_enabled``.
    """
    if not _otlp_metrics_enabled():
        return None
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").lower()
    try:
        if protocol in ("grpc",):
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
        return OTLPMetricExporter()
    except Exception as exc:
        logger.warning("OTLP metric exporter unavailable (%s); metrics will not export", exc)
        return None


def _install_global_instrumentors() -> None:
    """Instrument httpx outbound calls and python logging for trace correlation."""
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:
        logger.debug("httpx instrumentation unavailable: %s", exc)

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception as exc:
        logger.debug("logging instrumentation unavailable: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------


# Default span-exclusion list. Substring-matched by the instrumentor, so these
# exclude the infra endpoints (e.g. ``/metrics``, ``/health``) on every team app.
_DEFAULT_EXCLUDED_URLS = "health,healthz,ready,metrics"


def instrument_fastapi_app(
    app: Any, *, team_key: Optional[str] = None, excluded_urls: Optional[str] = None
) -> None:
    """Attach the FastAPI instrumentor to a team's app — idempotently.

    Teams that own their FastAPI app self-instrument at import time, and the
    generic team-service wrapper instruments again on every worker boot. The
    underlying ``FastAPIInstrumentor`` is *not* idempotent: a second call emits a
    confusing ``Attempting to instrument FastAPI app while already instrumented``
    WARNING that muddies crash debugging. A sentinel attribute makes the repeat
    call a quiet no-op instead.

    ``excluded_urls`` overrides the default span-exclusion list. Apps that host
    business routes whose path contains an excluded word (e.g. the unified API's
    ``/api/se/metrics`` alias) pass *anchored* patterns like ``^/metrics$`` so the
    scrape endpoint stays excluded while the business route is still traced.

    Preconditions:
        - ``app`` accepts attribute assignment (a FastAPI instance does).
    Postconditions:
        - When OpenTelemetry is enabled, ``app`` is instrumented exactly once
          across any number of calls and ``app._khala_otel_instrumented`` is True.
        - No-ops (returns without raising) when OpenTelemetry is not initialized,
          the instrumentor package is missing, or the app was already
          instrumented by a prior call.
    """
    if not _enabled:
        return
    if getattr(app, "_khala_otel_instrumented", False):
        logger.debug("FastAPI already instrumented for team=%s; skipping", team_key or _team_key)
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=excluded_urls or _DEFAULT_EXCLUDED_URLS,
        )
        app._khala_otel_instrumented = True
        logger.debug("FastAPI instrumented for team=%s", team_key or _team_key)
    except Exception as exc:
        logger.warning("FastAPI instrumentation failed: %s", exc)


# ---------------------------------------------------------------------------
# Tracer / Meter access
# ---------------------------------------------------------------------------


def get_tracer(name: str) -> Any:
    """Return a tracer by name. Falls back to the no-op tracer when SDK is absent."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:
        return _NoopTracer()


def get_meter(name: str) -> Any:
    """Return a meter by name. Falls back to a no-op meter when SDK is absent."""
    try:
        from opentelemetry import metrics

        return metrics.get_meter(name)
    except Exception:
        return _NoopMeter()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def shutdown_otel() -> None:
    """Flush exporters. Safe to call from a FastAPI lifespan shutdown hook."""
    global _tracer_provider, _meter_provider
    try:
        if _tracer_provider is not None and hasattr(_tracer_provider, "shutdown"):
            _tracer_provider.shutdown()
    except Exception:
        logger.debug("tracer provider shutdown failed", exc_info=True)
    try:
        if _meter_provider is not None and hasattr(_meter_provider, "shutdown"):
            _meter_provider.shutdown()
    except Exception:
        logger.debug("meter provider shutdown failed", exc_info=True)


# ---------------------------------------------------------------------------
# No-op fallbacks (used when SDK is unavailable)
# ---------------------------------------------------------------------------


class _NoopSpan:
    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, *_args: Any, **_kwargs: Any) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, *_args: Any, **_kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


class _NoopInstrument:
    def add(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _NoopMeter:
    def create_counter(self, *_args: Any, **_kwargs: Any) -> _NoopInstrument:
        return _NoopInstrument()

    def create_histogram(self, *_args: Any, **_kwargs: Any) -> _NoopInstrument:
        return _NoopInstrument()

    def create_up_down_counter(self, *_args: Any, **_kwargs: Any) -> _NoopInstrument:
        return _NoopInstrument()
