"""
Persistent story bank for author narratives.

Stores first-person stories elicited by the ghost writer so they can be reused
across future blog posts.  Each story is tagged with keywords, a one-sentence
semantic summary, and the section context it was originally written for.

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
    # Migration: add summary column (safe if already exists)
    try:
        conn.execute("ALTER TABLE stories ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
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
    llm_client: Any = None,
) -> str:
    """Persist a story narrative and return its ID.

    If *llm_client* is provided, generates a one-sentence semantic summary
    for improved retrieval relevance.
    """
    story_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    kw_json = json.dumps(keywords or [], ensure_ascii=False)

    # Generate semantic summary if LLM is available
    summary = ""
    if llm_client is not None:
        try:
            summary = (
                llm_client.complete(
                    f"Summarize this story in one sentence:\n\n{narrative}",
                    system_prompt="Write a single sentence summary. No preamble, no quotes.",
                )
                or ""
            ).strip()
        except Exception as e:
            logger.warning("Story bank: summary generation failed (non-fatal): %s", e)

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO stories (id, narrative, section_title, section_context, keywords, source_job_id, created_at, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, narrative, section_title, section_context, kw_json, source_job_id, now, summary),
        )
        conn.commit()
        logger.info(
            "Story bank: saved story %s (section=%s, keywords=%s, has_summary=%s)",
            story_id,
            section_title,
            keywords,
            bool(summary),
        )
        return story_id
    finally:
        conn.close()


def find_relevant_stories(
    query_keywords: List[str],
    limit: int = 5,
    story_opportunity: Optional[str] = None,
    llm_client: Any = None,
) -> List[Dict[str, Any]]:
    """Return stories relevant to *query_keywords*, ranked by relevance.

    Uses a two-stage approach:
    1. **Fast path**: keyword overlap scoring (set intersection) to get top candidates.
    2. **Slow path** (if *story_opportunity* and *llm_client* are provided): LLM-scored
       reranking of candidates with summaries for better semantic relevance.

    Each result is a dict with keys: id, narrative, section_title, section_context,
    keywords, summary, created_at.
    """
    if not query_keywords:
        return []

    # Stage 1: keyword-based candidate retrieval
    candidates = _keyword_scored_candidates(query_keywords, limit=max(limit, 10))

    if not candidates:
        return []

    # Stage 2: LLM reranking (optional, when we have summaries and an opportunity description)
    candidates_with_summaries = [c for c in candidates if c.get("summary")]
    if story_opportunity and llm_client and len(candidates_with_summaries) > limit:
        reranked = _llm_rerank(candidates_with_summaries, story_opportunity, llm_client, limit)
        if reranked:
            return reranked

    return candidates[:limit]


def _keyword_scored_candidates(query_keywords: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Retrieve stories ranked by keyword overlap count."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, narrative, section_title, section_context, keywords, summary, created_at FROM stories"
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


def _llm_rerank(
    candidates: List[Dict[str, Any]],
    story_opportunity: str,
    llm_client: Any,
    limit: int,
) -> List[Dict[str, Any]]:
    """Use LLM to rerank candidates by semantic relevance to the story opportunity."""
    summaries = "\n".join(
        f"{i + 1}. {c['summary']}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"Story needed: {story_opportunity}\n\n"
        f"Candidate stories (by summary):\n{summaries}\n\n"
        f"Return a JSON array of the top {limit} indices (1-based) ranked by relevance "
        f"to the story needed. Most relevant first."
    )
    try:
        data = llm_client.complete_json(
            prompt,
            system_prompt="Return a JSON array of integers only. No other text.",
        )
        # Handle various return formats
        indices = data if isinstance(data, list) else data.get("indices", data.get("text", []))
        if isinstance(indices, list):
            reranked = []
            for idx in indices[:limit]:
                i = int(idx) - 1  # 1-based to 0-based
                if 0 <= i < len(candidates):
                    reranked.append(candidates[i])
            if reranked:
                return reranked
    except Exception as e:
        logger.warning("Story bank LLM reranking failed (falling back to keyword scoring): %s", e)
    return []


def list_stories(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Return all stories, newest first."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, narrative, section_title, section_context, keywords, summary, created_at "
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
            "SELECT id, narrative, section_title, section_context, keywords, summary, created_at "
            "FROM stories WHERE id = ?",
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
