#!/usr/bin/env bash
# Samples unified-api process RSS (resident set size) across four operating states, using the
# process_resident_memory_bytes metric that prometheus-fastapi-instrumentator already exposes on
# /metrics (via prometheus_client's default ProcessCollector) — no new endpoint, no docker-stats
# shell-out. See docker/README.md's "Memory / RSS Measurement" section for the full methodology,
# including the two-boot recipe needed to isolate the Temporal client's incremental cost.
#
# Usage:
#   ./measure_unified_api_rss.sh idle
#   ./measure_unified_api_rss.sh db-pool-warm
#   ./measure_unified_api_rss.sh temporal-active
#   ./measure_unified_api_rss.sh peak-burst
#
# Each subcommand appends timestamped samples to the same CSV (default
# rss_measurements_<timestamp>.csv, override with OUTPUT_CSV) so a full run across container
# restarts accumulates one reproducible dataset, and prints a per-invocation median/max summary.
#
# Requires: a running docker-compose stack (docker/docker-compose.yml) with Prometheus scraping
# unified-api, curl, jq. peak-burst additionally requires `docker compose` (for the k6 harness).

set -euo pipefail

PROM_URL="${PROM_URL:-http://localhost:9090}"
BASE_URL="${BASE_URL:-http://localhost:8888}"
WARMUP_SECONDS="${WARMUP_SECONDS:-30}"
SAMPLE_INTERVAL_SECONDS="${SAMPLE_INTERVAL_SECONDS:-5}"
SAMPLE_COUNT="${SAMPLE_COUNT:-5}"
DB_WARM_CONCURRENCY="${DB_WARM_CONCURRENCY:-12}"
DB_WARM_SETTLE_SECONDS="${DB_WARM_SETTLE_SECONDS:-5}"
PEAK_VUS="${PEAK_VUS:-50}"
PEAK_DURATION="${PEAK_DURATION:-60s}"
PEAK_SAMPLE_INTERVAL_SECONDS="${PEAK_SAMPLE_INTERVAL_SECONDS:-2}"
OUTPUT_CSV="${OUTPUT_CSV:-rss_measurements_$(date -u +%Y%m%d_%H%M%S).csv}"

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_compose_file="${_here}/../docker-compose.yml"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

# Prints the current process_resident_memory_bytes value for the unified-api Prometheus target,
# or nothing if the query returns no result (e.g. the target hasn't been scraped yet).
sample_rss() {
  curl -sf "${PROM_URL}/api/v1/query?query=process_resident_memory_bytes%7Bjob%3D%22unified-api%22%7D" \
    | jq -r '.data.result[0].value[1] // empty'
}

wait_for_health() {
  echo "Waiting for ${BASE_URL}/health ..." >&2
  until curl -sf "${BASE_URL}/health" > /dev/null 2>&1; do
    sleep 2
  done
  echo "unified-api is up." >&2
}

ensure_csv_header() {
  if [[ ! -f "${OUTPUT_CSV}" ]]; then
    echo "timestamp,state,sample_index,rss_bytes" > "${OUTPUT_CSV}"
  fi
}

# Appends $count RSS samples labeled $state to $OUTPUT_CSV, $SAMPLE_INTERVAL_SECONDS apart.
record_samples() {
  local state="$1" count="$2"
  local i ts rss
  for ((i = 1; i <= count; i++)); do
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rss="$(sample_rss || true)"
    echo "${ts},${state},${i},${rss}" >> "${OUTPUT_CSV}"
    echo "  [${state} ${i}/${count}] rss_bytes=${rss:-<none>}" >&2
    sleep "${SAMPLE_INTERVAL_SECONDS}"
  done
}

# Prints a median/max summary (in MiB) for one state's rows already written to $OUTPUT_CSV.
summarize_state() {
  local state="$1"
  awk -F, -v state="$state" '
    $2 == state && $4 != "" { print $4 }
  ' "${OUTPUT_CSV}" | sort -n | awk '
    { values[NR] = $1; sum += $1 }
    END {
      if (NR == 0) { print "  (no samples)"; exit }
      mid = int((NR + 1) / 2)
      median_mib = values[mid] / 1048576
      max_mib = values[NR] / 1048576
      printf "  median=%.1f MiB  max=%.1f MiB  (n=%d)\n", median_mib, max_mib, NR
    }
  '
}

cmd_idle() {
  wait_for_health
  echo "Warming up ${WARMUP_SECONDS}s with no driven traffic ..." >&2
  sleep "${WARMUP_SECONDS}"
  ensure_csv_header
  record_samples "idle" "${SAMPLE_COUNT}"
  echo "idle summary:"; summarize_state "idle"
}

cmd_db_pool_warm() {
  wait_for_health
  echo "Firing ${DB_WARM_CONCURRENCY} concurrent /health requests to grow the Postgres pool ..." >&2
  local pids=()
  for ((i = 1; i <= DB_WARM_CONCURRENCY; i++)); do
    curl -sf "${BASE_URL}/health" > /dev/null &
    pids+=("$!")
  done
  wait "${pids[@]}" || echo "one or more warm-up requests failed (non-fatal, continuing)" >&2
  sleep "${DB_WARM_SETTLE_SECONDS}"
  ensure_csv_header
  record_samples "db_pool_warm" "${SAMPLE_COUNT}"
  echo "db_pool_warm summary:"; summarize_state "db_pool_warm"
}

cmd_temporal_active() {
  echo "NOTE: this state is sampled identically to 'idle' — the Temporal client is not" >&2
  echo "toggleable at runtime. To isolate its incremental cost, run this once against a" >&2
  echo "container booted with UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER=false and" >&2
  echo "UNIFIED_API_SANDBOX_TEMPORAL_WORKER=false (Temporal-disabled baseline), then again" >&2
  echo "against the default config (Temporal enabled) and diff the two." >&2
  wait_for_health
  sleep "${WARMUP_SECONDS}"
  ensure_csv_header
  record_samples "temporal_client_active" "${SAMPLE_COUNT}"
  echo "temporal_client_active summary:"; summarize_state "temporal_client_active"
}

cmd_peak_burst() {
  wait_for_health
  ensure_csv_header
  echo "Launching k6 harness at VUS=${PEAK_VUS} DURATION=${PEAK_DURATION} ..." >&2
  docker compose -f "${_compose_file}" --profile load-test run --rm \
    -e VUS="${PEAK_VUS}" -e DURATION="${PEAK_DURATION}" k6 &
  local k6_pid=$!
  local ts rss
  while kill -0 "${k6_pid}" 2> /dev/null; do
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rss="$(sample_rss || true)"
    echo "${ts},peak_concurrency_burst,,${rss}" >> "${OUTPUT_CSV}"
    echo "  [peak_concurrency_burst] rss_bytes=${rss:-<none>}" >&2
    sleep "${PEAK_SAMPLE_INTERVAL_SECONDS}"
  done
  wait "${k6_pid}" || echo "k6 exited non-zero (check its threshold output above); RSS samples above are still valid" >&2
  echo "peak_concurrency_burst summary:"; summarize_state "peak_concurrency_burst"
}

main() {
  local state="${1:-}"
  case "${state}" in
    idle) cmd_idle ;;
    db-pool-warm) cmd_db_pool_warm ;;
    temporal-active) cmd_temporal_active ;;
    peak-burst) cmd_peak_burst ;;
    *) usage ;;
  esac
  echo "Raw samples: ${OUTPUT_CSV}" >&2
}

main "$@"
