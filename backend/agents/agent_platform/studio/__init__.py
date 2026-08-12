"""Agent Studio backend — Stage 1 "build an agent" touchpoints.

This package implements the §5 Stage-1 backend the Agent Studio UX depends on
(``docs/design/agent-studio-ux-spec.md``):

* a **per-agent authoring assistant** (:mod:`agent_platform.studio.assistant`) that
  co-authors a single :class:`~agent_platform.studio.models.AgentDefinition` via LLM chat,
  modeled on ``agentic_team_provisioning``'s ``ProcessDesignerAgent``;
* **clone-from-registry** — projecting an existing registry manifest into an
  editable draft (the source manifest is never mutated);
* **save + register** — turning a finished definition into a live, invokable
  ``agent_registry`` manifest, reusing the generated-agent runtime so a saved
  Studio agent is invokable exactly like a generated team agent.

Conversation state is held in-process (:mod:`agent_platform.studio.store`); durable
cross-process persistence is a tracked follow-up, mirroring the same caveat the
generated-agent registration already carries.

Public façade (import these four from ``agent_platform.studio``). Everything else
(``temporal``, ``postgres``, ``drafts_runtime``, models) is a submodule import.
Internal modules in this package must import sibling submodules, never this
façade, so these re-exports cannot create an import cycle.
"""

from .registration import build_studio_agent_manifest, clone_from_manifest
from .routes import router
from .runtime import get_studio_service

__all__ = [
    "get_studio_service",
    "build_studio_agent_manifest",
    "clone_from_manifest",
    "router",
]
