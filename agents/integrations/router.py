from __future__ import annotations

from .contracts import IntegrationRequest
from .registry import IntegrationRegistry, ProviderConfig


class CapabilityRouter:
    """Resolves a request capability to an enabled integration provider."""

    def __init__(self, registry: IntegrationRegistry, preferred_providers: dict[str, str] | None = None) -> None:
        self.registry = registry
        self.preferred_providers = preferred_providers or {}

    def resolve(self, request: IntegrationRequest) -> ProviderConfig:
        candidates = self.registry.providers_for_capability(request.capability)
        if not candidates:
            raise LookupError(f"No integration provider found for capability '{request.capability}'")

        preferred = self.preferred_providers.get(request.capability)
        if preferred:
            for candidate in candidates:
                if candidate.name == preferred:
                    return candidate

        return candidates[0]
