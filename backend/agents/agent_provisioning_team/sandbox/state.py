"""On-disk checkpoint for per-agent sandbox lifecycle state.

Keyed by ``agent_id`` so the lifecycle owner can run one sandbox per
specialist agent.

Restart safety: if the unified API or the provisioning process restarts, the
Lifecycle reloads the last-known state from disk and reconciles with
``docker inspect`` on the next request.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Shared with the invoke proxy so a single change updates both producer and
# consumer of the cold-start log marker.
COLD_START_LOG_PREFIX = "sandbox.cold_start"


class SandboxStatus(str, Enum):
    """Lifecycle states for a per-agent sandbox."""

    COLD = "cold"
    WARMING = "warming"
    WARM = "warm"
    ERROR = "error"


class SandboxState(BaseModel):
    """Persistent state for one agent sandbox, checkpointed to JSON."""

    agent_id: str
    team: str
    container_name: str
    container_id: str | None = None
    host_port: int | None = None
    status: SandboxStatus
    created_at: datetime
    last_used_at: datetime
    error: str | None = None


class SandboxHandle(BaseModel):
    """Caller-facing view derived from :class:`SandboxState`."""

    agent_id: str
    team: str
    status: SandboxStatus
    url: str | None = Field(
        default=None,
        description="Base URL of the sandbox service (e.g. http://127.0.0.1:55123). "
        "None until the container is WARM.",
    )
    container_name: str
    container_id: str | None = None
    host_port: int | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    idle_seconds: int | None = None
    error: str | None = None
    boot_ms: int | None = Field(
        default=None,
        description="Cold-start latency in milliseconds — the wall-clock time from the start of "
        "`acquire()` to a successful `/health` probe. None on warm-path returns.",
    )

    @classmethod
    def from_state(cls, st: SandboxState, *, boot_ms: int | None = None) -> SandboxHandle:
        url = (
            f"http://127.0.0.1:{st.host_port}"
            if st.status == SandboxStatus.WARM and st.host_port is not None
            else None
        )
        idle = int((now() - st.last_used_at).total_seconds()) if st.last_used_at else None
        return cls(
            agent_id=st.agent_id,
            team=st.team,
            status=st.status,
            url=url,
            container_name=st.container_name,
            container_id=st.container_id,
            host_port=st.host_port,
            created_at=st.created_at,
            last_used_at=st.last_used_at,
            idle_seconds=idle,
            error=st.error,
            boot_ms=boot_ms,
        )


class AgeStats(BaseModel):
    """Age percentiles across every resident sandbox, in seconds."""

    min: int = 0
    p50: int = 0
    p95: int = 0
    max: int = 0


class BootMsStats(BaseModel):
    """Cold-start latency percentiles over the recent sample window."""

    p50: int = 0
    p95: int = 0
    samples: int = 0


class ReaperStats(BaseModel):
    """Idle-reaper observability — in-process, reset at Lifecycle construction."""

    last_tick_at: datetime | None = None
    interval_s: int | None = None
    threshold_s: int
    torn_down_total: int = 0
    torn_down_last_tick: int = 0


class SandboxMetrics(BaseModel):
    """Live snapshot of the per-agent sandbox pool (issue #302).

    Returned from ``GET /api/agents/sandboxes/metrics``. No persistence —
    counters reset when the unified API restarts.
    """

    resident: int
    by_team: dict[str, int]
    by_status: dict[str, int]
    ages_seconds: AgeStats
    reaper: ReaperStats
    boot_ms: BootMsStats


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_state(agent_id: str, team: str, container_name: str) -> SandboxState:
    """Construct a freshly-WARMING state row for ``agent_id``."""
    t = now()
    return SandboxState(
        agent_id=agent_id,
        team=team,
        container_name=container_name,
        status=SandboxStatus.WARMING,
        created_at=t,
        last_used_at=t,
    )


def resolve_cache_path(*parts: str) -> Path:
    """Resolve ``${AGENT_CACHE:-/tmp/agents}/<parts>`` for any sandbox/test artifact.

    Centralised so the half-dozen ad-hoc ``Path(os.environ.get("AGENT_CACHE", ...))``
    spellings stay in sync.
    """
    return Path(os.environ.get("AGENT_CACHE", "/tmp/agents")).joinpath(*parts)


def state_file_path() -> Path:
    """Where to persist sandbox state across restarts.

    Override with ``AGENT_PROVISIONING_SANDBOX_STATE_FILE``; otherwise defaults
    to ``${AGENT_CACHE:-/tmp/agents}/agent_provisioning/sandboxes/state.json``.
    """
    override = os.environ.get("AGENT_PROVISIONING_SANDBOX_STATE_FILE")
    if override:
        return Path(override)
    return resolve_cache_path("agent_provisioning", "sandboxes", "state.json")


def idle_teardown_seconds() -> int:
    """Read ``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES`` (default 5) from the env."""
    return int(os.environ.get("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES", "5")) * 60


def boot_timeout_seconds() -> int:
    """How long to wait for a sandbox ``/health`` probe to succeed. Default 90s."""
    return int(os.environ.get("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", "90"))


def sandbox_image() -> str:
    """Image tag for the unified single-agent sandbox (Phase 1, issue #263)."""
    return os.environ.get("AGENT_PROVISIONING_SANDBOX_IMAGE", "khala-agent-sandbox:latest")


def sandbox_network() -> str:
    """Docker bridge network for sandbox containers.

    Created on demand by :func:`provisioner.ensure_network` the first time a
    sandbox is provisioned; override with ``AGENT_PROVISIONING_SANDBOX_NETWORK``.
    """
    return os.environ.get("AGENT_PROVISIONING_SANDBOX_NETWORK", "khala-sandbox")


def load(path: Path) -> dict[str, SandboxState]:
    """Load state from disk. Missing file → empty dict. Corrupt file → warn + empty."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load sandbox state from %s: %s", path, exc)
        return {}
    out: dict[str, SandboxState] = {}
    for agent_id, entry in (raw or {}).items():
        try:
            out[agent_id] = SandboxState.model_validate(entry)
        except Exception as exc:
            logger.warning("Dropping malformed sandbox state entry %s: %s", agent_id, exc)
    return out


def save(path: Path, state: dict[str, SandboxState]) -> None:
    """Atomically persist ``state`` to ``path`` (tmpfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {agent_id: s.model_dump(mode="json") for agent_id, s in state.items()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
