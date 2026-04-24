"""Unit tests for the agent-keyed sandbox Lifecycle (issue #264, Phase 2).

Docker CLI calls and the ``/health`` probe are patched so tests run without
a real Docker daemon.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_provisioning_team.sandbox import (
    Lifecycle,
    SandboxStatus,
    UnknownAgentError,
)
from agent_provisioning_team.sandbox import provisioner as provisioner_mod
from agent_provisioning_team.sandbox.provisioner import _build_run_argv, container_name_for
from agent_provisioning_team.sandbox.state import SandboxHandle, SandboxState, now


def _lifecycle(tmp_path: Path) -> Lifecycle:
    return Lifecycle(state_file=tmp_path / "state.json")


def _patched_registry(team: str = "blogging"):
    return patch(
        "agent_provisioning_team.sandbox.lifecycle._resolve_team",
        return_value=team,
    )


@contextmanager
def _patched_docker(*, container_id: str = "abc123", host_port: int = 55123, running: bool = False):
    """Apply the five default docker-mock patches as a single context manager.

    Yields a namespace whose attributes are the individual mocks so tests can
    assert on call counts without unpacking a tuple in every `with` block.
    """
    with ExitStack() as stack:
        yield SimpleNamespace(
            run=stack.enter_context(
                patch.object(
                    provisioner_mod, "run_container", new=AsyncMock(return_value=container_id)
                )
            ),
            port=stack.enter_context(
                patch.object(
                    provisioner_mod, "inspect_host_port", new=AsyncMock(return_value=host_port)
                )
            ),
            running=stack.enter_context(
                patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=running))
            ),
            stop=stack.enter_context(
                patch.object(provisioner_mod, "stop_container", new=AsyncMock())
            ),
            wait=stack.enter_context(patch.object(Lifecycle, "_wait_healthy", new=AsyncMock())),
        )


@pytest.mark.asyncio
async def test_status_cold_for_unseen_agent(tmp_path: Path) -> None:
    """status() must return a COLD handle without touching docker for
    agents that have never been acquired."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        handle = await lc.status("blogging.planner")
    assert handle.status == SandboxStatus.COLD
    assert handle.team == "blogging"
    assert handle.url is None
    # status() on an unseen agent must not provision anything.
    d.run.assert_not_awaited()
    d.port.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_reconciles_warm_handle_with_docker(tmp_path: Path) -> None:
    """If we think a sandbox is WARM but docker says the container is gone,
    status() must flip the stored state back to COLD."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    assert lc._state["blogging.planner"].status == SandboxStatus.WARM

    with (
        _patched_registry(),
        patch.object(provisioner_mod, "is_running", new=AsyncMock(return_value=False)),
    ):
        handle = await lc.status("blogging.planner")
    assert handle.status == SandboxStatus.COLD
    assert lc._state["blogging.planner"].status == SandboxStatus.COLD


@pytest.mark.asyncio
async def test_acquire_cold_to_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        handle = await lc.acquire("blogging.planner")
    d.run.assert_awaited_once()
    assert handle.status == SandboxStatus.WARM
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.team == "blogging"
    assert handle.container_id == "abc123"


@pytest.mark.asyncio
async def test_acquire_is_idempotent_when_already_warm(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker(running=True) as d:
        first = await lc.acquire("blogging.planner")
        second = await lc.acquire("blogging.planner")
    assert d.run.await_count == 1
    assert first.status == SandboxStatus.WARM
    assert second.status == SandboxStatus.WARM
    assert second.container_id == first.container_id


@pytest.mark.asyncio
async def test_acquire_reports_error_on_health_timeout(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        d.wait.side_effect = RuntimeError("boom")
        handle = await lc.acquire("blogging.planner")
    assert handle.status == SandboxStatus.ERROR
    assert handle.error == "boom"


@pytest.mark.asyncio
async def test_acquire_reprovisions_when_container_vanished(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d1:
        await lc.acquire("blogging.planner")
    assert d1.run.await_count == 1

    with _patched_registry(), _patched_docker(container_id="xyz789", host_port=55999) as d2:
        handle = await lc.acquire("blogging.planner")
    assert d2.run.await_count == 1
    assert handle.container_id == "xyz789"
    assert handle.host_port == 55999


@pytest.mark.asyncio
async def test_teardown_removes_state(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        await lc.teardown("blogging.planner")
    d.stop.assert_awaited()
    assert await lc.list_active() == []


@pytest.mark.asyncio
async def test_teardown_preserves_state_when_docker_errors(tmp_path: Path) -> None:
    # When `stop_container` raises a DockerError (daemon unreachable, etc.),
    # state must NOT be evicted — we need the record to retry against the
    # still-alive container on the next tick.
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        d.stop.side_effect = provisioner_mod.DockerError("docker daemon unreachable")
        with pytest.raises(provisioner_mod.DockerError):
            await lc.teardown("blogging.planner")
    assert "blogging.planner" in lc._state


@pytest.mark.asyncio
async def test_note_activity_updates_last_used(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    before = lc._state["blogging.planner"].last_used_at
    lc._state["blogging.planner"].last_used_at = before - timedelta(seconds=60)
    await lc.note_activity("blogging.planner")
    after = lc._state["blogging.planner"].last_used_at
    assert after > before - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_reap_once_tears_down_idle(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        lc._state["blogging.planner"].last_used_at = lc._state[
            "blogging.planner"
        ].last_used_at - timedelta(minutes=30)
        reaped = await lc.reap_once(threshold=60)
    assert reaped == ["blogging.planner"]
    d.stop.assert_awaited()
    assert "blogging.planner" not in lc._state


@pytest.mark.asyncio
async def test_reap_preserves_fresh_sandbox(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
        reaped = await lc.reap_once(threshold=3600)
    assert reaped == []
    assert "blogging.planner" in lc._state


@pytest.mark.asyncio
async def test_reap_once_continues_after_teardown_failure(tmp_path: Path) -> None:
    # If one sandbox's teardown hits a DockerError, the reaper should log and
    # continue so sibling sandboxes still get reclaimed this tick.
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker() as d:
        await lc.acquire("blogging.planner")
        await lc.acquire("blogging.writer")
        for aid in ("blogging.planner", "blogging.writer"):
            lc._state[aid].last_used_at = lc._state[aid].last_used_at - timedelta(minutes=30)

        # Fail the first teardown, succeed the second.
        calls = {"n": 0}

        async def flaky_stop(_container_id: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise provisioner_mod.DockerError("daemon blip")

        d.stop.side_effect = flaky_stop
        reaped = await lc.reap_once(threshold=60)

    # Exactly one sandbox torn down; the other remains for the next tick.
    assert len(reaped) == 1
    assert len(lc._state) == 1


@pytest.mark.asyncio
async def test_unknown_agent_raises_without_docker_call(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with (
        patch(
            "agent_provisioning_team.sandbox.lifecycle._resolve_team",
            side_effect=UnknownAgentError("No agent manifest for 'ghost.agent'"),
        ),
        patch.object(provisioner_mod, "run_container", new=AsyncMock()) as run_mock,
    ):
        with pytest.raises(UnknownAgentError):
            await lc.acquire("ghost.agent")
    run_mock.assert_not_awaited()


def test_state_persists_across_lifecycle_instances(tmp_path: Path) -> None:
    lc1 = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        asyncio.run(lc1.acquire("blogging.planner"))
    lc2 = _lifecycle(tmp_path)
    assert set(lc2._state) == {"blogging.planner"}
    assert lc2._state["blogging.planner"].status == SandboxStatus.WARM
    assert lc2._state["blogging.planner"].container_id == "abc123"


def test_build_run_argv_applies_hardening() -> None:
    argv = _build_run_argv(agent_id="blogging.planner", container_name="khala-sbx-blogging.planner")
    assert "--cap-drop=ALL" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges:true" in argv
    assert "--security-opt=seccomp=default" in argv
    assert "--pids-limit=512" in argv
    assert "--memory=1g" in argv
    assert "--cpus=1.0" in argv
    p_index = argv.index("-p")
    assert argv[p_index + 1] == "127.0.0.1::8090"
    assert argv.count("--tmpfs") == 2
    assert any(a == "SANDBOX_AGENT_ID=blogging.planner" for a in argv)
    n_index = argv.index("--network")
    assert argv[n_index + 1] == "khala-sandbox"


def test_build_run_argv_excludes_secret_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #257: secrets must never reach the sandbox via ``-e``.

    With a secrets bind-mount path supplied, ``_build_run_argv`` must carry the
    mount + the ``SANDBOX_SECRETS_FILE`` pointer, and must NOT emit any ``-e``
    entry whose value matches a host secret.
    """
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret-xyz")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pg-secret-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret-xyz")

    argv = _build_run_argv(
        agent_id="blogging.planner",
        container_name="khala-sbx-blogging.planner",
        secrets_host_path=Path("/tmp/secrets/blogging.env"),
    )

    joined = " ".join(argv)
    for secret in ("ollama-secret-xyz", "pg-secret-xyz", "ant-secret-xyz"):
        assert secret not in joined, f"{secret!r} leaked into docker run argv"
    # Bind-mount + pointer env are present.
    assert any(
        a.startswith("type=bind,source=/tmp/secrets/blogging.env,target=/run/secrets/sandbox-env")
        for a in argv
    )
    assert "SANDBOX_SECRETS_FILE=/run/secrets/sandbox-env" in argv


def test_build_run_argv_without_secrets_skips_mount() -> None:
    """The bind-mount flags only appear when the caller supplies a path.

    Tests that don't care about secrets (e.g. the hardening assertions above)
    still get a clean argv.
    """
    argv = _build_run_argv(
        agent_id="blogging.planner",
        container_name="khala-sbx-blogging.planner",
    )
    assert not any("sandbox-env" in a for a in argv)
    assert not any(a.startswith("SANDBOX_SECRETS_FILE=") for a in argv)


def test_write_sandbox_secrets_file_per_team_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-team password var wins over the global POSTGRES_* creds."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setenv("POSTGRES_USER", "global_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "global_pw")
    monkeypatch.setenv("POSTGRES_DB", "global_db")
    monkeypatch.setenv("POSTGRES_PASSWORD_SANDBOX_BLOGGING", "team-pw-xyz")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    path = provisioner_mod._write_sandbox_secrets_file("khala-sbx-blog", "blogging")
    assert path.exists()
    # Permissions: 0400 — read-only for the owning process, no group/other.
    assert (path.stat().st_mode & 0o777) == 0o400
    body = path.read_text(encoding="utf-8")
    assert "POSTGRES_USER=sandbox_blogging\n" in body
    assert "POSTGRES_PASSWORD=team-pw-xyz\n" in body
    assert "POSTGRES_DB=sandbox_blogging\n" in body
    assert "OLLAMA_API_KEY=ollama-xyz\n" in body
    # Values not in the host env must not appear with an empty RHS.
    assert "ANTHROPIC_API_KEY" not in body
    # Global creds must NOT leak when the per-team var is set.
    assert "global_user" not in body
    assert "global_pw" not in body
    assert "global_db" not in body


def test_write_sandbox_secrets_file_falls_back_to_global(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No team-scoped password → warn and fall back to global POSTGRES_* creds."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setenv("POSTGRES_USER", "global_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "global_pw")
    monkeypatch.setenv("POSTGRES_DB", "global_db")
    monkeypatch.delenv("POSTGRES_PASSWORD_SANDBOX_BRANDING", raising=False)

    with caplog.at_level("WARNING", logger="agent_provisioning_team.sandbox.provisioner"):
        path = provisioner_mod._write_sandbox_secrets_file("khala-sbx-brand", "branding")

    body = path.read_text(encoding="utf-8")
    assert "POSTGRES_USER=global_user\n" in body
    assert "POSTGRES_PASSWORD=global_pw\n" in body
    assert "POSTGRES_DB=global_db\n" in body
    assert any("POSTGRES_PASSWORD_SANDBOX_BRANDING" in record.message for record in caplog.records)


def test_cleanup_secrets_file_removes_host_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cleanup_secrets_file`` unlinks the host-side tmpfile; missing file is a no-op."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setenv("POSTGRES_PASSWORD_SANDBOX_BLOGGING", "x")
    path = provisioner_mod._write_sandbox_secrets_file("khala-sbx-blog", "blogging")
    assert path.exists()

    provisioner_mod.cleanup_secrets_file("khala-sbx-blog")
    assert not path.exists()
    # Idempotent: second call on the same name is a no-op.
    provisioner_mod.cleanup_secrets_file("khala-sbx-blog")


@pytest.mark.asyncio
async def test_run_container_writes_and_mounts_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public ``run_container`` path must write a 0400 secrets file and
    mount it into the sandbox; on docker failure it must clean up."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setenv("POSTGRES_PASSWORD_SANDBOX_BLOGGING", "team-pw")

    captured_argv: list[list[str]] = []

    async def fake_exec(cmd: list[str], *, timeout_s: int = 30):
        captured_argv.append(cmd)
        return 0, "abc123\n", ""

    async def fake_network() -> None:
        return None

    monkeypatch.setattr(provisioner_mod, "_exec", fake_exec)
    monkeypatch.setattr(provisioner_mod, "ensure_network", fake_network)

    container_id = await provisioner_mod.run_container(
        agent_id="blogging.planner", container_name="khala-sbx-blog", team="blogging"
    )
    assert container_id == "abc123"

    # The argv passed to docker carries the bind-mount spec.
    argv = captured_argv[-1]
    mount_specs = [a for a in argv if a.startswith("type=bind,source=")]
    assert len(mount_specs) == 1
    secrets_path = Path(mount_specs[0].split("source=", 1)[1].split(",", 1)[0])
    assert secrets_path.exists()
    assert (secrets_path.stat().st_mode & 0o777) == 0o400


def test_container_name_is_dns_safe() -> None:
    name = container_name_for("blogging.planner")
    assert name.startswith("khala-sbx-blogging.planner-")
    # readable prefix + 8 lowercase-hex char digest suffix.
    suffix = name.rsplit("-", 1)[1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    # Deterministic.
    assert container_name_for("blogging.planner") == name
    # Empty id still yields a valid container name.
    assert container_name_for("").startswith("khala-sbx-agent-")


@pytest.mark.asyncio
async def test_stop_container_is_idempotent_on_missing_container() -> None:
    with patch.object(
        provisioner_mod,
        "_exec",
        new=AsyncMock(return_value=(1, "", "Error: No such container: abc")),
    ):
        await provisioner_mod.stop_container("abc")  # must not raise


@pytest.mark.asyncio
async def test_stop_container_raises_on_real_failure() -> None:
    with patch.object(
        provisioner_mod,
        "_exec",
        new=AsyncMock(return_value=(1, "", "Cannot connect to the Docker daemon")),
    ):
        with pytest.raises(provisioner_mod.DockerError):
            await provisioner_mod.stop_container("abc")


def test_container_name_is_collision_resistant_under_sanitization() -> None:
    # Two ids that sanitize to the same readable prefix still get distinct
    # container names, so the acquire-time zombie reap cannot accidentally
    # tear down another agent's live sandbox.
    assert container_name_for("agent/1") != container_name_for("agent-1")
    assert container_name_for("a b") != container_name_for("a-b")


@pytest.mark.asyncio
async def test_run_idle_reaper_is_cancellable(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    with (
        patch(
            "agent_provisioning_team.sandbox.lifecycle.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch.object(Lifecycle, "reap_once", new=AsyncMock(return_value=[])),
    ):
        task = asyncio.create_task(lc.run_idle_reaper(interval_s=0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_handle_from_state_projects_url_and_idle() -> None:
    t = now()
    warm = SandboxState(
        agent_id="blogging.planner",
        team="blogging",
        container_name="khala-sbx-blogging.planner",
        container_id="abc123",
        host_port=55123,
        status=SandboxStatus.WARM,
        created_at=t,
        last_used_at=t,
    )
    handle = SandboxHandle.from_state(warm)
    assert handle.url == "http://127.0.0.1:55123"
    assert handle.container_id == "abc123"
    assert handle.host_port == 55123
    assert handle.team == "blogging"

    cold = SandboxState(
        agent_id="x.y",
        team="blogging",
        container_name="khala-sbx-x.y",
        status=SandboxStatus.COLD,
        created_at=t,
        last_used_at=t,
    )
    assert SandboxHandle.from_state(cold).url is None


# --- tests for the small module-level helpers ------------------------------


def test_sandbox_image_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IMAGE", "my/custom:tag")
    assert state_mod.sandbox_image() == "my/custom:tag"
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IMAGE")
    assert state_mod.sandbox_image() == "khala-agent-sandbox:latest"


def test_idle_threshold_reads_per_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES", "2")
    assert state_mod.idle_teardown_seconds() == 120
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES")
    assert state_mod.idle_teardown_seconds() == 300  # 5-minute default


def test_state_file_path_uses_agent_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_provisioning_team.sandbox import state as state_mod

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_STATE_FILE", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    path = state_mod.state_file_path()
    assert path == tmp_path / "agent_provisioning" / "sandboxes" / "state.json"


# ---------------------------------------------------------------------------
# Module-level free-function wrappers (Phase 3, issue #265).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /metrics snapshot — issue #302
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_empty_state(tmp_path: Path) -> None:
    """A fresh Lifecycle has no residents, no reaper ticks, no samples."""
    lc = _lifecycle(tmp_path)
    snap = await lc.metrics()
    assert snap.resident == 0
    assert snap.by_team == {}
    assert snap.by_status == {}
    assert snap.ages_seconds.min == 0 == snap.ages_seconds.max
    assert snap.ages_seconds.p50 == 0 == snap.ages_seconds.p95
    assert snap.boot_ms.samples == 0
    assert snap.boot_ms.p50 == 0 == snap.boot_ms.p95
    assert snap.reaper.last_tick_at is None
    assert snap.reaper.interval_s is None
    assert snap.reaper.torn_down_total == 0
    assert snap.reaper.torn_down_last_tick == 0
    # Threshold comes from the env-backed helper so it's always populated.
    assert snap.reaper.threshold_s > 0


def test_percentile_uses_nearest_rank() -> None:
    """Regression for PR #304 Codex feedback: the helper must use ceil, not
    round, so p95 on 11 samples lands on index 10 (the textbook nearest-rank
    rank) rather than underestimating at index 9."""
    from agent_provisioning_team.sandbox.lifecycle import _percentile

    samples = list(range(1, 12))  # 1..11, pre-sorted
    # p95 of 11 samples: ceil(0.95 * 11) = 11 → index 10 → value 11.
    assert _percentile(samples, 95) == 11
    # p50 of 11 samples: ceil(0.5 * 11) = 6 → index 5 → value 6.
    assert _percentile(samples, 50) == 6
    # Empty input → 0 (safe default for the all-empty /metrics snapshot).
    assert _percentile([], 95) == 0
    # Single sample → that sample, regardless of percentile.
    assert _percentile([42], 50) == 42
    assert _percentile([42], 95) == 42


@pytest.mark.asyncio
async def test_metrics_after_acquire_records_boot_sample(tmp_path: Path) -> None:
    """acquire() pushes a boot_ms sample and lights up the by_team/by_status
    counters — confirming the Phase 6 cold-start hook feeds /metrics."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")
    snap = await lc.metrics()
    assert snap.resident == 1
    assert snap.by_team == {"blogging": 1}
    assert snap.by_status == {"warm": 1}
    assert snap.boot_ms.samples == 1
    # Percentiles collapse to the single sample on n=1.
    assert snap.boot_ms.p50 == snap.boot_ms.p95
    assert snap.ages_seconds.min >= 0
    assert snap.ages_seconds.max >= snap.ages_seconds.min


@pytest.mark.asyncio
async def test_metrics_reaper_counters_advance_per_tick(tmp_path: Path) -> None:
    """Every reap_once() call must stamp last_tick_at and update the torn-down
    counters — even when it finds nothing to reap — so operators can tell the
    reaper is alive."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(), _patched_docker():
        await lc.acquire("blogging.planner")

    # First tick: fresh sandbox, nothing reaped, but last_tick_at stamps.
    await lc.reap_once(threshold=3600)
    snap = await lc.metrics()
    assert snap.reaper.last_tick_at is not None
    assert snap.reaper.torn_down_total == 0
    assert snap.reaper.torn_down_last_tick == 0

    # Back-date and reap — mirrors the pattern used in
    # test_reap_once_tears_down_idle.
    lc._state["blogging.planner"].last_used_at = lc._state[
        "blogging.planner"
    ].last_used_at - timedelta(minutes=30)
    with _patched_registry(), _patched_docker():
        reaped = await lc.reap_once(threshold=60)
    assert reaped == ["blogging.planner"]
    snap = await lc.metrics()
    assert snap.resident == 0
    assert snap.reaper.torn_down_total == 1
    assert snap.reaper.torn_down_last_tick == 1


@pytest.mark.asyncio
async def test_metrics_boot_ms_window_is_bounded(tmp_path: Path) -> None:
    """The boot_ms buffer must stay bounded so long-running APIs don't leak —
    guards against someone swapping the deque for an unbounded list."""
    lc = _lifecycle(tmp_path)
    for i in range(600):
        lc._boot_ms_samples.append(i)
    snap = await lc.metrics()
    # Deque with maxlen=500 drops the oldest 100 samples.
    assert snap.boot_ms.samples == 500
    assert snap.boot_ms.p50 > 0
    assert snap.boot_ms.p95 >= snap.boot_ms.p50


@pytest.mark.asyncio
async def test_metrics_by_status_groups_across_agents(tmp_path: Path) -> None:
    """Two acquires on different teams land in distinct by_team buckets;
    by_status uses the lowercase enum value."""
    lc = _lifecycle(tmp_path)
    with _patched_registry(team="blogging"), _patched_docker():
        await lc.acquire("blogging.planner")
    with (
        _patched_registry(team="branding"),
        _patched_docker(container_id="brand1", host_port=55124),
    ):
        await lc.acquire("branding.logo")
    snap = await lc.metrics()
    assert snap.resident == 2
    assert snap.by_team == {"blogging": 1, "branding": 1}
    assert snap.by_status == {"warm": 2}
    assert snap.boot_ms.samples == 2


@pytest.mark.asyncio
async def test_run_idle_reaper_records_interval(tmp_path: Path) -> None:
    """run_idle_reaper must publish its configured interval so /metrics can
    surface it — operators need both the threshold and the tick cadence."""
    lc = _lifecycle(tmp_path)

    async def _cancel(self_: Lifecycle, *, threshold: int) -> list[str]:
        raise asyncio.CancelledError

    with (
        # Mock the in-loop sleep so the reaper doesn't stall 42s before reaping.
        # Patching asyncio.sleep globally would also mock any `await sleep(0)`
        # in the test itself — hence we drive cancellation from reap_once below.
        patch(
            "agent_provisioning_team.sandbox.lifecycle.asyncio.sleep",
            new=AsyncMock(),
        ),
        patch.object(Lifecycle, "reap_once", new=_cancel),
    ):
        with pytest.raises(asyncio.CancelledError):
            await lc.run_idle_reaper(interval_s=42)

    assert lc._reaper_interval_s == 42
    snap = await lc.metrics()
    assert snap.reaper.interval_s == 42


@pytest.mark.asyncio
async def test_module_helpers_delegate_to_singleton(tmp_path: Path, monkeypatch) -> None:
    """`sandbox.acquire/teardown/…` must operate on the same Lifecycle instance
    so that status swings are observable across calls — the unified API routes
    rely on this to reconcile invoke + list + teardown."""
    from agent_provisioning_team import sandbox as sb
    from agent_provisioning_team.sandbox import lifecycle as lifecycle_mod

    lc = _lifecycle(tmp_path)
    lifecycle_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lifecycle_mod, "get_lifecycle", lambda: lc)

    with _patched_registry(), _patched_docker() as d:
        handle = await sb.acquire("blogging.planner")
    assert handle.status == SandboxStatus.WARM
    assert (await sb.list_active())[0].agent_id == "blogging.planner"

    # note_activity bumps last_used_at on the same state row.
    await sb.note_activity("blogging.planner")

    # teardown via the module helper clears state.
    with _patched_registry(), _patched_docker():
        await sb.teardown("blogging.planner")
    assert await sb.list_active() == []
    d.run.assert_awaited_once()
