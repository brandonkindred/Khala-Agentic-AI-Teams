"""File-based storage for chat conversation history (Nutrition & Meal Planning team).

Stores messages per client_id as a JSON file in the agent cache directory.
Each file contains a list of message dicts with role, content, timestamp, phase, and action.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _default_storage_dir() -> Path:
    base = os.environ.get("AGENT_CACHE", ".agent_cache")
    return Path(base) / "nutrition_meal_planning_team" / "conversations"


def _conversation_path(storage_dir: Path, client_id: str) -> Path:
    return storage_dir / f"{client_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conversation(
    client_id: str, storage_dir: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Load conversation history for a client. Returns empty list if not found."""
    directory = storage_dir or _default_storage_dir()
    path = _conversation_path(directory, client_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load conversation for %s", client_id)
        return []


def append_message(
    client_id: str,
    role: str,
    content: str,
    *,
    phase: Optional[str] = None,
    action: Optional[str] = None,
    storage_dir: Optional[Path] = None,
) -> None:
    """Append a single message to the conversation history."""
    directory = storage_dir or _default_storage_dir()
    directory.mkdir(parents=True, exist_ok=True)

    messages = get_conversation(client_id, storage_dir=directory)
    messages.append({
        "role": role,
        "content": content,
        "timestamp": _now(),
        "phase": phase,
        "action": action,
    })

    path = _conversation_path(directory, client_id)
    try:
        path.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Failed to save conversation for %s", client_id, exc_info=True)


def clear_conversation(
    client_id: str, storage_dir: Optional[Path] = None
) -> None:
    """Delete conversation history for a client."""
    directory = storage_dir or _default_storage_dir()
    path = _conversation_path(directory, client_id)
    if path.exists():
        path.unlink(missing_ok=True)
