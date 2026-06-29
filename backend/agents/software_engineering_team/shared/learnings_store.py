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
from typing import Any, Optional

from shared_postgres import pg_cursor
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
    length >= 3, sanitized to alphanumerics, and joined with ``|``.

    The alphanumeric class deliberately drops separators (``-``/``_``): a raw
    ``-`` is a syntax error inside ``to_tsquery`` and an ``_`` yields a
    multi-lexeme token (also a syntax error), either of which would make the whole
    query raise and silently return nothing. Splitting a compound like
    ``counter_measure`` into ``counter | measure`` is also closer to how the
    stored ``to_tsvector`` tokenizes the corpus, so it matches *more*, not less.

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
    # Bound the text feeding se_learnings.search_tsv (a GENERATED tsvector over
    # ``pattern || ' ' || trigger``): Postgres rejects a tsvector larger than ~1MB,
    # which would make the INSERT raise and the learning be silently dropped (the
    # except below returns False). 8000 chars is ample for a diagnostic snippet.
    for _field, _val in (
        ("pattern", pattern),
        ("trigger", trigger),
        ("counter_measure", counter_measure),
    ):
        if len(_val) > 8000:
            logger.debug("upsert_learning: %s truncated from %d to 8000 chars", _field, len(_val))
    pattern = pattern[:8000]
    trigger = trigger[:8000]
    counter_measure = counter_measure[:8000]
    try:
        with pg_cursor() as cur:
            if cur is None:
                return False
            fp = fingerprint(pattern, trigger, category)
            now = datetime.now(tz=timezone.utc)
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
        with pg_cursor(dict_rows=True) as cur:
            if cur is None:
                return []
            # ``tsquery`` is bound twice — once for the WHERE ``@@`` match and once
            # for the ORDER BY ``ts_rank`` — because each positional ``%s``
            # placeholder consumes its own argument; the appends below are ordered
            # to match the placeholders in the SQL.
            params: list[Any] = [tsquery]  # 1) WHERE search_tsv @@ to_tsquery(...)
            where = "search_tsv @@ to_tsquery('english', %s)"
            if category:
                where += " AND category = %s"
                params.append(category)  # 2) optional category filter
            params.append(tsquery)  # 3) ts_rank(...) in ORDER BY
            params.append(top_n)  # 4) LIMIT
            cur.execute(
                "SELECT pattern, trigger, counter_measure, source, category, occurrences "
                "FROM se_learnings "
                f"WHERE {where} "
                "ORDER BY ts_rank(search_tsv, to_tsquery('english', %s)) DESC, "
                "         occurrences DESC, last_seen DESC "
                "LIMIT %s",
                tuple(params),
            )
            rows = cur.fetchall()
        return [
            Learning(
                pattern=r["pattern"],
                trigger=r["trigger"],
                counter_measure=r["counter_measure"],
                source=r["source"],
                category=r["category"],
                occurrences=int(r["occurrences"] or 1),
            )
            for r in rows
        ]
    except Exception:
        logger.debug("failed to retrieve learnings", exc_info=True)
        return []


def count_learnings() -> int:
    """Return the number of stored learnings (0 when Postgres disabled)."""
    try:
        with pg_cursor() as cur:
            if cur is None:
                return 0
            cur.execute("SELECT COUNT(*) FROM se_learnings")
            row = cur.fetchone()
            count = int(row[0]) if row else 0
        return count
    except Exception:
        logger.debug("failed to count learnings", exc_info=True)
        return 0


def prune_learnings(retention_days: float | None = None) -> int:
    """Delete learnings whose ``last_seen`` is older than the retention window."""
    days = _retention_days() if retention_days is None else retention_days
    if days <= 0:
        return 0
    try:
        with pg_cursor() as cur:
            if cur is None:
                return 0
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
            cur.execute("DELETE FROM se_learnings WHERE last_seen < %s", (cutoff,))
            removed = cur.rowcount or 0
        return removed
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
