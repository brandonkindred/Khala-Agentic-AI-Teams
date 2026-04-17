"""Postgres-backed bank of winning social-media posts.

Ported from ``blogging.shared.story_bank``. Stores posts whose
engagement signals beat a configurable threshold, tagged with
keywords, platform, metrics, and a one-sentence semantic summary, so
they can be retrieved and injected as exemplars into future
concept-generation prompts.

Retrieval is two-stage:
  1. Keyword overlap (set intersection) picks candidates, optionally
     filtered by platform.
  2. If an ``rerank_context`` and ``llm_client`` are supplied and the
     ``SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED`` flag is on, an
     LLM reranks candidates-with-summaries by semantic relevance.

Storage: ``social_marketing_winning_posts`` table declared in
``social_media_marketing_team.postgres``. This module is pure data
access via ``shared_postgres.get_conn``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from psycopg.rows import dict_row

from shared_postgres import Json, get_conn
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "social_marketing_winning_posts_bank"


def _rerank_enabled() -> bool:
    return os.getenv("SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED", "true").lower() not in (
        "0",
        "false",
        "no",
        "",
    )


def _row_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _to_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ``dict_row`` cursor row into the public dict shape."""
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "platform": row["platform"] or "",
        "keywords": list(row["keywords"] or []),
        "metrics": dict(row["metrics"] or {}),
        "engagement_score": _to_float(row["engagement_score"]),
        "linked_goals": list(row["linked_goals"] or []),
        "summary": row["summary"] or "",
        "source_job_id": row.get("source_job_id"),
        "created_at": _row_ts(row["created_at"]),
    }


_SELECT_COLS = (
    "id, title, body, platform, keywords, metrics, engagement_score, "
    "linked_goals, summary, source_job_id, created_at"
)


@timed_query(store=_STORE, op="save_winning_post")
def save_winning_post(
    title: str,
    body: str,
    platform: str = "",
    keywords: Optional[list[str]] = None,
    metrics: Optional[dict[str, Any]] = None,
    engagement_score: float = 0.0,
    linked_goals: Optional[list[str]] = None,
    source_job_id: Optional[str] = None,
    summary: Optional[str] = None,
    llm_client: Any = None,
) -> str:
    """Persist a winning post and return its ID.

    If *summary* is None and *llm_client* is provided, generates a
    one-sentence semantic summary for improved retrieval. Summary
    generation is best-effort; failures are logged and the row still
    lands with ``summary=''``.
    """
    post_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)

    if summary is None and llm_client is not None and body:
        try:
            summary = (
                llm_client.complete(
                    f"Summarize this social post in one sentence:\n\n{title}\n\n{body}",
                    system_prompt="Write a single sentence summary. No preamble, no quotes.",
                )
                or ""
            ).strip()
        except Exception as e:
            logger.warning("Winning posts bank: summary generation failed (non-fatal): %s", e)
            summary = ""

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO social_marketing_winning_posts "
            "(id, title, body, platform, keywords, metrics, engagement_score, "
            "linked_goals, summary, source_job_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                post_id,
                title,
                body,
                platform or "",
                Json(list(keywords or [])),
                Json(dict(metrics or {})),
                float(engagement_score or 0.0),
                Json(list(linked_goals or [])),
                summary or "",
                source_job_id,
                now,
            ),
        )

    logger.info(
        "Winning posts bank: saved %s (platform=%s, score=%.2f, has_summary=%s)",
        post_id,
        platform,
        float(engagement_score or 0.0),
        bool(summary),
    )
    return post_id


@timed_query(store=_STORE, op="find_relevant_winning_posts")
def find_relevant_winning_posts(
    query_keywords: list[str],
    limit: int = 5,
    platforms: Optional[list[str]] = None,
    rerank_context: Optional[str] = None,
    llm_client: Any = None,
) -> list[dict[str, Any]]:
    """Return winning posts relevant to *query_keywords*, ranked by relevance.

    Two-stage retrieval:
      1. Keyword overlap (set intersection); optionally filtered by
         ``platforms`` (matches ``platform = ANY(...)``).
      2. Optional LLM rerank when ``rerank_context`` + ``llm_client``
         are supplied and more candidates-with-summaries than *limit*
         exist.
    """
    if not query_keywords:
        return []

    candidates = _keyword_scored_candidates(
        query_keywords, limit=max(limit, 10), platforms=platforms
    )
    if not candidates:
        return []

    candidates_with_summaries = [c for c in candidates if c.get("summary")]
    if (
        rerank_context
        and llm_client
        and _rerank_enabled()
        and len(candidates_with_summaries) > limit
    ):
        reranked = _llm_rerank(candidates_with_summaries, rerank_context, llm_client, limit)
        if reranked:
            return reranked

    return candidates[:limit]


def _keyword_scored_candidates(
    query_keywords: list[str],
    limit: int = 10,
    platforms: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Retrieve winning posts ranked by keyword-overlap count.

    Reads every row (optionally pre-filtered by platform) because the
    table is low-volume. Scoring is done in Python; a future
    optimization could push the overlap into Postgres via the JSONB
    ``?|`` operator.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        if platforms:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM social_marketing_winning_posts "
                "WHERE platform = ANY(%s)",
                (list(platforms),),
            )
        else:
            cur.execute(f"SELECT {_SELECT_COLS} FROM social_marketing_winning_posts")
        rows = cur.fetchall()

    query_lower = {k.lower().strip() for k in query_keywords if k and k.strip()}
    if not query_lower:
        return []

    scored: list[tuple[int, float, dict[str, Any]]] = []
    for row in rows:
        post_kw = {str(k).lower().strip() for k in (row["keywords"] or [])}
        overlap = len(query_lower & post_kw)
        if overlap > 0:
            row_dict = _row_to_dict(row)
            scored.append((overlap, row_dict["engagement_score"], row_dict))

    # Primary: overlap count desc. Secondary: engagement_score desc.
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def _llm_rerank(
    candidates: list[dict[str, Any]],
    rerank_context: str,
    llm_client: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Use an LLM to rerank candidates by semantic relevance."""
    summaries = "\n".join(f"{i + 1}. {c['summary']}" for i, c in enumerate(candidates))
    prompt = (
        f"Context: {rerank_context}\n\n"
        f"Candidate winning posts (by summary):\n{summaries}\n\n"
        f"Return a JSON array of the top {limit} indices (1-based) ranked by relevance "
        f"to the context. Most relevant first."
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
                try:
                    i = int(idx) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(candidates):
                    reranked.append(candidates[i])
            if reranked:
                return reranked
    except Exception as e:
        logger.warning(
            "Winning posts bank LLM rerank failed (falling back to keyword scoring): %s", e
        )
    return []


@timed_query(store=_STORE, op="list_winning_posts")
def list_winning_posts(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Return winning posts, newest first, with LIMIT/OFFSET pagination."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM social_marketing_winning_posts "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


@timed_query(store=_STORE, op="get_winning_post")
def get_winning_post(post_id: str) -> Optional[dict[str, Any]]:
    """Return a single winning post by ID, or None."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_SELECT_COLS} FROM social_marketing_winning_posts WHERE id = %s",
            (post_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_dict(row)


@timed_query(store=_STORE, op="delete_winning_post")
def delete_winning_post(post_id: str) -> bool:
    """Delete a winning post by ID. Returns True if a row was removed."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM social_marketing_winning_posts WHERE id = %s", (post_id,))
        return cur.rowcount > 0
