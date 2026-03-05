"""
Planning cache: reuse Tech Lead outputs when spec and architecture are unchanged.

When SW_ENABLE_PLANNING_CACHE=1, the orchestrator checks for a cached TaskAssignment
before running the Tech Lead. Cache key = hash(normalized spec + architecture + project overview).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CACHE_SUBDIR = "planning_cache"


def _cache_dir(plan_dir: Path) -> Path:
    """Return the planning cache directory under plan_dir."""
    d = Path(plan_dir).resolve() / CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def compute_planning_cache_key(
    spec_content: str,
    architecture_overview: str,
    project_overview: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Compute a stable cache key from planning inputs.
    Same inputs produce same key; used to detect when planning can be skipped.
    """
    parts = [
        (spec_content or "").strip(),
        (architecture_overview or "").strip(),
    ]
    if project_overview:
        po_str = json.dumps(
            {
                "primary_goal": project_overview.get("primary_goal"),
                "delivery_strategy": project_overview.get("delivery_strategy"),
                "features_and_functionality_doc": (project_overview.get("features_and_functionality_doc") or "")[:2000],
            },
            sort_keys=True,
        )
        parts.append(po_str)
    blob = "\n---\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def get_cached_plan(
    plan_dir: Path,
    cache_key: str,
) -> Optional[Dict[str, Any]]:
    """
    Load cached planning result if key matches.
    Returns dict with assignment, requirement_task_mapping, summary, or None if miss.
    """
    cache_path = _cache_dir(plan_dir) / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("cache_key") != cache_key:
            return None
        logger.info("Planning cache HIT (key=%s)", cache_key[:12])
        return data
    except Exception as e:
        logger.warning("Planning cache read failed: %s", e)
        return None


def set_cached_plan(
    plan_dir: Path,
    cache_key: str,
    assignment: Any,
    requirement_task_mapping: list,
    summary: str = "",
) -> None:
    """
    Store planning result for reuse.
    assignment should be serializable (e.g. model_dump() or dict).
    """
    cache_path = _cache_dir(plan_dir) / f"{cache_key}.json"
    try:
        assignment_dict = assignment.model_dump() if hasattr(assignment, "model_dump") else assignment
        data = {
            "cache_key": cache_key,
            "assignment": assignment_dict,
            "requirement_task_mapping": requirement_task_mapping,
            "summary": summary,
        }
        cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Planning cache SET (key=%s)", cache_key[:12])
    except Exception as e:
        logger.warning("Planning cache write failed: %s", e)
