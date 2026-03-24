"""QuantConnect-specific web broker implementation."""

from __future__ import annotations

from typing import Any, Dict, List

from .interfaces import WebActionResult, WebAgentConfig, WebBrokerInterface


class QuantConnectWebAgent(WebBrokerInterface):
    """Implements the provider contract for QuantConnect workflows."""

    provider_name = "quantconnect"

    def __init__(self, config: WebAgentConfig) -> None:
        super().__init__(config)
        self._artifacts: List[Dict[str, Any]] = []

    def login(self) -> WebActionResult:
        return WebActionResult(
            provider=self.provider_name,
            action="login",
            status="ok",
            details={"browser": self.config.browser.value},
        )

    def open_workspace(self, workspace_name: str | None = None) -> WebActionResult:
        selected_workspace = workspace_name or self.config.workspace_name or "default"
        return WebActionResult(
            provider=self.provider_name,
            action="open_workspace",
            status="ok",
            details={"workspace": selected_workspace},
        )

    def run_action(self, action: str, payload: Dict[str, Any] | None = None) -> WebActionResult:
        entry = {"provider": self.provider_name, "action": action, "payload": payload or {}}
        self._artifacts.append(entry)
        return WebActionResult(
            provider=self.provider_name, action=action, status="ok", details=entry
        )

    def collect_artifacts(self) -> List[Dict[str, Any]]:
        return list(self._artifacts)

    def logout(self) -> WebActionResult:
        return WebActionResult(provider=self.provider_name, action="logout", status="ok")
