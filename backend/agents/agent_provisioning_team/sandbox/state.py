"""On-disk checkpoint for per-agent sandbox lifecycle state.

Mirrors ``agent_sandbox/state.py`` but keyed by ``agent_id`` (not team) so the
Phase 2 lifecycle owner can run one sandbox per specialist agent rather than
one per team.

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
from threading import Lock

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_lock = Lock()


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
    """Caller-facing view returned by ``Lifecycle.acquire()``."""

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


def state_file_path() -> Path:
    """Where to persist sandbox state across restarts.

    Override with ``AGENT_PROVISIONING_SANDBOX_STATE_FILE``; otherwise defaults
    to ``${AGENT_CACHE:-/tmp/agents}/agent_provisioning/sandboxes/state.json``.
    """
    override = os.environ.get("AGENT_PROVISIONING_SANDBOX_STATE_FILE")
    if override:
        return Path(override)
    cache = os.environ.get("AGENT_CACHE", "/tmp/agents")
    return Path(cache) / "agent_provisioning" / "sandboxes" / "state.json"


def idle_teardown_seconds() -> int:
    """Read ``AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES`` (default 5) from the env.

    Falls back to ``SANDBOX_IDLE_TEARDOWN_MINUTES`` for shared configuration
    with the legacy per-team reaper; when neither is set, the per-agent
    lifecycle reaps after 5 minutes (shorter than the per-team default of 15
    because individual agent sandboxes are cheaper to churn).
    """
    raw = os.environ.get("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES")
    if raw is None:
        raw = os.environ.get("SANDBOX_IDLE_TEARDOWN_MINUTES", "5")
    return int(raw) * 60


def boot_timeout_seconds() -> int:
    """How long to wait for a sandbox ``/health`` probe to succeed. Default 90s."""
    return int(os.environ.get("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", "90"))


def sandbox_image() -> str:
    """Image tag for the unified single-agent sandbox (Phase 1, issue #263)."""
    return os.environ.get("AGENT_PROVISIONING_SANDBOX_IMAGE", "khala-agent-sandbox:latest")


def sandbox_network() -> str:
    """Docker bridge network already created by ``docker/sandbox.compose.yml``."""
    return os.environ.get("AGENT_PROVISIONING_SANDBOX_NETWORK", "khala-sandbox")


def _serialise(state: dict[str, SandboxState]) -> str:
    return json.dumps(
        {agent_id: s.model_dump(mode="json") for agent_id, s in state.items()},
        indent=2,
        sort_keys=True,
    )


def load(path: Path) -> dict[str, SandboxState]:
    """Load state from disk. Missing file → empty dict. Corrupt file → warn + empty."""
    with _lock:
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
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(_serialise(state), encoding="utf-8")
        os.replace(tmp, path)
