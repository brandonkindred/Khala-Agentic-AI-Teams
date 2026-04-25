"""LLM-as-judge critics for sales artifacts that reach customers.

Both critics follow the :class:`blogging.blog_plan_critic_agent.BlogPlanCriticAgent`
pattern: an independent Strands-free pass that scores the artifact against a
hard-coded rubric and returns a structured report. The orchestrator runs each
critic between agent emit and result wrap; on ``revise``, the agent is re-run
once with the violations appended to its context.
"""

from __future__ import annotations

from .outreach_critic import OutreachCriticAgent, format_critic_feedback
from .proposal_critic import ProposalCriticAgent

__all__ = ["OutreachCriticAgent", "ProposalCriticAgent", "format_critic_feedback"]
