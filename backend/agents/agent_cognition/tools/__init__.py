"""Tools layer for the Agent Cognition Core.

Binds a per-agent toolset from the existing registries, runs it through the
shared tool loop behind a broker that gates ``forbid_tool``-restricted calls
pre-dispatch and logs every call to memory, and defines the marker-wrapped invoke
envelope + runtime channels the sandbox shim and proxy use.

Importing this package has no side effects. The lightweight, dependency-free
``envelope`` and ``channel`` modules are imported eagerly (the invoke shim depends
on them); ``binding`` and ``runner`` — which pull in the git/LLM tool stack — are
loaded lazily on first attribute access, so unwrapping an envelope never imports
the heavy tool machinery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from agent_cognition.tools.channel import (
    get_cognition_context,
    runtime_channel,
)
from agent_cognition.tools.envelope import (
    ENVELOPE_MARKER,
    EnvelopeError,
    UnwrappedRequest,
    is_envelope,
    try_unwrap_request,
    wrap_request,
)

# name -> submodule, resolved lazily so `agent_cognition.tools.envelope` stays cheap.
_LAZY: dict[str, str] = {
    "BoundTool": "binding",
    "BoundToolset": "binding",
    "ExecutionSite": "binding",
    "ToolBindingError": "binding",
    "bind_tools": "binding",
    "ToolAudit": "runner",
    "ToolLoopPlan": "runner",
    "drive_platform_bound_loop": "runner",
    "execute_plan": "runner",
    "run_tool_loop": "runner",
}

if TYPE_CHECKING:  # pragma: no cover - import surface for type checkers only
    from agent_cognition.tools.binding import (
        BoundTool,
        BoundToolset,
        ExecutionSite,
        ToolBindingError,
        bind_tools,
    )
    from agent_cognition.tools.runner import (
        ToolAudit,
        ToolLoopPlan,
        drive_platform_bound_loop,
        execute_plan,
        run_tool_loop,
    )


def __getattr__(name: str) -> Any:
    submodule = _LAZY.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{submodule}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    # binding (lazy)
    "BoundTool",
    "BoundToolset",
    "ExecutionSite",
    "ToolBindingError",
    "bind_tools",
    # envelope
    "ENVELOPE_MARKER",
    "EnvelopeError",
    "UnwrappedRequest",
    "is_envelope",
    "try_unwrap_request",
    "wrap_request",
    # channel
    "get_cognition_context",
    "runtime_channel",
    # runner (lazy)
    "ToolAudit",
    "ToolLoopPlan",
    "drive_platform_bound_loop",
    "execute_plan",
    "run_tool_loop",
]
