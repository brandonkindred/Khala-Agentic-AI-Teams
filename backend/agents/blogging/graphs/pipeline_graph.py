"""Blogging pipeline graph — multi-phase blog writing with swarm gates.

Topology::

    planning → draft → copy_edit_swarm → rewrite_swarm → title_selection → finalize

The pipeline is split at human interaction points:
- Graph 1: planning → draft (human reviews draft)
- Graph 2: copy_edit → validation → rewrite → title → finalize

Temporal activity handles the pause/resume between graphs.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential

from .copy_edit_swarm import build_copy_edit_swarm
from .rewrite_swarm import build_rewrite_swarm


def build_pre_review_graph() -> Graph:
    """Build the pre-human-review pipeline (planning → draft).

    This graph runs before the human review checkpoint.
    """
    return build_sequential(
        stages=[
            ("planning", build_agent(
                name="blog_planner",
                system_prompt=(
                    "You are a content strategist. Analyze the topic, audience, and goals "
                    "to create a detailed content plan with sections, narrative flow, and "
                    "title candidates. Return structured JSON matching content_plan_json_v1."
                ),
                agent_key="blog",
                description="Creates content plan for blog post",
            )),
            ("draft", build_agent(
                name="blog_drafter",
                system_prompt=(
                    "You are a skilled blog writer. Based on the content plan, write a "
                    "complete first draft following the planned structure, voice guidelines, "
                    "and research notes. Return the full blog post text."
                ),
                agent_key="blog",
                description="Writes initial blog post draft",
            )),
        ],
        graph_id="blog_pre_review",
        execution_timeout=300.0,
        node_timeout=180.0,
    )


def build_post_review_graph() -> Graph:
    """Build the post-human-review pipeline (copy_edit → validate → title → finalize).

    This graph runs after the human has approved the draft.
    """
    return build_sequential(
        stages=[
            ("copy_edit", build_copy_edit_swarm()),
            ("validation_rewrite", build_rewrite_swarm()),
            ("title_selection", build_agent(
                name="title_selector",
                system_prompt=(
                    "You are a headline specialist. Review the finalized blog post and "
                    "title candidates. Select or craft the best title for SEO, engagement, "
                    "and accuracy. Return JSON with selected_title and rationale."
                ),
                agent_key="blog",
                description="Selects optimal blog post title",
            )),
            ("finalize", build_agent(
                name="blog_finalizer",
                system_prompt=(
                    "You are a publishing coordinator. Assemble the final blog post with "
                    "selected title, meta description, tags, and any final formatting. "
                    "Return the complete publishable blog post."
                ),
                agent_key="blog",
                description="Assembles final publishable blog post",
            )),
        ],
        graph_id="blog_post_review",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
