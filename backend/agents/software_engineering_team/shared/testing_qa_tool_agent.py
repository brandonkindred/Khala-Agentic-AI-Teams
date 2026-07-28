"""Shared declarative base for the code-v2 Testing/QA tool agents.

The ``backend_code_v2_team`` and ``frontend_code_v2_team`` each ship a
Testing/QA tool agent that finds testing/quality issues during review and
reports them for the coding agent to fix. The two were byte-for-byte identical
apart from per-team values (the review prompt text — "integration tests" vs
"e2e tests" — the path-normalizing parser, and the plan recommendations). This
module hoists everything they share onto one base so the concrete agents stay
purely declarative.

Load-bearing invariants inherited from
:class:`software_engineering_team.shared.tool_agent_base.BaseReviewToolAgent`:

* Each concrete ``agent.py`` must keep a top-level ``from strands import Agent``
  so tests can ``monkeypatch.setattr(<agent_module>, "Agent", ...)``; the base
  resolves ``Agent`` from the concrete subclass module via ``_agent_factory``.
* Per-team prompts, parsers, and ``plan_recommendations`` are set on the
  concrete subclass — only the team-invariant attributes live here.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_base import BaseReviewToolAgent

# Per-issue code-context budget shared by both teams' Testing/QA agents.
MAX_RELEVANT_CODE_CHARS = 8_000


class SharedTestingQAToolAgent(BaseReviewToolAgent):
    """Common attributes for the backend/frontend Testing/QA tool agents.

    Invariants: carries only the attributes that are identical across both teams.

    Required on every concrete subclass (omitting any of these leaves the
    inherited ``None``/empty default and the agent fails at review time,
    mirroring the rest of the ``BaseReviewToolAgent`` family):

    * ``review_prompt`` — the review LLM prompt (team-specific text).
    * ``plan_recommendations`` — list of plan-phase advice (integration vs e2e).
    * ``_parse_review`` — staticmethod parser whose path normalization differs
      per team.

    This agent only reports findings — fixing them is the coding agent's
    responsibility (see :class:`~software_engineering_team.shared.tool_agent_base.SingleIssueProblemSolveMixin`
    for why this class does not opt into self-fix).

    These are intentionally plain class attributes rather than
    ``abc.abstractmethod``: the whole ``BaseReviewToolAgent`` family is
    attribute-driven (no ABCMeta), so enforcing abstractness here would diverge
    from every sibling tool agent.
    """

    name = "Testing/QA"
    empty_label = "QA issues"
    issue_source = "qa"
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    plan_summary = "Testing/QA planning."
