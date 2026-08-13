"""Agent platform — in-process backend for discover, author, run, and sandbox.

Intended members of this package:

- ``registry`` — manifest catalog (Agent Console ``/api/agents``); present
- ``console`` — runs / saved-inputs / diff data layer
- ``sandbox`` — ephemeral per-agent runner; present
- ``studio`` — conversational single-agent authoring, including the
  ``/api/agent-studio`` HTTP router; present

Docker/environment provisioning infrastructure is not a member of this package.
Domain apps (agentic compose, persona runner) consume this package; they are
not members of it.

Subpackages are imported with fully-qualified dotted paths
(``agent_platform.registry``, ``agent_platform.sandbox``,
``agent_platform.studio``, …). This module re-exports nothing.

Boundary:
    * In: manifest catalog (``agent_platform.registry``), sandbox lifecycle
      (acquire / warm / teardown / reaper) plus its Temporal wiring
      (``agent_platform.sandbox``), and Studio authoring
      (``agent_platform.studio``).
    * Out: Docker/env provisioning infra under
      ``agent_team_studio.agent_provisioning_team`` (tool agents, phases,
      orchestrator, provisioning Temporal). Domain apps that merely consume
      the platform (agentic compose, persona runner) stay consumers.

Preconditions:
    * ``backend/agents`` is on ``PYTHONPATH``.
Postconditions:
    * Importing ``agent_platform`` has no side effects (no worker boot, no
      I/O). Subpackages are loaded only when callers import them.
"""
