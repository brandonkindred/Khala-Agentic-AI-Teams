from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class McpToolConfig:
    name: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderConfig:
    name: str
    transport: str
    capabilities: list[str]
    settings: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    display_name: str | None = None
    category: str = "general"
    description: str = ""
    auth: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, str] = field(default_factory=dict)
    mcp_tools: list[McpToolConfig] = field(default_factory=list)


class IntegrationRegistry:
    """Registry of integration providers available to all agent teams."""

    def __init__(self, providers: list[ProviderConfig] | None = None) -> None:
        self._providers: dict[str, ProviderConfig] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ProviderConfig) -> None:
        self._providers[provider.name] = provider

    def update_provider(self, provider: ProviderConfig) -> ProviderConfig:
        self._providers[provider.name] = provider
        return provider

    def list_enabled(self) -> list[ProviderConfig]:
        return [provider for provider in self._providers.values() if provider.enabled]

    def providers_for_capability(self, capability: str) -> list[ProviderConfig]:
        return [
            provider
            for provider in self.list_enabled()
            if capability in provider.capabilities or "*" in provider.capabilities
        ]

    def get_provider(self, provider_name: str) -> ProviderConfig:
        provider = self._providers.get(provider_name)
        if not provider or not provider.enabled:
            raise LookupError(f"No enabled integration provider found for '{provider_name}'")
        return provider

    def update_mcp_tool(
        self,
        provider_name: str,
        tool_name: str,
        *,
        enabled: bool | None = None,
        description: str | None = None,
        capabilities: list[str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> McpToolConfig:
        provider = self.get_provider(provider_name)
        if provider.transport != "mcp":
            raise LookupError(f"Provider '{provider_name}' is not configured for MCP transport")

        for tool in provider.mcp_tools:
            if tool.name != tool_name:
                continue
            if enabled is not None:
                tool.enabled = enabled
            if description is not None:
                tool.description = description
            if capabilities is not None:
                tool.capabilities = capabilities
            if config:
                tool.config.update(config)
            return tool

        raise LookupError(f"No MCP tool named '{tool_name}' found for provider '{provider_name}'")

    def add_mcp_tool(self, provider_name: str, tool: McpToolConfig) -> McpToolConfig:
        provider = self.get_provider(provider_name)
        if provider.transport != "mcp":
            raise LookupError(f"Provider '{provider_name}' is not configured for MCP transport")
        if any(existing_tool.name == tool.name for existing_tool in provider.mcp_tools):
            raise ValueError(f"Provider '{provider_name}' already has MCP tool '{tool.name}'")
        provider.mcp_tools.append(tool)
        return tool

    def describe(self) -> list[dict[str, Any]]:
        """Returns machine-readable integration metadata for agent planning."""
        return [
            {
                "name": provider.name,
                "display_name": provider.display_name or provider.name,
                "category": provider.category,
                "description": provider.description,
                "transport": provider.transport,
                "capabilities": provider.capabilities,
                "actions": provider.actions,
                "auth": provider.auth,
                "settings": provider.settings,
                "mcp_tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "capabilities": tool.capabilities,
                        "enabled": tool.enabled,
                        "config": tool.config,
                    }
                    for tool in provider.mcp_tools
                    if tool.enabled
                ],
            }
            for provider in self.list_enabled()
        ]

    @classmethod
    def from_yaml(cls, file_path: str | Path) -> "IntegrationRegistry":
        payload = yaml.safe_load(Path(file_path).read_text(encoding="utf-8")) or {}
        providers: list[ProviderConfig] = []
        for item in payload.get("providers", []):
            providers.append(
                ProviderConfig(
                    name=item["name"],
                    transport=item.get("transport", "api"),
                    capabilities=item.get("capabilities", []),
                    settings=item.get("settings", {}),
                    enabled=item.get("enabled", True),
                    display_name=item.get("display_name"),
                    category=item.get("category", "general"),
                    description=item.get("description", ""),
                    auth=item.get("auth", {}),
                    actions=item.get("actions", {}),
                    mcp_tools=[
                        McpToolConfig(
                            name=tool["name"],
                            description=tool.get("description", ""),
                            capabilities=tool.get("capabilities", []),
                            enabled=tool.get("enabled", True),
                            config=tool.get("config", {}),
                        )
                        for tool in item.get("mcp_tools", [])
                    ],
                )
            )
        return cls(providers)
