"""End-to-end smoke matrix for the per-agent sandbox lifecycle (issue #268, Phase 6).

Exercises the four-team smoke matrix from the Phase 6 epic against a live stack:
``blogging.planner``, ``se.backend.api_openapi``, ``planning_v3.intake``, and
``branding.creative_director``. Verifies that the unified ``khala-agent-sandbox``
image provisions one container per agent, persists a row to
``agent_console_runs``, exposes a loopback-only sandbox URL, and gets reaped
after going idle.

These tests need a live Docker daemon, the unified API, and Postgres — they are
**skipped** unless ``KHALA_E2E=1`` is in the environment so that ``make test``
stays offline-safe. Cold-start ``boot_ms`` and warm ``duration_ms`` numbers are
written one-per-line to ``KHALA_E2E_PERF_LOG`` (default
``$AGENT_CACHE/agent_provisioning/phase6_perf.jsonl``) for the
``phase6_perf_summary.py`` helper to consume.

Run::

    KHALA_E2E=1 pytest backend/agents/agent_provisioning_team/tests/test_e2e_smoke.py -v

Override the API base URL with ``KHALA_E2E_API_BASE`` (default
``http://127.0.0.1:8080``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_provisioning_team.sandbox.state import resolve_cache_path

E2E_ENABLED = os.environ.get("KHALA_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason="Phase 6 e2e smoke matrix needs live Docker + unified API; set KHALA_E2E=1 to enable.",
)


API_BASE = os.environ.get("KHALA_E2E_API_BASE", "http://127.0.0.1:8080").rstrip("/")
INVOKE_TIMEOUT_S = float(os.environ.get("KHALA_E2E_INVOKE_TIMEOUT_S", "180"))


def _perf_log_path() -> Path:
    override = os.environ.get("KHALA_E2E_PERF_LOG")
    if override:
        return Path(override)
    return resolve_cache_path("agent_provisioning", "phase6_perf.jsonl")


def _write_perf_sample(sample: dict[str, Any]) -> None:
    """Append one JSON line per invoke for the perf-summary helper."""
    path = _perf_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sample) + "\n")


# Smoke matrix — one runnable agent per currently-wired team. Payloads are kept
# minimal (the goal is sandbox plumbing, not agent quality). All four are
# verified to NOT carry the `requires-live-integration` tag.
SMOKE_MATRIX: list[tuple[str, str, dict[str, Any]]] = [
    (
        "blogging.planner",
        "blogging",
        {
            "topic": "Phase 6 sandbox smoke test",
            "audience": "internal engineers",
            "goal": "verify sandbox plumbing",
        },
    ),
    (
        "se.backend.api_openapi",
        "software_engineering",
        {
            "service_name": "smoke",
            "endpoints": [{"method": "GET", "path": "/ping"}],
        },
    ),
    (
        "planning_v3.intake",
        "planning_v3",
        {
            "repo_path": "/tmp/phase6-smoke",
            "client_name": "phase6-smoke",
            "initial_brief": "Sandbox smoke verification.",
        },
    ),
    (
        "branding.creative_director",
        "branding",
        {
            "company_name": "Phase 6 Smoke Co",
            "audience": "internal engineers",
            "values": ["clarity"],
            "differentiators": ["isolation"],
            "voice": "concise",
        },
    ),
]


def _docker_ps_for(agent_id: str) -> list[dict[str, Any]]:
    """Return parsed `docker ps` rows whose container name encodes ``agent_id``.

    Matches the deterministic ``khala-sbx-<sanitised-agent-id>-<sha1[:8]>``
    naming from ``provisioner.container_name_for``; the sha1 suffix is what
    proves one-sandbox-per-agent (two ids that sanitise the same way still get
    distinct containers).
    """
    out = subprocess.run(
        ["docker", "ps", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "khala-sbx-" in row.get("Names", ""):
            rows.append(row)
    safe_prefix = agent_id.replace(".", "-").replace("/", "-")[:40]
    return [r for r in rows if safe_prefix in r["Names"]]


def _docker_inspect_host_config(container_name: str) -> dict[str, Any]:
    out = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(out.stdout)[0]["HostConfig"]


async def _invoke(client: httpx.AsyncClient, agent_id: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"{API_BASE}/api/agents/{agent_id}/invoke", json=body)


# ---------------------------------------------------------------------------
# Smoke matrix — per-agent cold + warm invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id,team,payload", SMOKE_MATRIX, ids=[m[0] for m in SMOKE_MATRIX])
async def test_smoke_invoke(agent_id: str, team: str, payload: dict[str, Any]) -> None:
    """Cold + warm invoke per agent: response 200, runs row written, container exists."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S)) as client:
        cold_start = time.perf_counter()
        cold = await _invoke(client, agent_id, payload)
        cold_total_ms = int((time.perf_counter() - cold_start) * 1000)
        assert cold.status_code == 200, f"cold invoke failed: {cold.status_code} {cold.text[:300]}"
        cold_envelope = cold.json()
        assert isinstance(cold_envelope, dict)
        assert cold_envelope.get("sandbox", {}).get("agent_id") == agent_id

        sandbox_url = cold_envelope["sandbox"]["url"]
        assert sandbox_url.startswith("http://127.0.0.1:"), (
            f"sandbox must bind loopback only: {sandbox_url}"
        )

        running = _docker_ps_for(agent_id)
        assert len(running) == 1, f"expected exactly one sandbox for {agent_id}, got {len(running)}"

        warm_start = time.perf_counter()
        warm = await _invoke(client, agent_id, payload)
        warm_total_ms = int((time.perf_counter() - warm_start) * 1000)
        assert warm.status_code == 200
        assert warm.json()["sandbox"]["url"] == sandbox_url, (
            "warm invoke must reuse the same sandbox"
        )

        runs = await client.get(f"{API_BASE}/api/agents/{agent_id}/runs", params={"limit": 5})
    assert runs.status_code == 200, f"runs lookup failed: {runs.status_code} {runs.text[:200]}"
    assert any(row.get("status") == "ok" for row in runs.json()), (
        f"no ok runs persisted for {agent_id}"
    )

    for phase, total_ms in (("cold", cold_total_ms), ("warm", warm_total_ms)):
        _write_perf_sample(
            {
                "agent_id": agent_id,
                "team": team,
                "phase": phase,
                "total_ms": total_ms,
                "sandbox_url": sandbox_url,
            }
        )


# ---------------------------------------------------------------------------
# Concurrency — proves "one sandbox per agent, not per team"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_team_concurrency_uses_distinct_sandboxes() -> None:
    """Two agents from different teams launched in parallel get two containers
    with two distinct loopback ports."""
    a = ("blogging.planner", SMOKE_MATRIX[0][2])
    b = ("branding.creative_director", SMOKE_MATRIX[3][2])
    async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S)) as client:
        ra, rb = await asyncio.gather(
            _invoke(client, a[0], a[1]),
            _invoke(client, b[0], b[1]),
        )
    assert ra.status_code == 200 and rb.status_code == 200
    url_a = ra.json()["sandbox"]["url"]
    url_b = rb.json()["sandbox"]["url"]
    assert url_a != url_b, "cross-team concurrent invokes must land on distinct sandboxes"
    assert url_a.startswith("http://127.0.0.1:") and url_b.startswith("http://127.0.0.1:")


@pytest.mark.asyncio
async def test_intra_team_concurrency_uses_distinct_sandboxes() -> None:
    """Two agents from the same team (blogging) launched in parallel must end
    up in two distinct containers — proves the lifecycle keys on agent_id, not
    team. Uses ``blogging.planner`` and ``blogging.copy_editor``."""
    a = (
        "blogging.planner",
        {"topic": "intra-team A", "audience": "engineers", "goal": "smoke"},
    )
    b = (
        "blogging.copy_editor",
        {"draft_markdown": "# Hello\n\nIntra-team B smoke draft."},
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S)) as client:
        ra, rb = await asyncio.gather(
            _invoke(client, a[0], a[1]),
            _invoke(client, b[0], b[1]),
        )
    # copy_editor may not have a sandbox manifest in every config — skip if so.
    if rb.status_code == 404:
        pytest.skip("blogging.copy_editor not in registry on this build")
    assert ra.status_code == 200 and rb.status_code == 200
    url_a = ra.json()["sandbox"]["url"]
    url_b = rb.json()["sandbox"]["url"]
    assert url_a != url_b, (
        "two same-team agents must each get their own sandbox (proves one-per-agent rule)"
    )

    rows_a = _docker_ps_for(a[0])
    rows_b = _docker_ps_for(b[0])
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["Names"] != rows_b[0]["Names"]


# ---------------------------------------------------------------------------
# Hardening spot-check (full hardening matrix lives in phase6_hardening.sh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hardening_flags_applied_to_warm_sandbox() -> None:
    """A warmed sandbox container must have all Phase 2 hardening flags applied."""
    payload = SMOKE_MATRIX[0][2]
    async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S)) as client:
        resp = await _invoke(client, "blogging.planner", payload)
    assert resp.status_code == 200
    rows = _docker_ps_for("blogging.planner")
    assert rows, "no sandbox container after invoke"
    cfg = _docker_inspect_host_config(rows[0]["Names"])
    assert cfg["ReadonlyRootfs"] is True
    assert cfg["CapDrop"] == ["ALL"]
    sec = cfg.get("SecurityOpt") or []
    assert any("no-new-privileges" in s for s in sec)
    assert any("seccomp" in s for s in sec)
    assert cfg["Memory"] == 1073741824  # 1 GiB
    assert cfg["PidsLimit"] == 512


# ---------------------------------------------------------------------------
# Reaper — verify idle sandboxes are torn down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_reaper_tears_down_sandbox() -> None:
    """Invoke once, then wait past the idle threshold + reaper tick and assert
    the container is gone. Requires the unified API to be started with a
    shrunken ``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES`` (recommend 1 minute,
    so this test takes ~2 minutes wall-clock). The test is skipped if the
    threshold is the production default of 5 minutes."""
    idle_minutes = int(os.environ.get("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES", "5"))
    if idle_minutes >= 5:
        pytest.skip(
            "Set AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES=1 (or lower) on the API to run "
            "the reaper test in CI/dev — default 5 makes the test too slow."
        )

    agent_id = "blogging.planner"
    payload = SMOKE_MATRIX[0][2]
    async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S)) as client:
        resp = await _invoke(client, agent_id, payload)
    assert resp.status_code == 200
    assert _docker_ps_for(agent_id), "container should exist after invoke"

    # Reaper ticks every 60s (lifecycle.run_idle_reaper default).
    deadline = time.monotonic() + (idle_minutes * 60) + 75
    while time.monotonic() < deadline:
        if not _docker_ps_for(agent_id):
            return
        await asyncio.sleep(5)
    pytest.fail(f"sandbox for {agent_id} was not reaped within {idle_minutes}m + 75s of going idle")


# ---------------------------------------------------------------------------
# `requires-live-integration` block — backend should refuse with 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_live_integration_returns_409() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.post(f"{API_BASE}/api/agents/blogging.publication/invoke", json={})
    assert resp.status_code == 409, (
        f"requires-live-integration agent must be refused, got {resp.status_code}"
    )
