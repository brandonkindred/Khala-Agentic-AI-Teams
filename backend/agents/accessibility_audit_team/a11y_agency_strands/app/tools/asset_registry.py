"""Thread-safe YAML asset registry for the accessibility audit team.

Replaces scattered per-file global caches with a single, thread-safe
registry that loads and caches any YAML asset under the ``assets/``
directory by filename.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"


class AssetRegistry:
    """Thread-safe, lazy-loading cache for YAML asset files."""

    _cache: dict[str, Any] = {}
    _lock = threading.Lock()

    @classmethod
    def load(cls, name: str) -> dict:
        """Load a YAML asset by filename, returning the cached result.

        Args:
            name: Filename relative to the ``assets/`` directory,
                e.g. ``"site_architecture_audit_template.yaml"``.

        Returns:
            Parsed YAML dict.
        """
        if name in cls._cache:
            return cls._cache[name]
        with cls._lock:
            if name not in cls._cache:
                path = _ASSETS_DIR / name
                with open(path) as fh:
                    cls._cache[name] = yaml.safe_load(fh)
        return cls._cache[name]

    @classmethod
    def clear(cls) -> None:
        """Clear the cache (useful in tests)."""
        with cls._lock:
            cls._cache.clear()
