"""Blogging rewrite swarm — validator/rewriter iteration until PASS.

The validator checks the blog post against compliance, fact-check, and
quality gates. The rewriter applies fixes when issues are found. They
iterate until the validator declares PASS or max iterations are reached.
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_rewrite_swarm() -> Swarm:
    """Build the validation/rewrite iterative swarm."""
    validator = build_agent(
        name="blog_validator",
        system_prompt=(
            "You are a blog content validator. Check the post against these gates:\n"
            "1. Fact accuracy — claims should be verifiable or qualified\n"
            "2. Compliance — no prohibited content, proper disclaimers\n"
            "3. Brand consistency — voice and tone match guidelines\n"
            "4. SEO basics — title, meta description, heading structure\n\n"
            "If all gates pass, declare PASS. If issues are found, describe them "
            "clearly and hand off to the rewriter.\n\n"
            "Return JSON with: status (PASS/NEEDS_REWRITE/NEEDS_HUMAN_REVIEW), "
            "issues array, and gate_results."
        ),
        agent_key="blog",
        description="Validates blog post against quality gates",
    )

    rewriter = build_agent(
        name="blog_rewriter",
        system_prompt=(
            "You are a blog rewriter. When the validator identifies issues, apply "
            "targeted fixes while preserving the post's structure and voice. "
            "After applying fixes, hand back to the validator for re-check.\n\n"
            "Return the rewritten full text with a summary of changes made."
        ),
        agent_key="blog",
        description="Applies targeted fixes to blog posts",
    )

    return Swarm(
        nodes=[validator, rewriter],
        entry_point=validator,
        max_handoffs=20,
        execution_timeout=300.0,
    )
