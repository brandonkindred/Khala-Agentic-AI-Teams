"""Coordinator for selecting and invoking investment web interfaces."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict

from .interfaces import WebActionResult, WebAgentConfig, WebBrokerInterface
from .quantconnect_agent import QuantConnectWebAgent
from .tradingview_agent import TradingViewWebAgent


class WebProvider(str, Enum):
    QUANTCONNECT = "quantconnect"
    TRADINGVIEW = "tradingview"


class InvestmentWebInterfaceCoordinator:
    """Factory + façade for provider-agnostic web workflows."""

    def __init__(self, provider: WebProvider | str, config: WebAgentConfig) -> None:
        provider_value = provider.value if isinstance(provider, WebProvider) else provider.lower()
        self.provider = WebProvider(provider_value)
        self.agent = self._build_agent(self.provider, config)

    @staticmethod
    def _build_agent(provider: WebProvider, config: WebAgentConfig) -> WebBrokerInterface:
        providers = {
            WebProvider.QUANTCONNECT: QuantConnectWebAgent,
            WebProvider.TRADINGVIEW: TradingViewWebAgent,
        }
        return providers[provider](config)

    def execute_action(
        self,
        action: str,
        payload: Dict[str, Any] | None = None,
        workspace_name: str | None = None,
    ) -> Dict[str, Any]:
        login_result = self.agent.login()
        workspace_result = self.agent.open_workspace(workspace_name=workspace_name)
        action_result = self.agent.run_action(action, payload=payload)
        artifacts = self.agent.collect_artifacts()
        logout_result = self.agent.logout()

        return {
            "provider": self.provider.value,
            "results": {
                "login": self._serialize(login_result),
                "open_workspace": self._serialize(workspace_result),
                "run_action": self._serialize(action_result),
                "logout": self._serialize(logout_result),
            },
            "artifacts": artifacts,
        }

    @staticmethod
    def _serialize(result: WebActionResult) -> Dict[str, Any]:
        return {
            "provider": result.provider,
            "action": result.action,
            "status": result.status,
            "details": result.details,
        }
