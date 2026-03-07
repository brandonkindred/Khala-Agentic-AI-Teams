"""Web automation interfaces for broker/platform providers."""

from .coordinator import InvestmentWebInterfaceCoordinator, WebProvider
from .interfaces import BrowserType, WebActionResult, WebAgentConfig, WebBrokerInterface
from .quantconnect_agent import QuantConnectWebAgent
from .tradingview_agent import TradingViewWebAgent

__all__ = [
    "BrowserType",
    "InvestmentWebInterfaceCoordinator",
    "QuantConnectWebAgent",
    "TradingViewWebAgent",
    "WebActionResult",
    "WebAgentConfig",
    "WebBrokerInterface",
    "WebProvider",
]
