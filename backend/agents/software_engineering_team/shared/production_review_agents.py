"""Construct the real code-review / build / lint agents for production callers.

Background thread callers (``api/background.py``) and Temporal activity callers
(``temporal/activities.py``) all need the same three review-gate agents but
previously passed nothing, silently falling back to a free-text LLM reviewer
and stub build / lint checks.

Usage
-----
Thread-mode callers (``api/background.py``)::

    from software_engineering_team.shared.production_review_agents import (
        build_production_review_kwargs,
    )
    result = team_lead.run_workflow(**task_kwargs, **build_production_review_kwargs())

Temporal activity callers (``temporal/activities.py``)::

    from software_engineering_team.shared.production_review_agents import (
        build_production_review_kwargs_in_process,
    )
    result = team_lead.run_workflow(**task_kwargs, **build_production_review_kwargs_in_process())

The ``_in_process`` variant constructs ``CodeReviewAgent(force_in_process=True)``
so ``run()`` never attempts to launch a Temporal child-workflow from inside an
already-running Temporal activity (which would risk a nested-workflow deadlock
on the shared worker).

Both helpers degrade to ``{}`` on any construction failure so a broken review
agent can never turn a working pipeline into a hard outage — today's ``None``
fallback behaviour is preserved.

Invariants:
    - ``build_production_review_kwargs`` / ``build_production_review_kwargs_in_process``
      always return a ``dict``; they never raise.
    - Imports of heavy agent machinery (strands, httpx, boto3) are deferred into
      the helper body so importing this module is side-effect-free.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def build_production_review_kwargs() -> Dict[str, Any]:
    """Return a dict of real review agents for thread-mode (``api/background.py``) callers.

    Constructs:

    * ``code_review_agent``  — ``CodeReviewAgent`` with its own LLM client.  In
      thread mode the agent's default Temporal dispatch is used (enabled by
      ``code_review_temporal_enabled()``, which reads ``TEMPORAL_ADDRESS``).
    * ``build_verifier``     — ``_run_build_verification`` from ``build_fix``.
    * ``linting_tool_agent`` — ``LintingToolAgent`` with its own Strands model.

    Degrades to ``{}`` on any construction failure (preserving today's ``None``
    fallback behaviour so the pipeline can still run).

    Postconditions:
        - Returns a non-empty dict on success, or ``{}`` on failure.
        - Never raises.
    """
    try:
        from llm_service import get_client
        from software_engineering_team.build_fix import _run_build_verification
        from software_engineering_team.code_review_agent import CodeReviewAgent
        from software_engineering_team.linting_tool_agent import LintingToolAgent

        return {
            "code_review_agent": CodeReviewAgent(get_client("code_review")),
            "build_verifier": _run_build_verification,
            "linting_tool_agent": LintingToolAgent(get_client("linting_tool_agent")),
        }
    except Exception:
        logger.warning(
            "build_production_review_kwargs: failed to construct review agents; "
            "degrading to None fallbacks (today's behaviour)",
            exc_info=True,
        )
        return {}


def build_production_review_kwargs_in_process() -> Dict[str, Any]:
    """Return a dict of real review agents for Temporal activity callers.

    Identical to :func:`build_production_review_kwargs` except the
    ``CodeReviewAgent`` is constructed with ``force_in_process=True``.  That
    flag is checked inside ``CodeReviewAgent.run`` so review always uses the
    in-process coordinator and never starts a nested Temporal child-workflow
    from inside an already-running activity.

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
            "code_review_agent": CodeReviewAgent(
                get_client("code_review"), force_in_process=True
            ),
            "build_verifier": _run_build_verification,
            "linting_tool_agent": LintingToolAgent(get_client("linting_tool_agent")),
        }
    except Exception:
        logger.warning(
            "build_production_review_kwargs_in_process: failed to construct review agents; "
            "degrading to None fallbacks (today's behaviour)",
            exc_info=True,
        )
        return {}
