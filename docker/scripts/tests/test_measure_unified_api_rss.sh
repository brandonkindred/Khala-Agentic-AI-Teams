#!/usr/bin/env bash
# Automated tests for ../measure_unified_api_rss.sh.
#
# Covers, with no live docker-compose stack required:
#   - median/max math in summarize_state, for both odd and even sample counts
#   - record_samples writes the exact requested state label and a 1..N sample_index sequence
#   - the default OUTPUT_CSV is the fixed (non-timestamped) filename the README documents
#   - state-label consistency for temporal-active/peak-burst against source-level regressions
#   - usage/bad-argument handling exits non-zero and prints usage text
#
# Plus an end-to-end smoke test of the idle and db-pool-warm subcommands against local mock HTTP
# servers standing in for unified-api's /health and Prometheus's /api/v1/query (requires python3;
# already a dependency of this repo's backend).
#
# Run: bash docker/scripts/tests/test_measure_unified_api_rss.sh

set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_script="${_here}/../measure_unified_api_rss.sh"
_tmp="$(mktemp -d)"
_mock_pid=""

cleanup() {
  [[ -n "${_mock_pid}" ]] && kill "${_mock_pid}" 2> /dev/null
  rm -rf "${_tmp}"
}
trap cleanup EXIT

_failures=0

assert_eq() {
  local expected="$1" actual="$2" label="$3"
  if [[ "${expected}" != "${actual}" ]]; then
    echo "FAIL: ${label}: expected [${expected}], got [${actual}]" >&2
    _failures=$((_failures + 1))
  else
    echo "ok - ${label}"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" label="$3"
  if [[ "${haystack}" != *"${needle}"* ]]; then
    echo "FAIL: ${label}: expected output to contain [${needle}]" >&2
    _failures=$((_failures + 1))
  else
    echo "ok - ${label}"
  fi
}

# --- Static source checks: guard against the exact label/default regressions already seen once ---

grep -q "OUTPUT_CSV:-rss_measurements.csv" "${_script}" \
  && echo "ok - default OUTPUT_CSV is the fixed, non-timestamped filename the README documents" \
  || { echo "FAIL: default OUTPUT_CSV is not the documented fixed filename" >&2; _failures=$((_failures + 1)); }

temporal_label_count="$(grep -c '"temporal-active"' "${_script}")"
if [[ "${temporal_label_count}" -ge 2 ]]; then
  echo "ok - temporal-active state label used consistently (record_samples + summarize_state)"
else
  echo "FAIL: expected >=2 occurrences of \"temporal-active\" in ${_script}, got ${temporal_label_count}" >&2
  _failures=$((_failures + 1))
fi

peak_label_count="$(grep -c '"peak-burst"' "${_script}")"
if [[ "${peak_label_count}" -ge 1 ]]; then
  echo "ok - peak-burst state label used consistently"
else
  echo "FAIL: expected >=1 occurrence of \"peak-burst\" in ${_script}, got ${peak_label_count}" >&2
  _failures=$((_failures + 1))
fi

grep -q "DB_WARM_PATH:-/api/product-delivery/products" "${_script}" \
  && echo "ok - db-pool-warm targets a route that isn't funneled through the 2-worker health-probe executor" \
  || { echo "FAIL: DB_WARM_PATH no longer defaults to a real pool-growing route" >&2; _failures=$((_failures + 1)); }

grep -q "PEAK_SAMPLE_INTERVAL_SECONDS:-15" "${_script}" \
  && echo "ok - peak-burst sampling interval defaults to >= Prometheus's 15s scrape_interval" \
  || { echo "FAIL: PEAK_SAMPLE_INTERVAL_SECONDS default no longer matches the 15s scrape_interval" >&2; _failures=$((_failures + 1)); }

grep -q "sort -g" "${_script}" \
  && echo "ok - summarize_state sorts numerically with scientific-notation support (sort -g, not -n)" \
  || { echo "FAIL: summarize_state no longer uses the exponent-aware sort -g" >&2; _failures=$((_failures + 1)); }

# --- Source the script for direct function-level testing (guarded main; see script's tail) ---

OUTPUT_CSV="${_tmp}/direct.csv"
# shellcheck source=/dev/null
source "${_script}"

# Median/max math: odd sample count. Values are realistic RSS-scale magnitudes (100-400 MiB)
# specifically so the correct vs. buggy (pre-fix) median produce visibly different %.1f MiB
# output — tiny toy byte values would round to the same "0.0 MiB" either way and silently fail
# to catch a regression.
{
  echo "timestamp,state,sample_index,rss_bytes"
  echo "t1,idle,1,104857600"
  echo "t2,idle,2,209715200"
  echo "t3,idle,3,314572800"
} > "${OUTPUT_CSV}"
out="$(summarize_state idle)"
assert_contains "${out}" "median=200.0 MiB" "odd-count median is the middle value"
assert_contains "${out}" "max=300.0 MiB" "odd-count max"

# Median/max math: even sample count (the reported bug — must average the two middle values,
# not just take the lower-middle one).
{
  echo "timestamp,state,sample_index,rss_bytes"
  echo "t1,idle,1,104857600"
  echo "t2,idle,2,209715200"
  echo "t3,idle,3,314572800"
  echo "t4,idle,4,419430400"
} > "${OUTPUT_CSV}"
out="$(summarize_state idle)"
assert_contains "${out}" "median=250.0 MiB" "even-count median averages the two middle values (200.0 and 300.0), not just the lower one"
assert_contains "${out}" "max=400.0 MiB" "even-count max"

# summarize_state on an empty/absent state: no crash, explicit "no samples", AND a non-zero
# exit so callers can fail loudly instead of reporting an empty run as complete.
: > "${OUTPUT_CSV}"
echo "timestamp,state,sample_index,rss_bytes" > "${OUTPUT_CSV}"
out="$(summarize_state idle)" && zero_sample_exit=0 || zero_sample_exit=$?
assert_contains "${out}" "(no samples)" "summarize_state on zero rows reports no samples, not an error"
assert_eq "1" "${zero_sample_exit}" "summarize_state exits non-zero on zero samples"

report_out="$(report_summary_or_fail idle 2>&1)" && report_exit=0 || report_exit=$?
assert_eq "1" "${report_exit}" "report_summary_or_fail exits non-zero on zero samples"
assert_contains "${report_out}" "ERROR: no usable RSS samples" "report_summary_or_fail explains the failure"

# Median/max math with Prometheus-style scientific-notation values (the reported bug — plain
# sort -n misorders these, corrupting both median and max).
{
  echo "timestamp,state,sample_index,rss_bytes"
  echo "t1,idle,1,1.2e+06"
  echo "t2,idle,2,9.4e+07"
  echo "t3,idle,3,1.05e+08"
} > "${OUTPUT_CSV}"
out="$(summarize_state idle)"
assert_contains "${out}" "max=100.1 MiB" "scientific-notation values are sorted numerically, not lexicographically, for max"

# record_samples: exact state label and a 1..N sample_index sequence, no live network.
sample_rss() { echo "123456789"; }
SAMPLE_INTERVAL_SECONDS=0
echo "timestamp,state,sample_index,rss_bytes" > "${OUTPUT_CSV}"
record_samples "idle" 3 > /dev/null
rows="$(grep -c ',idle,' "${OUTPUT_CSV}")"
assert_eq "3" "${rows}" "record_samples writes exactly N rows for the requested state"
indices="$(awk -F, '$2=="idle"{print $3}' "${OUTPUT_CSV}" | tr '\n' ',')"
assert_eq "1,2,3," "${indices}" "record_samples writes a 1..N sample_index sequence"

# --- End-to-end smoke test against local mock servers (idle + db-pool-warm subcommands) ---

if command -v python3 > /dev/null 2>&1; then
  python3 - "${_tmp}" <<'PYEOF' &
import http.server
import json
import random
import sys
import threading

tmp_dir = sys.argv[1]


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class PromHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/v1/query"):
            rss = random.randint(140_000_000, 160_000_000)
            body = {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"job": "unified-api"}, "value": [1, str(rss)]}],
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


health_server = http.server.HTTPServer(("127.0.0.1", 18899), HealthHandler)
prom_server = http.server.HTTPServer(("127.0.0.1", 19099), PromHandler)
threading.Thread(target=health_server.serve_forever, daemon=True).start()
threading.Thread(target=prom_server.serve_forever, daemon=True).start()
open(f"{tmp_dir}/mock_ready", "w").close()
threading.Event().wait()
PYEOF
  _mock_pid=$!

  for _ in $(seq 1 50); do
    [[ -f "${_tmp}/mock_ready" ]] && break
    sleep 0.1
  done

  e2e_csv="${_tmp}/e2e.csv"

  idle_out="$(BASE_URL=http://127.0.0.1:18899 PROM_URL=http://127.0.0.1:19099 \
    WARMUP_SECONDS=0 SAMPLE_INTERVAL_SECONDS=0 SAMPLE_COUNT=2 OUTPUT_CSV="${e2e_csv}" \
    bash "${_script}" idle 2>&1)"
  assert_contains "${idle_out}" "idle summary:" "idle subcommand prints the documented state name in its summary"
  idle_rows="$(awk -F, '$2=="idle"' "${e2e_csv}" | wc -l | tr -d ' ')"
  assert_eq "2" "${idle_rows}" "idle subcommand writes SAMPLE_COUNT rows with state=idle"

  dbw_out="$(BASE_URL=http://127.0.0.1:18899 PROM_URL=http://127.0.0.1:19099 \
    SAMPLE_INTERVAL_SECONDS=0 SAMPLE_COUNT=2 DB_WARM_CONCURRENCY=6 DB_WARM_SETTLE_SECONDS=0 \
    DB_WARM_PATH=/health \
    OUTPUT_CSV="${e2e_csv}" bash "${_script}" db-pool-warm 2>&1)"
  assert_contains "${dbw_out}" "db-pool-warm summary:" "db-pool-warm subcommand prints the documented state name (matches README, not db_pool_warm)"
  dbw_rows="$(awk -F, '$2=="db-pool-warm"' "${e2e_csv}" | wc -l | tr -d ' ')"
  assert_eq "2" "${dbw_rows}" "db-pool-warm subcommand writes SAMPLE_COUNT rows with state=db-pool-warm"

  kill "${_mock_pid}" 2> /dev/null || true
  _mock_pid=""
else
  echo "SKIP: python3 not found — skipping end-to-end mock-server smoke test" >&2
fi

# --- Usage / bad-argument handling ---

if bash "${_script}" > /dev/null 2>&1; then
  echo "FAIL: no-argument invocation should exit non-zero" >&2
  _failures=$((_failures + 1))
else
  echo "ok - no-argument invocation exits non-zero"
fi

bad_out="$(bash "${_script}" bogus 2>&1 || true)"
assert_contains "${bad_out}" "Usage:" "unknown subcommand prints usage text"

if ((_failures > 0)); then
  echo "${_failures} test(s) failed." >&2
  exit 1
fi
echo "All tests passed."
