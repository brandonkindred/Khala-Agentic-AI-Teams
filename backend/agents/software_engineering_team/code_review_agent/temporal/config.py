"""Temporal target resolution for the code review agent.

Unlike every other team, the code review agent runs in **Temporal mode by
default**: it does not wait for an operator to set ``TEMPORAL_ADDRESS``. When
nothing is configured it targets the application's own deployed Temporal
container (``temporal:7233`` — the address the docker stack already wires into
every service), and an operator overrides that by pointing ``TEMPORAL_ADDRESS``
at a different Temporal server. Setting ``TEMPORAL_ADDRESS`` to an empty /
``disabled`` / ``none`` / ``off`` value, selecting the ``dummy`` LLM harness, or
running under ``pytest`` falls the agent back to the in-process thread-mode
coordinator.

There is deliberately **no** code-review-specific address override: the code
review worker connects through the process-wide ``shared_temporal`` client, which
reads only ``TEMPORAL_ADDRESS`` (``shared_temporal.client.get_temporal_address``),
so a distinct per-agent address could not actually route to a different cluster —
it would be silently ignored. Code review therefore shares the process Temporal
address; ``TEMPORAL_ADDRESS`` is the single override.

The "Temporal by default" flip is scoped to the code review agent: this resolver
is separate from ``shared_temporal.is_temporal_enabled`` (which stays
``None``-default), so the *other* teams' thread-default dispatch decision is
unchanged even though they read the same ``TEMPORAL_ADDRESS``.

Invariants:
    - ``resolve_code_review_temporal_address`` is pure with respect to the
      environment (it only reads ``TEMPORAL_ADDRESS``) and never raises.
    - ``code_review_temporal_enabled`` never returns ``True`` while the resolved
      address is ``None``.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

# The app's own Temporal container, as wired by ``docker/docker-compose.yml`` and
# ``docker/.env.example`` (``TEMPORAL_ADDRESS: temporal:7233``). Used when no
# address is configured so the code review agent defaults onto the deployed
# server rather than waiting for an operator to opt in.
DEFAULT_CODE_REVIEW_TEMPORAL_ADDRESS = "temporal:7233"

# Task queue + workflow-id prefix for the code review workflow. Kept here (a
# side-effect-free module) so ``workflows.py`` can import them without pulling in
# the activity bodies.
TASK_QUEUE = "code_review-queue"
WORKFLOW_ID_PREFIX = "code-review-"

# Values that, when set on the address var, mean "run the review in-process
# (thread mode)" rather than naming a server. An empty string counts: an operator
# who exports ``TEMPORAL_ADDRESS=`` to disable Temporal elsewhere disables it here
# too.
_DISABLE_SENTINELS = frozenset({"", "disabled", "none", "off", "0", "false", "no"})

# Truthy spellings for the test-only force flag.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def resolve_code_review_temporal_address() -> Optional[str]:
    """Resolve the Temporal server address the code review agent should target.

    Postconditions:
        - Returns ``TEMPORAL_ADDRESS`` when it is set to a real value (the
          operator override onto a different Temporal server — honored because the
          shared client connects to exactly this address).
        - Returns ``None`` when ``TEMPORAL_ADDRESS`` *is present* but holds a
          disable sentinel (empty / ``disabled`` / ``none`` / ``off`` / ``0`` /
          ``false`` / ``no``) — an explicit "use thread mode" signal.
        - Returns :data:`DEFAULT_CODE_REVIEW_TEMPORAL_ADDRESS` when
          ``TEMPORAL_ADDRESS`` is unset, so an unconfigured deployment defaults
          onto the app's own Temporal container.
        - Never raises.
    """
    value = os.environ.get("TEMPORAL_ADDRESS")
    if value is None:
        return DEFAULT_CODE_REVIEW_TEMPORAL_ADDRESS
    stripped = value.strip()
    if stripped.lower() in _DISABLE_SENTINELS:
        return None
    return stripped


def _force_enabled() -> bool:
    """Test hook: ``CODE_REVIEW_TEMPORAL_FORCE`` in a truthy spelling.

    Lets an integration test opt back into Temporal mode despite the ``pytest``
    guard below. Never load-bearing outside tests.
    """
    return os.environ.get("CODE_REVIEW_TEMPORAL_FORCE", "").strip().lower() in _TRUE_VALUES


def _dummy_harness() -> bool:
    """True when the no-LLM ``dummy`` provider harness is selected."""
    return os.environ.get("LLM_PROVIDER", "").strip().lower() == "dummy"


def code_review_temporal_enabled() -> bool:
    """Whether ``CodeReviewAgent.run`` should dispatch to Temporal by default.

    Postconditions:
        - Returns ``True`` iff a Temporal address resolves and no disabling
          condition applies. The disabling conditions are: the ``dummy`` LLM
          harness, and running under ``pytest`` (so the existing in-process test
          suite never dials a Temporal server) — both overridable by the
          ``CODE_REVIEW_TEMPORAL_FORCE`` test hook.
        - Never returns ``True`` when :func:`resolve_code_review_temporal_address`
          is ``None``.
        - Never raises.
    """
    if _force_enabled():
        return resolve_code_review_temporal_address() is not None
    if _dummy_harness():
        return False
    if "pytest" in sys.modules:
        return False
    return resolve_code_review_temporal_address() is not None
