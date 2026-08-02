"""Construct the real code-review / build / lint agents for production callers.

Temporal activity callers (``temporal/activities.py``) need the same three
review-gate agents but previously passed nothing, silently falling back to a
free-text LLM reviewer and stub build / lint checks.

Usage
-----
Temporal activity callers (``temporal/activities.py``)::

    from software_engineering_team.shared.production_review_agents import (
        build_production_review_kwargs_in_process,
    )
    result = team_lead.run_workflow(**task_kwargs, **build_production_review_kwargs_in_process())

The ``_in_process`` variant constructs ``CodeReviewAgent(force_in_process=True)``
so ``run()`` never attempts to launch a Temporal child-workflow from inside an
already-running Temporal activity (which would risk a nested-workflow deadlock
on the shared worker).

This helper degrades to ``{}`` on any construction failure so a broken review
agent can never turn a working pipeline into a hard outage — today's ``None``
fallback behaviour is preserved.

Invariants:
    - ``build_production_review_kwargs_in_process`` always returns a ``dict``;
      it never raises.
    - Imports of heavy agent machinery (strands, httpx, boto3) are deferred into
      the helper body so importing this module is side-effect-free.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def build_production_review_kwargs_in_process() -> Dict[str, Any]:
    """Return a dict of real review agents for Temporal activity callers.

    Constructs:

    * ``code_review_agent``  — ``CodeReviewAgent`` with its own LLM client,
      constructed with ``force_in_process=True``. That flag is checked inside
      ``CodeReviewAgent.run`` so review always uses the in-process coordinator
      and never starts a nested Temporal child-workflow from inside an
      already-running activity (which would risk a nested-workflow deadlock
      on the shared worker).
    * ``build_verifier``     — ``_run_build_verification`` from ``build_fix``.
    * ``linting_tool_agent`` — ``LintingToolAgent`` with its own Strands model.

    Postconditions:
        - Returns a non-empty dict on success, or ``{}`` on failure.
        - Never raises.
        - Does not mutate ``os.environ`` (including ``TEMPORAL_ADDRESS``).
    """
    try:
        from llm_service import get_client
        from software_engineering_team.build_fix import _run_build_verification
        from software_engineering_team.code_review_agent import CodeReviewAgent
        from software_engineering_team.linting_tool_agent import LintingToolAgent

        return {
            "code_review_agent": CodeReviewAgent(get_client("code_review"), force_in_process=True),
            "build_verifier": _run_build_verification,
            "linting_tool_agent": LintingToolAgent(get_client("linting_tool_agent")),
        }
    except Exception:
        # ERROR (not WARNING): a degraded gate is otherwise indistinguishable
        # from a passing one downstream -- the job still completes "successfully"
        # with code_review/build/lint silently skipped. This must be loud enough
        # to alert on rather than get lost with routine warnings.
        logger.error(
            "GATE_DEGRADED: build_production_review_kwargs_in_process failed to "
            "construct review agents (code_review_agent/build_verifier/"
            "linting_tool_agent all degrade to None) -- this job's build/lint/"
            "code-review gates will be silently skipped",
            exc_info=True,
        )
        return {}
