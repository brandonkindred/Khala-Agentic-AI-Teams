"""Neutral, team-agnostic recovery parsers for imperfect LLM output.

When a model returns prose-wrapped JSON, think-blocks, markdown-fenced code, or a
``{"content": "..."}`` wrapper instead of the requested structured object, these
helpers salvage a usable result instead of failing outright. Promoted out of
``software_engineering_team.shared.llm_response_utils`` so any team (the coding
team's Tech Lead included) can share the same resilience.

Layout:
    - ``recovery`` — the salvage parsers (was
      ``software_engineering_team/shared/llm_response_utils.py``).

Public API:
    - ``extract_json_object``            — first balanced JSON object → dict
    - ``extract_task_assignment_from_content`` — a task-assignment dict (has ``tasks``)
    - ``extract_files_from_content`` / ``heuristic_extract_files_from_content``
      — ``{path: content}`` from JSON or fenced blocks
    - ``extract_single_python_block``    — a lone ```` ```python ```` body
    - ``looks_truncated``                — cheap "was this reply cut off?" heuristic

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Stdlib-only; importing has no side effects and never raises on bad input.
"""

from __future__ import annotations

from shared_llm_recovery.recovery import (
    extract_files_from_content,
    extract_json_object,
    extract_single_python_block,
    extract_task_assignment_from_content,
    heuristic_extract_files_from_content,
    looks_truncated,
)

__all__ = [
    "extract_json_object",
    "extract_task_assignment_from_content",
    "extract_files_from_content",
    "heuristic_extract_files_from_content",
    "extract_single_python_block",
    "looks_truncated",
]
