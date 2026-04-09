# shared_observability

Shared OpenTelemetry bootstrap for every Strands agent team. This module
is the single source of truth for tracing, metrics, and log-trace
correlation across the platform.

## What you get

Calling `init_otel()` once at process start and
`instrument_fastapi_app(app)` right after constructing a FastAPI app
gives a team microservice:

- OTLP tracer + meter providers (HTTP/protobuf by default, gRPC if
  `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`).
- FastAPI server spans with route templates, status codes, and request
  timing (`/health`, `/ready`, `/metrics` excluded).
- httpx client spans for every outbound call — so LLM requests and
  cross-team API calls nest under the triggering server span.
- `trace_id` / `span_id` attributes injected into Python log records via
  `LoggingInstrumentor` for correlation in log aggregators.
- A shared resource with `service.name`, `service.version`,
  `service.namespace=strands-agents`, and `strands.team=<team_key>`.

Everything is best-effort. If the `opentelemetry-*` packages are not
installed (for example in a minimal test env), the helpers log a warning
and return no-op tracers/meters. Teams do not need any `try`/`except`
wrappers.

## Minimal team wiring

```python
# my_team/api/main.py
from fastapi import FastAPI
from shared_observability import init_otel, instrument_fastapi_app

init_otel(service_name="my-team", team_key="my_team")

app = FastAPI(title="My Team API", version="1.0.0")
instrument_fastapi_app(app, team_key="my_team")
```

That is the entire contract. Any additional spans can be added with
`get_tracer(__name__).start_as_current_span(...)`.

## Environment variables

| Variable | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint (e.g. `http://otel-collector:4318`) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` (default) or `grpc` |
| `OTEL_SERVICE_NAME` | Overrides the service name passed to `init_otel` |
| `OTEL_RESOURCE_ATTRIBUTES` | Standard OTel resource attributes (e.g. `deployment.environment=prod`) |
| `OTEL_SDK_DISABLED` | Set to `true` to force no-op mode (useful in tests) |

The SDK honours every standard `OTEL_*` variable in addition to the
ones above — see the OpenTelemetry specification for the full list.

## LLM span emission

`llm_service.telemetry.record_llm_call` also emits an OpenTelemetry span
for every LLM invocation (status, latency, token counts, error type),
so every team's LLM traffic is visible in the trace backend without any
per-team wiring.
