"""Tests for the per-agent tool binding (Step 7).

No Postgres / LLM — binding resolves ids against in-memory registries.
"""

from __future__ import annotations

import pytest

from agent_cognition.tools.binding import (
    BoundTool,
    BoundToolset,
    ExecutionSite,
    ToolBindingError,
    bind_tools,
)
from agent_git_tools import GitToolContext
from integrations.registry import IntegrationRegistry, ProviderConfig


def _git_ctx(tmp_path) -> GitToolContext:
    return GitToolContext(repo_path=tmp_path)


def _http_registry() -> IntegrationRegistry:
    return IntegrationRegistry(
        [
            ProviderConfig(
                name="http_api",
                transport="api",
                capabilities=["http"],
                description="Generic HTTP API integration.",
            )
        ]
    )


class _FakeCatalog:
    """Minimal LlmToolsService stand-in: knows one id, raises on others."""

    def __init__(self, known: set[str]) -> None:
        self._known = known

    def get_tool(self, tool_id: str):
        if tool_id not in self._known:
            raise KeyError(tool_id)
        return {"tool_id": tool_id}


# ---------------------------------------------------------------------------
# Execution-site resolution
# ---------------------------------------------------------------------------
def test_git_resolves_sandbox_local_by_default(tmp_path) -> None:
    ts = bind_tools(["git"], git_context=_git_ctx(tmp_path))
    assert [t.tool_id for t in ts.tools] == ["git"]
    assert ts.tools[0].site is ExecutionSite.SANDBOX_LOCAL
    # Every advertised function has a handler.
    names = {d["function"]["name"] for d in ts.definitions()}
    assert names == set(ts.handlers().keys())
    assert "git_status" in names


def test_git_resolves_in_process_when_flagged(tmp_path) -> None:
    ts = bind_tools(["git"], in_process=True, git_context=_git_ctx(tmp_path))
    assert ts.tools[0].site is ExecutionSite.IN_PROCESS


def test_integration_resolves_platform_bound() -> None:
    ts = bind_tools(["http_api"], integration_registry=_http_registry())
    tool = ts.tools[0]
    assert tool.site is ExecutionSite.PLATFORM_BOUND
    assert ts.site_for("http_api__call") is ExecutionSite.PLATFORM_BOUND
    # Default handler is the deliberate not-yet-wired stub.
    result = tool.handlers["http_api__call"]({"action": "get"})
    assert result["success"] is False
    assert result["error"] == "platform_tool_not_wired"


def test_mixed_toolset_partitions_by_site(tmp_path) -> None:
    ts = bind_tools(
        ["git", "http_api"],
        git_context=_git_ctx(tmp_path),
        integration_registry=_http_registry(),
    )
    platform = ts.for_site(ExecutionSite.PLATFORM_BOUND)
    assert [t.tool_id for t in platform.tools] == ["http_api"]
    local = ts.for_site(ExecutionSite.SANDBOX_LOCAL)
    assert [t.tool_id for t in local.tools] == ["git"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
def test_unknown_id_errors() -> None:
    with pytest.raises(ToolBindingError, match="unknown tool id"):
        bind_tools(["does_not_exist"])


def test_git_without_context_errors() -> None:
    with pytest.raises(ToolBindingError, match="requires a GitToolContext"):
        bind_tools(["git"])


def test_catalogued_without_handler_errors() -> None:
    # A tool the catalog knows but has no executable handler factory: precise
    # error, not a generic "unknown id".
    with pytest.raises(ToolBindingError, match="no executable handler binding"):
        bind_tools(["fancy_tool"], tools_service=_FakeCatalog({"fancy_tool"}))


def test_empty_id_errors() -> None:
    with pytest.raises(ToolBindingError, match="non-empty string"):
        bind_tools([""])


def test_unknown_id_when_registries_raise_falls_through_to_error() -> None:
    # Both a catalog that KeyErrors and a registry that LookupErrors on the id:
    # binding must treat it as unknown, not crash.
    with pytest.raises(ToolBindingError, match="unknown tool id"):
        bind_tools(
            ["ghost"],
            tools_service=_FakeCatalog(set()),
            integration_registry=_http_registry(),
        )


def test_function_name_collision_errors(tmp_path) -> None:
    # Two integration providers that synthesize the same function name collide.
    reg = IntegrationRegistry(
        [
            ProviderConfig(name="dup", transport="api", capabilities=[]),
        ]
    )
    with pytest.raises(ToolBindingError, match="collides"):
        # Declaring the same provider twice yields the same `dup__call` name.
        bind_tools(["dup", "dup"], integration_registry=reg)


# ---------------------------------------------------------------------------
# BoundToolset accessors
# ---------------------------------------------------------------------------
def test_site_for_unknown_function_raises() -> None:
    ts = BoundToolset(
        (
            BoundTool(
                tool_id="x",
                site=ExecutionSite.SANDBOX_LOCAL,
                definitions=(),
                handlers={"x_op": lambda a: a},
            ),
        )
    )
    assert ts.site_for("x_op") is ExecutionSite.SANDBOX_LOCAL
    with pytest.raises(KeyError):
        ts.site_for("missing")


def test_empty_toolset_is_inert() -> None:
    ts = bind_tools([])
    assert ts.tools == ()
    assert ts.definitions() == []
    assert ts.handlers() == {}
