"""Neutral orchestration core for the personal assistant team.

This module owns the process-wide :class:`PersonalAssistantOrchestrator`
singleton. It is deliberately kept free of any FastAPI import
(``personal_assistant_team.api.main``) so the Temporal worker — which imports
the per-step activities — can build the orchestrator without pulling in the
HTTP layer, the same neutral-module shape used by ``market_research_team.pipeline``.

The lazy double-checked-locking singleton below is the same pattern already
used by ``accessibility_audit_team`` and ``branding_team`` to solve the same
problem: sharing one stateful, per-request-caching orchestrator between a
thread-mode HTTP handler and Temporal activities, which run in a separate
``ThreadPoolExecutor`` and can't hold a live object reference across the
Temporal activity boundary.

Both runtime modes share the same orchestrator instance *within one process*:

- **Thread mode** — ``api.main`` binds its module-level ``orchestrator`` to
  :func:`get_orchestrator`.
- **Temporal mode** — each ``@activity.defn`` in ``temporal.activities`` calls
  :func:`get_orchestrator`.

Sharing one instance means one profile-agent cache, matching the pre-existing
single-orchestrator behaviour.

Caveat: "one shared instance" is a per-process guarantee, not a per-deployment
one. ``team_service/entrypoint.py``'s ``TEAM_WORKERS`` spawns that many uvicorn
*processes* for pa-service, each an independent Python interpreter with its own
module state — so ``TEAM_WORKERS > 1`` means that many independent
orchestrators (and profile-agent caches), not a single shared one across the
deployment. ``docker/docker-compose.yml`` pins pa-service's ``TEAM_WORKERS`` to
1 by default precisely so this module's guarantee holds; a deployment that
overrides it to scale out accepts one profile-agent cache per process as a
known, supported tradeoff, not a bug.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .orchestrator.agent import PersonalAssistantOrchestrator

logger = logging.getLogger(__name__)

_orchestrator: "Optional[PersonalAssistantOrchestrator]" = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> "PersonalAssistantOrchestrator":
    """Return the process-wide personal-assistant orchestrator.

    The orchestrator is built lazily on first use with a lazily-resolved LLM
    client (so a missing LLM provider fails an individual run, not process
    startup) and the shared credential/profile stores — exactly how
    ``api.main`` constructed it before this module existed.

    Preconditions:
        - None.

    Postconditions:
        - Returns a fully-constructed ``PersonalAssistantOrchestrator``.
        - Every call returns the *same* instance within this process (one
          shared profile cache) — see the module docstring's caveat on
          ``TEAM_WORKERS`` for what that does and doesn't guarantee across a
          multi-process deployment.
        - Safe to call concurrently from any thread and from a Temporal
          activity's own thread pool: the double-checked locking below
          serializes the one-time construction.

    Invariants:
        - The orchestrator is constructed at most once per process
          (double-checked locking).
    """
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                # Imported lazily: pulls in strands/specialist agents, which we
                # must not do at module import time (keeps this module cheap to
                # import and avoids import cycles with ``api.main``).
                from .orchestrator.agent import PersonalAssistantOrchestrator
                from .shared.credential_store import CredentialStore
                from .shared.llm import get_llm_client
                from .shared.user_profile_store import UserProfileStore

                _orchestrator = PersonalAssistantOrchestrator(
                    get_llm_client("personal_assistant", lazy=True),
                    CredentialStore(),
                    UserProfileStore(),
                )
                logger.info("PersonalAssistantOrchestrator constructed (shared singleton)")
    return _orchestrator


def reset_orchestrator() -> None:
    """Drop the cached orchestrator (test-support hook).

    Preconditions:
        - None.

    Postconditions:
        - The next :func:`get_orchestrator` call builds a fresh instance.
    """
    global _orchestrator
    with _orchestrator_lock:
        _orchestrator = None
