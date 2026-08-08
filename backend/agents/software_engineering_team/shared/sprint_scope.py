"""Sprint-scope spec synthesis, shared between the V1 orchestrator (via
``discovery.py``) and any Temporal activity that needs the same behavior.

Extracted out of ``discovery.py`` (a private, orchestrator-only helper) so a
future V2 activity can call the exact same synthesis logic instead of
duplicating the ``product_delivery``-read logic — pure structural move, no
behavior change.
"""

from __future__ import annotations

from typing import Any, Tuple


def load_requirements_from_sprint(sprint_id: str) -> Tuple[Any, str]:
    """Synthesize ``(ProductRequirements, spec_markdown)`` from a sprint's stories.

    Imports are lazy so the SE team doesn't take an import-time dependency on
    product_delivery (the two are sibling teams).

    Preconditions:
        - ``sprint_id`` is a non-empty sprint identifier.
    Postconditions:
        - Returns ``(requirements, spec_markdown)`` where ``requirements`` is a
          ``ProductRequirements`` synthesized from the sprint's non-terminal-status
          stories (title = sprint name, ``metadata`` carries ``sprint_id``,
          ``story_ids``, and ``synthesized_from_sprint=True``) and
          ``spec_markdown == requirements.description``.
        - Raises ``UnknownProductDeliveryEntity`` when the sprint id is missing.
        - Raises ``ValueError`` when the sprint has no planned stories, or every
          planned story is in a terminal status (done/completed/cancelled/closed) —
          we never silently fall back to repo spec parsing, since the caller
          explicitly asked for a sprint-scoped run.
    """
    from product_delivery import (  # noqa: PLC0415 — lazy to avoid cross-team import at module load
        TERMINAL_STORY_STATUSES,
        UnknownProductDeliveryEntity,
        get_store,
    )
    from shared.dev_models.models import ProductRequirements

    sprint_view = get_store().get_sprint_with_stories(sprint_id)
    if sprint_view is None:
        raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
    if not sprint_view.stories:
        raise ValueError(
            f"sprint {sprint_id!r} has no planned stories; run "
            "POST /api/product-delivery/sprints/{id}/plan first."
        )
    sprint = sprint_view.sprint
    # Filter terminal-status stories before synthesis so the SE
    # pipeline doesn't re-execute work that's already done /
    # cancelled / closed (Codex review on PR #396). Stories may be
    # marked terminal *after* planning — the planner only excludes
    # them at *selection* time, so without this filter execution and
    # planning would diverge. Uses the same `TERMINAL_STORY_STATUSES`
    # set the planner does, with case-insensitive compare so a row
    # stored as ``Done`` doesn't smuggle past the lowercase set.
    executable_stories = [
        s
        for s in sprint_view.stories
        if (s.status or "").strip().lower() not in TERMINAL_STORY_STATUSES
    ]
    if not executable_stories:
        raise ValueError(
            f"sprint {sprint_id!r} has no executable stories — every planned "
            "story is in a terminal status (done/completed/cancelled/closed)."
        )
    story_ids = [s.id for s in executable_stories]

    # Markdown synthesis: per-story heading + user_story + bulleted ACs.
    # `acceptance_criteria_by_story_id` was populated by
    # `get_sprint_with_stories` inside the same REPEATABLE READ
    # transaction as the story fetch (Codex review on PR #396), so the
    # AC rows we render here are guaranteed consistent with the story
    # rows — no risk of a stale stories + fresh ACs mix from
    # concurrent backlog edits.
    flat_ac_strings: list[str] = []
    sections: list[str] = [f"# Sprint: {sprint.name}", ""]
    if sprint.starts_at or sprint.ends_at:
        window = []
        if sprint.starts_at:
            window.append(f"start={sprint.starts_at.isoformat()}")
        if sprint.ends_at:
            window.append(f"end={sprint.ends_at.isoformat()}")
        sections.append("> " + ", ".join(window))
        sections.append("")
    acs_by_story = sprint_view.acceptance_criteria_by_story_id or {}
    for story in executable_stories:
        sections.append(f"## {story.title}")
        if story.user_story:
            sections.append(f"**User Story:** {story.user_story}")
        ac_rows = acs_by_story.get(story.id, [])
        if ac_rows:
            sections.append("")
            sections.append("**Acceptance criteria:**")
            for ac in ac_rows:
                sections.append(f"- {ac.text}")
                flat_ac_strings.append(ac.text)
        sections.append("")
    spec_markdown = "\n".join(sections).rstrip() + "\n"

    requirements = ProductRequirements(
        title=sprint.name,
        description=spec_markdown,
        acceptance_criteria=flat_ac_strings or ["Deliver according to planned story scope."],
        constraints=[],
        priority="medium",
        metadata={
            "sprint_id": sprint_id,
            "story_ids": story_ids,
            "synthesized_from_sprint": True,
        },
    )
    return requirements, spec_markdown
