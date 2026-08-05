#!/usr/bin/env bash
# Phase 6 hardening probes for the per-agent sandbox (issue #268).
#
# Runs `docker inspect`, `ss`, `docker exec` probes against the live sandbox
# stack to confirm Phase 2 hardening flags are still in effect, that ports are
# loopback-only, the rootfs is read-only, the user is non-root, and the
# pids-limit contains a fork bomb.
#
# Prereqs:
#   - Unified API up (default base http://127.0.0.1:8080).
#   - Docker daemon reachable; `jq` and `ss` on PATH.
#   - At least one warmed sandbox exists (the script will warm
#     `blogging.planner` if none are present).
#
# Outputs a markdown summary on success that can be pasted into the sandbox
# README's Capacity section.
#
# Exit code: 0 = all probes passed; non-zero = at least one probe failed.

set -uo pipefail

API_BASE="${KHALA_E2E_API_BASE:-http://127.0.0.1:8080}"
WARM_AGENT="${PHASE6_WARM_AGENT:-blogging.planner}"
LIVE_INT_AGENT="${PHASE6_LIVE_INT_AGENT:-blogging.publication}"

PASS=0
FAIL=0
FAIL_REASONS=()

ok()   { printf "  \e[32mPASS\e[0m %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  \e[31mFAIL\e[0m %s\n" "$1"; FAIL=$((FAIL + 1)); FAIL_REASONS+=("$1"); }
note() { printf "  \e[33mNOTE\e[0m %s\n" "$1"; }
hdr()  { printf "\n\e[1m== %s ==\e[0m\n" "$1"; }

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required tool: $1" >&2
    exit 2
  fi
}

require docker
require jq
require curl
require ss
require timeout

hdr "Warm a sandbox if none exist"
if ! docker ps --format '{{.Names}}' | grep -q '^khala-sbx-'; then
  note "no sandbox containers found; warming ${WARM_AGENT}"
  curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' \
    "${API_BASE}/api/agents/sandboxes/${WARM_AGENT}" >/dev/null \
    || { echo "failed to warm ${WARM_AGENT}" >&2; exit 2; }
  # Poll until WARM (max 90s).
  for _ in $(seq 1 18); do
    status=$(curl -fsS "${API_BASE}/api/agents/sandboxes/${WARM_AGENT}" | jq -r '.status // empty')
    if [[ "$status" == "warm" ]]; then break; fi
    sleep 5
  done
fi

CONTAINER=$(docker ps --format '{{.Names}}' | grep '^khala-sbx-' | head -n1)
if [[ -z "$CONTAINER" ]]; then
  echo "no warm sandbox container found" >&2
  exit 2
fi
note "probing container: ${CONTAINER}"

hdr "docker inspect — HostConfig hardening flags"
HOST_CFG=$(docker inspect "$CONTAINER" | jq '.[0].HostConfig')

[[ "$(jq -r '.ReadonlyRootfs' <<<"$HOST_CFG")" == "true" ]] \
  && ok "ReadonlyRootfs == true" || fail "ReadonlyRootfs is not true"

cap_drop=$(jq -c '.CapDrop' <<<"$HOST_CFG")
[[ "$cap_drop" == '["ALL"]' ]] \
  && ok "CapDrop == [\"ALL\"]" || fail "CapDrop is ${cap_drop}, expected [\"ALL\"]"

sec_opt=$(jq -c '.SecurityOpt' <<<"$HOST_CFG")
echo "$sec_opt" | grep -q 'no-new-privileges' \
  && ok "SecurityOpt contains no-new-privileges" \
  || fail "SecurityOpt missing no-new-privileges (${sec_opt})"
echo "$sec_opt" | grep -q 'seccomp' \
  && ok "SecurityOpt contains seccomp" \
  || fail "SecurityOpt missing seccomp (${sec_opt})"

mem=$(jq -r '.Memory' <<<"$HOST_CFG")
[[ "$mem" == "1073741824" ]] \
  && ok "Memory == 1 GiB (1073741824)" || fail "Memory is ${mem}, expected 1073741824"

pids=$(jq -r '.PidsLimit' <<<"$HOST_CFG")
[[ "$pids" == "512" ]] \
  && ok "PidsLimit == 512" || fail "PidsLimit is ${pids}, expected 512"

hdr "Port binding — loopback only"
sandbox_ports=$(docker ps --format '{{.Names}} {{.Ports}}' | grep '^khala-sbx-' || true)
if [[ -z "$sandbox_ports" ]]; then
  fail "no sandbox port bindings found"
else
  if grep -E '0\.0\.0\.0:[0-9]+->' <<<"$sandbox_ports" >/dev/null; then
    fail "at least one sandbox port is bound to 0.0.0.0 (must be loopback only)"
    echo "$sandbox_ports" | sed 's/^/    /'
  else
    ok "all sandbox ports bound to 127.0.0.1"
  fi
fi

hdr "In-container probes"
user=$(docker exec "$CONTAINER" whoami 2>/dev/null || echo "?")
[[ "$user" != "root" && "$user" != "?" ]] \
  && ok "non-root user inside sandbox (\"$user\")" \
  || fail "user inside sandbox is \"$user\""

if docker exec "$CONTAINER" sh -c 'touch /app/evil.py' 2>/dev/null; then
  fail "writable /app — read-only rootfs not enforced"
else
  ok "rootfs is read-only (touch /app/evil.py blocked)"
fi

if docker exec "$CONTAINER" sh -c 'touch /tmp/ok.py' 2>/dev/null; then
  ok "/tmp tmpfs is writable"
  docker exec "$CONTAINER" sh -c 'rm -f /tmp/ok.py' || true
else
  fail "/tmp tmpfs not writable"
fi

hdr "Fork-bomb containment (pids-limit)"
load_before=$(awk '{print $1}' /proc/loadavg)
note "host loadavg before: ${load_before}"
# The shell forks until the kernel's pids-limit refuses more processes. We
# read PASS as: `timeout` killed a still-running bomb (rc=124), which means
# the container kept the shell busy within its pids cap. We must treat
# docker-CLI failures (rc 125/126/127) as inconclusive rather than pass, or
# a broken probe looks green.
timeout 8 docker exec "$CONTAINER" sh -c ':() { :|:& }; :' >/dev/null 2>&1
rc=$?
sleep 2
load_after=$(awk '{print $1}' /proc/loadavg)
note "host loadavg after:  ${load_after}"

fork_bomb_verdict=""
case "$rc" in
  124)
    fork_bomb_verdict="pass: exec timed out under pids-limit (rc=124)"
    ;;
  125|126|127)
    # 125 = docker daemon error, 126 = container cannot exec, 127 = command not found.
    fork_bomb_verdict="fail: docker exec failed (rc=${rc}) — probe inconclusive"
    ;;
  0)
    fork_bomb_verdict="fail: fork-bomb exec returned 0 — container did not refuse"
    ;;
  *)
    # Non-timeout non-zero: the shell was killed (SIGKILL 137, etc.), also
    # acceptable — but only if the container itself survived the probe.
    fork_bomb_verdict="pass: exec shell terminated (rc=${rc})"
    ;;
esac

if [[ "$fork_bomb_verdict" == pass:* ]]; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]]; then
    ok "${fork_bomb_verdict#pass: }; container still running"
  else
    fail "fork-bomb probe killed the container (rc=${rc}) — not contained"
  fi
else
  fail "${fork_bomb_verdict#fail: }"
fi

hdr "requires-live-integration block — backend returns 409"
http_code=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' -d '{}' \
  "${API_BASE}/api/agents/${LIVE_INT_AGENT}/invoke")
if [[ "$http_code" == "409" ]]; then
  ok "POST /api/agents/${LIVE_INT_AGENT}/invoke -> 409"
else
  fail "expected 409 for ${LIVE_INT_AGENT}, got HTTP ${http_code}"
fi

hdr "Summary"
echo "passed: ${PASS}"
echo "failed: ${FAIL}"

if [[ "$FAIL" -gt 0 ]]; then
  echo
  echo "Failed checks:"
  for r in "${FAIL_REASONS[@]}"; do echo "  - $r"; done
  exit 1
fi

cat <<EOF

# Capacity-table snippet for sandbox/README.md (paste under Capacity section)

| Probe | Result |
|---|---|
| ReadonlyRootfs | PASS |
| CapDrop=[ALL] | PASS |
| no-new-privileges + seccomp | PASS |
| Memory=1 GiB / PidsLimit=512 | PASS |
| Loopback-only port binding | PASS |
| Non-root in-container user | PASS |
| /app read-only, /tmp writable | PASS |
| Fork-bomb contained by pids-limit | PASS |
| \`requires-live-integration\` -> 409 | PASS |

Container probed: \`${CONTAINER}\`
Probed at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

exit 0
