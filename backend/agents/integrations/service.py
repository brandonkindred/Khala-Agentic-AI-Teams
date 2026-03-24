from __future__ import annotations

from typing import Any, Protocol

from .contracts import IntegrationRequest, IntegrationResponse
from .registry import IntegrationRegistry, McpToolConfig, ProviderConfig
from .router import CapabilityRouter


class ProviderAdapter(Protocol):
    def execute(
        self, provider: ProviderConfig, request: IntegrationRequest
    ) -> IntegrationResponse: ...


class McpToolGateway:
    """Gateway for MCP tool discovery, selection, and configuration."""

    def __init__(self, registry: IntegrationRegistry) -> None:
        self.registry = registry

    def list_tools(self, provider_name: str | None = None) -> list[dict[str, Any]]:
        providers = self.registry.list_enabled()
        if provider_name:
            providers = [self.registry.get_provider(provider_name)]

        tools: list[dict[str, Any]] = []
        for provider in providers:
            if provider.transport != "mcp":
                continue
            for tool in provider.mcp_tools:
                if not tool.enabled:
                    continue
                tools.append(
                    {
                        "provider": provider.name,
                        "name": tool.name,
                        "description": tool.description,
                        "capabilities": tool.capabilities,
                        "config": tool.config,
                    }
                )
        return tools

    def add_tool(self, provider_name: str, tool: McpToolConfig) -> McpToolConfig:
        return self.registry.add_mcp_tool(provider_name, tool)

    def configure_tool(
        self, provider_name: str, tool_name: str, config: dict[str, Any]
    ) -> McpToolConfig:
        return self.registry.update_mcp_tool(provider_name, tool_name, config=config)


class IntegrationService:
    """Single entry point agents can use for tool integrations."""

    def __init__(self, router: CapabilityRouter, adapter: ProviderAdapter) -> None:
        self.router = router
        self.adapter = adapter
        self.mcp_gateway = McpToolGateway(router.registry)

    @property
    def registry(self) -> IntegrationRegistry:
        return self.router.registry

    def call(self, request: IntegrationRequest) -> IntegrationResponse:
        provider = self.router.resolve(request)
        return self.adapter.execute(provider=provider, request=request)

    def list_mcp_tools(self, provider_name: str | None = None) -> list[dict[str, Any]]:
        return self.mcp_gateway.list_tools(provider_name=provider_name)

    def add_mcp_tool(self, provider_name: str, tool: McpToolConfig) -> McpToolConfig:
        return self.mcp_gateway.add_tool(provider_name=provider_name, tool=tool)

    def configure_mcp_tool(
        self, provider_name: str, tool_name: str, config: dict[str, Any]
    ) -> McpToolConfig:
        return self.mcp_gateway.configure_tool(
            provider_name=provider_name, tool_name=tool_name, config=config
        )

    def discover_integrations(self) -> dict[str, Any]:
        """Expose enabled integrations and capabilities so agents can plan tool usage."""
        integrations = self.registry.describe()
        capability_index: dict[str, list[str]] = {}
        for provider in self.registry.list_enabled():
            for capability in provider.capabilities:
                capability_index.setdefault(capability, []).append(provider.name)

        return {
            "integrations": integrations,
            "capability_index": capability_index,
            "mcp_tools": self.mcp_gateway.list_tools(),
        }
