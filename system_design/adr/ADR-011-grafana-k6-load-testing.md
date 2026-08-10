# ADR-011 — Grafana k6 as the standard load-testing tool

- **Status**: Accepted
- **Date**: 2026-08-10
- **Owner**: Platform / Observability
- **Related**:
  - Sub-issue of the effort to re-measure unified-api's memory footprint under realistic load and
    right-size container limits + per-team httpx pools; this ADR is the design-decision deliverable
    that gated the concurrent load-test harness itself.
  - `docker/k6/load_test_unified_api.js` — the first concrete harness built on this decision.
  - `docker/docker-compose.yml`'s `k6` service (opt-in via the `load-test` Compose profile).
  - `docker/README.md`'s "Load Testing (k6)" section.
  - `docker/prometheus/`, `docker/grafana/` — the existing Prometheus + Grafana stack this decision
    builds on.

## Context

The repo had no reusable load-testing tooling. `docker/README.md`'s Verification section only had a
manual `for i in {1..20}; do curl -sf http://localhost:8888/health > /dev/null; done` loop, written
once to warm a Grafana dashboard's panels — not configurable, not reusable, and not designed to
generate or measure sustained concurrent load.

A real load generator was needed to validate unified-api memory/right-sizing changes against
realistic concurrent traffic across multiple proxied teams (`backend/unified_api/config.py`
`TEAM_CONFIGS`), runnable against the local `docker compose` stack, with configurable concurrency
and duration, and with throughput/latency reporting to sanity-check that load was actually applied.

The stack already runs Prometheus and Grafana (`docker/docker-compose.yml`), both from Grafana
Labs. `httpx` is a Python dependency already used elsewhere in the backend, which made a hand-rolled
`asyncio` + `httpx` script the path of least resistance — but that path requires hand-writing
percentile/throughput aggregation, connection-pool management, and CLI/env-var plumbing that a
purpose-built load-testing tool already provides.

## Decision

Standardize on **Grafana k6** as the load-testing tool for this repository going forward — not only
for this one harness, but as the default choice for any future load/perf-testing need.

Concretely:

- Load-test scripts are JavaScript files under `docker/k6/`, one per subsystem/surface under test
  (e.g. `docker/k6/load_test_unified_api.js`), committed alongside the `docker-compose.yml` stack
  they exercise.
- Each script reads its configuration (target base URL, concurrency, duration, and any
  subsystem-specific target list) from `__ENV`, so it is configurable both via k6's native
  `--vus`/`--duration`/`-e KEY=value` CLI flags and via environment variables — no custom argument
  parsing needed.
- k6 runs via the official `grafana/k6` Docker image, wired into `docker-compose.yml` as a service
  gated behind an opt-in Compose profile (`load-test`), so it never starts on a plain
  `docker compose up` and never becomes a dependency of the normal dev/test stack.
- Reporting relies on k6's built-in end-of-test summary (request counts/rate, latency percentiles)
  rather than custom aggregation code; scripts only add `thresholds`/`tags` where a specific pass/fail
  gate or per-target breakdown is actually needed.

## Rejected alternatives

- **Hand-rolled Python `asyncio` + `httpx` script.** Rejected because it requires writing and
  testing its own concurrency harness, percentile/throughput math, and CLI/env-var precedence
  logic — all of which k6 already provides, well-tested, out of the box. It would also need to live
  in `backend/` and clear the repo's 90%-coverage gate for genuinely new code, adding maintenance
  surface for a tool that has no production code paths (per the parent issue's own scope note).
- **Locust.** Also Python-based like the rejected hand-rolled option, so it inherits the same
  coverage/maintenance concerns, and its distributed master/worker model and web UI are overhead
  this repo's single-host `docker compose` stack doesn't need. Not already a dependency anywhere in
  the repo, unlike `httpx` — but that only mattered relative to the hand-rolled option, and doesn't
  outweigh k6 being purpose-built and vendor-aligned with the existing Grafana/Prometheus stack.

## Consequences

- The only new tooling dependency is the `grafana/k6` Docker image, pulled on demand only when the
  `load-test` profile is invoked — no new Python or Node package is installed into any service
  image, and no change to the 90%-coverage gate's scope.
- Future load-test scripts for other subsystems (e.g. a dedicated SE-pipeline or job-service load
  test) should follow the same `docker/k6/*.js` + opt-in profile pattern rather than each picking a
  different tool, keeping load-testing tooling consistent and discoverable in one place.
- Anyone extending or debugging a load-test script needs at least a passing familiarity with k6's
  JS-based scripting API (`k6/http`, `k6/metrics`, `__ENV`, `__VU`/`__ITER`) rather than Python —
  a reasonable tradeoff given k6 scripts are intentionally small and self-contained.
