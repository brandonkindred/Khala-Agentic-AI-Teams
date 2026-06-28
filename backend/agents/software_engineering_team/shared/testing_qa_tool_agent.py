"""Shared declarative base for the code-v2 Testing/QA tool agents.

The ``backend_code_v2_team`` and ``frontend_code_v2_team`` each ship a
Testing/QA tool agent that finds testing/quality issues during review and fixes
them one at a time in problem-solving. The two were byte-for-byte identical
apart from per-team values (the review/problem-solving prompt text — "integration
tests" vs "e2e tests" — the path-normalizing parsers, and the plan
recommendations). This module hoists everything they share onto one base so the
concrete agents stay purely declarative.

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

# Per-issue code-context budgets shared by both teams' Testing/QA agents.
MAX_QA_CODE_CHARS = 12_000
MAX_RELEVANT_CODE_CHARS = 8_000


class SharedTestingQAToolAgent(BaseReviewToolAgent):
    """Common attributes for the backend/frontend Testing/QA tool agents.

    Invariants: carries only the attributes that are identical across both
    teams. The concrete subclass supplies ``review_prompt``,
    ``problem_solving_prompt``, ``plan_recommendations``, and the
    ``_parse_review`` / ``_parse_single_issue`` staticmethods (whose path
    normalization differs per team).
    """

    name = "Testing/QA"
    empty_label = "QA issues"
    issue_source = "qa"
    problem_solve_sources = ("qa", "testing_qa", "tool_testing_qa")
    max_code_chars = MAX_QA_CODE_CHARS
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    default_recommendation = "Fix the issue."
    plan_summary = "Testing/QA planning."
