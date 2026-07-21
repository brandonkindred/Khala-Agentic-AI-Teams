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
review worker connects through the process-wide ``shared.temporal`` client, which
reads only ``TEMPORAL_ADDRESS`` (``shared.temporal.client.get_temporal_address``),
so a distinct per-agent address could not actually route to a different cluster —
it would be silently ignored. Code review therefore shares the process Temporal
address; ``TEMPORAL_ADDRESS`` is the single override.

The "Temporal by default" flip is scoped to the code review agent: this resolver
is separate from ``shared.temporal.is_temporal_enabled`` (which stays
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

from shared.env_config import env_int

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


# --- Client-side / server-side execute-and-wait tuning -----------------------
# Ceiling on how long execute_code_review_workflow_sync's synchronous caller
# waits for the whole durable review (seconds). Generous because the map
# fan-out may include long LLM calls; the workflow's own per-activity
# timeouts bound each phase. Unlike, e.g., agent_provisioning_team's
# client-side ceilings (temporal/constants.py's "must exceed worst-case retry
# budget" derivation), this is NOT computed as "per-attempt timeout x max
# attempts + margin": chunking.build_review_chunks has no cap on chunk count,
# so chunk count — and thus worst-case wall-clock — scales with PR size with
# no enforced ceiling. No finite number is formally provable to exceed every
# run's worst case the way agent_provisioning_team's are; this is a pragmatic
# ceiling ("how large a PR review do we support synchronously"), not a proof.
# CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES (worker.py) is the primary lever for
# keeping large-PR reviews inside this ceiling — an operator whose fleet
# regularly reviews very large PRs should raise this var, not expect one
# constant to cover every PR size.
DEFAULT_EXECUTE_TIMEOUT_S = 6 * 3600
_MIN_EXECUTE_TIMEOUT_S = 60


def resolve_execute_timeout_s() -> float:
    """Resolve the client-side execute-and-wait ceiling (seconds).

    Preconditions:
        - none (environment may be unset or garbage).
    Postconditions:
        - Returns ``CODE_REVIEW_EXECUTE_TIMEOUT_S`` when it parses to an int
          >= :data:`_MIN_EXECUTE_TIMEOUT_S`, else :data:`DEFAULT_EXECUTE_TIMEOUT_S`
          (unset, garbage, or below the floor all fall back to the default),
          via the shared ``env_int`` parser. Never raises.
    """
    return float(
        env_int(
            "CODE_REVIEW_EXECUTE_TIMEOUT_S",
            DEFAULT_EXECUTE_TIMEOUT_S,
            floor=_MIN_EXECUTE_TIMEOUT_S,
        )
    )


# Flat margin (seconds) subtracted from the client-side ceiling to derive the
# Temporal-side ``execution_timeout`` passed to ``client.execute_workflow(...)``.
# Kept strictly BELOW the client ceiling — never equal, never above — so the
# server always reclaims an abandoned execution's worker slots before, not
# after, the client itself gives up waiting, and so the common "still
# running" case surfaces to the caller as a message-bearing
# ``WorkflowFailureError`` instead of an empty-message client-side
# ``TimeoutError``. Same 120s-margin convention as agent_provisioning_team's
# ``CLIENT_TIMEOUT_MARGIN_S``, applied in the opposite direction (padding the
# server timeout DOWN from the client ceiling, rather than padding the client
# ceiling UP from a workflow's own worst case).
EXECUTION_TIMEOUT_MARGIN_S = 120
_MIN_EXECUTION_TIMEOUT_S = 60


def resolve_execution_timeout_s(execute_timeout_s: float) -> float:
    """Derive the Temporal-side ``execution_timeout`` from the client ceiling.

    Preconditions:
        - ``execute_timeout_s`` > 0.
    Postconditions:
        - Returns ``max(_MIN_EXECUTION_TIMEOUT_S, execute_timeout_s -
          EXECUTION_TIMEOUT_MARGIN_S)``. Strictly less than
          ``execute_timeout_s`` whenever ``execute_timeout_s >
          EXECUTION_TIMEOUT_MARGIN_S + _MIN_EXECUTION_TIMEOUT_S`` (true for the
          documented default and any reasonable override); below that
          threshold the floor takes over and the invariant is not
          guaranteed — an accepted edge case for a deliberately tiny override
          (e.g. a fast test), not the production path.
        - Never raises.
    """
    return max(_MIN_EXECUTION_TIMEOUT_S, execute_timeout_s - EXECUTION_TIMEOUT_MARGIN_S)
