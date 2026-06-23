"""Closed-loop learnings store (``se_learnings``).

Distilled lessons — ``pattern`` / ``trigger`` / ``counter_measure`` — ingested
from post-mortems and quality-gate rejections, deduplicated on a stable
fingerprint, and retrieved by Postgres full-text relevance to inject the top-N
most relevant into the Tech Lead's Design prompt.

:func:`retrieve_learnings` is the single pluggable retrieval seam — swapping in
an embedding-based ranker later requires changing only this function.

All operations are guarded by ``is_postgres_enabled()`` and never raise into the
pipeline; without Postgres, upserts are no-ops and retrieval returns ``[]``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from software_engineering_team.shared.env_config import env_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Learning:
    """One retrieved learning row."""

    pattern: str
    trigger: str
    counter_measure: str
    source: str
    category: str
    occurrences: int


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def fingerprint(pattern: str, trigger: str, category: str) -> str:
    """Stable dedup key for a learning.

    Preconditions: arguments are strings.
    Postconditions: returns a 32-char hex digest; equal (case/whitespace-
        insensitive) ``pattern``/``trigger``/``category`` triples map to the
        same fingerprint.
    """
    raw = f"{_norm(pattern)}|{_norm(trigger)}|{_norm(category)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _or_tsquery_terms(query_text: str, *, limit: int = 40) -> str:
    """Build an OR ``to_tsquery`` string from the words in ``query_text``.

    Relevance retrieval wants OR semantics (any shared term contributes), not
    the AND semantics of ``plainto_tsquery`` — a long spec rarely shares *every*
    word with a short learning. Words are lowercased, de-duplicated, filtered to
    length >= 3, sanitized to alphanumerics (safe for ``to_tsquery``), and joined
    with ``|``.

    Postconditions: returns ``""`` when no usable term remains.
    """
    words = re.findall(r"[a-z0-9]+", query_text.lower())
    terms = [w for w in dict.fromkeys(words) if len(w) >= 3][:limit]
    return " | ".join(terms)


def _retention_days() -> float:
    return env_float("SE_LEARNINGS_RETENTION_DAYS", 365.0, 0.0)


def upsert_learning(
    *,
    pattern: str,
    trigger: str = "",
    counter_measure: str = "",
    source: str = "",
    category: str = "",
) -> bool:
    """Insert a learning, or bump ``occurrences``/``last_seen`` if it already exists.

    Preconditions:
        - ``pattern`` is a non-empty string.
    Postconditions:
        - Returns ``True`` when a row was inserted or updated; ``False`` when
          Postgres is disabled or the write failed (logged at DEBUG, not raised).
        - A repeat of an existing ``(pattern, trigger, category)`` increments
          its ``occurrences`` and refreshes ``last_seen`` and ``counter_measure``
          rather than creating a duplicate.
    """
    if not pattern or not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    try:
        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return False
    if not is_postgres_enabled():
        return False
    fp = fingerprint(pattern, trigger, category)
    now = datetime.now(tz=timezone.utc)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO se_learnings "
                "(fingerprint, pattern, trigger, counter_measure, source, category, "
                " occurrences, created_at, last_seen) "
                "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s) "
                "ON CONFLICT (fingerprint) DO UPDATE SET "
                "  occurrences = se_learnings.occurrences + 1, "
                "  last_seen = EXCLUDED.last_seen, "
                "  counter_measure = EXCLUDED.counter_measure",
                (fp, pattern, trigger, counter_measure, source, category, now, now),
            )
        return True
    except Exception:
        logger.debug("failed to upsert learning %r", pattern[:80], exc_info=True)
        return False


def retrieve_learnings(
    query_text: str,
    *,
    top_n: int = 5,
    category: Optional[str] = None,
) -> list[Learning]:
    """Return up to ``top_n`` learnings most relevant to ``query_text``.

    Ranking: Postgres full-text ``ts_rank`` of ``search_tsv`` against
    ``to_tsquery('english', tsquery)``, where ``tsquery`` is an OR of the
    sanitized terms from ``query_text`` (built by :func:`_or_tsquery_terms`),
    tie-broken by ``occurrences`` then ``last_seen`` (both descending).

    Preconditions:
        - ``top_n >= 1``.
    Postconditions:
        - Returns a list of at most ``top_n`` :class:`Learning` ordered by
          descending relevance; ``[]`` for empty/whitespace ``query_text``, when
          Postgres is disabled, or on error.
    """
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if not query_text or not query_text.strip():
        return []
    # Cap the query text so a whole spec/architecture doc doesn't blow the tsquery.
    tsquery = _or_tsquery_terms(query_text[:8000])
    if not tsquery:
        return []
    try:
        from shared_postgres import dict_row, get_conn, is_postgres_enabled
    except Exception:
        return []
    if not is_postgres_enabled():
        return []
    params: list = [tsquery]
    where = "search_tsv @@ to_tsquery('english', %s)"
    if category:
        where += " AND category = %s"
        params.append(category)
    params.append(tsquery)  # for the ts_rank in ORDER BY
    params.append(top_n)
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT pattern, trigger, counter_measure, source, category, occurrences "
                "FROM se_learnings "
                f"WHERE {where} "
                "ORDER BY ts_rank(search_tsv, to_tsquery('english', %s)) DESC, "
                "         occurrences DESC, last_seen DESC "
                "LIMIT %s",
                tuple(params),
            )
            return [
                Learning(
                    pattern=r["pattern"],
                    trigger=r["trigger"],
                    counter_measure=r["counter_measure"],
                    source=r["source"],
                    category=r["category"],
                    occurrences=int(r["occurrences"] or 1),
                )
                for r in cur.fetchall()
            ]
    except Exception:
        logger.debug("failed to retrieve learnings", exc_info=True)
        return []


def count_learnings() -> int:
    """Return the number of stored learnings (0 when Postgres disabled)."""
    try:
        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return 0
    if not is_postgres_enabled():
        return 0
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM se_learnings")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        logger.debug("failed to count learnings", exc_info=True)
        return 0


def prune_learnings(retention_days: float | None = None) -> int:
    """Delete learnings whose ``last_seen`` is older than the retention window."""
    days = _retention_days() if retention_days is None else retention_days
    if days <= 0:
        return 0
    try:
        from shared_postgres import get_conn, is_postgres_enabled
    except Exception:
        return 0
    if not is_postgres_enabled():
        return 0
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM se_learnings WHERE last_seen < %s", (cutoff,))
            return cur.rowcount or 0
    except Exception:
        logger.debug("failed to prune learnings", exc_info=True)
        return 0


__all__ = [
    "Learning",
    "fingerprint",
    "upsert_learning",
    "retrieve_learnings",
    "count_learnings",
    "prune_learnings",
]
