from __future__ import annotations

from typing import Any

from .contracts import IntegrationRequest, IntegrationResponse
from .registry import ProviderConfig


class ApiMcpAdapter:
    """Minimal adapter that normalizes outbound API/MCP requests.

    This class is intentionally transport-agnostic for now: it returns a
    normalized execution plan that teams can wire to concrete clients.
    """

    def execute(self, provider: ProviderConfig, request: IntegrationRequest) -> IntegrationResponse:
        result: dict[str, Any] = {
            "transport": provider.transport,
            "capability": request.capability,
            "operation": request.operation.value,
            "settings": provider.settings,
            "payload": request.payload,
        }

        if provider.name == "slack" and request.capability == "chat.notify":
            default_channel = provider.settings.get("default_channel")
            selected_channel = request.payload.get("channel") or default_channel
            if selected_channel:
                result["target"] = {"channel": selected_channel}

        return IntegrationResponse(provider=provider.name, status="planned", result=result)
