"""Postgres-backed story bank for author narratives.

Rewritten in PR 4 of the SQLite → Postgres migration. Stores
first-person stories elicited by the ghost writer so they can be
reused across future blog posts. Each story is tagged with keywords,
a one-sentence semantic summary, and the section context it was
originally written for.

Storage: the ``blogging_stories`` table declared in
``blogging.postgres`` and registered from the blogging team's FastAPI
lifespan. DDL lives there — this module is pure data access via
``shared_postgres.get_conn`` (pool-backed since PR 0).

The public API (module-level functions ``save_story``,
``find_relevant_stories``, ``list_stories``, ``get_story``,
``delete_story``) is unchanged so callers in
``blog_writing_process_v2.py`` and ``api/main.py`` need no edits.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row

from shared_postgres import Json, get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "blogging_story_bank"


def _row_ts(value: Any) -> str:
    """Normalize a Postgres TIMESTAMPTZ to an ISO-8601 string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``dict_row`` cursor row into the public dict shape.

    psycopg3 returns JSONB columns as Python lists/dicts already, so
    ``keywords`` needs no ``json.loads``. Timestamps become ISO strings
    to preserve the pre-migration contract.
    """
    return {
        "id": row["id"],
        "narrative": row["narrative"],
        "section_title": row["section_title"],
        "section_context": row["section_context"],
        "keywords": list(row["keywords"] or []),
        "summary": row["summary"] or "",
        "created_at": _row_ts(row["created_at"]),
    }


@timed_query(store=_STORE, op="save_story")
def save_story(
    narrative: str,
    section_title: str = "",
    section_context: str = "",
    keywords: Optional[list[str]] = None,
    source_job_id: Optional[str] = None,
    llm_client: Any = None,
) -> str:
    """Persist a story narrative and return its ID.

    If *llm_client* is provided, generates a one-sentence semantic
    summary for improved retrieval relevance. The summary step is
    best-effort — failures are logged and the row still lands with
    ``summary=''``.
    """
    story_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)

    # Best-effort semantic summary. Done before the DB call so a slow
    # LLM doesn't hold the Postgres connection open longer than needed.
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

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blogging_stories "
            "(id, narrative, section_title, section_context, keywords, summary, source_job_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                story_id,
                narrative,
                section_title,
                section_context,
                Json(keywords or []),
                summary,
                source_job_id,
                now,
            ),
        )

    logger.info(
        "Story bank: saved story %s (section=%s, keywords=%s, has_summary=%s)",
        story_id,
        section_title,
        keywords,
        bool(summary),
    )
    return story_id


@timed_query(store=_STORE, op="find_relevant_stories")
def find_relevant_stories(
    query_keywords: list[str],
    limit: int = 5,
    story_opportunity: Optional[str] = None,
    llm_client: Any = None,
) -> list[dict[str, Any]]:
    """Return stories relevant to *query_keywords*, ranked by relevance.

    Two-stage retrieval, unchanged from the SQLite implementation:
    1. Fast path: keyword overlap scoring (set intersection) selects
       the top candidates.
    2. Slow path (optional): if ``story_opportunity`` and
       ``llm_client`` are provided and more candidates than the limit
       have summaries, an LLM rerank picks the most relevant by
       semantic similarity.

    Each result is a dict with keys: id, narrative, section_title,
    section_context, keywords, summary, created_at.
    """
    if not query_keywords:
        return []

    candidates = _keyword_scored_candidates(query_keywords, limit=max(limit, 10))
    if not candidates:
        return []

    candidates_with_summaries = [c for c in candidates if c.get("summary")]
    if story_opportunity and llm_client and len(candidates_with_summaries) > limit:
        reranked = _llm_rerank(candidates_with_summaries, story_opportunity, llm_client, limit)
        if reranked:
            return reranked

    return candidates[:limit]


def _keyword_scored_candidates(query_keywords: list[str], limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve stories ranked by keyword-overlap count (fast path).

    Reads every row because the table is low-volume (dozens of
    stories, not millions). Scoring is done in Python for simplicity
    — a future optimization could push the overlap into Postgres via
    the JSONB ``?|`` operator.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, narrative, section_title, section_context, keywords, "
            "summary, created_at FROM blogging_stories"
        )
        rows = cur.fetchall()

    query_lower = {k.lower().strip() for k in query_keywords if k.strip()}
    if not query_lower:
        return []

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        story_kw = {str(k).lower().strip() for k in (row["keywords"] or [])}
        overlap = len(query_lower & story_kw)
        if overlap > 0:
            scored.append((overlap, _row_to_dict(row)))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [item for _, item in scored[:limit]]


def _llm_rerank(
    candidates: list[dict[str, Any]],
    story_opportunity: str,
    llm_client: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Use an LLM to rerank candidates by semantic relevance."""
    summaries = "\n".join(f"{i + 1}. {c['summary']}" for i, c in enumerate(candidates))
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
        indices = data if isinstance(data, list) else data.get("indices", data.get("text", []))
        if isinstance(indices, list):
            reranked: list[dict[str, Any]] = []
            for idx in indices[:limit]:
                i = int(idx) - 1  # 1-based to 0-based
                if 0 <= i < len(candidates):
                    reranked.append(candidates[i])
            if reranked:
                return reranked
    except Exception as e:
        logger.warning("Story bank LLM reranking failed (falling back to keyword scoring): %s", e)
    return []


@timed_query(store=_STORE, op="list_stories")
def list_stories(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Return stories, newest first, with LIMIT/OFFSET pagination."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, narrative, section_title, section_context, keywords, "
            "summary, created_at FROM blogging_stories "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


@timed_query(store=_STORE, op="get_story")
def get_story(story_id: str) -> Optional[dict[str, Any]]:
    """Return a single story by ID, or None."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, narrative, section_title, section_context, keywords, "
            "summary, created_at FROM blogging_stories WHERE id = %s",
            (story_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_dict(row)


@timed_query(store=_STORE, op="delete_story")
def delete_story(story_id: str) -> bool:
    """Delete a story by ID. Returns True if a row was removed."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM blogging_stories WHERE id = %s", (story_id,))
        return cur.rowcount > 0
