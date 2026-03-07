"""File-based storage for client profiles (Nutrition & Meal Planning team)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import ClientProfile


def _default_storage_dir() -> Path:
    base = os.environ.get("AGENT_CACHE", ".agent_cache")
    return Path(base) / "nutrition_meal_planning_team" / "profiles"


def _profile_path(storage_dir: Path, client_id: str) -> Path:
    return storage_dir / f"{client_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_profile(client_id: str, storage_dir: Optional[Path] = None) -> Optional[ClientProfile]:
    """Load client profile by client_id. Returns None if not found."""
    directory = storage_dir or _default_storage_dir()
    path = _profile_path(directory, client_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = ClientProfile.model_validate(data)
        profile.client_id = client_id
        return profile
    except Exception:
        return None


def save_profile(client_id: str, profile: ClientProfile, storage_dir: Optional[Path] = None) -> None:
    """Save client profile. Creates directory if needed."""
    directory = storage_dir or _default_storage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    profile.client_id = client_id
    profile.updated_at = _now()
    path = _profile_path(directory, client_id)
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def create_profile(client_id: str, storage_dir: Optional[Path] = None) -> ClientProfile:
    """Create a new empty profile for client_id and save it. Returns the new profile."""
    profile = ClientProfile(client_id=client_id)
    save_profile(client_id, profile, storage_dir)
    return profile


class ClientProfileStore:
    """File-based store for client profiles. Use get_profile, save_profile, create_profile."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = storage_dir or _default_storage_dir()

    def get_profile(self, client_id: str) -> Optional[ClientProfile]:
        return get_profile(client_id, self.storage_dir)

    def save_profile(self, client_id: str, profile: ClientProfile) -> None:
        save_profile(client_id, profile, self.storage_dir)

    def create_profile(self, client_id: str) -> ClientProfile:
        return create_profile(client_id, self.storage_dir)
