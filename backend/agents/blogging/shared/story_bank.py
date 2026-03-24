"""
Persistent story bank for author narratives.

Stores first-person stories elicited by the ghost writer so they can be reused
across future blog posts.  Each story is tagged with keywords and the section
context it was originally written for, enabling relevance-based retrieval.

Storage: SQLite database at ``{AGENT_CACHE}/blogging_team/story_bank.db``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CACHE = ".agent_cache"
_DB_FILENAME = "story_bank.db"
_lock = threading.Lock()


def _db_path() -> str:
    cache = os.environ.get("AGENT_CACHE", _DEFAULT_CACHE)
    directory = Path(cache) / "blogging_team"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / _DB_FILENAME)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stories (
            id          TEXT PRIMARY KEY,
            narrative   TEXT NOT NULL,
            section_title   TEXT NOT NULL DEFAULT '',
            section_context TEXT NOT NULL DEFAULT '',
            keywords    TEXT NOT NULL DEFAULT '[]',
            source_job_id   TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stories_keywords
        ON stories (keywords)
        """
    )
    conn.commit()


_schema_ensured = False


def _conn() -> sqlite3.Connection:
    global _schema_ensured
    c = _get_conn()
    if not _schema_ensured:
        with _lock:
            if not _schema_ensured:
                _ensure_schema(c)
                _schema_ensured = True
    return c


def save_story(
    narrative: str,
    section_title: str = "",
    section_context: str = "",
    keywords: Optional[List[str]] = None,
    source_job_id: Optional[str] = None,
) -> str:
    """Persist a story narrative and return its ID."""
    story_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    kw_json = json.dumps(keywords or [], ensure_ascii=False)

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO stories (id, narrative, section_title, section_context, keywords, source_job_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (story_id, narrative, section_title, section_context, kw_json, source_job_id, now),
        )
        conn.commit()
        logger.info(
            "Story bank: saved story %s (section=%s, keywords=%s)",
            story_id,
            section_title,
            keywords,
        )
        return story_id
    finally:
        conn.close()


def find_relevant_stories(
    query_keywords: List[str],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Return stories whose keywords overlap with *query_keywords*, ranked by overlap count.

    Each result is a dict with keys: id, narrative, section_title, section_context, keywords, created_at.
    """
    if not query_keywords:
        return []

    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, narrative, section_title, section_context, keywords, created_at FROM stories"
        ).fetchall()
    finally:
        conn.close()

    query_lower = {k.lower().strip() for k in query_keywords if k.strip()}
    if not query_lower:
        return []

    scored: List[tuple] = []
    for row in rows:
        try:
            story_kw = {k.lower().strip() for k in json.loads(row["keywords"])}
        except (json.JSONDecodeError, TypeError):
            story_kw = set()
        overlap = len(query_lower & story_kw)
        if overlap > 0:
            scored.append((overlap, dict(row)))

    scored.sort(key=lambda t: t[0], reverse=True)
    results = []
    for _, item in scored[:limit]:
        item["keywords"] = (
            json.loads(item["keywords"]) if isinstance(item["keywords"], str) else item["keywords"]
        )
        results.append(item)
    return results


def list_stories(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Return all stories, newest first."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, narrative, section_title, section_context, keywords, created_at "
            "FROM stories ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        item = dict(row)
        try:
            item["keywords"] = json.loads(item["keywords"])
        except (json.JSONDecodeError, TypeError):
            item["keywords"] = []
        results.append(item)
    return results


def get_story(story_id: str) -> Optional[Dict[str, Any]]:
    """Return a single story by ID, or None."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, narrative, section_title, section_context, keywords, created_at FROM stories WHERE id = ?",
            (story_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    item = dict(row)
    try:
        item["keywords"] = json.loads(item["keywords"])
    except (json.JSONDecodeError, TypeError):
        item["keywords"] = []
    return item


def delete_story(story_id: str) -> bool:
    """Delete a story by ID. Returns True if a row was removed."""
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
