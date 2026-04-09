"""Shared OpenTelemetry helpers for all Strands agent teams.

Every team microservice calls ``init_otel()`` once at startup and
``instrument_fastapi_app(app)`` right after creating its FastAPI instance.
That gives every team:

* A tracer provider with OTLP export (HTTP or gRPC) when
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, otherwise a no-op provider that
  keeps ``get_tracer()`` / ``get_meter()`` usable without side effects.
* A meter provider with the same exporter.
* Automatic FastAPI request instrumentation (server spans, route
  attributes, status codes).
* Automatic httpx client instrumentation (outgoing LLM / integration
  calls appear as child spans).
* Automatic logging instrumentation (trace_id / span_id are injected
  into every log record so logs can be correlated with traces).

Everything is best-effort: if the OpenTelemetry packages are missing
(for example in a minimal test environment) the helpers degrade to
no-ops instead of raising, so nothing blocks startup.

Environment variables:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — collector endpoint. When empty the
  SDK is still configured but defaults to the standard localhost
  collector; set ``OTEL_SDK_DISABLED=true`` to force full no-op mode.
* ``OTEL_EXPORTER_OTLP_PROTOCOL`` — ``http/protobuf`` (default) or ``grpc``.
* ``OTEL_SERVICE_NAME`` — overrides the service name passed to
  ``init_otel``.
* ``OTEL_RESOURCE_ATTRIBUTES`` — standard OTel resource attributes
  (e.g. ``deployment.environment=prod``).

Usage from a team's ``api/main.py``::

    from shared_observability import init_otel, instrument_fastapi_app

    init_otel(service_name="blogging-team", team_key="blogging")

    app = FastAPI(...)
    instrument_fastapi_app(app, team_key="blogging")
"""

from shared_observability.otel import (
    get_meter,
    get_tracer,
    init_otel,
    instrument_fastapi_app,
    is_otel_enabled,
    shutdown_otel,
)

__all__ = [
    "get_meter",
    "get_tracer",
    "init_otel",
    "instrument_fastapi_app",
    "is_otel_enabled",
    "shutdown_otel",
]
