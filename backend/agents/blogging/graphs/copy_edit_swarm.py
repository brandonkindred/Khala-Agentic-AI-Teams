"""Blogging copy-edit swarm — iterative refinement between editor and writer.

The copy editor and writer hand off until the editor is satisfied with
the quality, matching the Swarm heuristic of reasoning-based iteration.

Replaces the hand-rolled copy-edit loop (COPY_EDIT_ESCALATION_THRESHOLD=10).
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_copy_edit_swarm() -> Swarm:
    """Build the copy-edit iterative refinement swarm."""
    editor = build_agent(
        name="copy_editor",
        system_prompt=(
            "You are a meticulous copy editor. Review the blog post for grammar, style, "
            "clarity, flow, and consistency. If changes are needed, provide specific edits "
            "and hand off to the writer for revision. If the post meets quality standards, "
            "declare it PASS and produce the final edited version.\n\n"
            "Assessment criteria: grammar correctness, reading flow, consistent voice, "
            "concise language, proper transitions, engaging hooks.\n\n"
            "Return JSON with: status (PASS/NEEDS_REVISION), edits array, and the "
            "full edited text if PASS."
        ),
        agent_key="blog",
        description="Reviews and edits blog post quality",
    )

    writer = build_agent(
        name="blog_writer",
        system_prompt=(
            "You are a skilled blog writer. When the copy editor requests revisions, "
            "apply them thoughtfully while maintaining the post's voice and message. "
            "After applying revisions, hand back to the copy editor for re-review.\n\n"
            "Return the revised full text."
        ),
        agent_key="blog",
        description="Applies editor revisions to blog post",
    )

    return Swarm(
        nodes=[editor, writer],
        entry_point=editor,
        max_handoffs=10,
        execution_timeout=300.0,
    )
