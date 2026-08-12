#!/usr/bin/env bash
# Samples unified-api process RSS (resident set size) across four operating states, using the
# process_resident_memory_bytes metric that prometheus-fastapi-instrumentator already exposes on
# /metrics (via prometheus_client's default ProcessCollector) — no new endpoint, no docker-stats
# shell-out. See docker/README.md's "Memory / RSS Measurement" section for the full methodology,
# including the two-boot recipe needed to isolate the Temporal client's incremental cost.
#
# --- usage begin ---
# Usage:
#   ./measure_unified_api_rss.sh idle
#   ./measure_unified_api_rss.sh db-pool-warm
#   ./measure_unified_api_rss.sh temporal-active
#   ./measure_unified_api_rss.sh peak-burst
#
# Each subcommand appends timestamped samples, labeled with the exact subcommand name in the
# CSV's "state" column, to the same CSV (default rss_measurements.csv in the current directory,
# override with OUTPUT_CSV) so a full run across container restarts accumulates one reproducible
# dataset, and prints a per-invocation median/max summary. Since the default filename has no
# timestamp, move or rename it (or set OUTPUT_CSV) between unrelated measurement sessions so they
# don't mix in one file.
#
# Requires: a running docker-compose stack (docker/docker-compose.yml) with Prometheus scraping
# unified-api, curl, jq. peak-burst additionally requires `docker compose` (for the k6 harness).
# --- usage end ---

set -euo pipefail

PROM_URL="${PROM_URL:-http://localhost:9090}"
BASE_URL="${BASE_URL:-http://localhost:8888}"
WARMUP_SECONDS="${WARMUP_SECONDS:-30}"
SAMPLE_INTERVAL_SECONDS="${SAMPLE_INTERVAL_SECONDS:-5}"
SAMPLE_COUNT="${SAMPLE_COUNT:-5}"
DB_WARM_CONCURRENCY="${DB_WARM_CONCURRENCY:-12}"
DB_WARM_SETTLE_SECONDS="${DB_WARM_SETTLE_SECONDS:-5}"
# /health's live-DB-probe branch runs through a fixed 2-worker executor
# (_get_probe_executor in backend/unified_api/main.py), so no amount of concurrent /health
# traffic can grow the shared pool past that cap. /api/product-delivery/products is a plain
# synchronous FastAPI route (list_products) that hits Postgres directly per request through
# Starlette's much larger default thread pool, so concurrent hits actually drive pool growth.
DB_WARM_PATH="${DB_WARM_PATH:-/api/product-delivery/products}"
PEAK_VUS="${PEAK_VUS:-50}"
PEAK_DURATION="${PEAK_DURATION:-60s}"
# Prometheus's global scrape_interval (docker/prometheus/prometheus.yml) is 15s, so sampling
# faster than that just re-reads the same cached value — this must stay >= that interval or the
# reported peak will silently undercount how many independent observations were actually taken.
PEAK_SAMPLE_INTERVAL_SECONDS="${PEAK_SAMPLE_INTERVAL_SECONDS:-15}"
OUTPUT_CSV="${OUTPUT_CSV:-rss_measurements.csv}"

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_compose_file="${_here}/../docker-compose.yml"

# Extracts the header comment between the --- usage begin/end --- markers, rather than a
# hardcoded line range, so the usage text can't silently drift out of sync as the header comment
# is edited.
usage() {
  awk '/--- usage begin ---/{flag=1;next} /--- usage end ---/{flag=0} flag' "${BASH_SOURCE[0]}" \
    | sed 's/^# \{0,1\}//'
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
# Returns non-zero when there are no usable samples, so callers can fail loudly instead of
# reporting a run as complete when Prometheus never actually returned an RSS value.
summarize_state() {
  local state="$1"
  # sort -g (not -n): Prometheus can serialize large sample values in scientific notation
  # (e.g. 9.4e+07), which -n compares only by its leading digits and misorders.
  awk -F, -v state="$state" '
    $2 == state && $4 != "" { print $4 }
  ' "${OUTPUT_CSV}" | sort -g | awk '
    { values[NR] = $1 }
    END {
      if (NR == 0) { print "  (no samples)"; exit 1 }
      if (NR % 2 == 1) {
        median_bytes = values[(NR + 1) / 2]
      } else {
        median_bytes = (values[NR / 2] + values[NR / 2 + 1]) / 2
      }
      median_mib = median_bytes / 1048576
      max_mib = values[NR] / 1048576
      printf "  median=%.1f MiB  max=%.1f MiB  (n=%d)\n", median_mib, max_mib, NR
    }
  '
}

# Fails loudly (rather than reporting a run as complete) when a state has zero usable RSS
# samples — e.g. Prometheus hadn't scraped unified-api yet, or was unreachable.
report_summary_or_fail() {
  local state="$1"
  echo "${state} summary:"
  if ! summarize_state "${state}"; then
    echo "ERROR: no usable RSS samples for '${state}' — Prometheus likely hasn't scraped" >&2
    echo "unified-api yet, or is unreachable at ${PROM_URL}. Re-run once it has." >&2
    return 1
  fi
}

cmd_idle() {
  wait_for_health
  echo "Warming up ${WARMUP_SECONDS}s with no driven traffic ..." >&2
  sleep "${WARMUP_SECONDS}"
  ensure_csv_header
  record_samples "idle" "${SAMPLE_COUNT}"
  report_summary_or_fail "idle"
}

cmd_db_pool_warm() {
  wait_for_health
  echo "Firing ${DB_WARM_CONCURRENCY} concurrent requests at ${DB_WARM_PATH} to grow the Postgres pool ..." >&2
  local pids=() pid succeeded=0 failed=0
  for ((i = 1; i <= DB_WARM_CONCURRENCY; i++)); do
    curl -sf "${BASE_URL}${DB_WARM_PATH}" > /dev/null &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    if wait "${pid}"; then
      succeeded=$((succeeded + 1))
    else
      failed=$((failed + 1))
    fi
  done
  echo "Warm-up requests: ${succeeded} succeeded, ${failed} failed." >&2
  if ((succeeded * 2 < DB_WARM_CONCURRENCY)); then
    echo "ERROR: fewer than half the warm-up requests succeeded; the pool is likely not warmed." >&2
    echo "Aborting db-pool-warm measurement rather than report misleading numbers." >&2
    return 1
  fi
  sleep "${DB_WARM_SETTLE_SECONDS}"
  ensure_csv_header
  record_samples "db-pool-warm" "${SAMPLE_COUNT}"
  report_summary_or_fail "db-pool-warm"
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
  record_samples "temporal-active" "${SAMPLE_COUNT}"
  report_summary_or_fail "temporal-active"
}

cmd_peak_burst() {
  wait_for_health
  ensure_csv_header
  echo "Launching k6 harness at VUS=${PEAK_VUS} DURATION=${PEAK_DURATION} ..." >&2
  docker compose -f "${_compose_file}" --profile load-test run --rm \
    -e VUS="${PEAK_VUS}" -e DURATION="${PEAK_DURATION}" k6 &
  local k6_pid=$!
  local ts rss i=0
  while kill -0 "${k6_pid}" 2> /dev/null; do
    i=$((i + 1))
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rss="$(sample_rss || true)"
    echo "${ts},peak-burst,${i},${rss}" >> "${OUTPUT_CSV}"
    echo "  [peak-burst ${i}] rss_bytes=${rss:-<none>}" >&2
    sleep "${PEAK_SAMPLE_INTERVAL_SECONDS}"
  done
  wait "${k6_pid}" || echo "k6 exited non-zero (check its threshold output above); RSS samples above are still valid" >&2
  report_summary_or_fail "peak-burst"
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

# Guarded so tests can `source` this file for direct function-level testing without triggering
# a real run.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
