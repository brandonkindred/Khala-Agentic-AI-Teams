from __future__ import annotations

from typing import Any, Protocol

from .contracts import IntegrationRequest, IntegrationResponse
from .registry import IntegrationRegistry, ProviderConfig
from .router import CapabilityRouter


class ProviderAdapter(Protocol):
    def execute(self, provider: ProviderConfig, request: IntegrationRequest) -> IntegrationResponse: ...


class IntegrationService:
    """Single entry point agents can use for tool integrations."""

    def __init__(self, router: CapabilityRouter, adapter: ProviderAdapter) -> None:
        self.router = router
        self.adapter = adapter

    @property
    def registry(self) -> IntegrationRegistry:
        return self.router.registry

    def call(self, request: IntegrationRequest) -> IntegrationResponse:
        provider = self.router.resolve(request)
        return self.adapter.execute(provider=provider, request=request)

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
        }
