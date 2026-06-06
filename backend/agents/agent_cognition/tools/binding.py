"""Resolve a manifest's ``cognition.tools`` ids into an executable toolset.

``bind_tools`` takes the list of tool ids declared on an agent and resolves each
against the existing registries — ``agent_git_tools`` (bundled, repo-backed),
``LlmToolsService`` (the LLM tool catalog), and ``IntegrationRegistry`` (platform
integrations) — producing OpenAI-style tool *definitions* and their *handlers*,
and tagging each tool with the **execution site** it must run at:

* ``in_process`` — the agent itself runs in the Unified API (no sandbox), so a
  bundled tool's loop runs in-process with full platform access.
* ``sandbox_local`` — the agent runs in a sandbox and the tool is bundled in the
  image (e.g. ``git`` on the mounted repo); the loop runs inside the sandbox and
  the shim brokers each handler. This is the v1 default for sandboxed agents.
* ``platform_bound`` — the tool needs platform registries/secrets/egress, so it
  can only execute platform-side; the invoke proxy drives the loop (see
  :mod:`agent_cognition.tools.runner`). Live use for *generated* agents is gated
  on the Step 14 runtime scaffold.

The list of ids comes straight from the manifest as ``list[str]`` — binding does
**not** depend on the (later) ``CognitionSpec`` model, only on the ids.

Design by Contract:

* :func:`bind_tools` — Preconditions: ``tool_ids`` is a list of non-empty
  strings. Postconditions: every returned :class:`BoundTool` has at least one
  definition whose function name has a handler, and a resolved
  :class:`ExecutionSite`; an unknown id, a duplicate function name across tools,
  or ``git`` without a :class:`GitToolContext` raises :class:`ToolBindingError`
  and nothing partial is returned.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent_git_tools import (
    GIT_TOOL_DEFINITIONS,
    GitToolContext,
    build_git_tool_handlers,
)

__all__ = [
    "ExecutionSite",
    "ToolBindingError",
    "BoundTool",
    "BoundToolset",
    "bind_tools",
]

# The one bundled, repo-backed tool every agent may declare. Other ``LlmToolsService``
# catalog entries are metadata-only today (no executable handler factory), so they
# resolve to a definition but are rejected for binding until a handler source exists.
_GIT_TOOL_ID = "git"


class ExecutionSite(str, Enum):
    """Where a bound tool's handler must execute."""

    IN_PROCESS = "in_process"
    SANDBOX_LOCAL = "sandbox_local"
    PLATFORM_BOUND = "platform_bound"


class ToolBindingError(ValueError):
    """A declared tool id cannot be resolved to an executable, sited binding."""


@dataclass(frozen=True)
class BoundTool:
    """One resolved tool: its OpenAI definitions, handlers, and execution site.

    Invariant: every function name in :attr:`definitions` has an entry in
    :attr:`handlers`, and vice-versa.
    """

    tool_id: str
    site: ExecutionSite
    definitions: tuple[dict[str, Any], ...]
    handlers: Mapping[str, Callable[[dict[str, Any]], Any]]

    def function_names(self) -> tuple[str, ...]:
        return tuple(self.handlers.keys())


@dataclass(frozen=True)
class BoundToolset:
    """The full per-agent toolset, indexable by function name and by site."""

    tools: tuple[BoundTool, ...]

    def definitions(self) -> list[dict[str, Any]]:
        """All OpenAI tool definitions, flattened (loop ``tools=`` argument)."""
        out: list[dict[str, Any]] = []
        for tool in self.tools:
            out.extend(tool.definitions)
        return out

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """Merged function-name → handler map (loop ``tool_handlers=`` argument)."""
        out: dict[str, Callable[[dict[str, Any]], Any]] = {}
        for tool in self.tools:
            out.update(tool.handlers)
        return out

    def site_for(self, function_name: str) -> ExecutionSite:
        """Return the execution site of the tool owning ``function_name``.

        Precondition: ``function_name`` belongs to some bound tool. Raises
        :class:`KeyError` otherwise (a programmer error — the loop only ever calls
        names this toolset advertised).
        """
        for tool in self.tools:
            if function_name in tool.handlers:
                return tool.site
        raise KeyError(function_name)

    def for_site(self, site: ExecutionSite) -> BoundToolset:
        """Return the sub-toolset whose tools run at ``site`` (e.g. split the
        proxy-driven ``platform_bound`` tools from the sandbox-local ones)."""
        return BoundToolset(tuple(t for t in self.tools if t.site == site))


def bind_tools(
    tool_ids: list[str],
    *,
    in_process: bool = False,
    git_context: GitToolContext | None = None,
    tools_service: Any | None = None,
    integration_registry: Any | None = None,
) -> BoundToolset:
    """Resolve ``tool_ids`` into a :class:`BoundToolset`.

    Args:
        in_process: ``True`` when the owning agent runs in the Unified API rather
            than a sandbox — bundled tools are then sited ``in_process`` instead of
            ``sandbox_local``.
        git_context: required to bind the ``git`` tool (the model never chooses the
            repo path — the host injects it).
        tools_service: optional ``LlmToolsService`` used to confirm a non-git id is
            a known catalog tool before rejecting it as unbindable.
        integration_registry: optional ``IntegrationRegistry``; a provider id
            resolves to a ``platform_bound`` tool.

    Preconditions: ``tool_ids`` is a list of non-empty strings.
    Postconditions: see module docstring — fully resolved or :class:`ToolBindingError`.
    """
    assert isinstance(tool_ids, list), "bind_tools: tool_ids must be a list"
    bundled_site = ExecutionSite.IN_PROCESS if in_process else ExecutionSite.SANDBOX_LOCAL
    bound: list[BoundTool] = []
    seen_functions: dict[str, str] = {}  # function name → owning tool id
    for tool_id in tool_ids:
        if not isinstance(tool_id, str) or not tool_id:
            raise ToolBindingError(f"tool id must be a non-empty string, got {tool_id!r}")
        tool = _resolve_one(
            tool_id,
            bundled_site=bundled_site,
            git_context=git_context,
            tools_service=tools_service,
            integration_registry=integration_registry,
        )
        for fn in tool.function_names():
            if fn in seen_functions:
                raise ToolBindingError(
                    f"tool '{tool_id}' function '{fn}' collides with tool '{seen_functions[fn]}'"
                )
            seen_functions[fn] = tool_id
        bound.append(tool)
    return BoundToolset(tuple(bound))


def _resolve_one(
    tool_id: str,
    *,
    bundled_site: ExecutionSite,
    git_context: GitToolContext | None,
    tools_service: Any | None,
    integration_registry: Any | None,
) -> BoundTool:
    """Resolve a single id against git → integration registry → LLM catalog."""
    if tool_id == _GIT_TOOL_ID:
        if git_context is None:
            raise ToolBindingError("binding 'git' requires a GitToolContext")
        return BoundTool(
            tool_id=tool_id,
            site=bundled_site,
            definitions=tuple(GIT_TOOL_DEFINITIONS),
            handlers=build_git_tool_handlers(git_context),
        )

    if integration_registry is not None and _is_known_provider(integration_registry, tool_id):
        return _bind_integration(integration_registry, tool_id)

    # Known to the LLM tool catalog but with no executable handler factory yet:
    # surface a precise error rather than a generic "unknown id".
    if tools_service is not None and _is_catalog_tool(tools_service, tool_id):
        raise ToolBindingError(
            f"tool '{tool_id}' is catalogued but has no executable handler binding"
        )

    raise ToolBindingError(f"unknown tool id: {tool_id!r}")


def _is_catalog_tool(tools_service: Any, tool_id: str) -> bool:
    try:
        tools_service.get_tool(tool_id)
    except Exception:
        return False
    return True


def _is_known_provider(integration_registry: Any, tool_id: str) -> bool:
    try:
        integration_registry.get_provider(tool_id)
    except Exception:
        return False
    return True


def _bind_integration(integration_registry: Any, tool_id: str) -> BoundTool:
    """Bind a platform integration to a single ``platform_bound`` call tool.

    The handler is a deliberate not-yet-wired stub: live integration execution for
    sandboxed agents needs the proxy-driven runtime (Step 14). The proxy supplies
    real platform-side handlers when it drives the loop; until then a call returns
    a structured "not wired" result instead of pretending to execute.
    """
    provider = integration_registry.get_provider(tool_id)
    description = getattr(provider, "description", "") or f"Call the {tool_id} integration."
    function_name = f"{tool_id}__call"
    definition = {
        "type": "function",
        "function": {
            "name": function_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Provider action to invoke."},
                    "args": {"type": "object", "description": "Action arguments."},
                },
                "additionalProperties": True,
            },
        },
    }

    def _unwired(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "error": "platform_tool_not_wired",
            "message": (
                f"integration '{tool_id}' is platform-bound; live execution "
                "requires the proxy-driven runtime"
            ),
            "tool_id": tool_id,
        }

    return BoundTool(
        tool_id=tool_id,
        site=ExecutionSite.PLATFORM_BOUND,
        definitions=(definition,),
        handlers={function_name: _unwired},
    )
