"""Shared ``AgentManifest`` construction: helpers, constants, and canonical rules.

This package is the single source for generated/studio manifest construction.
Studio (``agent_platform.studio.registration``) constructs and projects
manifests through the helpers here; the agentic provisioning surface still
has local builders. :mod:`shared.manifests.builders` owns the
value-driven helper functions (``build_manifest``, ``clone_manifest``,
``io_schema``, ``project_manifest``). :mod:`shared.manifests.constants` owns
the generated-agent entrypoint, invoke-schema refs, anatomy path, default
rule packs, and :func:`~shared.manifests.constants.default_cognition_block`.

Canonical hashing / team / cognition rules
-----------------------------------------

**Hashing.** Id-construction primitives live in
:mod:`agent_platform.registry.manifest_projection`: ``slug(value, max_len)``
lowercases, hyphenates, and bounds a string (empty/all-symbol input becomes
``"agent"``); ``hash_suffix(value, length)`` returns
``sha256(utf-8)[:length]``. Surfaces pick their own digest lengths and wrap
those primitives — they do not reimplement hashing.

* Studio ids: ``agent_studio.<slug(name)>-<hash8(name)>``.
* Agentic ids: ``agentic_team_provisioning.<slug12(team_id)>-<hash16(team_id)>.<slug40(name)>-<hash16(team_id + NUL + name)>``.
  The 16-hex (64-bit) digest keeps accidental collisions negligible at
  realistic roster sizes; the NUL separator binds the pair hash to both
  ``team_id`` and ``agent_name``.

**Team keys.** Manifest ``team`` values must match a ``TEAM_CONFIGS`` key in
``unified_api.config``. Studio files agents under ``agent_studio``; the
agentic roster files them under ``agentic_team_provisioning``. Those keys
are still owned by each surface (call-site migration has not happened);
changing a key would orphan already-registered agents.

**Cognition.** Every generated/authored manifest stamps
:func:`~shared.manifests.constants.default_cognition_block`: 90-day episodic
memory, empty ``tools`` (the caller overrides with roster labels or Studio
tool ids), the ``default_guardrails`` seed pack, a default-on knowledge
graph, and ``requires_idempotency_key=False``.
"""

from __future__ import annotations

from shared.manifests.builders import build_manifest, clone_manifest, io_schema, project_manifest
from shared.manifests.constants import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    default_cognition_block,
)

__all__ = [
    "AGENT_ANATOMY_REF",
    "DEFAULT_RULE_PACKS",
    "GENERATED_AGENT_ENTRYPOINT",
    "GENERATED_AGENT_INPUT_REF",
    "GENERATED_AGENT_OUTPUT_REF",
    "build_manifest",
    "clone_manifest",
    "default_cognition_block",
    "io_schema",
    "project_manifest",
]
