"""Prompts for the Acceptance Criteria Verifier agent.

The acceptance criteria now live in the shared review engine as the
``acceptance`` profile (see
``code_review_agent.profiles.ReviewProfile.ACCEPTANCE``). The agent routes
through ``code_review_agent.CodeReviewAgent`` with that profile and derives the
per-criterion status from the engine's findings, so there is no standalone
prompt to maintain here.
"""
