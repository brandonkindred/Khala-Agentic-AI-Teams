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

logger = logging.getLogger(__name__)

# The writer emits ``## Failure: {timestamp} - {agent}``; the timestamp contains
# its own dashes (``2026-01-01``), so the separator must be a *spaced* dash.
_FAILURE_HEADER = re.compile(r"^##\s+Failure:\s*(?P<ts>.+?)\s+-\s+(?P<agent>.+?)\s*$", re.MULTILINE)
_FINAL_ERROR = re.compile(r"\*\*Final error\*\*:\s*`?(?P<err>.+?)`?\s*$", re.MULTILINE)


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
    if not agent_name:
        return False
    try:
        from software_engineering_team.shared.learnings_store import upsert_learning

        pattern = f"Recovery failure in {agent_name}"
        trigger = (str(error) or task_description or "")[:500]
        measure = (
            counter_measure
            or "Simplify or split the task/prompt; review LLM_MAX_TOKENS and model context size; "
            "check for non-terminating/repetitive generation."
        )
        return upsert_learning(
            pattern=pattern,
            trigger=trigger,
            counter_measure=measure,
            source="post_mortem",
            category="recovery_failure",
        )
    except Exception:
        logger.debug("failed to ingest post-mortem learning for %s", agent_name, exc_info=True)
        return False


def ingest_post_mortems_file(path: str | Path) -> int:
    """Backfill learnings from every ``## Failure:`` entry in a POST_MORTEMS.md file.

    Postconditions:
        - Returns the number of entries ingested; ``0`` when the file is missing,
          Postgres is disabled, or on error.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return 0

    headers = list(_FAILURE_HEADER.finditer(text))
    if not headers:
        return 0

    ingested = 0
    for idx, match in enumerate(headers):
        agent = match.group("agent").strip()
        start = match.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        body = text[start:end]
        err_match = _FINAL_ERROR.search(body)
        error = err_match.group("err").strip() if err_match else ""
        if learning_from_failure(agent, "", error):
            ingested += 1
    return ingested


__all__ = ["learning_from_failure", "ingest_post_mortems_file"]
