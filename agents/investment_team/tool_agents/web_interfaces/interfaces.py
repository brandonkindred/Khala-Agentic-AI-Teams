"""Provider-agnostic browser interfaces for investment web actions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class BrowserType(str, Enum):
    """Supported browser engines for automation runtimes."""

    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


@dataclass
class WebAgentConfig:
    """Runtime settings for web broker agents."""

    browser: BrowserType | str = BrowserType.CHROMIUM
    headless: bool = True
    workspace_name: str | None = None
    provider_options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize browser values from enum or string configs."""
        if isinstance(self.browser, BrowserType):
            return
        self.browser = BrowserType(self.browser.lower())


@dataclass
class WebActionResult:
    """Outcome payload returned by web interface actions."""

    provider: str
    action: str
    status: str
    details: Dict[str, Any] = field(default_factory=dict)


class WebBrokerInterface(ABC):
    """Abstract contract for provider-specific browser-driven integrations."""

    def __init__(self, config: WebAgentConfig) -> None:
        self.config = config

    @abstractmethod
    def login(self) -> WebActionResult:
        """Authenticate to provider workspace."""

    @abstractmethod
    def open_workspace(self, workspace_name: str | None = None) -> WebActionResult:
        """Open a named workspace or use provider default."""

    @abstractmethod
    def run_action(self, action: str, payload: Dict[str, Any] | None = None) -> WebActionResult:
        """Execute a provider-specific action."""

    @abstractmethod
    def collect_artifacts(self) -> List[Dict[str, Any]]:
        """Collect run artifacts (screenshots, logs, reports)."""

    @abstractmethod
    def logout(self) -> WebActionResult:
        """Terminate authenticated web session."""
