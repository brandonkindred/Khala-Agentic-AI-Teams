"""Web automation interfaces for broker/platform providers."""

from .coordinator import InvestmentWebInterfaceCoordinator, WebProvider
from .interfaces import BrowserType, WebAgentConfig, WebBrokerInterface

__all__ = [
    "BrowserType",
    "InvestmentWebInterfaceCoordinator",
    "WebAgentConfig",
    "WebBrokerInterface",
    "WebProvider",
]
