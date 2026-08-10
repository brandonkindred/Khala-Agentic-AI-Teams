// Concurrent load-test harness for unified-api, built on Grafana k6.
//
// Fans out concurrent GET requests across a rotation of unified-api's *proxied*
// teams (see ../../backend/unified_api/config.py TEAM_CONFIGS), hitting each
// team's forwarded `/health` endpoint. In-process teams (user-profile,
// product-delivery, agent-studio) are never proxied and the disabled
// investment-strategy-lab sub-team isn't reachable at all, so none of them
// exercise the proxy path this harness targets — all three are intentionally
// left out of the default team list below.
//
// Usage (see docker/README.md's "Load Testing (k6)" section for the full
// walkthrough):
//   docker compose -f docker/docker-compose.yml --profile load-test run --rm k6
//   K6_VUS=20 K6_DURATION=15s docker compose -f docker/docker-compose.yml --profile load-test run --rm k6
//   k6 run docker/k6/load_test_unified_api.js -e BASE_URL=http://localhost:8888 --vus 20 --duration 15s
//
// Concurrency (VUS) and duration are configurable via env vars (VUS, DURATION,
// TEAMS, BASE_URL, read through __ENV below) or k6's native --vus/--duration
// CLI flags, which take precedence over the `options` exported here.
//
// Throughput and latency are reported automatically by k6's built-in
// end-of-test summary (http_reqs for request count/rate, http_req_duration for
// latency percentiles) — no custom aggregation code needed.

import http from "k6/http";
import { check } from "k6";

const DEFAULT_TEAMS = [
  "blogging",
  "personal-assistant",
  "market-research",
  "soc2-compliance",
  "social-marketing",
  "branding",
];

const BASE_URL = __ENV.BASE_URL || "http://khala:8080";

const TEAMS = (__ENV.TEAMS ? __ENV.TEAMS.split(",") : DEFAULT_TEAMS)
  .map((team) => team.trim())
  .filter((team) => team.length > 0);

export const options = {
  vus: Number(__ENV.VUS) || 10,
  duration: __ENV.DURATION || "30s",
  thresholds: {
    http_req_failed: ["rate<0.05"],
  },
};

export default function () {
  const team = TEAMS[(__VU + __ITER) % TEAMS.length];
  const url = `${BASE_URL}/api/${team}/health`;
  const response = http.get(url, { tags: { team } });
  check(response, { "status is 200": (r) => r.status === 200 }, { team });
}
