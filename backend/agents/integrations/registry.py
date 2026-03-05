from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


class IntegrationRegistry:
    """Registry of integration providers available to all agent teams."""

    def __init__(self, providers: list[ProviderConfig] | None = None) -> None:
        self._providers: dict[str, ProviderConfig] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ProviderConfig) -> None:
        self._providers[provider.name] = provider

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
                )
            )
        return cls(providers)
