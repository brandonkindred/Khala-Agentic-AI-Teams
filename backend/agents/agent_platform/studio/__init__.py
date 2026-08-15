"""Agent Studio backend — Stage 1 "build an agent" touchpoints.

This package implements the §5 Stage-1 backend the Agent Studio UX depends on
(``docs/design/agent-studio-ux-spec.md``):

* a **per-agent authoring assistant** (:mod:`agent_platform.studio.assistant`) that
  co-authors a single :class:`~agent_platform.studio.models.AgentDefinition` via LLM chat,
  modeled on ``agentic_team_provisioning``'s ``ProcessDesignerAgent``;
* **clone-from-registry** — projecting an existing registry manifest into an
  editable draft (the source manifest is never mutated);
* **save + register** — turning a finished definition into a live, invokable
  ``agent_platform.registry`` manifest, reusing the generated-agent runtime so a saved
  Studio agent is invokable exactly like a generated team agent.

Conversation state is held in-process (:mod:`agent_platform.studio.store`); durable
cross-process persistence is a tracked follow-up, mirroring the same caveat the
generated-agent registration already carries.

Public façade (import these four from ``agent_platform.studio``). Everything else
(``postgres``, ``drafts_runtime``, models) is a submodule import. Internal modules in
this package must import sibling submodules, never this façade, so these re-exports
cannot create an import cycle.

The four public names resolve lazily via PEP 562 ``__getattr__`` so importing the
package (or one of its lightweight submodules) does not eagerly pull ``routes`` or
``runtime``, which construct process singletons at import time.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .registration import build_studio_agent_manifest, clone_from_manifest
    from .routes import router
    from .runtime import get_studio_service

_LAZY_RUNTIME_EXPORTS = {"get_studio_service"}
_LAZY_REGISTRATION_EXPORTS = {"build_studio_agent_manifest", "clone_from_manifest"}
_LAZY_ROUTES_EXPORTS = {"router"}


def __getattr__(name: str) -> Any:
    """Resolve a public façade export lazily (PEP 562).

    Preconditions:
        * ``name`` is a ``str``.
    Postconditions:
        * Returns the named façade export, or raises ``AttributeError`` if
          ``name`` is not a public façade export.
    """
    if name in _LAZY_RUNTIME_EXPORTS:
        from . import runtime  # noqa: PLC0415 - intentional lazy import

        value = getattr(runtime, name)
        globals()[name] = value
        return value
    if name in _LAZY_REGISTRATION_EXPORTS:
        from . import registration  # noqa: PLC0415 - intentional lazy import

        value = getattr(registration, name)
        globals()[name] = value
        return value
    if name in _LAZY_ROUTES_EXPORTS:
        from . import routes  # noqa: PLC0415 - intentional lazy import

        value = getattr(routes, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_studio_service",
    "build_studio_agent_manifest",
    "clone_from_manifest",
    "router",
]
