"""JSON-on-disk checkpoint of warm sandboxes.

Restart safety: if the unified_api restarts while sandboxes are running, we
reload the last-known state from disk and reconcile with ``docker compose ps``
on the next request.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .models import SandboxState, SandboxStatus

logger = logging.getLogger(__name__)

_lock = Lock()


def _serialise(state: dict[str, SandboxState]) -> str:
    return json.dumps(
        {team: s.model_dump(mode="json") for team, s in state.items()},
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
        for team, entry in (raw or {}).items():
            try:
                out[team] = SandboxState.model_validate(entry)
            except Exception as exc:
                logger.warning("Dropping malformed sandbox state entry %s: %s", team, exc)
        return out


def save(path: Path, state: dict[str, SandboxState]) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialise(state), encoding="utf-8")


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_state(team: str, service_name: str, container_name: str, host_port: int) -> SandboxState:
    t = now()
    return SandboxState(
        team=team,
        service_name=service_name,
        container_name=container_name,
        host_port=host_port,
        status=SandboxStatus.WARMING,
        created_at=t,
        last_used_at=t,
    )
