"""Blogging API — story-bank endpoints (author story capture/reuse)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/stories", tags=["story-bank"])
def list_stories(limit: int = 50, offset: int = 0) -> list:
    """List all persisted author stories, newest first."""
    from agents.blogging.shared.story_bank import list_stories as _list

    return _list(limit=limit, offset=offset)


@router.get("/stories/{story_id}", tags=["story-bank"])
def get_story(story_id: str) -> dict:
    """Retrieve a single story by ID."""
    from agents.blogging.shared.story_bank import get_story as _get

    result = _get(story_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Story not found")
    return result


@router.delete("/stories/{story_id}", tags=["story-bank"])
def delete_story(story_id: str) -> dict:
    """Delete a story from the bank."""
    from agents.blogging.shared.story_bank import delete_story as _delete

    if not _delete(story_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Story not found")
    return {"deleted": True}


@router.get("/stories/search/{keywords}", tags=["story-bank"])
def search_stories(keywords: str, limit: int = 5) -> list:
    """Search stories by comma-separated keywords."""
    from agents.blogging.shared.story_bank import find_relevant_stories

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    return find_relevant_stories(kw_list, limit=limit)
