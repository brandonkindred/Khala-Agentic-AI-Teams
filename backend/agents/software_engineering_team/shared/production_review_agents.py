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

The ``_in_process`` variant forces ``CodeReviewAgent`` into thread/in-process
mode so it never attempts to launch its own Temporal child-workflow from inside
an already-running Temporal activity (which would cause a nested-workflow
deadlock on the shared task queue).

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
    ``CodeReviewAgent`` is forced into **in-process / thread mode** by patching
    the environment before construction.  This prevents a nested-workflow
    deadlock: ``CodeReviewAgent.run`` defaults to dispatching a Temporal
    child-workflow (``code_review_temporal_enabled()`` is ``True`` in production)
    — if that call comes from *inside* a running Temporal activity, it tries to
    schedule work on the same task queue the activity is consuming, which hangs
    indefinitely.  Overriding ``TEMPORAL_ADDRESS`` to the ``"disabled"``
    sentinel for the duration of the constructor (which reads the env at call
    time via ``code_review_temporal_enabled``) makes ``CodeReviewAgent`` always
    use the fast in-process coordinator instead.

    Postconditions:
        - Returns a non-empty dict on success, or ``{}`` on failure.
        - Never raises.
        - Does not permanently mutate ``os.environ``; the env is restored after
          the constructor returns.
    """
    try:
        import os

        from llm_service import get_client
        from software_engineering_team.build_fix import _run_build_verification
        from software_engineering_team.code_review_agent import CodeReviewAgent
        from software_engineering_team.linting_tool_agent import LintingToolAgent

        # Force in-process code review while inside a Temporal activity.
        # The ``disabled`` sentinel is recognised by ``resolve_code_review_temporal_address``
        # (``code_review_agent/temporal/config.py``) as "use thread mode".
        _prev = os.environ.get("TEMPORAL_ADDRESS")
        os.environ["TEMPORAL_ADDRESS"] = "disabled"
        try:
            code_review_agent = CodeReviewAgent(get_client("code_review"))
        finally:
            # Restore regardless of success/failure so the activity's own
            # Temporal client still resolves its address correctly afterwards.
            if _prev is None:
                os.environ.pop("TEMPORAL_ADDRESS", None)
            else:
                os.environ["TEMPORAL_ADDRESS"] = _prev

        return {
            "code_review_agent": code_review_agent,
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
