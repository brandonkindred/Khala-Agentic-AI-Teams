"""Turn post-mortems into closed-loop learnings.

Two entry points:

- :func:`learning_from_failure` — called from ``write_post_mortem`` so every new
  post-mortem immediately becomes a ``se_learnings`` row.
- :func:`ingest_post_mortems_file` — one-shot backfill that parses an existing
  ``POST_MORTEMS.md`` (the format written by :mod:`post_mortem`) into learnings.

Both are best-effort and no-ops without Postgres; they never raise into the
pipeline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from software_engineering_team.shared.learnings_store import LearningEntry

logger = logging.getLogger(__name__)

# The writer emits ``## Failure: {timestamp} - {agent}``; the timestamp contains
# its own dashes (``2026-01-01``), so the separator must be a *spaced* dash.
_FAILURE_HEADER = re.compile(r"^##\s+Failure:\s*(?P<ts>.+?)\s+-\s+(?P<agent>.+?)\s*$", re.MULTILINE)
_FINAL_ERROR = re.compile(r"\*\*Final error\*\*:\s*`?(?P<err>.+?)`?\s*$", re.MULTILINE)

_DEFAULT_COUNTER_MEASURE = (
    "Simplify or split the task/prompt; review LLM_MAX_OUTPUT_TOKENS and model context size; "
    "check for non-terminating/repetitive generation."
)


def _build_entry(
    agent_name: str,
    task_description: str,
    error: object,
    *,
    counter_measure: str = "",
) -> LearningEntry | None:
    """Build the ``LearningEntry`` for one recovery failure, or ``None`` for a blank agent.

    Pure (no I/O): shared by :func:`learning_from_failure` (single upsert) and
    :func:`ingest_post_mortems_file` (batch upsert) so the pattern/trigger
    construction can't drift between the two paths.
    """
    if not agent_name:
        return None
    return LearningEntry(
        pattern=f"Recovery failure in {agent_name}",
        trigger=(str(error) or task_description or ""),
        counter_measure=(counter_measure or _DEFAULT_COUNTER_MEASURE),
        source="post_mortem",
        category="recovery_failure",
    )


def learning_from_failure(
    agent_name: str,
    task_description: str,
    error: object,
    *,
    counter_measure: str = "",
) -> bool:
    """Upsert a learning distilled from one recovery failure.

    Preconditions:
        - ``agent_name`` is a non-empty string.
    Postconditions:
        - Returns ``True`` when a learning row was written/updated; ``False``
          when Postgres is disabled or on error (never raises).
    """
    entry = _build_entry(agent_name, task_description, error, counter_measure=counter_measure)
    if entry is None:
        return False
    try:
        from software_engineering_team.shared.learnings_store import upsert_learning

        return upsert_learning(
            pattern=entry.pattern,
            trigger=entry.trigger,
            counter_measure=entry.counter_measure,
            source=entry.source,
            category=entry.category,
        )
    except Exception:
        logger.debug("failed to ingest post-mortem learning for %s", agent_name, exc_info=True)
        return False


def ingest_post_mortems_file(path: str | Path) -> int:
    """Backfill learnings from every ``## Failure:`` entry in a POST_MORTEMS.md file.

    All entries are upserted in a single batched round trip (see
    :func:`software_engineering_team.shared.learnings_store.upsert_learnings_batch`)
    instead of one Postgres round trip per entry.

    Postconditions:
        - Returns the number of entries ingested; ``0`` when the file is missing,
          has no failure entries, Postgres is disabled, or on error.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return 0

    headers = list(_FAILURE_HEADER.finditer(text))
    if not headers:
        return 0

    entries: list[LearningEntry] = []
    for idx, match in enumerate(headers):
        agent = match.group("agent").strip()
        start = match.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        body = text[start:end]
        err_match = _FINAL_ERROR.search(body)
        error = err_match.group("err").strip() if err_match else ""
        entry = _build_entry(agent, "", error)
        if entry is not None:
            entries.append(entry)
    if not entries:
        return 0

    try:
        from software_engineering_team.shared.learnings_store import upsert_learnings_batch

        return upsert_learnings_batch(entries)
    except Exception:
        logger.debug("failed to batch-ingest post-mortem learnings from %s", path, exc_info=True)
        return 0


__all__ = ["learning_from_failure", "ingest_post_mortems_file"]
