"""Prompts for change review agent.

The change-review criteria now live in the shared review engine as the
``devops_maintainability`` profile (see
``code_review_agent.profiles.ReviewProfile.DEVOPS_MAINTAINABILITY``). The agent
routes through ``code_review_agent.CodeReviewAgent`` with that profile, so there
is no standalone prompt to maintain here.
"""
