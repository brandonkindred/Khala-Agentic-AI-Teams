"""Storage for meal recommendations and feedback (Nutrition & Meal Planning team)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import FeedbackRecord, MealHistoryEntry


def _default_storage_dir() -> Path:
    base = os.environ.get("AGENT_CACHE", ".agent_cache")
    return Path(base) / "nutrition_meal_planning_team" / "recommendations"


def _recommendation_path(storage_dir: Path, recommendation_id: str) -> Path:
    return storage_dir / f"{recommendation_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_recommendation(
    client_id: str,
    meal_snapshot: Dict[str, Any],
    storage_dir: Optional[Path] = None,
) -> str:
    """Store a meal recommendation and return its recommendation_id."""
    directory = storage_dir or _default_storage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    recommendation_id = str(uuid4())
    path = _recommendation_path(directory, recommendation_id)
    payload = {
        "recommendation_id": recommendation_id,
        "client_id": client_id,
        "recommended_at": _now(),
        "meal_snapshot": meal_snapshot,
        "feedback": None,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return recommendation_id


def record_feedback(
    recommendation_id: str,
    rating: Optional[int] = None,
    would_make_again: Optional[bool] = None,
    notes: Optional[str] = None,
    storage_dir: Optional[Path] = None,
) -> bool:
    """Attach feedback to an existing recommendation. Returns True if found and updated."""
    directory = storage_dir or _default_storage_dir()
    path = _recommendation_path(directory, recommendation_id)
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    feedback = payload.get("feedback") or {}
    if rating is not None:
        feedback["rating"] = rating
    if would_make_again is not None:
        feedback["would_make_again"] = would_make_again
    if notes is not None:
        feedback["notes"] = notes
    feedback["recommendation_id"] = recommendation_id
    feedback["submitted_at"] = _now()
    payload["feedback"] = feedback
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return True


def get_meal_history(
    client_id: str,
    limit: int = 50,
    date_range_start: Optional[str] = None,
    date_range_end: Optional[str] = None,
    storage_dir: Optional[Path] = None,
) -> List[MealHistoryEntry]:
    """Return past recommendations with feedback for the client, newest first."""
    directory = storage_dir or _default_storage_dir()
    if not directory.exists():
        return []
    entries: List[MealHistoryEntry] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("client_id") != client_id:
                continue
            rec_at = payload.get("recommended_at") or ""
            if date_range_start and rec_at < date_range_start:
                continue
            if date_range_end and rec_at > date_range_end:
                continue
            feedback = payload.get("feedback")
            feedback_record = None
            if feedback:
                feedback_record = FeedbackRecord(
                    recommendation_id=payload.get("recommendation_id", ""),
                    rating=feedback.get("rating"),
                    would_make_again=feedback.get("would_make_again"),
                    notes=feedback.get("notes", ""),
                    submitted_at=feedback.get("submitted_at"),
                )
            entries.append(
                MealHistoryEntry(
                    recommendation_id=payload.get("recommendation_id", ""),
                    client_id=payload.get("client_id", ""),
                    meal_snapshot=payload.get("meal_snapshot", {}),
                    recommended_at=rec_at,
                    feedback=feedback_record,
                )
            )
        except Exception:
            continue
    entries.sort(key=lambda e: e.recommended_at or "", reverse=True)
    return entries[:limit]


class MealFeedbackStore:
    """Store for meal recommendations and feedback. Use module-level functions or instance methods."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = storage_dir or _default_storage_dir()

    def record_recommendation(self, client_id: str, meal_snapshot: Dict[str, Any]) -> str:
        return record_recommendation(client_id, meal_snapshot, self.storage_dir)

    def record_feedback(
        self,
        recommendation_id: str,
        rating: Optional[int] = None,
        would_make_again: Optional[bool] = None,
        notes: Optional[str] = None,
    ) -> bool:
        return record_feedback(
            recommendation_id, rating, would_make_again, notes, self.storage_dir
        )

    def get_meal_history(
        self,
        client_id: str,
        limit: int = 50,
        date_range_start: Optional[str] = None,
        date_range_end: Optional[str] = None,
    ) -> List[MealHistoryEntry]:
        return get_meal_history(
            client_id, limit, date_range_start, date_range_end, self.storage_dir
        )
