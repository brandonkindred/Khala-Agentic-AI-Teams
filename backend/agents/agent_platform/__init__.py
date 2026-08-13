"""Agent platform — in-process backend for discover, author, run, and sandbox.

Intended members of this package:

- ``registry`` — manifest catalog (Agent Console ``/api/agents``); present
- ``console`` — runs / saved-inputs / diff data layer
- ``sandbox`` — ephemeral per-agent runner
- ``studio`` — conversational single-agent authoring

Docker/environment provisioning infrastructure is not a member of this package.
Domain apps (agentic compose, persona runner) consume this package; they are
not members of it.

Subpackages are imported with fully-qualified dotted paths
(``agent_platform.registry``, …). This module re-exports nothing.
"""
